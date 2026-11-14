from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import AuthDep, OwnerDep, PlatformDep, SessionDep
from app.connectors.base import ConnectorError
from app.core.db import utcnow
from app.modules.review_reminders import service
from app.stores.models import Store
from app.stores.service import NotFound as StoreNotFound
from app.stores.service import get_store

router = APIRouter(prefix="/stores/{store_id}/reminders", tags=["review reminders"])


class CandidateOut(BaseModel):
    order_id: int
    remote_id: str
    buyer: str
    title: str | None
    placed_at: datetime
    rating: int | None


class ReminderOut(BaseModel):
    id: int
    remote_order_id: str
    buyer_name: str | None
    state: str
    note: str | None
    queued_by: str
    created_at: datetime
    sent_at: datetime | None


class StatusOut(BaseModel):
    enabled: bool
    automatic: bool
    shadow: bool
    daily_cap: int
    sent_last_day: int
    queued: int
    review_filter: str
    history: list[ReminderOut]


class QueueIn(BaseModel):
    order_ids: list[int] = Field(min_length=1, max_length=200)


async def _store(session: SessionDep, store_id: int) -> Store:
    try:
        return await get_store(session, store_id)
    except StoreNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from None


@router.get("", response_model=StatusOut)
async def reminders_status(store_id: int, auth: AuthDep, session: SessionDep) -> StatusOut:
    store = await _store(session, store_id)
    settings, enabled = await service.settings_for(session, store_id)
    return StatusOut(
        enabled=enabled,
        automatic=settings.automatic,
        shadow=store.shadow_mode,
        daily_cap=settings.daily_cap,
        sent_last_day=await service.sent_last_day(session, store_id, utcnow()),
        queued=await service.queued_count(session, store_id),
        review_filter=settings.review_filter,
        history=[
            ReminderOut(
                id=r.id,
                remote_order_id=r.remote_order_id,
                buyer_name=r.buyer_name,
                state=r.state.value,
                note=r.note,
                queued_by=r.queued_by,
                created_at=r.created_at,
                sent_at=r.sent_at,
            )
            for r in await service.history(session, store_id)
        ],
    )


@router.get("/candidates", response_model=list[CandidateOut])
async def list_candidates(
    store_id: int, auth: AuthDep, platform: PlatformDep, session: SessionDep
) -> list[CandidateOut]:
    """Buyers who would get a reminder now (reads the store's reviews from the marketplace)."""
    await _store(session, store_id)
    try:
        found = await service.candidates(session, platform, store_id)
    except ConnectorError as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"Could not read the reviews: {exc}"
        ) from None
    return [CandidateOut(**c.__dict__) for c in found]


@router.post("/queue")
async def queue(
    store_id: int, body: QueueIn, auth: OwnerDep, platform: PlatformDep, session: SessionDep
) -> dict[str, int]:
    await _store(session, store_id)
    try:
        count = await service.queue(session, platform, auth.actor, store_id, body.order_ids)
    except service.ReminderError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    except ConnectorError as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"Could not read the reviews: {exc}"
        ) from None
    await session.commit()
    return {"queued": count}


@router.post("/cancel")
async def cancel(store_id: int, auth: OwnerDep, session: SessionDep) -> dict[str, int]:
    await _store(session, store_id)
    count = await service.cancel_queued(session, auth.actor, store_id)
    await session.commit()
    return {"cancelled": count}
