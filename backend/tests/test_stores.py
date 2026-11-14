"""Stores, encrypted connections (I7), module switches and versioned config (I5, I8)."""

from __future__ import annotations

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import AuditLog
from app.core.events import OutboxEvent
from app.stores.models import MarketplaceConnection

STORE = {"name": "ZephyrStore", "slug": "zephyrstore", "timezone": "America/Argentina/Buenos_Aires"}


async def _store(client: httpx.AsyncClient, **overrides: str) -> dict:
    response = await client.post("/api/v1/stores", json={**STORE, **overrides})
    assert response.status_code == 201, response.text
    return response.json()


async def _connect(client: httpx.AsyncClient, store_id: int, key: str = "test-key") -> dict:
    response = await client.post(
        f"/api/v1/stores/{store_id}/connections",
        json={"connector": "fake", "credentials": {"api_key": key}},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _module(modules: list[dict], key: str) -> dict:
    return next(m for m in modules if m["key"] == key)


async def test_new_store_starts_in_shadow_mode_with_default_modules(auth_client) -> None:
    store = await _store(auth_client)
    assert store["shadow_mode"] is True
    modules = (await auth_client.get(f"/api/v1/stores/{store['id']}/modules")).json()
    assert {m["key"] for m in modules} >= {"deliveries", "restock", "topups", "review_reminders"}
    assert _module(modules, "deliveries")["enabled"] is True
    assert _module(modules, "topups")["enabled"] is False
    assert _module(modules, "review_reminders")["enabled"] is False


async def test_duplicate_slug_is_rejected(auth_client) -> None:
    await _store(auth_client)
    response = await auth_client.post("/api/v1/stores", json=STORE)
    assert response.status_code == 409


async def test_invalid_timezone_is_rejected(auth_client) -> None:
    response = await auth_client.post("/api/v1/stores", json={**STORE, "timezone": "Mars/Base"})
    assert response.status_code == 422


async def test_modules_need_the_marketplace_capabilities(auth_client) -> None:
    store = await _store(auth_client)
    url = f"/api/v1/stores/{store['id']}/modules/topups"
    modules = (await auth_client.get(f"/api/v1/stores/{store['id']}/modules")).json()
    assert _module(modules, "topups")["available"] is False
    assert _module(modules, "topups")["missing_capabilities"] == ["orders"]
    assert (await auth_client.put(url, json={"enabled": True})).status_code == 409

    await _connect(auth_client, store["id"])
    response = await auth_client.put(url, json={"enabled": True})
    assert response.status_code == 200
    assert response.json()["enabled"] is True


async def test_each_store_switches_modules_independently(auth_client) -> None:
    zephyr = await _store(auth_client)
    kawai = await _store(auth_client, name="Kawai smurf", slug="kawai")
    for s in (zephyr, kawai):
        await _connect(auth_client, s["id"])
    await auth_client.put(
        f"/api/v1/stores/{zephyr['id']}/modules/review_reminders", json={"enabled": True}
    )
    zephyr_modules = (await auth_client.get(f"/api/v1/stores/{zephyr['id']}/modules")).json()
    kawai_modules = (await auth_client.get(f"/api/v1/stores/{kawai['id']}/modules")).json()
    assert _module(zephyr_modules, "review_reminders")["enabled"] is True
    assert _module(kawai_modules, "review_reminders")["enabled"] is False


async def test_module_change_is_versioned_audited_and_announced(
    auth_client, session: AsyncSession
) -> None:
    store = await _store(auth_client)
    await _connect(auth_client, store["id"])
    url = f"/api/v1/stores/{store['id']}/modules/review_reminders"
    response = await auth_client.put(
        url, json={"enabled": True, "settings": {"delay_hours": 48}, "expected_version": 1}
    )
    body = response.json()
    assert body["version"] == 2
    assert body["settings"]["delay_hours"] == 48
    assert body["settings"]["message_template"]  # defaults kept

    entry = await session.scalar(select(AuditLog).where(AuditLog.action == "module.updated"))
    assert entry is not None
    assert entry.before["enabled"] is False and entry.before["settings"]["delay_hours"] == 24
    assert entry.after["enabled"] is True and entry.after["version"] == 2
    event = await session.scalar(
        select(OutboxEvent)
        .where(OutboxEvent.type == "config.changed")
        .order_by(OutboxEvent.id.desc())
    )
    assert event is not None and event.payload["module"] == "review_reminders"

    # A stale form (version 1) cannot silently overwrite the newer settings.
    stale = await auth_client.put(url, json={"settings": {"delay_hours": 2}, "expected_version": 1})
    assert stale.status_code == 409


async def test_invalid_module_settings_are_rejected(auth_client) -> None:
    store = await _store(auth_client)
    url = f"/api/v1/stores/{store['id']}/modules/review_reminders"
    response = await auth_client.put(url, json={"settings": {"delay_hours": 0}})
    assert response.status_code == 422
    response = await auth_client.put(url, json={"settings": {"unknown_field": 1}})
    assert response.status_code == 422
    response = await auth_client.put(
        f"/api/v1/stores/{store['id']}/modules/reports",
        json={"settings": {"daily_report_time": "25:00"}},
    )
    assert response.status_code == 422


async def test_unchanged_module_does_not_bump_version(auth_client) -> None:
    store = await _store(auth_client)
    url = f"/api/v1/stores/{store['id']}/modules/deliveries"
    response = await auth_client.put(url, json={"enabled": True})
    assert response.json()["version"] == 1


async def test_credentials_are_encrypted_and_never_returned(
    auth_client, session: AsyncSession
) -> None:
    store = await _store(auth_client)
    secret = "test-key"
    created = await _connect(auth_client, store["id"], key=secret)
    assert created["status"] == "connected"
    listed = await auth_client.get(f"/api/v1/stores/{store['id']}/connections")
    assert secret not in listed.text
    assert listed.json()[0]["credentials"]["api_key"].startswith("••••")

    row = await session.scalar(select(MarketplaceConnection))
    assert row is not None and secret not in row.credentials_encrypted
    audit_rows = (await session.scalars(select(AuditLog))).all()
    assert all(secret not in str(a.before) + str(a.after) for a in audit_rows)


async def test_invalid_credentials_are_reported(auth_client) -> None:
    store = await _store(auth_client)
    created = await _connect(auth_client, store["id"], key="wrong")
    assert created["status"] == "invalid"


async def test_unknown_connector_is_rejected(auth_client) -> None:
    store = await _store(auth_client)
    response = await auth_client.post(
        f"/api/v1/stores/{store['id']}/connections",
        json={"connector": "nope", "credentials": {"api_key": "x"}},
    )
    assert response.status_code == 422


async def test_store_update_is_audited(auth_client, session: AsyncSession) -> None:
    store = await _store(auth_client)
    response = await auth_client.patch(f"/api/v1/stores/{store['id']}", json={"shadow_mode": False})
    assert response.json()["shadow_mode"] is False
    entry = await session.scalar(select(AuditLog).where(AuditLog.action == "store.updated"))
    assert entry is not None
    assert entry.before["shadow_mode"] is True and entry.after["shadow_mode"] is False
    audit = (await auth_client.get("/api/v1/audit", params={"store_id": store["id"]})).json()
    assert audit[0]["action"] == "store.updated"
    assert {a["action"] for a in audit} == {"store.created", "store.updated"}
