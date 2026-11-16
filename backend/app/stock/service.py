"""Stock operations: import (preview → commit), extraction with undo, state changes and moves.

Every operation that touches items takes a lock on the pool row first, so two operations on the
same pool run one after the other (no two extractions can take the same account).
"""

from __future__ import annotations

import hashlib
import secrets
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit, events
from app.core.crypto import hint
from app.core.db import utcnow
from app.core.platform import Platform
from app.stock.importer import ParsedAccount, normalise_login, parse
from app.stock.models import (
    ItemState,
    ItemType,
    StockExtraction,
    StockImport,
    StockItem,
    StockItemEvent,
    StockItemSecret,
    StockPool,
)

MAX_IMPORT_CHARS = 2_000_000
MAX_EXTRACT = 1000
TOKEN = "twitch_token"  # noqa: S105 - secret kind, not a secret


class StockError(Exception):
    pass


class NotFound(StockError):
    pass


def payload_context(store_id: int) -> str:
    return f"stock:{store_id}"


def secret_context(store_id: int, kind: str) -> str:
    return f"stock:{store_id}:{kind}"


# ---- pools -------------------------------------------------------------------------------


async def get_pool(
    session: AsyncSession, store_id: int, pool_id: int, *, lock: bool = False
) -> StockPool:
    stmt = select(StockPool).where(StockPool.id == pool_id, StockPool.store_id == store_id)
    if lock:
        stmt = stmt.with_for_update()
    pool = await session.scalar(stmt)
    if pool is None:
        raise NotFound(f"Stock pool {pool_id} not found")
    return pool


async def create_pool(
    session: AsyncSession,
    actor: audit.Actor,
    store_id: int,
    *,
    name: str,
    game: str = "Rocket League",
    item_type: ItemType = ItemType.account,
    low_stock_threshold: int = 5,
    legacy_id: str | None = None,
) -> StockPool:
    name = name.strip()
    if await session.scalar(
        select(StockPool.id).where(StockPool.store_id == store_id, StockPool.name == name)
    ):
        raise StockError(f"A pool named {name!r} already exists in this store.")
    pool = StockPool(
        store_id=store_id,
        name=name,
        game=game,
        item_type=item_type,
        low_stock_threshold=low_stock_threshold,
        legacy_id=legacy_id,
    )
    session.add(pool)
    await session.flush()
    audit.record(
        session,
        actor,
        "stock.pool_created",
        entity_type="stock_pool",
        entity_id=pool.id,
        store_id=store_id,
        after={"name": name, "game": game, "threshold": low_stock_threshold},
    )
    return pool


async def update_pool(
    session: AsyncSession, actor: audit.Actor, pool: StockPool, changes: dict[str, Any]
) -> StockPool:
    before = {k: getattr(pool, k) for k in changes}
    if "name" in changes and changes["name"] != pool.name:
        clash = await session.scalar(
            select(StockPool.id).where(
                StockPool.store_id == pool.store_id, StockPool.name == changes["name"]
            )
        )
        if clash:
            raise StockError(f"A pool named {changes['name']!r} already exists in this store.")
    for key, value in changes.items():
        setattr(pool, key, value)
    if before != changes:
        audit.record(
            session,
            actor,
            "stock.pool_updated",
            entity_type="stock_pool",
            entity_id=pool.id,
            store_id=pool.store_id,
            before=before,
            after=changes,
        )
    return pool


async def pool_counts(session: AsyncSession, store_id: int) -> dict[int, dict[str, int]]:
    rows = await session.execute(
        select(StockItem.pool_id, StockItem.state, func.count())
        .where(StockItem.store_id == store_id)
        .group_by(StockItem.pool_id, StockItem.state)
    )
    counts: dict[int, dict[str, int]] = {}
    for pool_id, state, n in rows:
        counts.setdefault(pool_id, {s.value: 0 for s in ItemState})[state.value] = n
    return counts


async def _available(session: AsyncSession, pool_id: int) -> int:
    return int(
        await session.scalar(
            select(func.count()).where(
                StockItem.pool_id == pool_id, StockItem.state == ItemState.available
            )
        )
        or 0
    )


