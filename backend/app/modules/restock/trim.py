"""Pull back extras: take accounts out of a card's vaults down to a number, back into the pool.

Restock only adds. When a vault holds more than wanted (the old system uploaded too many), the
owner chooses how many to keep on each platform (at least 1: Eldorado does not allow an empty
instant-delivery offer to stay listed) and the rest come back to the card's pool.

Safety:
- the vault is read live, never taken from the count shown in the panel;
- an account returns to the pool only when Eldorado confirmed the removal AND a fresh read of
  the vault no longer lists it. A removal with an unknown outcome (timeout) whose account is
  gone could also be a sale at that same moment, so it goes to quarantine for a person to
  check, never straight back to sale; one still listed stays where it is;
- accounts the platform already knows (taken out when they were uploaded) come back as the same
  row, keeping their history; unknown ones are added to the pool from the vault's credentials;
- the card's restock lock is held, so no restock pass runs on the same listing meanwhile.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.base import ConnectorError, MarketplaceConnector, VaultEntry
from app.connectors.runtime import open_connector
from app.core import audit
from app.core.crypto import hint
from app.core.platform import Platform
from app.listings.models import DEVICES, Listing, ListingGroup
from app.modules.restock.service import LOCK_NAMESPACE
from app.stock import service as stock
from app.stock.importer import ParsedAccount, normalise_login
from app.stock.models import ItemState, StockItem, StockItemEvent, StockPool
from app.stores.models import MarketplaceConnection

MIN_KEEP = 1


class TrimError(Exception):
    pass


@dataclass
class PlatformPlan:
    device: str
    listing_id: int
    offer_state: str
    in_vault: int
    keep: int
    remove: list[str] = field(default_factory=list)  # login hints
    known: int = 0  # of those, accounts the platform already has
    removed: int = 0
    returned: int = 0
    quarantined: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class TrimResult:
    group_id: int
    pool: str
    platforms: list[PlatformPlan] = field(default_factory=list)
    done: bool = False

    @property
    def to_remove(self) -> int:
        return sum(len(p.remove) for p in self.platforms)


def _pick(
    entries: list[VaultEntry], known: set[str], locked: set[str], count: int
) -> list[VaultEntry]:
    """Which entries go: accounts the platform took out by hand first (they come back as the
    same row), then unknown ones; newest first. Accounts in a final state (uploaded by the
    platform itself) stay in the vault: their state cannot go back."""
    if count <= 0:
        return []
    ordered = [e for e in reversed(entries) if normalise_login(e.login) not in locked]
    first = [e for e in ordered if normalise_login(e.login) in known]
    rest = [e for e in ordered if normalise_login(e.login) not in known]
    return (first + rest)[:count]


async def _known_logins(
    session: AsyncSession, platform: Platform, store_id: int, entries: list[VaultEntry]
) -> tuple[dict[str, StockItem], set[str]]:
    """(accounts that can come back, accounts that must stay in the vault)."""
    found: dict[str, StockItem] = {}
    locked: set[str] = set()
    for e in entries:
        item = await stock.find_by_login(session, platform, store_id, e.login)
        if item is None:
            continue
        if item.state is ItemState.extracted:
            found[normalise_login(e.login)] = item
        else:
            locked.add(normalise_login(e.login))
    return found, locked


async def _listings(session: AsyncSession, group: ListingGroup) -> list[Listing]:
    rows = await session.scalars(select(Listing).where(Listing.group_id == group.id))
    order = list(DEVICES)
    return sorted(rows, key=lambda lst: order.index(lst.device) if lst.device in order else 99)


async def trim(
    session: AsyncSession,
    platform: Platform,
    actor: audit.Actor,
    group: ListingGroup,
    keep: dict[str, int],
    *,
    apply: bool,
) -> TrimResult:
    """Plan (apply=False) or pull back (apply=True). ``keep`` maps platform → accounts to keep;
    platforms not named are left alone."""
    if group.pool_id is None:
        raise TrimError("This card has no pool: the accounts would have nowhere to go.")
    pool = await session.get(StockPool, group.pool_id)
    assert pool is not None
    for device, n in keep.items():
        if device not in DEVICES:
            raise TrimError(f"Unknown platform {device!r}.")
        if n < MIN_KEEP:
            raise TrimError(
                f"Keep at least {MIN_KEEP} on {DEVICES[device]}: Eldorado does not allow an "
                "empty instant-delivery offer."
            )
    result = TrimResult(group.id, pool.name)
    for listing in await _listings(session, group):
        if listing.device not in keep:
            continue
        connection = await session.get(MarketplaceConnection, listing.connection_id)
        assert connection is not None
        connector = await open_connector(session, platform, connection)
        try:
            async with platform.db.engine.connect() as lock_conn:
                got = await lock_conn.scalar(
                    text("SELECT pg_try_advisory_lock(:ns, :id)"),
                    {"ns": LOCK_NAMESPACE, "id": listing.id},
                )
                if not got:
                    raise TrimError(
                        f"A restock of the {DEVICES[listing.device or '']} offer is running; "
                        "try again in a minute."
                    )
                try:
                    plan = await _one(
                        session, platform, actor, connector, listing, pool, keep, apply=apply
                    )
                finally:
                    await lock_conn.execute(
                        text("SELECT pg_advisory_unlock(:ns, :id)"),
                        {"ns": LOCK_NAMESPACE, "id": listing.id},
                    )
                    await lock_conn.commit()
        finally:
            await connector.aclose()
        result.platforms.append(plan)
    result.done = apply
    if apply:
        audit.record(
            session,
            actor,
            "listing_group.vault_trimmed",
            entity_type="listing_group",
            entity_id=group.id,
            store_id=group.store_id,
            after={
                p.device: {
                    "kept": p.keep,
                    "removed": p.removed,
                    "returned": p.returned,
                    "quarantined": p.quarantined,
                    "errors": p.errors[:5],
                }
                for p in result.platforms
            },
        )
        await session.commit()
    return result


async def _one(
    session: AsyncSession,
    platform: Platform,
    actor: audit.Actor,
    connector: MarketplaceConnector,
    listing: Listing,
    pool: StockPool,
    keep: dict[str, int],
    *,
    apply: bool,
) -> PlatformPlan:
    device = listing.device or ""
    entries = await connector.vault_list(listing.remote_offer_id)
    known, locked = await _known_logins(session, platform, listing.store_id, entries)
    wanted = keep[device]
    chosen = _pick(entries, set(known), locked, len(entries) - wanted)
    plan = PlatformPlan(
        device=device,
        listing_id=listing.id,
        offer_state=listing.remote_state,
        in_vault=len(entries),
        keep=len(entries) - len(chosen),
        remove=[hint(e.login) for e in chosen],
        known=sum(1 for e in chosen if normalise_login(e.login) in known),
    )
    if not apply or not chosen:
        return plan
    batch = f"trim:{secrets.token_hex(6)}"
    confirmed: list[VaultEntry] = []  # Eldorado answered OK
    unsure: list[VaultEntry] = []  # no answer: may or may not have happened
    for entry in chosen:
        if not entry.remote_id:
            plan.errors.append(f"{hint(entry.login)}: Eldorado gave no id for this entry")
            continue
        try:
            await connector.vault_remove(listing.remote_offer_id, entry.remote_id)
        except ConnectorError as exc:
            if exc.retryable:
                unsure.append(entry)
            else:
                plan.errors.append(f"{hint(entry.login)}: {exc}")
            continue
        confirmed.append(entry)
    if not confirmed and not unsure:
        return plan
    # Read the vault again: an account returns to sale only when it is really gone.
    try:
        still = {
            normalise_login(e.login) for e in await connector.vault_list(listing.remote_offer_id)
        }
    except ConnectorError:
        still = None
    for entry in confirmed + unsure:
        login = normalise_login(entry.login)
        if still is None:
            state = ItemState.quarantined  # cannot check: a person does
        elif login in still:
            plan.errors.append(f"{hint(entry.login)}: still in the vault, left there")
            continue
        else:
            state = ItemState.available if entry in confirmed else ItemState.quarantined
        plan.removed += 1
        await _back_to_pool(session, platform, actor, listing, pool, entry, known, batch, state)
        if state is ItemState.available:
            plan.returned += 1
        else:
            plan.quarantined += 1
    await session.commit()
    return plan


async def _back_to_pool(
    session: AsyncSession,
    platform: Platform,
    actor: audit.Actor,
    listing: Listing,
    pool: StockPool,
    entry: VaultEntry,
    known: dict[str, StockItem],
    batch: str,
    state: ItemState,
) -> None:
    note = (
        None
        if state is ItemState.available
        else ("Removed from the vault but Eldorado did not confirm it: check it was not sold.")
    )
    position = await stock._next_position(session, pool.id)
    details = {"listing_id": listing.id, "device": listing.device, "to_pool": pool.id}
    item = known.get(normalise_login(entry.login))
    if item is not None:
        before = item.state.value
        item.pool_id, item.position, item.state, item.note = pool.id, position, state, note
        session.add(
            StockItemEvent(
                item_id=item.id,
                actor_label=actor.label,
                event="pulled_back",
                from_state=before,
                to_state=state.value,
                batch=batch,
                details=details,
            )
        )
        return
    account = ParsedAccount(
        line=0,
        login=entry.login.strip(),
        password=entry.password,
        format="vault",
        email=entry.email or None,
        email_password=entry.email_password or None,
        email_provider=entry.email_provider_url or None,
    )
    item_id = await session.scalar(
        insert(StockItem)
        .values(
            store_id=listing.store_id,
            pool_id=pool.id,
            fingerprint=platform.fingerprints(normalise_login(account.login)),
            payload_encrypted=stock._encrypt_item(platform, listing.store_id, account),
            login_hint=hint(account.login),
            state=state,
            position=position,
            has_token=False,
            note=note,
        )
        .on_conflict_do_nothing(index_elements=["store_id", "fingerprint"])
        .returning(StockItem.id)
    )
    if item_id is not None:
        session.add(
            StockItemEvent(
                item_id=item_id,
                actor_label=actor.label,
                event="pulled_back",
                from_state=None,
                to_state=state.value,
                batch=batch,
                details={**details, "new": True},
            )
        )
