"""Cross-platform cards: one product, one offer per platform, one stock pool.

The card holds the vault settings (pool, restock on/off, target, auto-resume, the extra text
uploaded with each account, e-mail details) and copies them to its listings. Restock then runs
per listing exactly as for a single offer, all of them drawing from the same pool.

A card can name an offer that is not synced yet (imported from the old system before "Read
offers from marketplace"): ``link_pending`` attaches it when the listing appears.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.base import OfferKind
from app.core import audit, events
from app.listings import service
from app.listings.models import DEVICES, DeliveryMode, Listing, ListingGroup
from app.stock.models import StockPool


class GroupError(service.ListingError):
    pass


def snapshot(group: ListingGroup) -> dict[str, Any]:
    return {
        "title": group.title,
        "pool_id": group.pool_id,
        "restock_enabled": group.restock_enabled,
        "restock_target": group.restock_target,
        "auto_resume": group.auto_resume,
        "vault_additional_info": group.vault_additional_info,
        "include_email_details": group.include_email_details,
        "devices": dict(group.devices or {}),
    }


async def get_group(session: AsyncSession, store_id: int, group_id: int) -> ListingGroup:
    group = await session.get(ListingGroup, group_id)
    if group is None or group.store_id != store_id:
        raise service.NotFound(f"Card {group_id} not found")
    return group


async def members(session: AsyncSession, group: ListingGroup) -> list[Listing]:
    return list(
        await session.scalars(
            select(Listing).where(Listing.group_id == group.id).order_by(Listing.id)
        )
    )


def _settings(group: ListingGroup) -> dict[str, Any]:
    return {
        "pool_id": group.pool_id,
        # Offers in the vault are delivered by the marketplace itself.
        "delivery_mode": DeliveryMode.manual,
        "restock_enabled": bool(group.restock_enabled and group.pool_id is not None),
        "restock_target": group.restock_target,
        "auto_resume": group.auto_resume,
        "vault_additional_info": group.vault_additional_info,
        "include_email_details": group.include_email_details,
    }


async def _release(session: AsyncSession, actor: audit.Actor, listing: Listing) -> None:
    """Out of the card: the listing keeps its settings but stops restocking by itself."""
    listing.group_id = None
    listing.device = None
    await service.update_listing(
        session, actor, listing, {"restock_enabled": False}, from_group=True
    )


async def apply(session: AsyncSession, actor: audit.Actor, group: ListingGroup) -> list[str]:
    """Attach the card's offers that exist as listings and copy the settings to them.
    Returns the platforms whose offer is not synced yet."""
    wanted = {d: str(o) for d, o in (group.devices or {}).items() if d in DEVICES and o}
    current = await members(session, group)
    by_offer = {
        x.remote_offer_id: x
        for x in await session.scalars(
            select(Listing).where(
                Listing.store_id == group.store_id,
                Listing.remote_offer_id.in_(list(wanted.values())),
            )
        )
    }
    for listing in current:
        if wanted.get(listing.device or "") != listing.remote_offer_id:
            await _release(session, actor, listing)
    missing: list[str] = []
    for device, offer_id in wanted.items():
        found = by_offer.get(offer_id)
        if found is None:
            missing.append(device)
            continue
        listing = found
        if listing.group_id not in (None, group.id):
            other = await session.get(ListingGroup, listing.group_id)
            raise GroupError(
                f"The {DEVICES[device]} offer is already in the card "
                f"“{other.title if other else listing.group_id}”."
            )
        listing.group_id, listing.device = group.id, device
        await service.update_listing(session, actor, listing, _settings(group), from_group=True)
    return missing


async def save_group(
    session: AsyncSession,
    actor: audit.Actor,
    store_id: int,
    data: dict[str, Any],
    group: ListingGroup | None = None,
) -> tuple[ListingGroup, list[str]]:
    """Create or update a card. ``data["devices"]`` maps platform → listing id (or None)."""
    title = str(data.get("title") or "").strip()
    if not title:
        raise GroupError("Give the card a name.")
    pool_id = data.get("pool_id")
    if pool_id is not None:
        pool = await session.get(StockPool, pool_id)
        if pool is None or pool.store_id != store_id:
            raise GroupError("That stock pool does not belong to this store.")
        if pool.archived:
            raise GroupError(f"The pool {pool.name} is archived.")
    target = data.get("restock_target")
    if target is not None and not 1 <= int(target) <= 100:
        raise GroupError("The vault target must be between 1 and 100.")
    if data.get("restock_enabled") and pool_id is None:
        raise GroupError("Restock needs a stock pool.")

    devices: dict[str, str] = {}
    for device, listing_id in (data.get("devices") or {}).items():
        if device not in DEVICES:
            raise GroupError(f"Unknown platform {device}.")
        if listing_id is None:
            continue
        listing = await service.get_listing(session, store_id, int(listing_id))
        if listing.kind != OfferKind.account.value:
            raise GroupError(f"“{listing.title[:60]}” is not an account offer.")
        if listing.remote_offer_id in devices.values():
            raise GroupError("The same offer is chosen for two platforms.")
        devices[device] = listing.remote_offer_id
    # Platforms not mentioned keep their (maybe not yet synced) offer.
    if group is not None:
        for device, offer_id in (group.devices or {}).items():
            if device not in (data.get("devices") or {}) and offer_id:
                devices.setdefault(device, offer_id)
    if not devices:
        raise GroupError("Choose the offer of at least one platform.")

    before = snapshot(group) if group else None
    if group is None:
        group = ListingGroup(store_id=store_id, title=title)
        session.add(group)
    group.title = title[:300]
    group.pool_id = pool_id
    group.restock_enabled = bool(data.get("restock_enabled"))
    group.restock_target = int(target) if target is not None else None
    group.auto_resume = bool(data.get("auto_resume", True))
    group.vault_additional_info = str(data.get("vault_additional_info") or "").strip() or None
    group.include_email_details = bool(data.get("include_email_details"))
    group.devices = devices
    await session.flush()
    missing = await apply(session, actor, group)
    audit.record(
        session,
        actor,
        "listing_group.saved",
        entity_type="listing_group",
        entity_id=group.id,
        store_id=store_id,
        before=before,
        after=snapshot(group),
    )
    events.publish(
        session, "config.changed", {"entity": "listing_group", "id": group.id}, store_id=store_id
    )
    return group, missing


async def set_enabled(
    session: AsyncSession, actor: audit.Actor, group: ListingGroup, enabled: bool
) -> list[str]:
    if enabled and group.pool_id is None:
        raise GroupError("Choose the stock pool first.")
    before = snapshot(group)
    group.restock_enabled = enabled
    missing = await apply(session, actor, group)
    audit.record(
        session,
        actor,
        "listing_group.saved",
        entity_type="listing_group",
        entity_id=group.id,
        store_id=group.store_id,
        before=before,
        after=snapshot(group),
    )
    return missing


async def delete_group(session: AsyncSession, actor: audit.Actor, group: ListingGroup) -> None:
    for listing in await members(session, group):
        await _release(session, actor, listing)
    audit.record(
        session,
        actor,
        "listing_group.deleted",
        entity_type="listing_group",
        entity_id=group.id,
        store_id=group.store_id,
        before=snapshot(group),
    )
    await session.delete(group)


async def link_pending(session: AsyncSession, actor: audit.Actor, store_id: int) -> int:
    """After a listing sync: attach offers that cards were waiting for. Returns how many."""
    linked = 0
    for group in await session.scalars(
        select(ListingGroup).where(ListingGroup.store_id == store_id)
    ):
        have = {x.device for x in await members(session, group)}
        if any(d not in have for d, o in (group.devices or {}).items() if o):
            before = len(have)
            missing = await apply(session, actor, group)
            linked += len([d for d, o in group.devices.items() if o]) - len(missing) - before
    return linked