def _announce_level(session: AsyncSession, pool: StockPool, before: int, after: int) -> None:
    """Publish stock.low / stock.depleted only when the level crosses the threshold."""
    payload = {"pool_id": pool.id, "pool": pool.name, "available": after}
    if after == 0 and before > 0:
        events.publish(session, "stock.depleted", payload, store_id=pool.store_id)
    elif after <= pool.low_stock_threshold < before:
        events.publish(session, "stock.low", payload, store_id=pool.store_id)


# ---- import ------------------------------------------------------------------------------


@dataclass
class PreviewLine:
    line: int
    login_hint: str
    # new | in_pool | in_other_pool | taken_out | in_other_store | previously_removed | repeated
    status: str
    format: str
    has_token: bool
    token_will_attach: bool = False
    where: str | None = None  # the other store, for in_other_store


@dataclass
class ImportPreview:
    text_sha256: str
    detected_format: str
    lines: list[PreviewLine] = field(default_factory=list)
    invalid_lines: list[int] = field(default_factory=list)
    ignored_lines: list[int] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        c = Counter(line.status for line in self.lines)
        return {
            "new": c["new"],
            "in_pool": c["in_pool"],
            "in_other_pool": c["in_other_pool"],
            "previously_removed": c["previously_removed"],
            "taken_out": c["taken_out"],
            "in_other_store": c["in_other_store"],
            "repeated": c["repeated"],
            "invalid": len(self.invalid_lines),
            "tokens_attached": sum(line.token_will_attach for line in self.lines),
        }


# Taken out in this panel (extraction, "Remove", moved to another store): can come back.
# Old-system history (legacy_extracted) stays blocked: those accounts may have been sold.
PUT_BACK_EVENTS = frozenset({"extracted", "removed", "moved_out"})
IN_STOCK = (ItemState.available, ItemState.reserved, ItemState.quarantined)


async def last_events(session: AsyncSession, item_ids: list[int]) -> dict[int, str]:
    if not item_ids:
        return {}
    latest = (
        select(func.max(StockItemEvent.id))
        .where(StockItemEvent.item_id.in_(item_ids))
        .group_by(StockItemEvent.item_id)
    )
    rows = await session.execute(
        select(StockItemEvent.item_id, StockItemEvent.event).where(StockItemEvent.id.in_(latest))
    )
    return {item_id: event for item_id, event in rows}


async def in_other_stores(
    session: AsyncSession, store_id: int, fingerprints: list[str]
) -> dict[str, str]:
    """Accounts in stock in another store (the fingerprint is the same in every store)."""
    if not fingerprints:
        return {}
    from app.stores.models import Store

    rows = await session.execute(
        select(StockItem.fingerprint, Store.name)
        .join(Store, Store.id == StockItem.store_id)
        .where(
            StockItem.store_id != store_id,
            StockItem.fingerprint.in_(fingerprints),
            StockItem.state.in_(IN_STOCK),
        )
    )
    return {fp: name for fp, name in rows}


def text_digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


async def _analyse(
    session: AsyncSession, platform: Platform, pool: StockPool, text: str
) -> tuple[ImportPreview, list[tuple[ParsedAccount, str]]]:
    if len(text) > MAX_IMPORT_CHARS:
        raise StockError("The text is too long (2 MB at most per import).")
    parsed = parse(text)
    preview = ImportPreview(
        text_digest(text),
        parsed.detected_format,
        invalid_lines=parsed.invalid_lines,
        ignored_lines=parsed.ignored_lines,
    )
    fingerprints = [platform.fingerprints(normalise_login(a.login)) for a in parsed.accounts]
    existing: dict[str, tuple[int, ItemState, bool, int]] = {}
    if fingerprints:
        rows = await session.execute(
            select(
                StockItem.fingerprint,
                StockItem.pool_id,
                StockItem.state,
                StockItem.has_token,
                StockItem.id,
            ).where(
                StockItem.store_id == pool.store_id, StockItem.fingerprint.in_(set(fingerprints))
            )
        )
        existing = {fp: (pid, st, tok, iid) for fp, pid, st, tok, iid in rows}
    last = await last_events(session, [v[3] for v in existing.values()])
    elsewhere = await in_other_stores(session, pool.store_id, list(set(fingerprints)))

    new: list[tuple[ParsedAccount, str]] = []
    seen: set[str] = set()
    for account, fp in zip(parsed.accounts, fingerprints, strict=True):
        line = PreviewLine(
            account.line, hint(account.login), "new", account.format, bool(account.token)
        )
        if fp in seen:
            line.status = "repeated"
        elif fp in existing:
            pool_id, state, has_token, _ = existing[fp]
            if state in (ItemState.available, ItemState.reserved, ItemState.quarantined):
                line.status = "in_pool" if pool_id == pool.id else "in_other_pool"
                line.token_will_attach = bool(
                    account.token and not has_token and state is ItemState.available
                )
            elif fp in elsewhere:  # taken out here, now in stock in another store
                line.status, line.where = "in_other_store", elsewhere[fp]
            elif state is ItemState.extracted and last.get(existing[fp][3]) in PUT_BACK_EVENTS:
                line.status = "taken_out"  # taken out in this panel: can be put back
            else:
                line.status = "previously_removed"  # sold, removed for good, or old history
        elif fp in elsewhere:
            line.status, line.where = "in_other_store", elsewhere[fp]
        else:
            new.append((account, fp))
        seen.add(fp)
        preview.lines.append(line)
    return preview, new


