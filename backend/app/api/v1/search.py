"""Global search, the activity feed of the Overview, and the list of database backups."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import or_, select

from app.api.deps import AuthDep, OwnerDep, PlatformDep, SessionDep
from app.core.audit import AuditLog
from app.core.config import get_settings
from app.core.db import utcnow
from app.orders.models import Fulfilment, Order
from app.stock.importer import normalise_login
from app.stock.models import StockItem, StockPool
from app.stores.service import NotFound as StoreNotFound
from app.stores.service import get_store

router = APIRouter(tags=["search"])


class OrderHit(BaseModel):
    id: int
    remote_id: str
    buyer_name: str | None
    offer_title: str | None
    state: str
    fulfilment_state: str | None
    placed_at: datetime


class ItemHit(BaseModel):
    id: int
    pool_id: int
    pool: str
    login_hint: str
    state: str


class SearchOut(BaseModel):
    orders: list[OrderHit]
    items: list[ItemHit]


def _like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@router.get("/stores/{store_id}/search", response_model=SearchOut)
async def search(
    store_id: int,
    auth: AuthDep,
    platform: PlatformDep,
    session: SessionDep,
    q: str = Query(min_length=2, max_length=200),
) -> SearchOut:
    """Orders by id or buyer; stock accounts by their exact login (logins are only stored as a
    keyed fingerprint, so a partial login cannot be searched)."""
    try:
        await get_store(session, store_id)
    except StoreNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from None
    text = q.strip()
    pattern = _like(text)
    order_rows = await session.execute(
        select(Order, Fulfilment.state)
        .outerjoin(Fulfilment, Fulfilment.order_id == Order.id)
        .where(
            Order.store_id == store_id,
            or_(
                Order.remote_id.ilike(f"{pattern}%", escape="\\"),
                Order.buyer_name.ilike(f"%{pattern}%", escape="\\"),
            ),
        )
        .order_by(Order.remote_created_at.desc())
        .limit(10)
    )
    orders = [
        OrderHit(
            id=o.id,
            remote_id=o.remote_id,
            buyer_name=o.buyer_name,
            offer_title=o.offer_title,
            state=o.state,
            fulfilment_state=f.value if f else None,
            placed_at=o.remote_created_at,
        )
        for o, f in order_rows
    ]
    items: list[ItemHit] = []
    login = text.split(":", 1)[0].strip()  # "login:password" pasted whole
    if login and platform.fingerprints.configured:
        fp = platform.fingerprints(normalise_login(login))
        rows = await session.execute(
            select(StockItem, StockPool.name)
            .join(StockPool, StockPool.id == StockItem.pool_id)
            .where(StockItem.store_id == store_id, StockItem.fingerprint == fp)
        )
        items = [
            ItemHit(
                id=i.id, pool_id=i.pool_id, pool=name, login_hint=i.login_hint, state=i.state.value
            )
            for i, name in rows
        ]
    return SearchOut(orders=orders, items=items)


class ActivityOut(BaseModel):
    id: int
    at: datetime
    actor: str
    action: str
    entity_type: str | None
    entity_id: str | None
    summary: dict[str, Any] | None


@router.get("/stores/{store_id}/activity", response_model=list[ActivityOut])
async def activity(
    store_id: int, auth: OwnerDep, session: SessionDep, limit: int = Query(15, ge=1, le=50)
) -> list[ActivityOut]:
    """The latest recorded actions of a store, for the Overview."""
    rows = await session.scalars(
        select(AuditLog)
        .where(AuditLog.store_id == store_id)
        .order_by(AuditLog.id.desc())
        .limit(limit)
    )
    return [
        ActivityOut(
            id=a.id,
            at=a.at,
            actor=a.actor_label,
            action=a.action,
            entity_type=a.entity_type,
            entity_id=a.entity_id,
            summary=a.after if isinstance(a.after, dict) else None,
        )
        for a in rows
    ]


class BackupFile(BaseModel):
    name: str
    size: int
    created_at: datetime


class BackupsOut(BaseModel):
    folder: str
    available: bool
    files: list[BackupFile]
    stale: bool  # no backup in the last 36 hours


def _list_backups(folder: Path) -> list[BackupFile]:
    files = []
    for path in folder.glob("zephyr-*.dump"):
        stat = path.stat()
        files.append(
            BackupFile(
                name=path.name,
                size=stat.st_size,
                created_at=datetime.fromtimestamp(stat.st_mtime).astimezone(),
            )
        )
    return sorted(files, key=lambda f: f.created_at, reverse=True)


@router.get("/backups", response_model=BackupsOut)
async def backups(auth: OwnerDep) -> BackupsOut:
    folder = Path(get_settings().backup_dir)
    available = await asyncio.to_thread(folder.is_dir)
    files = await asyncio.to_thread(_list_backups, folder) if available else []
    newest = files[0].created_at if files else None
    return BackupsOut(
        folder=str(folder),
        available=available,
        files=files[:30],
        stale=newest is None or utcnow() - newest > timedelta(hours=36),
    )
