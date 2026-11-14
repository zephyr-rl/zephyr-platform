"""Invariant I6: authentication always required, rate-limited login, revocable sessions."""

from __future__ import annotations

from datetime import timedelta

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import AuditLog
from app.core.db import utcnow
from app.core.events import OutboxEvent
from app.identity.models import UserSession
from tests.conftest import OWNER_EMAIL, login


async def test_protected_routes_require_login(client: httpx.AsyncClient) -> None:
    for method, path in [
        ("GET", "/api/v1/auth/me"),
        ("GET", "/api/v1/stores"),
        ("POST", "/api/v1/stores"),
        ("GET", "/api/v1/audit"),
        ("GET", "/api/v1/connectors"),
    ]:
        response = await client.request(method, path, json={})
        assert response.status_code == 401, path


async def test_proxy_headers_do_not_bypass_auth(client: httpx.AsyncClient) -> None:
    response = await client.get(
        "/api/v1/stores", headers={"X-Forwarded-For": "127.0.0.1", "X-Real-IP": "127.0.0.1"}
    )
    assert response.status_code == 401


async def test_health_is_public(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/v1/health")).json() == {"status": "ok"}
    assert (await client.get("/api/v1/ready")).json() == {"status": "ready"}


async def test_login_sets_httponly_cookie_and_returns_csrf(client, owner) -> None:
    response = await login(client)
    assert response.status_code == 200
    body = response.json()
    assert body["user"]["email"] == OWNER_EMAIL
    assert body["user"]["role"] == "owner"
    assert len(body["csrf_token"]) >= 32
    cookie_header = response.headers["set-cookie"]
    assert "zp_session=" in cookie_header
    assert "HttpOnly" in cookie_header
    assert "SameSite=lax" in cookie_header
    me = await client.get("/api/v1/auth/me")
    assert me.status_code == 200
    assert me.json()["session_id"] == body["session_id"]


async def test_wrong_password_and_unknown_email_fail_the_same_way(client, owner) -> None:
    wrong = await login(client, password="not the password at all")
    unknown = await client.post(
        "/api/v1/auth/login", json={"email": "nobody@example.com", "password": "whatever-123456"}
    )
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()


async def test_failed_login_is_audited_and_emits_event(
    client, owner, session: AsyncSession
) -> None:
    await login(client, password="bad password 123")
    actions = (await session.scalars(select(AuditLog.action))).all()
    assert "auth.login_failed" in actions
    types = (await session.scalars(select(OutboxEvent.type))).all()
    assert types == ["auth.login_failed"]


async def test_login_rate_limit_blocks_even_correct_password(client, owner, platform) -> None:
    limit = platform.settings.login_max_failures_per_email
    for _ in range(limit):
        assert (await login(client, password="wrong password!!")).status_code == 401
    blocked = await login(client)
    assert blocked.status_code == 429


async def test_unsafe_requests_need_csrf_token(auth_client: httpx.AsyncClient) -> None:
    token = auth_client.headers.pop("X-CSRF-Token")
    body = {"name": "Kawai", "slug": "kawai"}
    assert (await auth_client.post("/api/v1/stores", json=body)).status_code == 403
    auth_client.headers["X-CSRF-Token"] = "forged"
    assert (await auth_client.post("/api/v1/stores", json=body)).status_code == 403
    auth_client.headers["X-CSRF-Token"] = token
    assert (await auth_client.post("/api/v1/stores", json=body)).status_code == 201


async def test_logout_revokes_the_session(auth_client: httpx.AsyncClient) -> None:
    cookie = auth_client.cookies.get("zp_session")
    assert (await auth_client.post("/api/v1/auth/logout")).status_code == 204
    auth_client.cookies.set("zp_session", cookie)  # replaying the old cookie must not work
    assert (await auth_client.get("/api/v1/auth/me")).status_code == 401


async def test_log_out_everywhere_else(
    auth_client: httpx.AsyncClient, client: httpx.AsyncClient, platform
) -> None:
    other = httpx.AsyncClient(transport=client._transport, base_url="http://panel.test")
    async with other:
        assert (await login(other)).status_code == 200
        sessions = (await auth_client.get("/api/v1/auth/sessions")).json()
        assert len(sessions) == 2
        assert sum(s["current"] for s in sessions) == 1
        revoked = await auth_client.post("/api/v1/auth/sessions/revoke-others")
        assert revoked.json() == {"revoked": 1}
        assert (await other.get("/api/v1/auth/me")).status_code == 401
        assert (await auth_client.get("/api/v1/auth/me")).status_code == 200


async def test_expired_and_idle_sessions_are_rejected(
    auth_client: httpx.AsyncClient, session: AsyncSession, platform
) -> None:
    await session.execute(update(UserSession).values(expires_at=utcnow() - timedelta(seconds=1)))
    await session.commit()
    assert (await auth_client.get("/api/v1/auth/me")).status_code == 401

    await session.execute(
        update(UserSession).values(
            expires_at=utcnow() + timedelta(days=1),
            last_seen_at=utcnow() - timedelta(days=platform.settings.session_idle_days, seconds=1),
        )
    )
    await session.commit()
    assert (await auth_client.get("/api/v1/auth/me")).status_code == 401


async def test_session_token_is_not_stored_in_clear(
    auth_client: httpx.AsyncClient, session: AsyncSession
) -> None:
    cookie = auth_client.cookies.get("zp_session")
    hashes = (await session.scalars(select(UserSession.token_hash))).all()
    assert cookie not in hashes
    assert all(len(h) == 64 for h in hashes)


def test_short_passwords_are_refused() -> None:
    import pytest

    from app.identity.service import hash_password

    with pytest.raises(ValueError):
        hash_password("short")