async def preview_import(
    session: AsyncSession, platform: Platform, store_id: int, pool_id: int, text: str
) -> ImportPreview:
    pool = await get_pool(session, store_id, pool_id)
    preview, _ = await _analyse(session, platform, pool, text)
    return preview


def _encrypt_item(platform: Platform, store_id: int, account: ParsedAccount) -> str:
    return platform.secrets.encrypt_json(account.payload(), context=payload_context(store_id))


async def _next_position(session: AsyncSession, pool_id: int) -> int:
    return (
        int(
            await session.scalar(
                select(func.coalesce(func.max(StockItem.position), 0)).where(
                    StockItem.pool_id == pool_id
                )
            )
            or 0
        )
        + 1
    )


async def commit_import(
    session: AsyncSession,
    platform: Platform,
    actor: audit.Actor,
    store_id: int,
    pool_id: int,
    text: str,
    *,
    expected_sha256: str,
    put_back: bool = False,
) -> tuple[StockImport, ImportPreview]:
    """Save what the preview showed. Refuses if the text changed since the preview.
    ``put_back``: accounts taken out earlier in this panel come back into this pool."""
    if text_digest(text) != expected_sha256:
        raise StockError("The text changed after the preview. Preview it again.")
    pool = await get_pool(session, store_id, pool_id, lock=True)
    if pool.archived:
        raise StockError("This pool is archived.")
    preview, new = await _analyse(session, platform, pool, text)

    record = StockImport(
        store_id=store_id,
        pool_id=pool.id,
        actor_label=actor.label,
        text_sha256=preview.text_sha256,
        detected_format=preview.detected_format,
        counts={},
    )
    session.add(record)
    await session.flush()

    position = await _next_position(session, pool.id)
    added: list[tuple[int, ParsedAccount]] = []
    for account, fp in new:
        item_id = await session.scalar(
            insert(StockItem)
            .values(
                store_id=store_id,
                pool_id=pool.id,
                fingerprint=fp,
                payload_encrypted=_encrypt_item(platform, store_id, account),
                login_hint=hint(account.login),
                state=ItemState.available,
                position=position,
                has_token=bool(account.token),
                source_import_id=record.id,
            )
            .on_conflict_do_nothing(index_elements=["store_id", "fingerprint"])
            .returning(StockItem.id)
        )
        if item_id is None:  # imported concurrently by someone else
            continue
        position += 1
        added.append((item_id, account))
    await _add_tokens(session, platform, store_id, [(i, a.token) for i, a in added if a.token])
    session.add_all(
        StockItemEvent(
            item_id=item_id,
            actor_label=actor.label,
            event="imported",
            to_state="available",
            batch=f"import:{record.id}",
            details={"format": account.format},
        )
        for item_id, account in added
    )

    tokens_attached = await _attach_tokens_to_existing(session, platform, pool, preview, text)
    restored = 0
    if put_back and preview.counts["taken_out"]:
        restored = await _put_back_from_text(
            session, platform, actor, pool, preview, text, record.id
        )
    counts = {
        **preview.counts,
        "added": len(added),
        "tokens_attached": tokens_attached,
        "put_back": restored,
    }
    record.counts = counts
    audit.record(
        session,
        actor,
        "stock.imported",
        entity_type="stock_pool",
        entity_id=pool.id,
        store_id=store_id,
        after={"import_id": record.id, **counts},
    )
    if added or restored:
        events.publish(
            session,
            "stock.imported",
            {
                "pool_id": pool.id,
                "pool": pool.name,
                "added": len(added) + restored,
                "import_id": record.id,
            },
            store_id=store_id,
        )
    return record, preview


