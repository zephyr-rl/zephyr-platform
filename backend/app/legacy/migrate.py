"""Import the stock of an old store (``account_manager/accounts.sqlite3``) into the platform.

The old database is opened read-only and never modified. The import is idempotent: running it
again only adds what is missing, so it can be repeated right before the cut-over with a fresh
copy of the live database.

What is imported:
- each category → a stock pool (matched by the old category id on later runs)
- each account in stock → an ``available`` item, keeping the old order, with its Twitch token
- each entry of the extraction history (accounts taken out by hand: sold manually or moved to
  another store) → an ``extracted`` item, so the same account can never be imported and sold
  again from this store. If that account is ALSO in stock today (it was put back, or re-added),
  the stock item stays as it is and is listed in the report as an overlap to review.
- each login of ``restock_upload_ledger`` (accounts uploaded to the Eldorado vault by the old
  restock) → a ``void`` item if it is not known yet; quarantined if it is still in stock.

Before the cut-over (``sync_removed``): accounts imported earlier that are no longer in the old
stock (sold or removed by the old system during the shadow period) are taken out of sale
(``extracted``, can be undone), so they can never be sold a second time by the platform.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.crypto import hint
from app.core.platform import Platform
from app.stock import service
from app.stock.importer import normalise_login
from app.stock.models import ItemState, StockItem, StockItemEvent, StockItemSecret, StockPool

HISTORY_POOL = "Legacy history"
BATCH = 500


@dataclass
class PoolReport:
    legacy_id: str
    name: str
    legacy_accounts: int
    legacy_tokens: int
    migrated: int = 0
    migrated_tokens: int = 0

    @property
    def ok(self) -> bool:
        return self.migrated == self.legacy_accounts and self.migrated_tokens == self.legacy_tokens


@dataclass
class Report:
    database: str
    store_id: int
    dry_run: bool
    pools: list[PoolReport] = field(default_factory=list)
    created_items: int = 0
    already_present: int = 0
    history_entries: int = 0
    history_items_created: int = 0
    overlaps: Counter[str] = field(default_factory=Counter)
    overlap_item_ids: list[int] = field(default_factory=list)
    gone_from_old_stock: int = 0
    ledger_logins: int = 0
    ledger_void_created: int = 0
    ledger_quarantined: int = 0
    unreadable_values: int = 0

    @property
    def legacy_total(self) -> int:
        return sum(p.legacy_accounts for p in self.pools)

    @property
    def migrated_total(self) -> int:
        return sum(p.migrated for p in self.pools)

    @property
    def ok(self) -> bool:
        return all(p.ok for p in self.pools) and self.unreadable_values == 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "database": self.database,
            "store_id": self.store_id,
            "dry_run": self.dry_run,
            "ok": self.ok,
            "legacy_accounts": self.legacy_total,
            "migrated_in_stock": self.migrated_total,
            "created_items": self.created_items,
            "already_present": self.already_present,
            "history_entries": self.history_entries,
            "history_items_created": self.history_items_created,
            "overlaps_in_stock_and_history": sum(self.overlaps.values()),
            "overlaps_by_pool": dict(self.overlaps),
            "gone_from_old_stock": self.gone_from_old_stock,
            "ledger_logins": self.ledger_logins,
            "ledger_void_created": self.ledger_void_created,
            "ledger_quarantined": self.ledger_quarantined,
            "unreadable_values": self.unreadable_values,
            "pools": [{**p.__dict__, "ok": p.ok} for p in self.pools],
        }


@dataclass
class LegacyData:
    games: dict[str, str]
    categories: list[tuple[str, str, str | None]]
    accounts: list[tuple[int, str, int, str]]  # id, category_id, position, value
    tokens: dict[tuple[str, str], str]
    history: list[tuple[int, str, str, list[Any]]]
    ledger: list[tuple[str, str | None, str | None, str | None]]


def read_legacy(path: Path) -> LegacyData:
    if not path.is_file():
        raise FileNotFoundError(path)
    # Read-only. The path is quoted for the URI (Windows folders have spaces: "Eldorado API - X").
    uri = "file:" + quote(path.resolve().as_posix(), safe="/:") + "?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    try:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        games = dict(con.execute("SELECT id, name FROM games")) if "games" in tables else {}
        categories = list(con.execute("SELECT id, name, game_id FROM categories ORDER BY rowid"))
        accounts = list(
            con.execute(
                "SELECT id, category_id, position, value FROM accounts ORDER BY category_id, position, id"
            )
        )
        tokens: dict[tuple[str, str], str] = {}
        if "category_tokens" in tables:
            for cat, value, token in con.execute(
                "SELECT category_id, account_value, value FROM category_tokens ORDER BY position, id"
            ):
                tokens.setdefault((cat, value), token)
        history = (
            [
                (hid, name, at, json.loads(items))
                for hid, name, at, items in con.execute(
                    "SELECT id, category_name, extracted_at, items_json FROM extraction_history ORDER BY id"
                )
            ]
            if "extraction_history" in tables
            else []
        )
        ledger = (
            list(
                con.execute(
                    "SELECT login, category_id, offer_id, uploaded_at FROM restock_upload_ledger"
                )
            )
            if "restock_upload_ledger" in tables
            else []
        )
    finally:
        con.close()
    return LegacyData(games, categories, accounts, tokens, history, ledger)


def _split(value: str) -> tuple[str, str] | None:
    login, sep, password = value.strip().partition(":")
    if not sep or not login.strip() or not password.strip():
        return None
    return login.strip(), password.strip()


def _when(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


async def _pool_for_category(
    session: AsyncSession, actor: audit.Actor, store_id: int, legacy_id: str, name: str, game: str
) -> StockPool:
    pool = await session.scalar(
        select(StockPool).where(StockPool.store_id == store_id, StockPool.legacy_id == legacy_id)
    )
    if pool is not None:
        return pool
    taken = await session.scalar(
        select(StockPool.id).where(StockPool.store_id == store_id, StockPool.name == name)
    )
    pool = await service.create_pool(
        session,
        actor,
        store_id,
        name=f"{name} (legacy)" if taken else name,
        game=game,
        legacy_id=legacy_id,
    )
    return pool


async def _history_pool(session: AsyncSession, actor: audit.Actor, store_id: int) -> StockPool:
    pool = await session.scalar(
        select(StockPool).where(StockPool.store_id == store_id, StockPool.legacy_id == "history")
    )
    if pool is None:
        pool = await service.create_pool(
            session, actor, store_id, name=HISTORY_POOL, legacy_id="history", low_stock_threshold=0
        )
        pool.archived = True
    return pool


async def _insert_items(session: AsyncSession, rows: list[dict[str, Any]]) -> dict[str, int]:
    """Insert, skipping fingerprints already in the store. Returns legacy_ref → new item id."""
    created: dict[str, int] = {}
    for start in range(0, len(rows), BATCH):
        chunk = rows[start : start + BATCH]
        result = await session.execute(
            insert(StockItem)
            .values(chunk)
            .on_conflict_do_nothing(index_elements=["store_id", "fingerprint"])
            .returning(StockItem.legacy_ref, StockItem.id)
        )
        created.update({ref: item_id for ref, item_id in result if ref})
    return created


async def _insert_events(session: AsyncSession, rows: list[dict[str, Any]]) -> None:
    for start in range(0, len(rows), BATCH):
        await session.execute(
            insert(StockItemEvent)
            .values(rows[start : start + BATCH])
            .on_conflict_do_nothing(index_elements=["legacy_key"])
        )


async def migrate(
    session: AsyncSession,
    platform: Platform,
    actor: audit.Actor,
    *,
    db_path: Path,
    store_id: int,
    dry_run: bool = False,
    quarantine_overlaps: bool = False,
    sync_removed: bool = False,
) -> Report:
    data = read_legacy(db_path)
    report = Report(database=str(db_path), store_id=store_id, dry_run=dry_run)
    box, fingerprint = platform.secrets, platform.fingerprints
    context = service.payload_context(store_id)
    token_context = service.secret_context(store_id, service.TOKEN)

    # 1. Pools
    pools: dict[str, StockPool] = {}
    per_cat = Counter(cat for _, cat, _, _ in data.accounts)
    in_stock = {(cat, value) for _, cat, _, value in data.accounts}
    per_cat_tokens = Counter(cat for (cat, value) in data.tokens if (cat, value) in in_stock)
    for legacy_id, name, game_id in data.categories:
        pools[legacy_id] = await _pool_for_category(
            session,
            actor,
            store_id,
            legacy_id,
            name.strip(),
            data.games.get(game_id or "", "Rocket League"),
        )
        report.pools.append(
            PoolReport(
                legacy_id, pools[legacy_id].name, per_cat[legacy_id], per_cat_tokens[legacy_id]
            )
        )
    await session.flush()

    # 2. Accounts in stock, in their old order
    rows: list[dict[str, Any]] = []
    tokens_for: dict[str, str] = {}
    rank: Counter[str] = Counter()
    for account_id, cat, _position, value in data.accounts:
        split = _split(value)
        if split is None or cat not in pools:
            report.unreadable_values += 1
            continue
        login, password = split
        rank[cat] += 1
        ref = f"{store_id}:stock:{cat}:{account_id}"
        token = data.tokens.get((cat, value))
        if token:
            tokens_for[ref] = token
        rows.append(
            {
                "store_id": store_id,
                "pool_id": pools[cat].id,
                "fingerprint": fingerprint(normalise_login(login)),
                "payload_encrypted": box.encrypt_json(
                    {"login": login, "password": password}, context=context
                ),
                "login_hint": hint(login),
                "state": ItemState.available,
                "position": rank[cat],
                "has_token": bool(token),
                "legacy_ref": ref,
            }
        )
    created = await _insert_items(session, rows)
    report.created_items = len(created)
    report.already_present = len(rows) - len(created)
    session.add_all(
        StockItemSecret(
            item_id=item_id,
            kind=service.TOKEN,
            value_encrypted=box.encrypt(tokens_for[ref], context=token_context),
        )
        for ref, item_id in created.items()
        if ref in tokens_for
    )
    await _insert_events(
        session,
        [
            {
                "item_id": item_id,
                "actor_label": actor.label,
                "event": "imported",
                "to_state": "available",
                "batch": "legacy",
                "details": {"source": "legacy stock"},
                "legacy_key": ref,
            }
            for ref, item_id in created.items()
        ],
    )

    # 3. Extraction history
    pools_by_name = {p.name.lower(): p for p in pools.values()}
    report.history_entries = sum(len(items) for *_, items in data.history)
    hist_rows: list[dict[str, Any]] = []
    hist_events: list[dict[str, Any]] = []
    hist_tokens: dict[str, str] = {}
    pending_by_fp: dict[str, str] = {}
    history_pool: StockPool | None = None
    for hist_id, raw_name, at, items in data.history:
        category = raw_name.split(" / ", 1)[-1].strip()
        when = _when(at)
        for idx, item in enumerate(items):
            value = item if isinstance(item, str) else str(item.get("value", ""))
            tokens = (
                []
                if isinstance(item, str)
                else [t.get("value") for t in item.get("tokens") or [] if t.get("value")]
            )
            fields = value.split(":")
            split = _split(value) if len(fields) <= 2 else (fields[0].strip(), fields[1].strip())
            if split is None:
                report.unreadable_values += 1
                continue
            login, password = split
            if len(fields) > 2 and not tokens and fields[-1].strip():
                tokens = [fields[-1].strip()]
            fp = fingerprint(normalise_login(login))
            key = f"{store_id}:hist:{hist_id}:{idx}"
            details = {"old_category": raw_name, "extracted_at": at, "source": "legacy history"}
            existing = await session.scalar(
                select(StockItem.id).where(
                    StockItem.store_id == store_id, StockItem.fingerprint == fp
                )
            )
            if existing is not None:
                hist_events.append(
                    {
                        "item_id": existing,
                        "event": "legacy_extracted",
                        "key": key,
                        "at": when,
                        "details": details,
                    }
                )
                continue
            if fp in pending_by_fp:  # extracted twice in the old system (put back, then out again)
                hist_events.append(
                    {
                        "ref": pending_by_fp[fp],
                        "event": "legacy_extracted",
                        "key": key,
                        "at": when,
                        "details": details,
                    }
                )
                continue
            pool = pools_by_name.get(category.lower())
            if pool is None:
                history_pool = history_pool or await _history_pool(session, actor, store_id)
                pool = history_pool
            pending_by_fp[fp] = key
            if tokens:
                hist_tokens[key] = tokens[0]
            hist_rows.append(
                {
                    "store_id": store_id,
                    "pool_id": pool.id,
                    "fingerprint": fp,
                    "payload_encrypted": box.encrypt_json(
                        {"login": login, "password": password}, context=context
                    ),
                    "login_hint": hint(login),
                    "state": ItemState.extracted,
                    "position": 0,
                    "has_token": bool(tokens),
                    "legacy_ref": key,
                    "note": f"Taken out in the old system ({raw_name}, {at[:10]}).",
                }
            )
            hist_events.append(
                {
                    "ref": key,
                    "event": "legacy_extracted",
                    "key": key,
                    "at": when,
                    "details": details,
                }
            )
    await session.flush()
    created_hist = await _insert_items(session, hist_rows)
    report.history_items_created = len(created_hist)
    session.add_all(
        StockItemSecret(
            item_id=item_id,
            kind=service.TOKEN,
            value_encrypted=box.encrypt(hist_tokens[ref], context=token_context),
        )
        for ref, item_id in created_hist.items()
        if ref in hist_tokens
    )
    event_rows = []
    for ev in hist_events:
        item_id = ev.get("item_id") or created_hist.get(ev.get("ref", ""))
        if item_id is None:
            continue
        row = {
            "item_id": item_id,
            "actor_label": actor.label,
            "event": ev["event"],
            "batch": "legacy",
            "details": ev["details"],
            "legacy_key": ev["key"],
            "to_state": "extracted" if "ref" in ev else None,
        }
        if ev["at"] is not None:
            row["at"] = ev["at"]
        event_rows.append(row)
    await _insert_events(session, event_rows)

    # Overlaps: in stock today AND in the history (re-added or put back). Reported for review.
    overlap = await session.execute(
        select(StockItem.id, StockPool.name)
        .join(StockPool, StockPool.id == StockItem.pool_id)
        .where(
            StockItem.store_id == store_id,
            StockItem.state.in_([ItemState.available, ItemState.quarantined]),
            StockItem.id.in_(
                select(StockItemEvent.item_id).where(StockItemEvent.event == "legacy_extracted")
            ),
        )
    )
    for item_id, pool_name in overlap:
        report.overlaps[pool_name] += 1
        report.overlap_item_ids.append(item_id)
    if quarantine_overlaps and report.overlap_item_ids:
        available = list(
            await session.scalars(
                select(StockItem.id).where(
                    StockItem.id.in_(report.overlap_item_ids),
                    StockItem.state == ItemState.available,
                )
            )
        )
        if available:
            await service.change_state(
                session,
                actor,
                store_id,
                available,
                ItemState.quarantined,
                reason="In stock and also in the old extraction history: check before selling.",
            )

    # Before the cut-over: accounts imported earlier that left the old stock since then.
    if sync_removed:
        report.gone_from_old_stock = await _sync_removed(
            session, actor, store_id, {r["fingerprint"] for r in rows}
        )

    # 4. Restock upload ledger (accounts already uploaded to the Eldorado vault)
    report.ledger_logins = len(data.ledger)
    void_rows = []
    for login, _cat, offer_id, uploaded_at in data.ledger:
        if not login or not login.strip():
            continue
        fp = fingerprint(normalise_login(login))
        item = await session.scalar(
            select(StockItem).where(StockItem.store_id == store_id, StockItem.fingerprint == fp)
        )
        if item is None:
            history_pool = history_pool or await _history_pool(session, actor, store_id)
            void_rows.append(
                {
                    "store_id": store_id,
                    "pool_id": history_pool.id,
                    "fingerprint": fp,
                    "payload_encrypted": box.encrypt_json(
                        {"login": login.strip(), "password": ""}, context=context
                    ),
                    "login_hint": hint(login),
                    "state": ItemState.void,
                    "position": 0,
                    "has_token": False,
                    "legacy_ref": f"{store_id}:ledger:{fp[:16]}",
                    "note": f"Uploaded to Eldorado offer {offer_id} by the old restock ({(uploaded_at or '')[:10]}).",
                }
            )
        elif item.state is ItemState.available:
            await service.change_state(
                session,
                actor,
                store_id,
                [item.id],
                ItemState.quarantined,
                reason=f"Already uploaded to Eldorado offer {offer_id} by the old restock.",
            )
            report.ledger_quarantined += 1
    report.ledger_void_created = len(await _insert_items(session, void_rows))

    # 5. Reconciliation: count what is now in each pool from each old category
    for pool_report in report.pools:
        pool = pools[pool_report.legacy_id]
        prefix = f"{store_id}:stock:{pool_report.legacy_id}:%"
        pool_report.migrated = int(
            await session.scalar(
                select(func.count()).where(
                    StockItem.pool_id == pool.id,
                    StockItem.legacy_ref.like(prefix),
                    StockItem.state.in_([ItemState.available, ItemState.quarantined]),
                )
            )
            or 0
        )
        pool_report.migrated_tokens = int(
            await session.scalar(
                select(func.count()).where(
                    StockItem.pool_id == pool.id,
                    StockItem.legacy_ref.like(prefix),
                    StockItem.has_token.is_(True),
                )
            )
            or 0
        )

    audit.record(
        session,
        actor,
        "stock.legacy_migrated",
        store_id=store_id,
        after={k: v for k, v in report.as_dict().items() if k != "pools"},
    )
    if dry_run:
        await session.rollback()
    else:
        await session.commit()
    return report


async def _sync_removed(
    session: AsyncSession, actor: audit.Actor, store_id: int, current: set[str]
) -> int:
    """Take out of sale the legacy accounts still available here but gone from the old stock.
    Matched by login fingerprint, so an account moved to another category there is kept."""
    gone = list(
        await session.scalars(
            select(StockItem)
            .where(
                StockItem.store_id == store_id,
                StockItem.legacy_ref.like(f"{store_id}:stock:%"),
                StockItem.state == ItemState.available,
                StockItem.fingerprint.not_in(current) if current else StockItem.id.is_not(None),
            )
            .with_for_update()
        )
    )
    for item in gone:
        item.state = ItemState.extracted
        item.note = (
            "No longer in the old system's stock before the cut-over (sold or removed there)."
        )
    if gone:
        await _insert_events(
            session,
            [
                {
                    "item_id": item.id,
                    "actor_label": actor.label,
                    "event": "extracted",
                    "from_state": "available",
                    "to_state": "extracted",
                    "batch": "legacy-sync",
                    "details": {"reason": "gone from the old stock"},
                    "legacy_key": None,
                }
                for item in gone
            ],
        )
    return len(gone)


def format_report(report: Report) -> str:
    lines = [
        f"Legacy stock migration {'(DRY RUN, nothing saved)' if report.dry_run else ''}".strip(),
        f"Source: {report.database}",
        "",
        f"{'Pool':<40} {'old':>6} {'new':>6} {'tokens':>13}  ",
    ]
    for p in report.pools:
        tokens = f"{p.migrated_tokens}/{p.legacy_tokens}"
        lines.append(
            f"{p.name[:40]:<40} {p.legacy_accounts:>6} {p.migrated:>6} {tokens:>13}  {'OK' if p.ok else 'MISMATCH'}"
        )
    lines += [
        f"{'TOTAL':<40} {report.legacy_total:>6} {report.migrated_total:>6}",
        "",
        f"Items created now: {report.created_items} (already present from an earlier run: {report.already_present})",
        f"History entries: {report.history_entries} -> new 'extracted' items: {report.history_items_created}",
        f"In stock AND in the old history (review): {sum(report.overlaps.values())}"
        + (
            ""
            if not report.overlaps
            else " — " + ", ".join(f"{k}: {v}" for k, v in report.overlaps.most_common())
        ),
        f"Gone from the old stock since the first import (taken out of sale): {report.gone_from_old_stock}",
        f"Upload ledger logins: {report.ledger_logins} (void created {report.ledger_void_created}, quarantined {report.ledger_quarantined})",
        f"Unreadable values: {report.unreadable_values}",
        "",
        "RESULT: " + ("OK, counts reconcile" if report.ok else "MISMATCH, see above"),
    ]
    return "\n".join(lines)
