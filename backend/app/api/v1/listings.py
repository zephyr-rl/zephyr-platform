from __future__ import annotations

from datetime import datetime, time
from decimal import Decimal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.api.deps import AuthDep, OwnerDep, PlatformDep, SessionDep
from app.api.v1.orders import ATTENTION
from app.core import audit
from app.core.crypto import CryptoError
from app.listings import groups, schedule, service
from app.listings.models import DeliveryMode, Listing
from app.modules.restock import service as restock
from app.orders.models import Fulfilment, Order
from app.stock.models import StockPool
from app.stores.models import MarketplaceConnection, StoreModule
from app.stores.service import NotFound as StoreNotFound
from app.stores.service import get_store

router = APIRouter(prefix="/stores/{store_id}/listings", tags=["listings"])


class ListingOut(BaseModel):
    id: int
    connection_id: int
    connector: str
    remote_offer_id: str
    kind: str
    title: str
    remote_state: str
    price: Decimal
    currency: str
    remote_quantity: int | None
    removed_remotely: bool
    last_synced_at: datetime | None
    pool_id: int | None
    pool_name: str | None
    delivery_mode: str
    units_per_sale: int
    require_confirmation: bool
    delivery_template: str | None
    vault_additional_info: str | None
    include_email_details: bool
    restock_enabled: bool
    restock_target: int | None
    auto_resume: bool
    vault_count: int | None
    last_restock_at: datetime | None
    last_restock_note: str | None
    group_id: int | None
    device: str | None
    platform: str | None
    orders_need_you: int  # orders of this listing waiting for the owner (top-ups, approvals)
    schedule_off: str | None  # "HH:MM", store time
    schedule_on: str | None
    schedule_note: str | None


class ListingPatch(BaseModel):
    pool_id: int | None = None
    clear_pool: bool = False
    delivery_mode: DeliveryMode | None = None
    units_per_sale: int | None = Field(None, ge=1, le=100)
    require_confirmation: bool | None = None
    delivery_template: str | None = Field(None, max_length=4000)
    vault_additional_info: str | None = Field(None, max_length=1000)
    include_email_details: bool | None = None
    restock_enabled: bool | None = None
    restock_target: int | None = Field(None, ge=1, le=100)
    clear_restock_target: bool = False
    auto_resume: bool | None = None


class ScheduleIn(BaseModel):
    off: time | None = Field(None, description="Pause time, store time zone (HH:MM).")
    on: time | None = Field(None, description="Turn-on time, store time zone (HH:MM).")


class RestockOut(BaseModel):
    uploaded: int
    quarantined: int
    resolved: int
    resumed: bool
    shadow: bool
    vault_count: int | None
    target: int
    summary: str
    ran: bool


class UploadOut(BaseModel):
    id: int
    item_id: int
    login_hint: str
    state: str
    error: str | None
    created_at: datetime
    resolved_at: datetime | None


class SyncOut(BaseModel):
    created: int
    updated: int
    removed: int
    errors: list[str]
    linked: int = 0  # offers attached to the cross-platform cards that were waiting for them


async def _out(session: SessionDep, listings: list[Listing]) -> list[ListingOut]:
    need_you = (
        dict(
            (lid, n)
            for lid, n in await session.execute(
                select(Order.listing_id, func.count())
                .join(Fulfilment, Fulfilment.order_id == Order.id)
                .where(
                    Order.listing_id.in_([x.id for x in listings]),
                    Fulfilment.state.in_(ATTENTION),
                )
                .group_by(Order.listing_id)
            )
        )
        if listings
        else {}
    )
    pools = {p.id: p.name for p in await session.scalars(select(StockPool))} if listings else {}
    connectors = (
        {c.id: c.connector_key for c in await session.scalars(select(MarketplaceConnection))}
        if listings
        else {}
    )
    return [
        ListingOut(
            id=x.id,
            connection_id=x.connection_id,
            connector=connectors.get(x.connection_id, ""),
            remote_offer_id=x.remote_offer_id,
            kind=x.kind,
            title=x.title,
            remote_state=x.remote_state,
            price=x.price_amount,
            currency=x.currency,
            remote_quantity=x.remote_quantity,
            removed_remotely=x.removed_remotely,
            last_synced_at=x.last_synced_at,
            pool_id=x.pool_id,
            pool_name=pools.get(x.pool_id) if x.pool_id else None,
            delivery_mode=x.delivery_mode.value,
            units_per_sale=x.units_per_sale,
            require_confirmation=x.require_confirmation,
            delivery_template=x.delivery_template,
            vault_additional_info=x.vault_additional_info,
            include_email_details=x.include_email_details,
            restock_enabled=x.restock_enabled,
            restock_target=x.restock_target,
            auto_resume=x.auto_resume,
            vault_count=x.vault_count,
            last_restock_at=x.last_restock_at,
            last_restock_note=x.last_restock_note,
            group_id=x.group_id,
            device=x.device,
            platform=x.platform,
            orders_need_you=need_you.get(x.id, 0),
            schedule_off=f"{x.schedule_off:%H:%M}" if x.schedule_off else None,
            schedule_on=f"{x.schedule_on:%H:%M}" if x.schedule_on else None,
            schedule_note=x.schedule_note,
        )
        for x in listings
    ]