async def _revive(
    session: AsyncSession,
    platform: Platform,
    item: StockItem,
    pool: StockPool,
    account: ParsedAccount,
    position: int,
    state: ItemState = ItemState.available,
) -> None:
    """An account taken out earlier comes back (same row: its history stays in one place)."""
    item.pool_id, item.position, item.state = pool.id, position, state
    item.payload_encrypted = _encrypt_item(platform, item.store_id, account)
    if account.token and not item.has_token:
        item.has_token = True
        await _add_tokens(session, platform, item.store_id, [(item.id, account.token)])


async def _put_back_from_text(
    session: AsyncSession,
    platform: Platform,
    actor: audit.Actor,
    pool: StockPool,
    preview: ImportPreview,
    text: str,
    import_id: int,
) -> int:
    # (Lines in stock in another store are "in_other_store", never "taken_out".)
    wanted = {line.line for line in preview.lines if line.status == "taken_out"}
    accounts = [a for a in parse(text).accounts if a.line in wanted]
    by_fp = {platform.fingerprints(normalise_login(a.login)): a for a in accounts}
    items = list(
        await session.scalars(
            select(StockItem).where(
                StockItem.store_id == pool.store_id,
                StockItem.fingerprint.in_(list(by_fp)),
                StockItem.state == ItemState.extracted,
            )
        )
    )
    last = await last_events(session, [i.id for i in items])
    position = await _next_position(session, pool.id)
    done = 0
    for item in items:
        if last.get(item.id) not in PUT_BACK_EVENTS:
            continue
        await _revive(session, platform, item, pool, by_fp[item.fingerprint], position)
        session.add(
            StockItemEvent(
                item_id=item.id,
                actor_label=actor.label,
                event="put_back",
                from_state="extracted",
                to_state="available",
                batch=f"import:{import_id}",
                details={"to_pool": pool.id},
            )
        )
        position += 1
        done += 1
    return done


async def _add_tokens(
    session: AsyncSession, platform: Platform, store_id: int, pairs: list[tuple[int, str | None]]
) -> None:
    for item_id, token in pairs:
        if token:
            session.add(
                StockItemSecret(
                    item_id=item_id,
                    kind=TOKEN,
                    value_encrypted=platform.secrets.encrypt(
                        token, context=secret_context(store_id, TOKEN)
                    ),
                )
            )


async def _attach_tokens_to_existing(
    session: AsyncSession, platform: Platform, pool: StockPool, preview: ImportPreview, text: str
) -> int:
    """A re-import that brings a token for an account already in stock adds the token."""
    wanted = {line.line for line in preview.lines if line.token_will_attach}
    if not wanted:
        return 0
    attached = 0
    for account in parse(text).accounts:
        if account.line not in wanted or not account.token:
            continue
        fp = platform.fingerprints(normalise_login(account.login))
        item = await session.scalar(
            select(StockItem)
            .where(
                StockItem.store_id == pool.store_id,
                StockItem.fingerprint == fp,
                StockItem.has_token.is_(False),
                StockItem.state == ItemState.available,
            )
            .with_for_update()
        )
        if item is None:
            continue
        await _add_tokens(session, platform, pool.store_id, [(item.id, account.token)])
        item.has_token = True
        attached += 1
        wanted.discard(account.line)
    return attached


# ---- reading secrets ---------------------------------------------------------------------


@dataclass(frozen=True)
class RevealedItem:
    id: int
    login: str
    password: str
    token: str | None
    extra: dict[str, str]

    def line(self) -> str:
        return f"{self.login}:{self.password}"


