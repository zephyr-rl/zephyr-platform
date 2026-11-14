from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select

from app.api.deps import AuthDep, OwnerDep, SessionDep
from app.core import audit, jobs
from app.modules.chat_monitor import service
from app.modules.chat_monitor.models import ChatThread
from app.notifications.context import ORDER_URLS
from app.orders.models import Order
from app.stores.models import MarketplaceConnection
from app.stores.service import NotFound as StoreNotFound
from app.stores.service import get_store

router = APIRouter(prefix="/stores/{store_id}/messages", tags=["messages"])


class ThreadOut(BaseModel):
    id: int
    order_id: int | None
    order_remote_id: str | None
    order_url: str | None
    order_state: str | None
    buyer_name: str | None
    offer_title: str | None
    last_buyer_at: datetime | None
    last_buyer_text: str | None
    last_seller_at: datetime | None
    last_alert_at: datetime | None
    urgent: bool
    needs_reply: bool
    handled_at: datetime | None
    handled_by: str | None


async def _store(session: SessionDep, store_id: int) -> None:
    try:
        await get_store(session, store_id)
    except StoreNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from None


async def _out(session: SessionDep, threads: list[ChatThread]) -> list[ThreadOut]:
    orders = {
        o.id: o
        for o in await session.scalars(
            select(Order).where(Order.id.in_([t.order_id for t in threads if t.order_id]))
        )
    }
    keys = {c.id: c.connector_key for c in await session.scalars(select(MarketplaceConnection))}
    out = []
    for t in threads:
        order = orders.get(t.order_id) if t.order_id else None
        url = ORDER_URLS.get(keys.get(t.connection_id, ""))
        out.append(
            ThreadOut(
                id=t.id,
                order_id=t.order_id,
                order_remote_id=order.remote_id if order else None,
                order_url=url.format(id=order.remote_id) if url and order else None,
                order_state=order.state if order else None,
                buyer_name=order.buyer_name if order else None,
                offer_title=order.offer_title if order else None,
                last_buyer_at=t.last_buyer_at,
                last_buyer_text=t.last_buyer_text,
                last_seller_at=t.last_seller_at,
                last_alert_at=t.last_alert_at,
                urgent=t.urgent,
                needs_reply=t.needs_reply,
                handled_at=t.handled_at,
                handled_by=t.handled_by,
            )
        )
    return out


@router.get("", response_model=list[ThreadOut])
async def list_threads(
    store_id: int,
    auth: AuthDep,
    session: SessionDep,
    show: Literal["attention", "all"] = "attention",
    limit: int = Query(100, ge=1, le=300),
) -> list[ThreadOut]:
    await _store(session, store_id)
    rows = list(
        await session.scalars(
            select(ChatThread)
            .where(ChatThread.store_id == store_id, ChatThread.last_buyer_at.is_not(None))
            .order_by(ChatThread.last_buyer_at.desc())
            .limit(300 if show == "attention" else limit)
        )
    )
    if show == "attention":
        rows = [t for t in rows if t.needs_reply][:limit]
    return await _out(session, rows)


@router.get("/summary")
async def summary(store_id: int, auth: AuthDep, session: SessionDep) -> dict[str, int]:
    await _store(session, store_id)
    rows = await session.scalars(
        select(ChatThread)
        .where(ChatThread.store_id == store_id, ChatThread.last_buyer_at.is_not(None))
        .order_by(ChatThread.last_buyer_at.desc())
        .limit(300)
    )
    waiting = [t for t in rows if t.needs_reply]
    return {"needs_reply": len(waiting), "urgent": sum(1 for t in waiting if t.urgent)}


@router.post("/{thread_id}/handled", response_model=ThreadOut)
async def mark_handled(
    store_id: int, thread_id: int, auth: OwnerDep, session: SessionDep
) -> ThreadOut:
    thread = await session.get(ChatThread, thread_id)
    if thread is None or thread.store_id != store_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found")
    await service.mark_handled(session, thread, auth.actor.label)
    audit.record(
        session,
        auth.actor,
        "chat.marked_handled",
        entity_type="chat_thread",
        entity_id=thread.id,
        store_id=store_id,
    )
    await session.commit()
    return (await _out(session, [thread]))[0]


@router.post("/scan", status_code=status.HTTP_202_ACCEPTED)
async def scan_now(store_id: int, auth: OwnerDep, session: SessionDep) -> dict[str, int]:
    await _store(session, store_id)
    job_id = await jobs.enqueue(
        session,
        service.SCAN_JOB,
        {},
        store_id=store_id,
        idempotency_key=f"chat.scan:manual:{store_id}:{int(datetime.now().timestamp() // 10)}",
    )
    await session.commit()
    return {"job_id": job_id}
