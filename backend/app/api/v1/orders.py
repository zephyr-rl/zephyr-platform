from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import AuthDep, OwnerDep, SessionDep
from app.core import jobs
from app.modules.deliveries import service
from app.orders.models import (
    ACTIVE_STATES,
    Fulfilment,
    FulfilmentEvent,
    FulfilmentState,
    Order,
    Reservation,
)
from app.stock.models import StockItem
from app.stores.service import NotFound as StoreNotFound
from app.stores.service import get_store

router = APIRouter(prefix="/stores/{store_id}/orders", tags=["orders"])

ATTENTION = (
    FulfilmentState.needs_attention,
    FulfilmentState.awaiting_approval,
    FulfilmentState.awaiting_owner,
)
WAITING = (
    FulfilmentState.awaiting_buyer,
    FulfilmentState.pending,
    FulfilmentState.reserved,
    FulfilmentState.sent,
)


class OrderOut(BaseModel):
    id: int
    remote_id: str
    offer_title: str | None
    category: str | None
    quantity: int
    total: Decimal
    currency: str
    buyer_name: str | None
    remote_state: str | None
    created_at: datetime
    fulfilment_id: int | None
    fulfilment_state: str | None
    mode: str | None
    shadow: bool
    units: int | None
    last_error: str | None


class EventOut(BaseModel):
    at: datetime
    actor: str
    event: str
    message: str


class OrderDetail(OrderOut):
    events: list[EventOut]
    reserved: list[dict[str, str]]
    pool_name: str | None


class ResolveIn(BaseModel):
    reason: str = Field("", max_length=500)


def _out(order: Order, f: Fulfilment | None) -> OrderOut:
    return OrderOut(
        id=order.id,
        remote_id=order.remote_id,
        offer_title=order.offer_title,
        category=order.category,
        quantity=order.quantity,
        total=order.total_amount,
        currency=order.currency,
        buyer_name=order.buyer_name,
        remote_state=order.remote_state,
        created_at=order.remote_created_at,
        fulfilment_id=f.id if f else None,
        fulfilment_state=f.state.value if f else None,
        mode=f.mode if f else None,
        shadow=f.shadow if f else False,
        units=f.units if f else None,
        last_error=f.last_error if f else None,
    )


async def _store(session: SessionDep, store_id: int) -> None:
    try:
        await get_store(session, store_id)
    except StoreNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from None


async def _fulfilment(
    session: SessionDep, store_id: int, order_id: int, *, lock: bool = False
) -> tuple[Order, Fulfilment]:
    order = await session.get(Order, order_id)
    if order is None or order.store_id != store_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Order not found.")
    stmt = select(Fulfilment).where(Fulfilment.order_id == order.id)
    if lock:
        stmt = stmt.with_for_update(nowait=False)
    f = await session.scalar(stmt)
    if f is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "This order has nothing to deliver.")
    return order, f


@router.get("", response_model=list[OrderOut])
async def list_orders(
    store_id: int,
    auth: AuthDep,
    session: SessionDep,
    show: Literal["attention", "waiting", "active", "all"] = "all",
    limit: int = Query(100, ge=1, le=500),
) -> list[OrderOut]:
    await _store(session, store_id)
    stmt = (
        select(Order, Fulfilment)
        .outerjoin(Fulfilment, Fulfilment.order_id == Order.id)
        .where(Order.store_id == store_id)
        .order_by(Order.remote_created_at.desc())
        .limit(limit)
    )
    if show == "attention":
        stmt = stmt.where(Fulfilment.state.in_(ATTENTION))
    elif show == "waiting":
        stmt = stmt.where(Fulfilment.state.in_(WAITING))
    elif show == "active":
        stmt = stmt.where(Fulfilment.state.in_(ACTIVE_STATES | set(ATTENTION)))
    return [_out(o, f) for o, f in await session.execute(stmt)]


@router.get("/summary")
async def summary(store_id: int, auth: AuthDep, session: SessionDep) -> dict[str, int]:
    rows = await session.execute(
        select(Fulfilment.state).where(
            Fulfilment.store_id == store_id, Fulfilment.state.in_(ATTENTION + WAITING)
        )
    )
    states = [s for (s,) in rows]
    return {
        "attention": sum(s in ATTENTION for s in states),
        "waiting": sum(s in WAITING for s in states),
    }


@router.get("/{order_id}", response_model=OrderDetail)
async def order_detail(
    store_id: int, order_id: int, auth: AuthDep, session: SessionDep
) -> OrderDetail:
    order = await session.get(Order, order_id)
    if order is None or order.store_id != store_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Order not found.")
    f = await session.scalar(select(Fulfilment).where(Fulfilment.order_id == order.id))
    events: list[EventOut] = []
    reserved: list[dict[str, str]] = []
    pool_name = None
    if f is not None:
        events = [
            EventOut(at=e.at, actor=e.actor_label, event=e.event, message=e.message)
            for e in await session.scalars(
                select(FulfilmentEvent)
                .where(FulfilmentEvent.fulfilment_id == f.id)
                .order_by(FulfilmentEvent.id)
            )
        ]
        reserved = [
            {"login": i.login_hint, "state": i.state.value}
            for i in await session.scalars(
                select(StockItem)
                .join(Reservation, Reservation.item_id == StockItem.id)
                .where(Reservation.fulfilment_id == f.id)
            )
        ]
        if f.pool_id:
            pool_name = (await service.pools_by_id(session, store_id)).get(f.pool_id)
    return OrderDetail(
        **_out(order, f).model_dump(), events=events, reserved=reserved, pool_name=pool_name
    )


@router.post("/{order_id}/approve", response_model=OrderOut)
async def approve(store_id: int, order_id: int, auth: OwnerDep, session: SessionDep) -> OrderOut:
    order, f = await _fulfilment(session, store_id, order_id, lock=True)
    try:
        await service.approve(session, auth.actor, f)
    except service.ActionError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    await session.commit()
    return _out(order, f)


@router.post("/{order_id}/retry", response_model=OrderOut)
async def retry(store_id: int, order_id: int, auth: OwnerDep, session: SessionDep) -> OrderOut:
    order, f = await _fulfilment(session, store_id, order_id, lock=True)
    try:
        await service.retry(session, auth.actor, f)
    except service.ActionError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    await session.commit()
    return _out(order, f)


@router.post("/{order_id}/resolve", response_model=OrderOut)
async def resolve(
    store_id: int, order_id: int, body: ResolveIn, auth: OwnerDep, session: SessionDep
) -> OrderOut:
    order, f = await _fulfilment(session, store_id, order_id, lock=True)
    try:
        await service.resolve(session, auth.actor, f, body.reason)
    except service.ActionError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    await session.commit()
    return _out(order, f)


@router.post("/poll", status_code=status.HTTP_202_ACCEPTED)
async def poll_now(store_id: int, auth: OwnerDep, session: SessionDep) -> dict[str, int]:
    """Ask the worker to read the orders now (instead of waiting for the next minute)."""
    await _store(session, store_id)
    job_id = await jobs.enqueue(
        session,
        "orders.poll",
        {},
        store_id=store_id,
        idempotency_key=f"orders.poll:manual:{store_id}:{int(datetime.now().timestamp() // 10)}",
    )
    await session.commit()
    return {"job_id": job_id}
