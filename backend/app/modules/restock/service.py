"""Restock: keep the marketplace vault of instant-delivery listings stocked from their pools.

One pass over a listing, holding a lock on that listing so two workers never upload to it at once:

    read the vault ── resolve uploads whose outcome was not known ── top up to the target
                                                                      └─ resume the offer if paused

Each account sent to the vault goes through committed steps, so a crash always leaves it known:

    available ── reserved + upload "pending" (committed) ── vault_add ─┐
        ok: delivered (I1), upload "uploaded"  ─────────────────────────┤
        refused: back to stock, upload "failed" ────────────────────────┤
        unknown (timeout, crash): stays "pending" ──────────────────────┘

A "pending" upload is resolved on the next pass by reading the vault: if the account is there it
was uploaded; if not, it may have been uploaded and sold already, so it is quarantined for review
instead of going back to stock (never sold twice).

- An account whose login is already in the vault is quarantined, not uploaded again.
- I9: in shadow mode the uploads are recorded as decisions and no stock is touched.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.base import (
    ConnectorError,
    MarketplaceConnector,
    OfferKind,
    OfferState,
    VaultEntry,
)
from app.connectors.runtime import open_connector
from app.core import audit, events
from app.core.db import utcnow
from app.core.platform import Platform
from app.listings.models import Listing, VaultUpload, VaultUploadState
from app.modules.catalog import RestockSettings
from app.stock import service as stock
from app.stock.models import ItemState, StockItem, StockItemEvent
from app.stores.models import MarketplaceConnection, Store, StoreModule

log = logging.getLogger(__name__)
ACTOR = audit.Actor.system("restock")
RUN_JOB = "restock.run"
LOCK_NAMESPACE = 7301  # pg advisory lock (namespace, listing id)
MAX_SKIPPED = 50  # duplicates quarantined in one pass before giving up


@dataclass
class ListingResult:
    listing_id: int
    title: str
    vault_before: int | None = None
    vault_after: int | None = None
    target: int = 0
    uploaded: int = 0
    resolved: int = 0
    quarantined: int = 0
    resumed: bool = False
    shadow: bool = False
    skipped: str | None = None  # why nothing ran (locked, not due, ...)
    notes: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        if self.skipped:
            return self.skipped
        parts = [f"Vault {self.vault_after if self.vault_after is not None else '?'}/{self.target}"]
        verb = "would upload" if self.shadow else "uploaded"
        if self.uploaded:
            parts.append(f"{verb} {self.uploaded}")
        if self.resolved:
            parts.append(f"{self.resolved} earlier upload(s) confirmed")
        if self.quarantined:
            parts.append(f"{self.quarantined} quarantined")
        if self.resumed:
            parts.append("offer resumed" if not self.shadow else "would resume the offer")
        return ". ".join([", ".join(parts), *self.notes]).strip()


async def settings_for(session: AsyncSession, store_id: int) -> RestockSettings:
    row = await session.scalar(
        select(StoreModule).where(
            StoreModule.store_id == store_id, StoreModule.module_key == "restock"
        )
    )
    return RestockSettings.model_validate(row.settings if row else {})


def _norm(login: str) -> str:
    return login.strip().lower()


def _event(
    item: StockItem, event: str, before: str, after: str, listing: Listing, **details: object
) -> StockItemEvent:
    return StockItemEvent(
        item_id=item.id,
        actor_label=ACTOR.label,
        event=event,
        from_state=before,
        to_state=after,
        batch=f"vault:{listing.id}",
        details={"listing": listing.remote_offer_id, **details},
    )


# ---- store pass ----------------------------------------------------------------------------


async def run_store(platform: Platform, store_id: int) -> list[ListingResult]:
    """Restock every due listing of a store (the periodic task)."""
    async with platform.db.sessionmaker() as session:
        settings = await settings_for(session, store_id)
        due_before = utcnow() - timedelta(minutes=settings.interval_minutes) + timedelta(seconds=5)
        ids = list(
            await session.scalars(
                select(Listing.id)
                .where(
                    Listing.store_id == store_id,
                    Listing.restock_enabled.is_(True),
                    Listing.removed_remotely.is_(False),
                    (Listing.last_restock_at.is_(None)) | (Listing.last_restock_at <= due_before),
                )
                .order_by(Listing.id)
            )
        )
    results = []
    for listing_id in ids:
        try:
            results.append(await restock_listing(platform, listing_id))
        except Exception:  # one listing failing must not stop the others
            log.exception("restock failed for listing %s", listing_id)
    return results


# ---- listing pass --------------------------------------------------------------------------


async def restock_listing(platform: Platform, listing_id: int) -> ListingResult:
    """One pass over one listing. Safe to call at any time and from several workers."""
    async with platform.db.engine.connect() as lock_conn:
        got = await lock_conn.scalar(
            text("SELECT pg_try_advisory_lock(:ns, :id)"), {"ns": LOCK_NAMESPACE, "id": listing_id}
        )
        if not got:
            return ListingResult(
                listing_id, "", skipped="Another restock of this listing is running."
            )
        try:
            return await _pass(platform, listing_id)
        finally:
            await lock_conn.execute(
                text("SELECT pg_advisory_unlock(:ns, :id)"),
                {"ns": LOCK_NAMESPACE, "id": listing_id},
            )
            await lock_conn.commit()


async def _pass(platform: Platform, listing_id: int) -> ListingResult:
    sm = platform.db.sessionmaker
    async with sm() as session:
        listing = await session.get(Listing, listing_id)
        if listing is None:
            return ListingResult(listing_id, "", skipped="Listing not found.")
        result = ListingResult(listing.id, listing.title)
        problem = check_ready(listing)
        if problem:
            result.skipped = problem
            await _finish(session, listing, result)
            return result
        store = await session.get(Store, listing.store_id)
        connection = await session.get(MarketplaceConnection, listing.connection_id)
        assert store is not None and connection is not None
        settings = await settings_for(session, store.id)
        result.shadow = store.shadow_mode
        result.target = listing.restock_target or settings.target_quantity
        connector = await open_connector(session, platform, connection)
        try:
            await _run(session, platform, connector, listing, result)
        except ConnectorError as exc:
            await session.rollback()
            listing = await session.get(Listing, listing_id)
            assert listing is not None
            result.notes.append(f"Marketplace error: {exc}")
            if result.vault_before is not None:
                result.vault_after = result.vault_before + result.uploaded
        finally:
            await connector.aclose()
        await _finish(session, listing, result)
        return result


def check_ready(listing: Listing) -> str | None:
    if not listing.restock_enabled:
        return "Restock is off for this listing."
    if listing.kind != OfferKind.account.value:
        return "Only account listings have a vault."
    if listing.pool_id is None:
        return "Choose the stock pool to restock from."
    if listing.removed_remotely:
        return "The offer no longer exists on the marketplace."
    return None


async def _finish(session: AsyncSession, listing: Listing, result: ListingResult) -> None:
    listing.last_restock_at = utcnow()
    listing.last_restock_note = result.summary[:1000]
    if result.vault_after is not None:
        listing.vault_count = result.vault_after
    await session.commit()


async def _run(
    session: AsyncSession,
    platform: Platform,
    connector: MarketplaceConnector,
    listing: Listing,
    result: ListingResult,
) -> None:
    vault = await connector.vault_list(listing.remote_offer_id)
    in_vault = {_norm(e.login) for e in vault}
    result.vault_before = len(vault)

    if not result.shadow:
        await _resolve_pending(session, platform, listing, in_vault, result)
        await session.commit()

    needed = max(0, result.target - len(in_vault))
    if needed and result.shadow:
        await _shadow_uploads(session, platform, connector, listing, in_vault, needed, result)
    elif needed:
        await _upload(session, platform, connector, listing, in_vault, needed, result)

    count = result.vault_before + result.uploaded
    result.vault_after = count
    if result.uploaded and listing.auto_resume and listing.remote_state == OfferState.paused.value:
        await connector.set_offer_state(listing.remote_offer_id, OfferState.active)
        result.resumed = True
        if not result.shadow:
            listing.remote_state = OfferState.active.value
            audit.record(
                session,
                ACTOR,
                "listing.resumed",
                entity_type="listing",
                entity_id=listing.id,
                store_id=listing.store_id,
                after={"vault": count},
                reason="Paused with an empty vault; restocked.",
            )
    if result.uploaded and not result.shadow:
        events.publish(
            session,
            "offer.restocked",
            {
                "listing_id": listing.id,
                "title": listing.title,
                "uploaded": result.uploaded,
                "vault": count,
                "resumed": result.resumed,
            },
            store_id=listing.store_id,
        )


async def _login_of(session: AsyncSession, platform: Platform, item: StockItem) -> str:
    return _norm((await stock.reveal(session, platform, [item]))[0].login)


async def _resolve_pending(
    session: AsyncSession,
    platform: Platform,
    listing: Listing,
    in_vault: set[str],
    result: ListingResult,
) -> None:
    pending = list(
        await session.scalars(
            select(VaultUpload)
            .where(
                VaultUpload.listing_id == listing.id, VaultUpload.state == VaultUploadState.pending
            )
            .order_by(VaultUpload.id)
            .with_for_update()
        )
    )
    for upload in pending:
        item = await session.get(StockItem, upload.item_id)
        assert item is not None
        upload.resolved_at = utcnow()
        if await _login_of(session, platform, item) in in_vault:
            upload.state = VaultUploadState.uploaded
            if item.state is ItemState.reserved:
                item.state = ItemState.delivered
                session.add(
                    _event(item, "uploaded", "reserved", "delivered", listing, confirmed=True)
                )
            result.resolved += 1
        else:
            # Not in the vault now: never stored, or stored and already sold. Never sell it twice.
            upload.state = VaultUploadState.failed
            upload.error = "Outcome unknown and not in the vault: quarantined for review."
            if item.state is ItemState.reserved:
                item.state = ItemState.quarantined
                session.add(
                    _event(
                        item,
                        "quarantined",
                        "reserved",
                        "quarantined",
                        listing,
                        reason="vault upload outcome unknown; it may have been sold already",
                    )
                )
            result.quarantined += 1
            result.notes.append(
                "An earlier upload could not be confirmed: the account was quarantined. "
                "Check whether it was sold before returning it to stock."
            )
            events.publish(
                session,
                "stock.quarantined",
                {"count": 1, "reason": "vault upload not confirmed", "listing_id": listing.id},
                store_id=listing.store_id,
            )


def _entry(listing: Listing, revealed: stock.RevealedItem) -> VaultEntry:
    extra = revealed.extra
    with_email = listing.include_email_details and bool(extra.get("email"))
    return VaultEntry(
        login=revealed.login,
        password=revealed.password,
        email=extra.get("email") if with_email else None,
        email_password=extra.get("email_password") if with_email else None,
        email_provider_url=extra.get("email_provider") if with_email else None,
        additional_info=(listing.vault_additional_info or "").strip() or None,
    )


async def _next_item(session: AsyncSession, pool_id: int, skip: list[int]) -> StockItem | None:
    stmt = (
        select(StockItem)
        .where(StockItem.pool_id == pool_id, StockItem.state == ItemState.available)
        .order_by(StockItem.position, StockItem.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if skip:
        stmt = stmt.where(StockItem.id.not_in(skip))
    return await session.scalar(stmt)


async def _available(session: AsyncSession, pool_id: int) -> int:
    return int(
        await session.scalar(
            select(func.count()).where(
                StockItem.pool_id == pool_id, StockItem.state == ItemState.available
            )
        )
        or 0
    )


async def _quarantine_duplicate(session: AsyncSession, listing: Listing, item: StockItem) -> None:
    item.state = ItemState.quarantined
    session.add(
        _event(
            item, "quarantined", "available", "quarantined", listing, reason="already in the vault"
        )
    )
    events.publish(
        session,
        "stock.quarantined",
        {"count": 1, "reason": "already in the vault", "listing_id": listing.id},
        store_id=listing.store_id,
    )


async def _upload(
    session: AsyncSession,
    platform: Platform,
    connector: MarketplaceConnector,
    listing: Listing,
    in_vault: set[str],
    needed: int,
    result: ListingResult,
) -> None:
    assert listing.pool_id is not None
    skipped = 0
    while result.uploaded < needed:
        pool = await stock.get_pool(session, listing.store_id, listing.pool_id, lock=True)
        before = await _available(session, pool.id)
        item = await _next_item(session, pool.id, [])
        if item is None:
            result.notes.append(f"{pool.name} has no available accounts.")
            await session.commit()
            return
        revealed = (await stock.reveal(session, platform, [item]))[0]
        if _norm(revealed.login) in in_vault:
            await _quarantine_duplicate(session, listing, item)
            stock._announce_level(session, pool, before, before - 1)
            await session.commit()
            result.quarantined += 1
            skipped += 1
            if skipped >= MAX_SKIPPED:
                result.notes.append("Too many accounts were already in the vault; stopped.")
                return
            continue

        # Step 1, committed before calling the marketplace: the account is held for this upload.
        item.state = ItemState.reserved
        upload = VaultUpload(store_id=listing.store_id, listing_id=listing.id, item_id=item.id)
        session.add(upload)
        session.add(_event(item, "reserved", "available", "reserved", listing))
        stock._announce_level(session, pool, before, before - 1)
        await session.commit()

        # Step 2: send it.
        try:
            sent = await connector.vault_add(listing.remote_offer_id, _entry(listing, revealed))
        except ConnectorError as exc:
            upload.error = str(exc)[:2000]
            if exc.retryable:
                # Unknown outcome: kept pending, resolved on the next pass by reading the vault.
                await session.commit()
                raise
            upload.state = VaultUploadState.failed
            upload.resolved_at = utcnow()
            item.state = ItemState.available
            session.add(
                _event(item, "released", "reserved", "available", listing, error=str(exc)[:300])
            )
            await session.commit()
            raise

        # Step 3: it is in the vault, so it left the stock for good (I1).
        upload.state = VaultUploadState.uploaded
        upload.remote_entry_id = sent.remote_state
        upload.resolved_at = utcnow()
        item.state = ItemState.delivered
        session.add(_event(item, "uploaded", "reserved", "delivered", listing))
        await session.commit()
        in_vault.add(_norm(revealed.login))
        result.uploaded += 1


async def _shadow_uploads(
    session: AsyncSession,
    platform: Platform,
    connector: MarketplaceConnector,
    listing: Listing,
    in_vault: set[str],
    needed: int,
    result: ListingResult,
) -> None:
    """I9: record what would be uploaded; no stock changes state."""
    assert listing.pool_id is not None
    candidates = list(
        await session.scalars(
            select(StockItem)
            .where(StockItem.pool_id == listing.pool_id, StockItem.state == ItemState.available)
            .order_by(StockItem.position, StockItem.id)
            .limit(needed + MAX_SKIPPED)
        )
    )
    for item in candidates:
        if result.uploaded >= needed:
            break
        revealed = (await stock.reveal(session, platform, [item]))[0]
        if _norm(revealed.login) in in_vault:
            result.quarantined += 1  # would be quarantined
            continue
        await connector.vault_add(listing.remote_offer_id, _entry(listing, revealed))
        result.uploaded += 1
    if result.uploaded < needed:
        result.notes.append("The pool does not have enough available accounts.")


# ---- views ---------------------------------------------------------------------------------


@dataclass
class UploadView:
    id: int
    item_id: int
    login_hint: str
    state: str
    error: str | None
    created_at: datetime
    resolved_at: datetime | None


async def recent_uploads(
    session: AsyncSession, listing: Listing, limit: int = 50
) -> list[UploadView]:
    rows = await session.execute(
        select(VaultUpload, StockItem.login_hint)
        .join(StockItem, StockItem.id == VaultUpload.item_id)
        .where(VaultUpload.listing_id == listing.id)
        .order_by(VaultUpload.id.desc())
        .limit(limit)
    )
    return [
        UploadView(u.id, u.item_id, hint, u.state.value, u.error, u.created_at, u.resolved_at)
        for u, hint in rows
    ]