async def _store(session: SessionDep, store_id: int) -> None:
    try:
        await get_store(session, store_id)
    except StoreNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from None


@router.get("", response_model=list[ListingOut])
async def list_listings(store_id: int, auth: AuthDep, session: SessionDep) -> list[ListingOut]:
    await _store(session, store_id)
    rows = list(
        await session.scalars(
            select(Listing)
            .where(Listing.store_id == store_id)
            .order_by(Listing.removed_remotely, Listing.kind, Listing.title)
        )
    )
    return await _out(session, rows)


@router.post("/sync", response_model=SyncOut)
async def sync(
    store_id: int, auth: OwnerDep, platform: PlatformDep, session: SessionDep
) -> SyncOut:
    await _store(session, store_id)
    try:
        result = await service.sync_listings(session, platform, auth.actor, store_id)
        await session.flush()
        linked = await groups.link_pending(session, auth.actor, store_id)
    except service.ListingError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    except CryptoError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from None
    await session.commit()
    return SyncOut(**result.__dict__, linked=linked)


@router.patch("/{listing_id}", response_model=ListingOut)
async def update(
    store_id: int, listing_id: int, body: ListingPatch, auth: OwnerDep, session: SessionDep
) -> ListingOut:
    changes = body.model_dump(exclude_none=True, exclude={"clear_pool", "clear_restock_target"})
    if body.clear_pool:
        changes["pool_id"] = None
    if body.clear_restock_target:
        changes["restock_target"] = None
    try:
        listing = await service.get_listing(session, store_id, listing_id)
        await service.update_listing(session, auth.actor, listing, changes)
    except service.NotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from None
    except service.ListingError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    await session.commit()
    return (await _out(session, [listing]))[0]


@router.post("/{listing_id}/restock", response_model=RestockOut)
async def restock_now(
    store_id: int, listing_id: int, auth: OwnerDep, platform: PlatformDep, session: SessionDep
) -> RestockOut:
    """Check the vault of one listing now and top it up (same rules as the automatic pass)."""
    try:
        listing = await service.get_listing(session, store_id, listing_id)
    except service.NotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from None
    module = await session.scalar(
        select(StoreModule).where(
            StoreModule.store_id == store_id, StoreModule.module_key == "restock"
        )
    )
    if module is None or not module.enabled:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Turn on the Restock module for this store first."
        )
    problem = restock.check_ready(listing)
    if problem:
        raise HTTPException(status.HTTP_409_CONFLICT, problem)
    audit.record(
        session,
        auth.actor,
        "listing.restock_requested",
        entity_type="listing",
        entity_id=listing.id,
        store_id=store_id,
    )
    await session.commit()
    try:
        result = await restock.restock_listing(platform, listing.id)
    except CryptoError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from None
    await session.refresh(listing)
    return RestockOut(
        uploaded=result.uploaded,
        quarantined=result.quarantined,
        resolved=result.resolved,
        resumed=result.resumed,
        shadow=result.shadow,
        vault_count=listing.vault_count,
        target=result.target,
        summary=result.summary,
        ran=result.skipped is None,
    )


@router.get("/{listing_id}/uploads", response_model=list[UploadOut])
async def uploads(
    store_id: int, listing_id: int, auth: AuthDep, session: SessionDep
) -> list[UploadOut]:
    try:
        listing = await service.get_listing(session, store_id, listing_id)
    except service.NotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from None
    return [
        UploadOut(
            id=u.id,
            item_id=u.item_id,
            login_hint=u.login_hint,
            state=u.state,
            error=u.error,
            created_at=u.created_at,
            resolved_at=u.resolved_at,
        )
        for u in await restock.recent_uploads(session, listing)
    ]


@router.put("/{listing_id}/schedule", response_model=ListingOut)
async def set_schedule(
    store_id: int, listing_id: int, body: ScheduleIn, auth: OwnerDep, session: SessionDep
) -> ListingOut:
    """Pause the offer every day at ``off`` and turn it back on at ``on`` (both null: remove)."""
    try:
        listing = await service.get_listing(session, store_id, listing_id)
        schedule.set_schedule(
            session,
            auth.actor,
            listing,
            body.off.replace(second=0, microsecond=0) if body.off else None,
            body.on.replace(second=0, microsecond=0) if body.on else None,
        )
    except service.NotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from None
    except schedule.ScheduleError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    await session.commit()
    return (await _out(session, [listing]))[0]