async def reveal(
    session: AsyncSession, platform: Platform, items: list[StockItem]
) -> list[RevealedItem]:
    tokens: dict[int, str] = {}
    ids = [i.id for i in items if i.has_token]
    if ids:
        for item_id, value in await session.execute(
            select(StockItemSecret.item_id, StockItemSecret.value_encrypted).where(
                StockItemSecret.item_id.in_(ids), StockItemSecret.kind == TOKEN
            )
        ):
            tokens[item_id] = value
    out = []
    for item in items:
        data = platform.secrets.decrypt_json(
            item.payload_encrypted, context=payload_context(item.store_id)
        )
        token = tokens.get(item.id)
        out.append(
            RevealedItem(
                id=item.id,
                login=data.pop("login"),
                password=data.pop("password"),
                token=platform.secrets.decrypt(token, context=secret_context(item.store_id, TOKEN))
                if token
                else None,
                extra=data,
            )
        )
    return out


# ---- extraction --------------------------------------------------------------------------


async def extract(
    session: AsyncSession,
    platform: Platform,
    actor: audit.Actor,
    store_id: int,
    pool_id: int,
    count: int,
    *,
    reason: str | None = None,
) -> tuple[StockExtraction, list[RevealedItem]]:
    if not 1 <= count <= MAX_EXTRACT:
        raise StockError(f"Extract between 1 and {MAX_EXTRACT} items at a time.")
    pool = await get_pool(session, store_id, pool_id, lock=True)
    before = await _available(session, pool.id)
    items = list(
        (
            await session.scalars(
                select(StockItem)
                .where(StockItem.pool_id == pool.id, StockItem.state == ItemState.available)
                .order_by(StockItem.position, StockItem.id)
                .limit(count)
                .with_for_update()
            )
        ).all()
    )
    if not items:
        raise StockError(f"{pool.name} has no available items.")
    extraction = StockExtraction(
        store_id=store_id, pool_id=pool.id, actor_label=actor.label, count=len(items), reason=reason
    )
    session.add(extraction)
    await session.flush()
    for item in items:
        item.state = ItemState.extracted
    session.add_all(
        StockItemEvent(
            item_id=item.id,
            actor_label=actor.label,
            event="extracted",
            from_state="available",
            to_state="extracted",
            batch=f"extraction:{extraction.id}",
            details={"reason": reason} if reason else None,
        )
        for item in items
    )
    audit.record(
        session,
        actor,
        "stock.extracted",
        entity_type="stock_pool",
        entity_id=pool.id,
        store_id=store_id,
        after={"extraction_id": extraction.id, "count": len(items)},
        reason=reason,
    )
    _announce_level(session, pool, before, before - len(items))
    return extraction, await reveal(session, platform, items)


async def _extraction_items(session: AsyncSession, extraction: StockExtraction) -> list[StockItem]:
    ids = select(StockItemEvent.item_id).where(
        StockItemEvent.batch == f"extraction:{extraction.id}", StockItemEvent.event == "extracted"
    )
    return list(
        (
            await session.scalars(
                select(StockItem).where(StockItem.id.in_(ids)).order_by(StockItem.position)
            )
        ).all()
    )


async def get_extraction(
    session: AsyncSession, store_id: int, extraction_id: int
) -> StockExtraction:
    extraction = await session.get(StockExtraction, extraction_id)
    if extraction is None or extraction.store_id != store_id:
        raise NotFound(f"Extraction {extraction_id} not found")
    return extraction


async def view_extraction(
    session: AsyncSession, platform: Platform, actor: audit.Actor, store_id: int, extraction_id: int
) -> tuple[StockExtraction, list[RevealedItem]]:
    extraction = await get_extraction(session, store_id, extraction_id)
    items = await _extraction_items(session, extraction)
    audit.record(
        session,
        actor,
        "stock.extraction_viewed",
        entity_type="stock_extraction",
        entity_id=extraction.id,
        store_id=store_id,
    )
    return extraction, await reveal(session, platform, items)


async def undo_extraction(
    session: AsyncSession, actor: audit.Actor, store_id: int, extraction_id: int
) -> StockExtraction:
    extraction = await get_extraction(session, store_id, extraction_id)
    await get_pool(session, store_id, extraction.pool_id, lock=True)
    if extraction.undone_at is not None:
        raise StockError("This extraction was already undone.")
    items = await _extraction_items(session, extraction)
    changed = [i for i in items if i.state is not ItemState.extracted]
    if changed:
        raise StockError(
            f"{len(changed)} of these items changed since the extraction; it cannot be undone."
        )
    for item in items:
        item.state = ItemState.available
    session.add_all(
        StockItemEvent(
            item_id=item.id,
            actor_label=actor.label,
            event="extraction_undone",
            from_state="extracted",
            to_state="available",
            batch=f"extraction:{extraction.id}",
        )
        for item in items
    )
    extraction.undone_at = utcnow()
    audit.record(
        session,
        actor,
        "stock.extraction_undone",
        entity_type="stock_extraction",
        entity_id=extraction.id,
        store_id=store_id,
        after={"count": len(items)},
    )
    return extraction


