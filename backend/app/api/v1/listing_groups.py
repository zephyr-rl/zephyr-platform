"""Cross-platform cards (PC / PlayStation / Xbox offers of one product, one stock pool)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.api.deps import AuthDep, OwnerDep, PlatformDep, SessionDep
from app.core import audit
from app.core.crypto import CryptoError
from app.listings import groups, service
from app.listings.models import DEVICES, Listing, ListingGroup
from app.modules.catalog import RestockSettings
from app.modules.restock import service as restock
from app.stock.models import ItemState, StockItem, StockPool
from app.stores.models import StoreModule

router = APIRouter(prefix="/stores/{store_id}/listing-groups", tags=["cross-platform cards"])


class DeviceOut(BaseModel):
    device: str
    label: str
    remote_offer_id: str | None
    listing_id: int | None
    title: str | None
    remote_state: str | None
    price: Decimal | None
    currency: str | None
    vault_count: int | None
    remote_quantity: int | None
    removed_remotely: bool
    last_restock_at: datetime | None
    last_restock_note: str | None


class GroupOut(BaseModel):
    id: int
    title: str
    pool_id: int | None
    pool_name: str | None
    pool_available: int | None
    restock_enabled: bool
    restock_target: int | None
    effective_target: int
    auto_resume: bool
    vault_additional_info: str | None
    include_email_details: bool
    imported: bool
    devices: list[DeviceOut]


class GroupIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    pool_id: int | None = None
    restock_enabled: bool = False
    restock_target: int | None = Field(None, ge=1, le=100)
    auto_resume: bool = True
    vault_additional_info: str | None = Field(None, max_length=1000)
    include_email_details: bool = False
    # platform → listing id; None or missing: no offer for that platform
    devices: dict[str, int | None] = Field(default_factory=dict)


class EnabledIn(BaseModel):
    enabled: bool


class DeviceRestock(BaseModel):
    device: str
    label: str
    summary: str
    uploaded: int
    vault_count: int | None


class GroupRestockOut(BaseModel):
    results: list[DeviceRestock]
    shadow: bool


async def _restock_settings(session: SessionDep, store_id: int) -> tuple[bool, RestockSettings]:
    module = await session.scalar(
        select(StoreModule).where(
            StoreModule.store_id == store_id, StoreModule.module_key == "restock"
        )
    )
    on = module is not None and module.enabled
    return on, RestockSettings.model_validate(module.settings if module else {})


async def _out(session: SessionDep, store_id: int, items: list[ListingGroup]) -> list[GroupOut]:
    _, settings = await _restock_settings(session, store_id)
    pools = {
        p.id: p.name
        for p in await session.scalars(select(StockPool).where(StockPool.store_id == store_id))
    }
    available = dict(
        (pid, n)
        for pid, n in await session.execute(
            select(StockItem.pool_id, func.count())
            .where(StockItem.store_id == store_id, StockItem.state == ItemState.available)
            .group_by(StockItem.pool_id)
        )
    )
    listings = {
        (x.group_id, x.device): x
        for x in await session.scalars(
            select(Listing).where(Listing.store_id == store_id, Listing.group_id.is_not(None))
        )
    }
    out = []
    for g in items:
        devices = []
        for device, label in DEVICES.items():
            x = listings.get((g.id, device))
            devices.append(
                DeviceOut(
                    device=device,
                    label=label,
                    remote_offer_id=(g.devices or {}).get(device),
                    listing_id=x.id if x else None,
                    title=x.title if x else None,
                    remote_state=x.remote_state if x else None,
                    price=x.price_amount if x else None,
                    currency=x.currency if x else None,
                    vault_count=x.vault_count if x else None,
                    remote_quantity=x.remote_quantity if x else None,
                    removed_remotely=bool(x and x.removed_remotely),
                    last_restock_at=x.last_restock_at if x else None,
                    last_restock_note=x.last_restock_note if x else None,
                )
            )
        out.append(
            GroupOut(
                id=g.id,
                title=g.title,
                pool_id=g.pool_id,
                pool_name=pools.get(g.pool_id) if g.pool_id else None,
                pool_available=available.get(g.pool_id, 0) if g.pool_id else None,
                restock_enabled=g.restock_enabled,
                restock_target=g.restock_target,
                effective_target=g.restock_target or settings.target_quantity,
                auto_resume=g.auto_resume,
                vault_additional_info=g.vault_additional_info,
                include_email_details=g.include_email_details,
                imported=g.legacy_id is not None,
                devices=devices,
            )
        )
    return out


def _error(exc: service.ListingError) -> HTTPException:
    if isinstance(exc, service.NotFound):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    return HTTPException(status.HTTP_409_CONFLICT, str(exc))


@router.get("", response_model=list[GroupOut])
async def list_groups(store_id: int, auth: AuthDep, session: SessionDep) -> list[GroupOut]:
    items = list(
        await session.scalars(
            select(ListingGroup)
            .where(ListingGroup.store_id == store_id)
            .order_by(ListingGroup.title)
        )
    )
    return await _out(session, store_id, items)


@router.post("", response_model=GroupOut, status_code=status.HTTP_201_CREATED)
async def create_group(
    store_id: int, body: GroupIn, auth: OwnerDep, session: SessionDep
) -> GroupOut:
    try:
        group, _ = await groups.save_group(session, auth.actor, store_id, body.model_dump())
    except service.ListingError as exc:
        raise _error(exc) from None
    await session.commit()
    return (await _out(session, store_id, [group]))[0]


@router.put("/{group_id}", response_model=GroupOut)
async def update_group(
    store_id: int, group_id: int, body: GroupIn, auth: OwnerDep, session: SessionDep
) -> GroupOut:
    try:
        group = await groups.get_group(session, store_id, group_id)
        await groups.save_group(session, auth.actor, store_id, body.model_dump(), group)
    except service.ListingError as exc:
        raise _error(exc) from None
    await session.commit()
    return (await _out(session, store_id, [group]))[0]


@router.post("/{group_id}/enabled", response_model=GroupOut)
async def set_enabled(
    store_id: int, group_id: int, body: EnabledIn, auth: OwnerDep, session: SessionDep
) -> GroupOut:
    try:
        group = await groups.get_group(session, store_id, group_id)
        await groups.set_enabled(session, auth.actor, group, body.enabled)
    except service.ListingError as exc:
        raise _error(exc) from None
    await session.commit()
    return (await _out(session, store_id, [group]))[0]


@router.delete("/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_group(store_id: int, group_id: int, auth: OwnerDep, session: SessionDep) -> None:
    try:
        group = await groups.get_group(session, store_id, group_id)
        await groups.delete_group(session, auth.actor, group)
    except service.ListingError as exc:
        raise _error(exc) from None
    await session.commit()


@router.post("/{group_id}/restock", response_model=GroupRestockOut)
async def restock_group(
    store_id: int, group_id: int, auth: OwnerDep, platform: PlatformDep, session: SessionDep
) -> GroupRestockOut:
    """Check each platform's vault now and top it up (same rules as the automatic pass)."""
    try:
        group = await groups.get_group(session, store_id, group_id)
    except service.ListingError as exc:
        raise _error(exc) from None
    on, _ = await _restock_settings(session, store_id)
    if not on:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Turn on the Restock module for this store first."
        )
    targets = [
        (x.id, x.device or "")
        for x in await groups.members(session, group)
        if restock.check_ready(x) is None
    ]
    if not targets:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Nothing to restock: turn restock on and choose the pool and the offers.",
        )
    audit.record(
        session,
        auth.actor,
        "listing_group.restock_requested",
        entity_type="listing_group",
        entity_id=group.id,
        store_id=store_id,
    )
    await session.commit()
    results, shadow = [], False
    for listing_id, device in targets:
        try:
            r = await restock.restock_listing(platform, listing_id)
        except CryptoError as exc:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from None
        listing = await session.get(Listing, listing_id)
        if listing is not None:
            await session.refresh(listing)
        shadow = shadow or r.shadow
        results.append(
            DeviceRestock(
                device=device,
                label=DEVICES.get(device, device),
                summary=r.summary,
                uploaded=r.uploaded,
                vault_count=listing.vault_count if listing else None,
            )
        )
    return GroupRestockOut(results=results, shadow=shadow)
