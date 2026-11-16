from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select

from app.api.deps import AuthDep, OwnerDep, PlatformDep, SessionDep
from app.stores import service
from app.stores.models import MarketplaceConnection, Store

router = APIRouter(prefix="/stores", tags=["stores"])


def _valid_timezone(value: str) -> str:
    # Accepts canonical names and their aliases (browsers report e.g. America/Buenos_Aires).
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("Unknown time zone") from None
    return value


class StoreIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    slug: str = Field(min_length=1, max_length=60, pattern=r"^[a-z0-9][a-z0-9-]*$")
    timezone: str = "UTC"
    currency: str = Field("USD", pattern=r"^[A-Z]{3}$")

    @field_validator("timezone")
    @classmethod
    def _tz(cls, value: str) -> str:
        return _valid_timezone(value)


class StorePatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=120)
    timezone: str | None = None
    currency: str | None = Field(None, pattern=r"^[A-Z]{3}$")
    is_active: bool | None = None
    shadow_mode: bool | None = None

    @field_validator("timezone")
    @classmethod
    def _tz(cls, value: str | None) -> str | None:
        return None if value is None else _valid_timezone(value)


class StoreOut(BaseModel):
    id: int
    name: str
    slug: str
    timezone: str
    currency: str
    is_active: bool
    shadow_mode: bool


class ConnectionIn(BaseModel):
    connector: str
    label: str = Field("main", min_length=1, max_length=80)
    credentials: dict[str, str]


class ConnectionOut(BaseModel):
    id: int
    connector: str
    connector_name: str
    label: str
    credentials: dict[str, str]  # masked hints only, never the secrets
    status: str
    status_detail: str | None
    last_checked_at: datetime | None


class ModuleOut(BaseModel):
    key: str
    name: str
    description: str
    enabled: bool
    available: bool
    missing_capabilities: list[str]
    settings: dict[str, Any]
    settings_schema: dict[str, Any]
    version: int
    always_on: bool
    managed_in: str | None


class ModulePut(BaseModel):
    enabled: bool | None = None
    settings: dict[str, Any] | None = None
    expected_version: int | None = None


def _store_out(store: Store) -> StoreOut:
    return StoreOut(
        id=store.id,
        name=store.name,
        slug=store.slug,
        timezone=store.timezone,
        currency=store.currency,
        is_active=store.is_active,
        shadow_mode=store.shadow_mode,
    )


def _connection_out(platform: Any, c: MarketplaceConnection) -> ConnectionOut:
    return ConnectionOut(
        id=c.id,
        connector=c.connector_key,
        connector_name=platform.connectors.info(c.connector_key).name,
        label=c.label,
        credentials=c.credentials_hint,
        status=c.status,
        status_detail=c.status_detail,
        last_checked_at=c.last_checked_at,
    )


async def _store_or_404(session: Any, store_id: int) -> Store:
    try:
        return await service.get_store(session, store_id)
    except service.NotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from None


@router.get("", response_model=list[StoreOut])
async def list_stores(auth: AuthDep, session: SessionDep) -> list[StoreOut]:
    rows = await session.scalars(select(Store).order_by(Store.name))
    return [_store_out(s) for s in rows]


@router.post("", response_model=StoreOut, status_code=status.HTTP_201_CREATED)
async def create_store(
    body: StoreIn, auth: OwnerDep, platform: PlatformDep, session: SessionDep
) -> StoreOut:
    try:
        store = await service.create_store(session, platform, auth.actor, **body.model_dump())
    except service.StoreError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    await session.commit()
    return _store_out(store)


@router.patch("/{store_id}", response_model=StoreOut)
async def update_store(
    store_id: int, body: StorePatch, auth: OwnerDep, session: SessionDep
) -> StoreOut:
    store = await _store_or_404(session, store_id)
    await service.update_store(session, auth.actor, store, body.model_dump(exclude_none=True))
    await session.commit()
    return _store_out(store)


@router.get("/{store_id}/connections", response_model=list[ConnectionOut])
async def list_connections(
    store_id: int, auth: AuthDep, platform: PlatformDep, session: SessionDep
) -> list[ConnectionOut]:
    await _store_or_404(session, store_id)
    rows = await session.scalars(
        select(MarketplaceConnection).where(MarketplaceConnection.store_id == store_id)
    )
    return [_connection_out(platform, c) for c in rows]


@router.post(
    "/{store_id}/connections", response_model=ConnectionOut, status_code=status.HTTP_201_CREATED
)
async def add_connection(
    store_id: int, body: ConnectionIn, auth: OwnerDep, platform: PlatformDep, session: SessionDep
) -> ConnectionOut:
    store = await _store_or_404(session, store_id)
    try:
        connection = await service.add_connection(
            session,
            platform,
            auth.actor,
            store,
            connector_key=body.connector,
            credentials=body.credentials,
            label=body.label,
        )
    except KeyError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from None
    except service.StoreError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from None
    await service.check_connection(session, platform, connection)
    await session.commit()
    return _connection_out(platform, connection)


@router.post("/{store_id}/connections/{connection_id}/check", response_model=ConnectionOut)
async def check_connection(
    store_id: int, connection_id: int, auth: OwnerDep, platform: PlatformDep, session: SessionDep
) -> ConnectionOut:
    connection = await session.get(MarketplaceConnection, connection_id)
    if connection is None or connection.store_id != store_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Connection not found.")
    await service.check_connection(session, platform, connection)
    await session.commit()
    return _connection_out(platform, connection)


def _module_out(state: service.ModuleState) -> ModuleOut:
    return ModuleOut(
        key=state.module.key,
        name=state.module.name,
        description=state.module.description,
        enabled=state.row.enabled,
        available=state.available,
        missing_capabilities=sorted(c.value for c in state.missing),
        settings=state.row.settings,
        settings_schema=state.module.settings_schema.model_json_schema(),
        version=state.row.version,
        always_on=state.module.always_on,
        managed_in=state.module.managed_in,
    )


@router.get("/{store_id}/modules", response_model=list[ModuleOut])
async def list_modules(
    store_id: int, auth: AuthDep, platform: PlatformDep, session: SessionDep
) -> list[ModuleOut]:
    await _store_or_404(session, store_id)
    states = await service.list_modules(session, platform, store_id)
    await session.commit()  # persists rows for modules added after the store was created
    return [_module_out(s) for s in states]


@router.put("/{store_id}/modules/{module_key}", response_model=ModuleOut)
async def set_module(
    store_id: int,
    module_key: str,
    body: ModulePut,
    auth: OwnerDep,
    platform: PlatformDep,
    session: SessionDep,
) -> ModuleOut:
    await _store_or_404(session, store_id)
    try:
        await service.set_module(
            session,
            platform,
            auth.actor,
            store_id,
            module_key,
            enabled=body.enabled,
            settings=body.settings,
            expected_version=body.expected_version,
        )
    except service.NotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from None
    except service.InvalidSettings as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, exc.errors) from None
    except service.StoreError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    await session.commit()
    states = await service.list_modules(session, platform, store_id)
    return _module_out(next(s for s in states if s.module.key == module_key))