# ---- state changes and moves -------------------------------------------------------------

ALLOWED: dict[ItemState, set[ItemState]] = {
    ItemState.quarantined: {ItemState.available},
    ItemState.available: {ItemState.quarantined},
    ItemState.void: {ItemState.available, ItemState.quarantined, ItemState.extracted},
}
EVENT_FOR = {
    ItemState.quarantined: "quarantined",
    ItemState.available: "released",
    ItemState.void: "voided",
}


async def _load_items(session: AsyncSession, store_id: int, item_ids: list[int]) -> list[StockItem]:
    if not item_ids or len(item_ids) > 5000:
        raise StockError("Select between 1 and 5000 items.")
    pool_ids = set(
        await session.scalars(
            select(StockItem.pool_id).where(
                StockItem.id.in_(item_ids), StockItem.store_id == store_id
            )
        )
    )
    for pool_id in sorted(pool_ids):  # same lock order everywhere: no deadlocks
        await get_pool(session, store_id, pool_id, lock=True)
    items = list(
        (
            await session.scalars(
                select(StockItem)
                .where(StockItem.id.in_(item_ids), StockItem.store_id == store_id)
                .with_for_update()
            )
        ).all()
    )
    if len(items) != len(set(item_ids)):
        raise NotFound("Some items do not exist in this store.")
    return items


async def change_state(
    session: AsyncSession,
    actor: audit.Actor,
    store_id: int,
    item_ids: list[int],
    to_state: ItemState,
    *,
    reason: str | None = None,
) -> int:
    if to_state not in ALLOWED:
        raise StockError(f"Items cannot be set to {to_state} by hand.")
    items = await _load_items(session, store_id, item_ids)
    wrong = [i for i in items if i.state not in ALLOWED[to_state]]
    if wrong:
        states = ", ".join(sorted({i.state.value for i in wrong}))
        raise StockError(f"{len(wrong)} selected items are {states} and cannot become {to_state}.")
    pools = {i.pool_id for i in items}
    before = {pid: await _available(session, pid) for pid in pools}
    batch = f"state:{secrets.token_hex(6)}"
    for item in items:
        session.add(
            StockItemEvent(
                item_id=item.id,
                actor_label=actor.label,
                event=EVENT_FOR[to_state],
                from_state=item.state.value,
                to_state=to_state.value,
                batch=batch,
                details={"reason": reason} if reason else None,
            )
        )
        item.state = to_state
    await session.flush()
    for pid in pools:
        pool = await session.get(StockPool, pid)
        assert pool is not None
        _announce_level(session, pool, before[pid], await _available(session, pid))
    if to_state is ItemState.quarantined:
        events.publish(
            session, "stock.quarantined", {"count": len(items), "reason": reason}, store_id=store_id
        )
    audit.record(
        session,
        actor,
        f"stock.items_{EVENT_FOR[to_state]}",
        entity_type="stock_items",
        entity_id=batch,
        store_id=store_id,
        after={"count": len(items), "item_ids": item_ids[:200]},
        reason=reason,
    )
    return len(items)


