"""Legacy stock migration against a database with the old Account Manager schema."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import Actor
from app.core.platform import Platform
from app.legacy.migrate import format_report, migrate
from app.stock import service
from app.stock.models import ItemState, StockItem, StockItemEvent, StockPool
from app.stores import service as stores

ACTOR = Actor.system("tests")
OLD_SCHEMA = """
CREATE TABLE games (id TEXT PRIMARY KEY, name TEXT NOT NULL, position INTEGER NOT NULL DEFAULT 0);
CREATE TABLE categories (id TEXT PRIMARY KEY, name TEXT NOT NULL, game_id TEXT);
CREATE TABLE accounts (id INTEGER PRIMARY KEY AUTOINCREMENT, category_id TEXT NOT NULL,
    position INTEGER NOT NULL, value TEXT NOT NULL);
CREATE TABLE category_tokens (id INTEGER PRIMARY KEY AUTOINCREMENT, category_id TEXT NOT NULL,
    position INTEGER NOT NULL, account_value TEXT NOT NULL, value TEXT NOT NULL,
    UNIQUE(category_id, account_value));
CREATE TABLE extraction_history (id INTEGER PRIMARY KEY AUTOINCREMENT, category_name TEXT NOT NULL,
    extracted_at TEXT NOT NULL, items_json TEXT NOT NULL);
CREATE TABLE restock_upload_ledger (login TEXT PRIMARY KEY, category_id TEXT, offer_id TEXT,
    uploaded_at TEXT NOT NULL);
