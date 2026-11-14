"""Test fixtures. Tests run against a real PostgreSQL database (``ZP_TEST_DATABASE_URL``)."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.audit import Actor
from app.core.config import Settings
from app.core.crypto import generate_key
from app.core.platform import Platform
from app.identity import service as identity
from app.identity.models import User
from app.main import create_app

TEST_DB_URL = os.environ.get(
    "ZP_TEST_DATABASE_URL", "postgresql+asyncpg://postgres@127.0.0.1:5432/zephyr_test"
)
BACKEND = Path(__file__).resolve().parents[1]
OWNER_EMAIL = "owner@example.com"
OWNER_PASSWORD = "correct horse battery staple"


async def _reset_schema() -> None:
    engine = create_async_engine(TEST_DB_URL)
    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
    await engine.dispose()


@pytest.fixture(scope="session", autouse=True)
def migrated_database() -> None:
    """Build the schema with the real migrations, so they are tested on every run."""
    asyncio.run(_reset_schema())
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "migrations"))
    cfg.attributes["database_url"] = TEST_DB_URL
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "head")


@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings(
        env="test",
        database_url=TEST_DB_URL,
        secret_keys=SecretStr(generate_key()),
        fingerprint_key=SecretStr(generate_key()),
        cookie_secure=False,
        log_level="WARNING",
    )


@pytest.fixture
async def app_platform(settings: Settings) -> AsyncIterator[tuple[object, Platform]]:
    app = create_app(settings)
    platform: Platform = app.state.platform
    async with platform.db.engine.begin() as conn:
        # Triggers (append-only audit log) are disabled only for this cleanup statement.
        await conn.execute(text("SET LOCAL session_replication_role = replica"))
        tables = ", ".join(
            f'"{t}"'
            for t in (
                "vault_uploads",
                "fulfilment_events",
                "reservations",
                "fulfilments",
                "orders",
                "listings",
                "stock_item_events",
                "stock_item_secrets",
                "stock_items",
                "stock_extractions",
                "stock_imports",
                "stock_pools",
                "audit_log",
                "outbox_events",
                "jobs",
                "login_attempts",
                "sessions",
                "store_modules",
                "marketplace_connections",
                "stores",
                "users",
            )
        )
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    yield app, platform
    await platform.db.dispose()


@pytest.fixture
def platform(app_platform: tuple[object, Platform]) -> Platform:
    return app_platform[1]


@pytest.fixture
async def session(platform: Platform) -> AsyncIterator[AsyncSession]:
    async with platform.db.sessionmaker() as s:
        yield s


@pytest.fixture
async def client(app_platform: tuple[object, Platform]) -> AsyncIterator[httpx.AsyncClient]:
    app = app_platform[0]
    transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=transport, base_url="http://panel.test") as c:
        yield c


@pytest.fixture
async def owner(session: AsyncSession) -> User:
    user = await identity.create_user(
        session,
        email=OWNER_EMAIL,
        password=OWNER_PASSWORD,
        display_name="Owner",
        actor=Actor.system("tests"),
    )
    await session.commit()
    return user


async def login(client: httpx.AsyncClient, password: str = OWNER_PASSWORD) -> httpx.Response:
    response = await client.post(
        "/api/v1/auth/login", json={"email": OWNER_EMAIL, "password": password}
    )
    if response.status_code == 200:
        client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    return response


@pytest.fixture
async def auth_client(client: httpx.AsyncClient, owner: User) -> httpx.AsyncClient:
    response = await login(client)
    assert response.status_code == 200, response.text
    return client


@pytest.fixture
def anyio_backend() -> Iterator[str]:
    yield "asyncio"
