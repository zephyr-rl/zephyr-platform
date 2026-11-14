from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import AuthDep, OwnerDep, PlatformDep, SessionDep
from app.core.crypto import CryptoError
from app.listings import service
from app.listings.models import DeliveryMode, Listing
from app.stock.models import StockPool
from app.stores.models import MarketplaceConnection
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


class ListingPatch(BaseModel):
    pool_id: int | None = None
    clear_pool: bool = False
    delivery_mode: DeliveryMode | None = None
    units_per_sale: int | None = Field(None, ge=1, le=100)
    require_confirmation: bool | None = None
    delivery_template: str | None = Field(None, max_length=4000)
    vault_additional_info: str | None = Field(None, max_length=1000)
    include_email_details: bool | None = None


class SyncOut(BaseModel):
    created: int
    updated: int
    removed: int
    errors: list[str]


async def _out(session: SessionDep, listings: list[Listing]) -> list[ListingOut]:
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
    except service.ListingError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    except CryptoError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from None
    await session.commit()
    return SyncOut(**result.__dict__)


@router.patch("/{listing_id}", response_model=ListingOut)
async def update(
    store_id: int, listing_id: int, body: ListingPatch, auth: OwnerDep, session: SessionDep
) -> ListingOut:
    changes = body.model_dump(exclude_none=True, exclude={"clear_pool"})
    if body.clear_pool:
        changes["pool_id"] = None
    try:
        listing = await service.get_listing(session, store_id, listing_id)
        await service.update_listing(session, auth.actor, listing, changes)
    except service.NotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from None
    except service.ListingError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    await session.commit()
    return (await _out(session, [listing]))[0]