async def move_items(
    session: AsyncSession,
    platform: Platform,
    actor: audit.Actor,
    store_id: int,
    item_ids: list[int],
    target_pool_id: int,
    *,
    target_store_id: int | None = None,
) -> dict[str, int]:
    """Move items to another pool. Across stores the item is re-created in the target store
    (the source item is marked extracted, so its history stays where it was)."""
    target_store_id = target_store_id or store_id
    target = await get_pool(session, target_store_id, target_pool_id)
    items = await _load_items(session, store_id, item_ids)
    movable = {ItemState.available, ItemState.quarantined}
    wrong = [i for i in items if i.state not in movable]
    if wrong:
        raise StockError(f"{len(wrong)} selected items are not available or quarantined.")
    batch = f"move:{secrets.token_hex(6)}"
    sources = {i.pool_id for i in items}
    before = {pid: await _available(session, pid) for pid in sources}
    position = await _next_position(session, target.id)
    moved = skipped = conflicts = 0

    if target_store_id == store_id:
        for item in items:
            if item.pool_id == target.id:
                skipped += 1
                continue
            session.add(
                StockItemEvent(
                    item_id=item.id,
                    actor_label=actor.label,
                    event="moved",
                    from_state=item.state.value,
                    to_state=item.state.value,
                    batch=batch,
                    details={"from_pool": item.pool_id, "to_pool": target.id},
                )
            )
            item.pool_id, item.position = target.id, position
            position += 1
            moved += 1
    else:
        revealed = {r.id: r for r in await reveal(session, platform, items)}
        for item in items:
            r = revealed[item.id]
            account = ParsedAccount(
                0,
                r.login,
                r.password,
                "moved",
                token=r.token,
                **{
                    k: v
                    for k, v in r.extra.items()
                    if k in ("email", "email_password", "email_provider")
                },
            )
            new_id = await session.scalar(
                insert(StockItem)
                .values(
                    store_id=target_store_id,
                    pool_id=target.id,
                    fingerprint=item.fingerprint,
                    payload_encrypted=_encrypt_item(platform, target_store_id, account),
                    login_hint=item.login_hint,
                    state=item.state,
                    position=position,
                    has_token=item.has_token,
                    note=item.note,
                )
                .on_conflict_do_nothing(index_elements=["store_id", "fingerprint"])
                .returning(StockItem.id)
            )
            if new_id is None:
                # Already known in the target store. Taken out there earlier (by hand, moved
                # away, or old history while it was in this store): it comes back. Sold or
                # still in stock there: it stays here.
                there = await session.scalar(
                    select(StockItem).where(
                        StockItem.store_id == target_store_id,
                        StockItem.fingerprint == item.fingerprint,
                    )
                )
                if there is None or there.state is not ItemState.extracted:
                    skipped += 1
                    if there is not None and there.state is ItemState.delivered:
                        conflicts += 1
                    continue
                await _revive(session, platform, there, target, account, position, item.state)
                new_id = there.id
            else:
                await _add_tokens(session, platform, target_store_id, [(new_id, r.token)])
            session.add_all(
                [
                    StockItemEvent(
                        item_id=new_id,
                        actor_label=actor.label,
                        event="moved_in",
                        to_state=item.state.value,
                        batch=batch,
                        details={"from_store": store_id, "from_item": item.id},
                    ),
                    StockItemEvent(
                        item_id=item.id,
                        actor_label=actor.label,
                        event="moved_out",
                        from_state=item.state.value,
                        to_state="extracted",
                        batch=batch,
                        details={"to_store": target_store_id, "to_item": new_id},
                    ),
                ]
            )
            item.state = ItemState.extracted
            position += 1
            moved += 1
    await session.flush()
    for pid in sources:
        pool = await session.get(StockPool, pid)
        assert pool is not None
        _announce_level(session, pool, before[pid], await _available(session, pid))
    audit.record(
        session,
        actor,
        "stock.items_moved",
        entity_type="stock_pool",
        entity_id=target.id,
        store_id=store_id,
        after={
            "moved": moved,
            "skipped": skipped,
            "sold_there": conflicts,
            "target_store": target_store_id,
        },
    )
    return {"moved": moved, "skipped": skipped, "sold_there": conflicts}


async def find_by_login(
    session: AsyncSession, platform: Platform, store_id: int, login: str
) -> StockItem | None:
    fp = platform.fingerprints(normalise_login(login))
    return await session.scalar(
        select(StockItem).where(StockItem.store_id == store_id, StockItem.fingerprint == fp)
    )


async def item_history(session: AsyncSession, item_id: int) -> list[StockItemEvent]:
    return list(
        (
            await session.scalars(
                select(StockItemEvent)
                .where(StockItemEvent.item_id == item_id)
                .order_by(StockItemEvent.id)
            )
        ).all()
    )


# ---- removing and putting back, export, search -------------------------------------------


