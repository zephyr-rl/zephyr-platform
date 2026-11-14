"""The shadow comparison against the old system's logs."""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.audit import Actor
from app.core.db import utcnow
from app.core.platform import Platform
from app.legacy.compare import compare, format_comparison
from app.orders.models import Fulfilment, FulfilmentState, Order
from app.stores import service as stores

ACTOR = Actor.system("tests")
NOW = utcnow()


def _iso(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).isoformat()


def old_folder(tmp: Path) -> Path:
    folder = tmp / "Eldorado API - ZephyrStore"
    folder.mkdir()
    secret = "Thank you!\nlogin: someone@mail.com\npassword: hunter2"
    deliveries = [
        {
            "timestamp": _iso(2),
            "type": "SUCCESS",
            "orderId": "SAME-1",
            "details": {"orderId": "SAME-1", "totalAccounts": 2, "formattedMessage": secret},
        },
        {
            "timestamp": _iso(3),
            "type": "SUCCESS",
            "orderId": "OLD-ONLY",
            "details": {"orderId": "OLD-ONLY", "totalAccounts": 1, "formattedMessage": secret},
        },
        {
            "timestamp": _iso(4),
            "type": "SUCCESS",
            "orderId": "DIFF-1",
            "details": {"orderId": "DIFF-1", "totalAccounts": 1},
        },
        {"timestamp": _iso(5), "type": "ERROR", "orderId": "", "message": "worker error"},
        {
            "timestamp": _iso(50),
            "type": "SUCCESS",
            "orderId": "TOO-OLD",
            "details": {"orderId": "TOO-OLD", "totalAccounts": 1},
        },
    ]
    (folder / "item_delivery_logs.json").write_text(json.dumps(deliveries), encoding="utf-8")
    topups = [
        {"timestamp": _iso(1), "level": "SUCCESS", "orderId": "TOP-1", "buyer": "x"},
        {"timestamp": _iso(1), "level": "CONFIG", "orderId": None},
    ]
    (folder / "topup_logs.json").write_text(json.dumps(topups), encoding="utf-8")
    restock = [
        {
            "timestamp": _iso(2),
            "type": "SUCCESS",
            "offerId": "acc-1",
            "details": {"login": "a@b.c"},
        },
        {
            "timestamp": _iso(2),
            "type": "SUCCESS",
            "offerId": "acc-1",
            "details": {"login": "d@e.f"},
        },
    ]
    (folder / "restock_logs.json").write_text(json.dumps(restock), encoding="utf-8")
    return folder


async def _platform_side(session: AsyncSession, platform: Platform) -> int:
    store = await stores.create_store(session, platform, ACTOR, name="ZephyrStore", slug="zephyr")
    connection = await stores.add_connection(
        session, platform, ACTOR, store, connector_key="fake", credentials={"api_key": "test-key"}
    )
    await session.flush()
    for remote_id, mode, units in (
        ("SAME-1", "auto", 2),
        ("DIFF-1", "auto", 2),
        ("NEW-ONLY", "assisted", 1),
        ("TOP-1", "topup", 1),
    ):
        order = Order(
            store_id=store.id,
            connection_id=connection.id,
            remote_id=remote_id,
            state="paid",
            quantity=1,
            total_amount=Decimal("1"),
            remote_created_at=NOW - timedelta(hours=2),
        )
        session.add(order)
        await session.flush()
        session.add(
            Fulfilment(
                order_id=order.id,
                store_id=store.id,
                mode=mode,
                units=units,
                state=FulfilmentState.simulated,
            )
        )
    for _ in range(2):
        audit.record(
            session,
            Actor.system("shadow"),
            "shadow.vault_add",
            entity_type="remote",
            entity_id="acc-1",
            store_id=store.id,
        )
    await session.commit()
    return store.id


async def test_comparison(session: AsyncSession, platform: Platform, tmp_path: Path) -> None:
    store_id = await _platform_side(session, platform)
    result = await compare(
        session,
        store_id,
        old_folder(tmp_path),
        NOW - timedelta(hours=24),
        NOW + timedelta(minutes=1),
    )
    assert result.deliveries.both == ["SAME-1"]
    assert result.deliveries.only_old == ["OLD-ONLY"]
    assert result.deliveries.only_new == ["NEW-ONLY"]
    assert result.deliveries.different == ["DIFF-1: old 1 accounts, new 2 accounts"]
    assert result.topups.ok and result.topups.both == ["TOP-1"]
    assert result.restock.ok and result.restock.both == ["acc-1"]
    assert not result.ok
    text = format_comparison(result, "ZephyrStore")
    assert "DIFFERENCES to check" in text and "OLD-ONLY" in text
    assert "hunter2" not in text and "someone@mail.com" not in text


async def test_missing_files_are_empty(
    session: AsyncSession, platform: Platform, tmp_path: Path
) -> None:
    store = await stores.create_store(session, platform, ACTOR, name="Jewelry", slug="jewelry")
    await session.commit()
    result = await compare(session, store.id, tmp_path, NOW - timedelta(hours=24), NOW)
    assert result.ok