"""


def make_legacy_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript(OLD_SCHEMA)
    con.execute("INSERT INTO games VALUES ('g1', 'Rocket League', 0)")
    con.executemany(
        "INSERT INTO categories VALUES (?, ?, 'g1')", [("c1", "RLCS x6"), ("c2", "Ranked Ready")]
    )
    # Stored out of order on purpose: the old position decides the order.
    con.executemany(
        "INSERT INTO accounts (category_id, position, value) VALUES (?, ?, ?)",
        [
            ("c1", 2, "acc2:pw2"),
            ("c1", 1, "acc1:pw1"),
            ("c1", 3, "acc3:pw:with:colons"),
            ("c2", 1, "ranked@inbox.lv:rpw"),
        ],
    )
    con.execute(
        "INSERT INTO category_tokens (category_id, position, account_value, value) "
        "VALUES ('c1', 1, 'acc1:pw1', 'tok-1')"
    )
    con.executemany(
        "INSERT INTO extraction_history (category_name, extracted_at, items_json) VALUES (?, ?, ?)",
        [
            (
                "Rocket League / RLCS x6",
                "2026-10-02T18:00:00+00:00",
                json.dumps(
                    [
                        {
                            "position": 0,
                            "value": "sold1:pw",
                            "tokens": [{"position": 0, "value": "tok-s"}],
                        },
                        {"position": 1, "value": "acc2:pw2", "tokens": []},
                    ]
                ),
            ),  # acc2 was put back
            (
                "Old category",
                "2026-10-03T10:00:00+00:00",
                json.dumps(["moved1:pw", "three:part:tok3"]),
            ),
            (
                "Rocket League / RLCS x6",
                "2026-10-04T10:00:00+00:00",
                json.dumps(["sold1:pw"]),
            ),  # twice
        ],
    )
    con.executemany(
        "INSERT INTO restock_upload_ledger VALUES (?, ?, ?, ?)",
        [
            ("vaulted1", "c1", "offer-9", "2026-10-05T00:00:00+00:00"),
            ("acc3", "c1", "offer-9", "2026-10-05T00:00:00+00:00"),
        ],  # still in stock: suspicious
    )
    con.commit()
    con.close()


async def test_migration_reconciles_and_is_idempotent(
    tmp_path: Path, session: AsyncSession, platform: Platform
) -> None:
    db = tmp_path / "accounts.sqlite3"
    make_legacy_db(db)
    before = db.read_bytes()
    store_id = (
        await stores.create_store(session, platform, ACTOR, name="Zephyr", slug="zephyr")
    ).id
    await session.commit()

    dry = await migrate(session, platform, ACTOR, db_path=db, store_id=store_id, dry_run=True)
    assert dry.ok and dry.legacy_total == 4
    assert await session.scalar(select(func.count()).select_from(StockItem)) == 0

    report = await migrate(session, platform, ACTOR, db_path=db, store_id=store_id)
    assert db.read_bytes() == before, "the old database must never be modified"
    assert report.ok, format_report(report)
    assert report.legacy_total == report.migrated_total == 4
    assert [
        (p.name, p.legacy_accounts, p.migrated, p.legacy_tokens, p.migrated_tokens)
        for p in report.pools
    ] == [
        ("RLCS x6", 3, 3, 1, 1),
        ("Ranked Ready", 1, 1, 0, 0),
    ]
    assert report.history_items_created == 3  # sold1, moved1, three (acc2 is in stock)
    assert dict(report.overlaps) == {"RLCS x6": 1}
    assert (report.ledger_void_created, report.ledger_quarantined) == (1, 1)

    pool = await session.scalar(select(StockPool).where(StockPool.name == "RLCS x6"))
    assert pool is not None
    pool_id = pool.id
    _, items = await service.extract(session, platform, ACTOR, store_id, pool_id, 1)
    assert (items[0].login, items[0].password, items[0].token) == ("acc1", "pw1", "tok-1")
    await session.rollback()

    acc3 = await service.find_by_login(session, platform, store_id, "acc3")
    assert acc3 is not None and acc3.state is ItemState.quarantined
    [revealed] = await service.reveal(session, platform, [acc3])
    assert revealed.password == "pw:with:colons"

    sold = await service.find_by_login(session, platform, store_id, "sold1")
    assert sold is not None and sold.state is ItemState.extracted and sold.has_token
    events = await service.item_history(session, sold.id)
    assert [e.event for e in events] == ["legacy_extracted", "legacy_extracted"]

    moved = await service.find_by_login(session, platform, store_id, "moved1")
    assert moved is not None
    archived = await session.scalar(select(StockPool.archived).where(StockPool.id == moved.pool_id))
    assert archived is True

    # Already sold in the old system: importing it again is refused.
    preview = await service.preview_import(
        session, platform, store_id, pool_id, "sold1:pw\nvaulted1:x"
    )
    assert [line.status for line in preview.lines] == ["previously_removed", "previously_removed"]

    counts = (
        await session.scalar(select(func.count()).select_from(StockItem)),
        await session.scalar(select(func.count()).select_from(StockItemEvent)),
    )
    again = await migrate(session, platform, ACTOR, db_path=db, store_id=store_id)
    assert again.ok and again.created_items == 0 and again.history_items_created == 0
    assert again.already_present == 4
    assert counts == (
        await session.scalar(select(func.count()).select_from(StockItem)),
        await session.scalar(select(func.count()).select_from(StockItemEvent)),
    )


async def test_overlaps_can_be_quarantined(
    tmp_path: Path, session: AsyncSession, platform: Platform
) -> None:
    db = tmp_path / "accounts.sqlite3"
    make_legacy_db(db)
    store_id = (
        await stores.create_store(session, platform, ACTOR, name="Zephyr", slug="zephyr")
    ).id
    await session.commit()
    report = await migrate(
        session, platform, ACTOR, db_path=db, store_id=store_id, quarantine_overlaps=True
    )
    assert report.ok
    acc2 = await service.find_by_login(session, platform, store_id, "acc2")
    assert acc2 is not None and acc2.state is ItemState.quarantined


async def test_two_stores_with_the_same_old_ids_keep_their_own_history(
    tmp_path: Path, session: AsyncSession, platform: Platform
) -> None:
    """Old databases number their history from 1: keys must not collide between stores."""
    ids = []
    for slug in ("zephyr", "kawai"):
        db = tmp_path / f"{slug}.sqlite3"
        make_legacy_db(db)
        store_id = (await stores.create_store(session, platform, ACTOR, name=slug, slug=slug)).id
        await session.commit()
        report = await migrate(session, platform, ACTOR, db_path=db, store_id=store_id)
        assert report.ok and dict(report.overlaps) == {"RLCS x6": 1}
        ids.append(store_id)
    for store_id in ids:
        sold = await service.find_by_login(session, platform, store_id, "sold1")
        assert sold is not None
        assert len(await service.item_history(session, sold.id)) == 2


async def test_sync_before_cut_over_takes_out_what_the_old_system_sold(
    tmp_path: Path, session: AsyncSession, platform: Platform
) -> None:
    db = tmp_path / "accounts.sqlite3"
    make_legacy_db(db)
    store_id = (
        await stores.create_store(session, platform, ACTOR, name="Zephyr", slug="zephyr")
    ).id
    await session.commit()
    await migrate(session, platform, ACTOR, db_path=db, store_id=store_id)

    # During the shadow period the old system sells acc1, and moves "ranked" to category c1.
    con = sqlite3.connect(db)
    con.execute("DELETE FROM accounts WHERE value = 'acc1:pw1'")
    con.execute("UPDATE accounts SET category_id = 'c1', position = 9 WHERE value LIKE 'ranked%'")
    con.commit()
    con.close()

    plain = await migrate(session, platform, ACTOR, db_path=db, store_id=store_id, dry_run=True)
    assert plain.gone_from_old_stock == 0  # only with sync_removed

    report = await migrate(
        session, platform, ACTOR, db_path=db, store_id=store_id, sync_removed=True
    )
    assert report.gone_from_old_stock == 1
    assert "Gone from the old stock" in format_report(report)
    acc1 = await service.find_by_login(session, platform, store_id, "acc1")
    assert acc1 is not None and acc1.state is ItemState.extracted
    ranked = await service.find_by_login(session, platform, store_id, "ranked@inbox.lv")
    assert ranked is not None and ranked.state is ItemState.available  # moved, not sold
    again = await migrate(
        session, platform, ACTOR, db_path=db, store_id=store_id, sync_removed=True
    )
    assert again.gone_from_old_stock == 0
