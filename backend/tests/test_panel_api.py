"""Stage 9 endpoints: global search, activity feed, backups list, password change."""

from __future__ import annotations

import os
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import Actor
from app.core.config import get_settings
from app.core.db import utcnow
from app.core.platform import Platform
from app.orders.models import Order
from app.stock import service as stock
from app.stores import service as stores
from tests.conftest import OWNER_PASSWORD

ACTOR = Actor.system("tests")


async def _store(session: AsyncSession, platform: Platform) -> tuple[int, int]:
    store = await stores.create_store(session, platform, ACTOR, name="ZephyrStore", slug="zephyr")
    connection = await stores.add_connection(
        session, platform, ACTOR, store, connector_key="fake", credentials={"api_key": "test-key"}
    )
    pool = await stock.create_pool(session, ACTOR, store.id, name="Fortnite accounts")
    await session.commit()
    body = "Night.Owl@mail.com:secret1\nother@mail.com:secret2"
    await stock.commit_import(
        session, platform, ACTOR, store.id, pool.id, body, expected_sha256=stock.text_digest(body)
    )
    for remote, buyer in (("8f3c2a1e-aaaa", "NightOwl92"), ("77b4c4e0-bbbb", "PixelFox")):
        session.add(
            Order(
                store_id=store.id,
                connection_id=connection.id,
                remote_id=remote,
                state="delivered",
                quantity=1,
                total_amount=Decimal("5"),
                buyer_name=buyer,
                remote_created_at=utcnow(),
            )
        )
    await session.commit()
    return store.id, pool.id


async def test_search_orders_and_exact_logins(
    auth_client, session: AsyncSession, platform: Platform
) -> None:  # type: ignore[no-untyped-def]
    store_id, pool_id = await _store(session, platform)
    url = f"/api/v1/stores/{store_id}/search"
    by_id = (await auth_client.get(url, params={"q": "8f3c"})).json()
    assert [o["buyer_name"] for o in by_id["orders"]] == ["NightOwl92"]
    by_buyer = (await auth_client.get(url, params={"q": "pixel"})).json()
    assert [o["remote_id"] for o in by_buyer["orders"]] == ["77b4c4e0-bbbb"]
    # A login is found only exactly (case and spaces ignored), also when pasted with its password.
    hit = (await auth_client.get(url, params={"q": " night.owl@MAIL.com:secret1 "})).json()
    assert [(i["pool_id"], i["state"]) for i in hit["items"]] == [(pool_id, "available")]
    assert "secret1" not in str(hit)
    assert (await auth_client.get(url, params={"q": "night.owl"})).json()["items"] == []
    # LIKE wildcards are plain text.
    assert (await auth_client.get(url, params={"q": "%%"})).json()["orders"] == []


async def test_activity_feed(auth_client, session: AsyncSession, platform: Platform) -> None:  # type: ignore[no-untyped-def]
    store_id, _ = await _store(session, platform)
    rows = (await auth_client.get(f"/api/v1/stores/{store_id}/activity")).json()
    actions = [r["action"] for r in rows]
    assert "stock.imported" in actions or any(a.startswith("stock.") for a in actions)
    assert all("secret" not in str(r) for r in rows)


async def test_backups_list(auth_client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(get_settings(), "backup_dir", str(tmp_path))
    empty = (await auth_client.get("/api/v1/backups")).json()
    assert empty["available"] and empty["files"] == [] and empty["stale"]
    old = tmp_path / "zephyr-20261101-0300.dump"
    old.write_bytes(b"x" * 10)
    stamp = (utcnow() - timedelta(days=3)).timestamp()
    os.utime(old, (stamp, stamp))
    (tmp_path / "zephyr-20261114-0300.dump").write_bytes(b"y" * 20)
    (tmp_path / "notes.txt").write_text("ignored")
    data = (await auth_client.get("/api/v1/backups")).json()
    assert [f["name"] for f in data["files"]] == [
        "zephyr-20261114-0300.dump",
        "zephyr-20261101-0300.dump",
    ]
    assert data["stale"] is False
    monkeypatch.setattr(get_settings(), "backup_dir", str(tmp_path / "missing"))
    assert (await auth_client.get("/api/v1/backups")).json()["available"] is False


async def test_change_password(auth_client) -> None:  # type: ignore[no-untyped-def]
    wrong = await auth_client.post(
        "/api/v1/auth/password", json={"current_password": "nope", "new_password": "x" * 20}
    )
    assert wrong.status_code == 422 and "current password" in wrong.json()["detail"]
    short = await auth_client.post(
        "/api/v1/auth/password",
        json={"current_password": OWNER_PASSWORD, "new_password": "short"},
    )
    assert short.status_code == 422
    ok = await auth_client.post(
        "/api/v1/auth/password",
        json={"current_password": OWNER_PASSWORD, "new_password": "a-brand-new-long-password"},
    )
    assert ok.status_code == 200
    assert (await auth_client.get("/api/v1/auth/me")).status_code == 200  # this session stays
