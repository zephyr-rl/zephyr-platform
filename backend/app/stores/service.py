"""Stores, marketplace connections and per-store module switches."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.base import Capability, ConnectorError
from app.core import audit, events
from app.core.crypto import mask
from app.core.db import utcnow
from app.core.modules import Module, default_settings
from app.core.platform import Platform
from app.stores.models import MarketplaceConnection, Store, StoreModule


class StoreError(Exception):
    pass


class NotFound(StoreError):
    pass


class InvalidSettings(StoreError):
    def __init__(self, errors: list[dict[str, Any]]) -> None:
        super().__init__("Invalid module settings")
        self.errors = errors


def _store_snapshot(store: Store) -> dict[str, Any]:
    return {
        "name": store.name,
        "slug": store.slug,
        "timezone": store.timezone,
        "currency": store.currency,
        "is_active": store.is_active,
        "shadow_mode": store.shadow_mode,
    }


async def get_store(session: AsyncSession, store_id: int) -> Store:
    store = await session.get(Store, store_id)
    if store is None:
        raise NotFound(f"Store {store_id} not found")
    return store


async def create_store(
    session: AsyncSession, platform: Platform, actor: audit.Actor, **fields: Any
) -> Store:
    if await session.scalar(select(Store.id).where(Store.slug == fields["slug"])):
        raise StoreError(f"A store with slug {fields['slug']!r} already exists.")
    store = Store(**fields)
    session.add(store)
    await session.flush()
    for module in platform.modules.all():
        session.add(
            StoreModule(
                store_id=store.id,
                module_key=module.key,
                enabled=module.default_enabled,
                settings=default_settings(module),
            )
        )
    audit.record(
        session,
        actor,
        "store.created",
        entity_type="store",
        entity_id=store.id,
        store_id=store.id,
        after=_store_snapshot(store),
    )
    return store


async def update_store(
    session: AsyncSession, actor: audit.Actor, store: Store, changes: dict[str, Any]
) -> Store:
    before = _store_snapshot(store)
    for key, value in changes.items():
        setattr(store, key, value)
    after = _store_snapshot(store)
    if before != after:
        audit.record(
            session,
            actor,
            "store.updated",
            entity_type="store",
            entity_id=store.id,
            store_id=store.id,
            before=before,
            after=after,
        )
        events.publish(
            session, "config.changed", {"entity": "store", "id": store.id}, store_id=store.id
        )
    return store


# ---- marketplace connections -------------------------------------------------------------


def _context(store_id: int, connector_key: str) -> str:
    # Binds each ciphertext to its row: a value copied to another store will not decrypt.
    return f"connection:{store_id}:{connector_key}"


async def add_connection(
    session: AsyncSession,
    platform: Platform,
    actor: audit.Actor,
    store: Store,
    *,
    connector_key: str,
    credentials: dict[str, str],
    label: str = "main",
) -> MarketplaceConnection:
    info = platform.connectors.info(connector_key)
    missing = [f for f in info.credential_fields if not credentials.get(f)]
    if missing:
        raise StoreError(f"Missing credential fields: {', '.join(missing)}")
    connection = MarketplaceConnection(
        store_id=store.id,
        connector_key=connector_key,
        label=label,
        credentials_encrypted=platform.secrets.encrypt_json(
            credentials, context=_context(store.id, connector_key)
        ),
        credentials_hint={k: mask(str(v)) for k, v in credentials.items()},
    )
    session.add(connection)
    await session.flush()
    audit.record(
        session,
        actor,
        "connection.created",
        entity_type="connection",
        entity_id=connection.id,
        store_id=store.id,
        after={"connector": connector_key, "label": label},
    )
    return connection


def connection_credentials(platform: Platform, connection: MarketplaceConnection) -> dict[str, Any]:
    value = platform.secrets.decrypt_json(
        connection.credentials_encrypted,
        context=_context(connection.store_id, connection.connector_key),
    )
    assert isinstance(value, dict)
    return value


async def check_connection(
    session: AsyncSession, platform: Platform, connection: MarketplaceConnection
) -> MarketplaceConnection:
    connector = platform.connectors.create(
        connection.connector_key, connection_credentials(platform, connection)
    )
    try:
        status = await connector.test_credentials()
        connection.status = "connected" if status.ok else "invalid"
        connection.status_detail = status.detail or status.account_name
    except ConnectorError as exc:
        connection.status, connection.status_detail = "error", str(exc)
    finally:
        await connector.aclose()
    connection.last_checked_at = utcnow()
    return connection


async def store_capabilities(
    session: AsyncSession, platform: Platform, store_id: int
) -> frozenset[Capability]:
    keys = await session.scalars(
        select(MarketplaceConnection.connector_key).where(
            MarketplaceConnection.store_id == store_id
        )
    )
    caps: set[Capability] = set()
    for key in set(keys):
        caps |= platform.connectors.info(key).capabilities
    return frozenset(caps)


# ---- modules -----------------------------------------------------------------------------


@dataclass(frozen=True)
class ModuleState:
    module: Module
    row: StoreModule
    available: bool
    missing: frozenset[Capability]


async def _module_row(
    session: AsyncSession, platform: Platform, store_id: int, key: str
) -> StoreModule:
    module = platform.modules.get(key)
    row = await session.scalar(
        select(StoreModule).where(StoreModule.store_id == store_id, StoreModule.module_key == key)
    )
    if row is None:  # module added after the store was created
        row = StoreModule(
            store_id=store_id,
            module_key=key,
            enabled=False,
            settings=default_settings(module),
            version=1,
        )
        session.add(row)
        await session.flush()
    return row


async def list_modules(
    session: AsyncSession, platform: Platform, store_id: int
) -> list[ModuleState]:
    caps = await store_capabilities(session, platform, store_id)
    states = []
    for module in platform.modules.all():
        row = await _module_row(session, platform, store_id, module.key)
        missing = frozenset(module.requires - caps)
        states.append(ModuleState(module, row, available=not missing, missing=missing))
    return states


async def set_module(
    session: AsyncSession,
    platform: Platform,
    actor: audit.Actor,
    store_id: int,
    key: str,
    *,
    enabled: bool | None = None,
    settings: dict[str, Any] | None = None,
    expected_version: int | None = None,
) -> StoreModule:
    try:
        module = platform.modules.get(key)
    except KeyError as exc:
        raise NotFound(str(exc)) from None
    row = await _module_row(session, platform, store_id, key)
    await session.refresh(row, with_for_update=True)
    if expected_version is not None and expected_version != row.version:
        raise StoreError("These settings were changed elsewhere. Reload and try again.")
    if enabled is False and module.always_on:
        raise StoreError(
            f"{module.name} is always on. To hold automatic deliveries, use the "
            "“Automatic deliveries” switch in Listings."
        )

    before = {"enabled": row.enabled, "settings": row.settings, "version": row.version}
    if settings is not None:
        try:
            row.settings = module.settings_schema.model_validate(settings).model_dump(mode="json")
        except ValidationError as exc:
            raise InvalidSettings(
                [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()]
            ) from None
    if enabled is not None:
        if enabled and not row.enabled:
            caps = await store_capabilities(session, platform, store_id)
            missing = module.requires - caps
            if missing:
                raise StoreError(
                    f"{module.name} needs a marketplace connection with: "
                    + ", ".join(sorted(c.value for c in missing))
                )
        row.enabled = enabled

    after_cmp = {"enabled": row.enabled, "settings": row.settings}
    if after_cmp != {"enabled": before["enabled"], "settings": before["settings"]}:
        row.version += 1
        audit.record(
            session,
            actor,
            "module.updated",
            entity_type="store_module",
            entity_id=f"{store_id}:{key}",
            store_id=store_id,
            before=before,
            after={**after_cmp, "version": row.version},
        )
        events.publish(
            session,
            "config.changed",
            {"entity": "module", "module": key, "enabled": row.enabled, "version": row.version},
            store_id=store_id,
        )
    return row
