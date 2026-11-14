"""Listing sync (read-only) and fulfilment settings."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.base import ConnectorError, OfferKind
from app.connectors.runtime import open_connector
from app.core import audit, events
from app.core.db import utcnow
from app.core.platform import Platform
from app.listings.models import NEEDS_POOL, DeliveryMode, Listing
from app.stock.models import StockPool
from app.stores.models import MarketplaceConnection


class ListingError(Exception):
    pass


class NotFound(ListingError):
    pass


@dataclass
class SyncResult:
    created: int = 0
    updated: int = 0
    removed: int = 0
    errors: list[str] = field(default_factory=list)


async def sync_listings(
    session: AsyncSession, platform: Platform, actor: audit.Actor, store_id: int
) -> SyncResult:
    """Read the store's offers from each marketplace connection and update the listings.
    Only reads: nothing is changed on the marketplace."""
    result = SyncResult()
    connections = list(
        await session.scalars(
            select(MarketplaceConnection).where(MarketplaceConnection.store_id == store_id)
        )
    )
    if not connections:
        raise ListingError("Connect a marketplace to this store first.")
    now = utcnow()
    for connection in connections:
        connector = await open_connector(session, platform, connection)
        try:
            offers = await connector.list_offers()
        except ConnectorError as exc:
            connection.status, connection.status_detail = "error", str(exc)
            result.errors.append(f"{connection.connector_key}: {exc}")
            continue
        finally:
            await connector.aclose()
        connection.status, connection.last_checked_at = "connected", now
        existing = {
            listing.remote_offer_id: listing
            for listing in await session.scalars(
                select(Listing).where(Listing.connection_id == connection.id)
            )
        }
        seen: set[str] = set()
        for offer in offers:
            seen.add(offer.remote_id)
            listing = existing.get(offer.remote_id)
            values = {
                "kind": offer.kind.value,
                "title": offer.title[:300],
                "remote_state": offer.state.value,
                "price_amount": offer.price.amount,
                "currency": offer.price.currency[:3],
                "remote_quantity": offer.quantity,
                "removed_remotely": False,
                "last_synced_at": now,
            }
            if listing is None:
                session.add(
                    Listing(
                        store_id=store_id,
                        connection_id=connection.id,
                        remote_offer_id=offer.remote_id,
                        delivery_mode=DeliveryMode.manual,
                        **values,
                    )
                )
                result.created += 1
            else:
                for key, value in values.items():
                    setattr(listing, key, value)
                result.updated += 1
        for remote_id, listing in existing.items():
            if remote_id not in seen and not listing.removed_remotely:
                listing.removed_remotely = True
                result.removed += 1
    audit.record(
        session,
        actor,
        "listings.synced",
        store_id=store_id,
        after={
            "created": result.created,
            "updated": result.updated,
            "removed": result.removed,
            "errors": len(result.errors),
        },
    )
    return result


SETTINGS = (
    "pool_id",
    "delivery_mode",
    "units_per_sale",
    "require_confirmation",
    "delivery_template",
    "vault_additional_info",
    "include_email_details",
    "restock_enabled",
    "restock_target",
    "auto_resume",
)


async def get_listing(session: AsyncSession, store_id: int, listing_id: int) -> Listing:
    listing = await session.get(Listing, listing_id)
    if listing is None or listing.store_id != store_id:
        raise NotFound(f"Listing {listing_id} not found")
    return listing


async def update_listing(
    session: AsyncSession, actor: audit.Actor, listing: Listing, changes: dict[str, Any]
) -> Listing:
    before = {k: _plain(getattr(listing, k)) for k in SETTINGS}
    merged = {**before, **{k: v for k, v in changes.items() if k in SETTINGS}}
    mode = DeliveryMode(merged["delivery_mode"])
    pool_id = merged["pool_id"]

    if pool_id is not None:
        pool = await session.get(StockPool, pool_id)
        if pool is None or pool.store_id != listing.store_id:
            raise ListingError("That stock pool does not belong to this store.")
        if pool.archived:
            raise ListingError(f"The pool {pool.name} is archived.")
    if mode in NEEDS_POOL and pool_id is None:
        label = "Automatic" if mode is DeliveryMode.auto else "Assisted"
        raise ListingError(f"{label} delivery needs a stock pool.")
    if listing.kind == OfferKind.topup.value and mode in NEEDS_POOL:
        raise ListingError(
            "Top-up listings are fulfilled by hand or with a message, not from stock."
        )
    if mode is DeliveryMode.text and not (merged["delivery_template"] or "").strip():
        raise ListingError("Message-only delivery needs the message to send.")
    if not 1 <= int(merged["units_per_sale"]) <= 100:
        raise ListingError("Units per sale must be between 1 and 100.")
    if merged["restock_target"] is not None and not 1 <= int(merged["restock_target"]) <= 100:
        raise ListingError("The vault target must be between 1 and 100.")
    if merged["restock_enabled"]:
        if listing.kind != OfferKind.account.value:
            raise ListingError("Only account listings with instant delivery can be restocked.")
        if pool_id is None:
            raise ListingError("Restock needs a stock pool.")
        if mode is not DeliveryMode.manual:
            raise ListingError(
                "Restocked listings are delivered by the marketplace from the vault: "
                "set the delivery to Manual."
            )

    for key in SETTINGS:
        setattr(listing, key, merged[key] if key != "delivery_mode" else mode)
    after = {k: _plain(getattr(listing, k)) for k in SETTINGS}
    if after != before:
        audit.record(
            session,
            actor,
            "listing.updated",
            entity_type="listing",
            entity_id=listing.id,
            store_id=listing.store_id,
            before=before,
            after=after,
        )
        events.publish(
            session,
            "config.changed",
            {"entity": "listing", "id": listing.id},
            store_id=listing.store_id,
        )
    return listing


def _plain(value: Any) -> Any:
    return value.value if isinstance(value, DeliveryMode) else value