async def remove_items(
    session: AsyncSession,
    actor: audit.Actor,
    store_id: int,
    item_ids: list[int],
    *,
    reason: str | None = None,
) -> int:
    """Take accounts out of sale by hand. Not deleted: they can be put back."""
    items = await _load_items(session, store_id, item_ids)
    wrong = [i for i in items if i.state not in (ItemState.available, ItemState.quarantined)]
    if wrong:
        raise StockError(f"{len(wrong)} selected accounts are not available or quarantined.")
    pools = {i.pool_id for i in items}
    before = {pid: await _available(session, pid) for pid in pools}
    batch = f"remove:{secrets.token_hex(6)}"
    for item in items:
        session.add(
            StockItemEvent(
                item_id=item.id,
                actor_label=actor.label,
                event="removed",
                from_state=item.state.value,
                to_state="extracted",
                batch=batch,
                details={"reason": reason} if reason else None,
            )
        )
        item.state = ItemState.extracted
    await session.flush()
    for pid in pools:
        pool = await session.get(StockPool, pid)
        assert pool is not None
        _announce_level(session, pool, before[pid], await _available(session, pid))
    audit.record(
        session,
        actor,
        "stock.items_removed",
        entity_type="stock_items",
        entity_id=batch,
        store_id=store_id,
        after={"count": len(items), "item_ids": item_ids[:200]},
        reason=reason,
    )
    return len(items)


async def put_back(
    session: AsyncSession, actor: audit.Actor, store_id: int, item_ids: list[int]
) -> int:
    """Undo "Remove": the accounts are available again in their pool."""
    items = await _load_items(session, store_id, item_ids)
    last = await last_events(session, [i.id for i in items])
    wrong = [i for i in items if i.state is not ItemState.extracted or last.get(i.id) != "removed"]
    if wrong:
        raise StockError(
            f"{len(wrong)} selected accounts were not removed by hand; "
            "undo an extraction from the extraction history instead."
        )
    batch = f"putback:{secrets.token_hex(6)}"
    for item in items:
        item.state = ItemState.available
        session.add(
            StockItemEvent(
                item_id=item.id,
                actor_label=actor.label,
                event="put_back",
                from_state="extracted",
                to_state="available",
                batch=batch,
            )
        )
    audit.record(
        session,
        actor,
        "stock.items_put_back",
        entity_type="stock_items",
        entity_id=batch,
        store_id=store_id,
        after={"count": len(items), "item_ids": item_ids[:200]},
    )
    return len(items)


EXPORT_FORMATS = ("login_password", "login_password_token", "tokens")


async def export_pool(
    session: AsyncSession,
    platform: Platform,
    actor: audit.Actor,
    pool: StockPool,
    fmt: str,
) -> tuple[str, int]:
    """The pool's available accounts as text. They stay in stock (use Extract to take them out)."""
    if fmt not in EXPORT_FORMATS:
        raise StockError("Unknown format.")
    items = list(
        await session.scalars(
            select(StockItem)
            .where(StockItem.pool_id == pool.id, StockItem.state == ItemState.available)
            .order_by(StockItem.position, StockItem.id)
        )
    )
    revealed = await reveal(session, platform, items)
    if fmt == "tokens":
        lines = [r.token for r in revealed if r.token]
    elif fmt == "login_password_token":
        lines = [f"{r.line()}:{r.token}" if r.token else r.line() for r in revealed]
    else:
        lines = [r.line() for r in revealed]
    audit.record(
        session,
        actor,
        "stock.exported",
        entity_type="stock_pool",
        entity_id=pool.id,
        store_id=pool.store_id,
        after={"format": fmt, "count": len(lines)},
    )
    return "\n".join(lines), len(lines)


def matches(r: RevealedItem, words: list[str]) -> bool:
    text = " ".join(
        [r.login, r.token or "", r.extra.get("email", ""), r.extra.get("email_provider", "")]
    ).lower()
    return all(w in text for w in words)


async def delete_pool(session: AsyncSession, actor: audit.Actor, pool: StockPool) -> None:
    """Only a pool that never had accounts can be deleted; any other is archived."""
    used = await session.scalar(select(func.count()).where(StockItem.pool_id == pool.id))
    if used:
        raise StockError(
            "This pool has accounts or their history: archive it instead "
            "(the history keeps those accounts from being sold twice)."
        )
    audit.record(
        session,
        actor,
        "stock.pool_deleted",
        entity_type="stock_pool",
        entity_id=pool.id,
        store_id=pool.store_id,
        before={"name": pool.name, "game": pool.game},
    )
    await session.delete(pool)
