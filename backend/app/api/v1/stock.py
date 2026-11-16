from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthDep, OwnerDep, PlatformDep, SessionDep
from app.core import audit
from app.core.crypto import CryptoError
from app.stock import service
from app.stock.importer import parse
from app.stock.models import ItemState, ItemType, StockExtraction, StockItem, StockPool
from app.stores.service import NotFound as StoreNotFound
from app.stores.service import get_store

router = APIRouter(prefix="/stores/{store_id}/stock", tags=["stock"])


# ---- schemas -----------------------------------------------------------------------------


class PoolIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    game: str = Field("Rocket League", min_length=1, max_length=80)
    item_type: ItemType = ItemType.account
    low_stock_threshold: int = Field(5, ge=0, le=100_000)


class PoolPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=120)
    game: str | None = Field(None, min_length=1, max_length=80)
    low_stock_threshold: int | None = Field(None, ge=0, le=100_000)
    archived: bool | None = None


class PoolOut(BaseModel):
    id: int
    name: str
    game: str
    item_type: str
    low_stock_threshold: int
    archived: bool
    counts: dict[str, int]
    low: bool
    tokens: int  # available accounts with a token


class ItemOut(BaseModel):
    id: int
    pool_id: int
    login_hint: str
    state: str
    position: int
    has_token: bool
    note: str | None
    created_at: datetime
    state_changed_at: datetime


class AccountOut(ItemOut):
    """An item with its credentials (the pool view shows them, owner's choice)."""

    login: str
    password: str
    token: str | None
    extra: dict[str, str]
    last_event: str | None


class ItemsPage(BaseModel):
    items: list[ItemOut]
    total: int
    next_after: int | None


class AccountsPage(BaseModel):
    items: list[AccountOut]
    total: int
    offset: int
    limit: int


class ItemIdsIn(BaseModel):
    item_ids: list[int] = Field(min_length=1, max_length=5000)
    reason: str | None = Field(None, max_length=500)


class ExportOut(BaseModel):
    text: str
    count: int


class ConvertIn(BaseModel):
    text: str = Field(min_length=1, max_length=service.MAX_IMPORT_CHARS)
    with_token: bool = False


class ConvertOut(BaseModel):
    text: str
    count: int
    invalid_lines: list[int]
    detected_format: str


class ImportIn(BaseModel):
    text: str = Field(min_length=1, max_length=service.MAX_IMPORT_CHARS)


class ImportCommitIn(ImportIn):
    expected_sha256: str = Field(min_length=64, max_length=64)
    # Accounts taken out earlier in this panel come back into this pool.
    put_back: bool = False


class PreviewLineOut(BaseModel):
    line: int
    login_hint: str
    status: str
    format: str
    has_token: bool
    token_will_attach: bool
    where: str | None = None


class PreviewOut(BaseModel):
    text_sha256: str
    detected_format: str
    counts: dict[str, int]
    lines: list[PreviewLineOut]
    invalid_lines: list[int]
    ignored_lines: list[int]


class ImportOut(BaseModel):
    id: int
    counts: dict[str, int]


class ExtractIn(BaseModel):
    count: int = Field(ge=1, le=service.MAX_EXTRACT)
    reason: str | None = Field(None, max_length=500)


class RevealedOut(BaseModel):
    id: int
    login: str
    password: str
    token: str | None
    extra: dict[str, str]


class ExtractionOut(BaseModel):
    id: int
    pool_id: int
    pool_name: str
    actor: str
    created_at: datetime
    count: int
    reason: str | None
    undone_at: datetime | None


class ExtractionDetail(ExtractionOut):
    items: list[RevealedOut]
    text: str
    tokens_text: str


class StateIn(BaseModel):
    item_ids: list[int] = Field(min_length=1, max_length=5000)
    state: ItemState
    reason: str | None = Field(None, max_length=500)


class MoveIn(BaseModel):
    item_ids: list[int] = Field(min_length=1, max_length=5000)
    target_pool_id: int
    target_store_id: int | None = None


class EventOut(BaseModel):
    at: datetime
    actor: str
    event: str
    from_state: str | None
    to_state: str | None
    details: dict[str, Any] | None


# ---- helpers -----------------------------------------------------------------------------


async def _store(session: AsyncSession, store_id: int) -> None:
    try:
        await get_store(session, store_id)
    except StoreNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from None


def _fail(exc: Exception) -> HTTPException:
    if isinstance(exc, service.NotFound):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    if isinstance(exc, CryptoError):
        return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc))
    return HTTPException(status.HTTP_409_CONFLICT, str(exc))


def _pool_out(pool: StockPool, counts: dict[str, int] | None, tokens: int = 0) -> PoolOut:
    counts = counts or {s.value: 0 for s in ItemState}
    return PoolOut(
        id=pool.id,
        name=pool.name,
        game=pool.game,
        item_type=pool.item_type.value,
        low_stock_threshold=pool.low_stock_threshold,
        archived=pool.archived,
        counts=counts,
        low=counts.get("available", 0) <= pool.low_stock_threshold,
        tokens=tokens,
    )


async def _token_counts(session: AsyncSession, store_id: int) -> dict[int, int]:
    rows = await session.execute(
        select(StockItem.pool_id, func.count())
        .where(
            StockItem.store_id == store_id,
            StockItem.state == ItemState.available,
            StockItem.has_token.is_(True),
        )
        .group_by(StockItem.pool_id)
    )
    return {pid: n for pid, n in rows}


def _item_out(i: StockItem) -> ItemOut:
    return ItemOut(
        id=i.id,
        pool_id=i.pool_id,
        login_hint=i.login_hint,
        state=i.state.value,
        position=i.position,
        has_token=i.has_token,
        note=i.note,
        created_at=i.created_at,
        state_changed_at=i.state_changed_at,
    )


def _extraction_out(e: StockExtraction, pool_name: str) -> ExtractionOut:
    return ExtractionOut(
        id=e.id,
        pool_id=e.pool_id,
        pool_name=pool_name,
        actor=e.actor_label,
        created_at=e.created_at,
        count=e.count,
        reason=e.reason,
        undone_at=e.undone_at,
    )


def _detail(
    e: StockExtraction, pool_name: str, items: list[service.RevealedItem]
) -> ExtractionDetail:
    return ExtractionDetail(
        **_extraction_out(e, pool_name).model_dump(),
        items=[
            RevealedOut(id=r.id, login=r.login, password=r.password, token=r.token, extra=r.extra)
            for r in items
        ],
        text="\n".join(r.line() for r in items),
        tokens_text="\n".join(r.token for r in items if r.token),
    )


# ---- pools -------------------------------------------------------------------------------


@router.get("/pools", response_model=list[PoolOut])
async def list_pools(store_id: int, auth: AuthDep, session: SessionDep) -> list[PoolOut]:
    await _store(session, store_id)
    pools = await session.scalars(
        select(StockPool)
        .where(StockPool.store_id == store_id)
        .order_by(StockPool.archived, StockPool.name)
    )
    counts = await service.pool_counts(session, store_id)
    tokens = await _token_counts(session, store_id)
    return [_pool_out(p, counts.get(p.id), tokens.get(p.id, 0)) for p in pools]


@router.post("/pools", response_model=PoolOut, status_code=status.HTTP_201_CREATED)
async def create_pool(store_id: int, body: PoolIn, auth: OwnerDep, session: SessionDep) -> PoolOut:
    await _store(session, store_id)
    try:
        pool = await service.create_pool(session, auth.actor, store_id, **body.model_dump())
    except service.StockError as exc:
        raise _fail(exc) from None
    await session.commit()
    return _pool_out(pool, None)


@router.patch("/pools/{pool_id}", response_model=PoolOut)
async def update_pool(
    store_id: int, pool_id: int, body: PoolPatch, auth: OwnerDep, session: SessionDep
) -> PoolOut:
    try:
        pool = await service.get_pool(session, store_id, pool_id)
        await service.update_pool(session, auth.actor, pool, body.model_dump(exclude_none=True))
    except service.StockError as exc:
        raise _fail(exc) from None
    await session.commit()
    counts = await service.pool_counts(session, store_id)
    tokens = await _token_counts(session, store_id)
    return _pool_out(pool, counts.get(pool.id), tokens.get(pool.id, 0))


@router.get("/pools/{pool_id}/items", response_model=ItemsPage)
async def list_items(
    store_id: int,
    pool_id: int,
    auth: AuthDep,
    session: SessionDep,
    state: ItemState | None = None,
    after: int | None = Query(None, description="Position of the last item of the previous page"),
    limit: int = Query(100, ge=1, le=500),
) -> ItemsPage:
    try:
        await service.get_pool(session, store_id, pool_id)
    except service.StockError as exc:
        raise _fail(exc) from None
    base = select(StockItem).where(StockItem.pool_id == pool_id)
    if state is not None:
        base = base.where(StockItem.state == state)
    total = await session.scalar(select(func.count()).select_from(base.subquery()))
    page = base.order_by(StockItem.position, StockItem.id).limit(limit + 1)
    if after is not None:
        page = page.where(StockItem.position > after)
    rows = list((await session.scalars(page)).all())
    more = len(rows) > limit
    rows = rows[:limit]
    return ItemsPage(
        items=[_item_out(i) for i in rows],
        total=int(total or 0),
        next_after=rows[-1].position if more and rows else None,
    )


@router.get("/pools/{pool_id}/accounts", response_model=AccountsPage)
async def list_accounts(
    store_id: int,
    pool_id: int,
    auth: OwnerDep,
    platform: PlatformDep,
    session: SessionDep,
    state: ItemState | Literal["all"] = ItemState.available,
    q: str | None = Query(None, max_length=200, description="Words in the login, e-mail or token"),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
) -> AccountsPage:
    """The pool's accounts with their credentials, in delivery order."""
    try:
        pool = await service.get_pool(session, store_id, pool_id)
    except service.StockError as exc:
        raise _fail(exc) from None
    base = select(StockItem).where(StockItem.pool_id == pool_id)
    if state != "all":
        base = base.where(StockItem.state == state)
    ordered = base.order_by(StockItem.position, StockItem.id)
    words = [w for w in (q or "").lower().split() if w]
    try:
        if words:
            # Credentials are stored encrypted: a text search reads the whole pool.
            everything = list((await session.scalars(ordered)).all())
            revealed = await service.reveal(session, platform, everything)
            hits = [
                (i, r)
                for i, r in zip(everything, revealed, strict=True)
                if service.matches(r, words)
            ]
            total = len(hits)
            page = hits[offset : offset + limit]
        else:
            total = int(
                await session.scalar(select(func.count()).select_from(base.subquery())) or 0
            )
            rows = list((await session.scalars(ordered.offset(offset).limit(limit))).all())
            page = list(zip(rows, await service.reveal(session, platform, rows), strict=True))
    except CryptoError as exc:
        raise _fail(exc) from None
    last = await service.last_events(session, [i.id for i, _ in page])
    audit.record(
        session,
        auth.actor,
        "stock.viewed",
        entity_type="stock_pool",
        entity_id=pool.id,
        store_id=store_id,
        after={"count": len(page), "offset": offset, "search": bool(words)},
    )
    await session.commit()
    return AccountsPage(
        items=[
            AccountOut(
                **_item_out(i).model_dump(),
                login=r.login,
                password=r.password,
                token=r.token,
                extra=r.extra,
                last_event=last.get(i.id),
            )
            for i, r in page
        ],
        total=total,
        offset=offset,
        limit=limit,
    )


@router.get("/pools/{pool_id}/export", response_model=ExportOut)
async def export_pool(
    store_id: int,
    pool_id: int,
    auth: OwnerDep,
    platform: PlatformDep,
    session: SessionDep,
    format: str = Query("login_password", pattern="^(login_password|login_password_token|tokens)$"),
) -> ExportOut:
    """Copy all / Download .txt / Copy tokens. The accounts stay in stock."""
    try:
        pool = await service.get_pool(session, store_id, pool_id)
        text, count = await service.export_pool(session, platform, auth.actor, pool, format)
    except (service.StockError, CryptoError) as exc:
        raise _fail(exc) from None
    await session.commit()
    return ExportOut(text=text, count=count)


@router.delete("/pools/{pool_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_pool(store_id: int, pool_id: int, auth: OwnerDep, session: SessionDep) -> None:
    try:
        pool = await service.get_pool(session, store_id, pool_id)
        await service.delete_pool(session, auth.actor, pool)
        await session.flush()
    except service.StockError as exc:
        raise _fail(exc) from None
    except IntegrityError:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Listings still use this pool: change them first."
        ) from None
    await session.commit()


@router.post("/items/remove")
async def remove_items(
    store_id: int, body: ItemIdsIn, auth: OwnerDep, session: SessionDep
) -> dict[str, int]:
    try:
        n = await service.remove_items(
            session, auth.actor, store_id, body.item_ids, reason=body.reason
        )
    except service.StockError as exc:
        raise _fail(exc) from None
    await session.commit()
    return {"removed": n}


@router.post("/items/put-back")
async def put_back(
    store_id: int, body: ItemIdsIn, auth: OwnerDep, session: SessionDep
) -> dict[str, int]:
    try:
        n = await service.put_back(session, auth.actor, store_id, body.item_ids)
    except service.StockError as exc:
        raise _fail(exc) from None
    await session.commit()
    return {"put_back": n}


@router.post("/convert", response_model=ConvertOut)
async def convert(store_id: int, body: ConvertIn, auth: OwnerDep) -> ConvertOut:
    """Structured formatter: any supported format in, login:password[:token] lines out."""
    parsed = parse(body.text)
    lines = [
        f"{a.login}:{a.password}:{a.token}"
        if body.with_token and a.token
        else f"{a.login}:{a.password}"
        for a in parsed.accounts
    ]
    return ConvertOut(
        text="\n".join(lines),
        count=len(lines),
        invalid_lines=parsed.invalid_lines,
        detected_format=parsed.detected_format,
    )


# ---- import ------------------------------------------------------------------------------


@router.post("/pools/{pool_id}/import/preview", response_model=PreviewOut)
async def preview_import(
    store_id: int,
    pool_id: int,
    body: ImportIn,
    auth: OwnerDep,
    platform: PlatformDep,
    session: SessionDep,
) -> PreviewOut:
    try:
        preview = await service.preview_import(session, platform, store_id, pool_id, body.text)
    except (service.StockError, CryptoError) as exc:
        raise _fail(exc) from None
    return PreviewOut(
        text_sha256=preview.text_sha256,
        detected_format=preview.detected_format,
        counts=preview.counts,
        invalid_lines=preview.invalid_lines,
        ignored_lines=preview.ignored_lines,
        lines=[PreviewLineOut(**line.__dict__) for line in preview.lines],
    )


@router.post(
    "/pools/{pool_id}/import", response_model=ImportOut, status_code=status.HTTP_201_CREATED
)
async def commit_import(
    store_id: int,
    pool_id: int,
    body: ImportCommitIn,
    auth: OwnerDep,
    platform: PlatformDep,
    session: SessionDep,
) -> ImportOut:
    try:
        record, _ = await service.commit_import(
            session,
            platform,
            auth.actor,
            store_id,
            pool_id,
            body.text,
            expected_sha256=body.expected_sha256,
            put_back=body.put_back,
        )
    except (service.StockError, CryptoError) as exc:
        raise _fail(exc) from None
    await session.commit()
    return ImportOut(id=record.id, counts=record.counts)


# ---- extraction --------------------------------------------------------------------------


@router.post(
    "/pools/{pool_id}/extract", response_model=ExtractionDetail, status_code=status.HTTP_201_CREATED
)
async def extract(
    store_id: int,
    pool_id: int,
    body: ExtractIn,
    auth: OwnerDep,
    platform: PlatformDep,
    session: SessionDep,
) -> ExtractionDetail:
    try:
        extraction, items = await service.extract(
            session, platform, auth.actor, store_id, pool_id, body.count, reason=body.reason
        )
        pool = await service.get_pool(session, store_id, pool_id)
    except (service.StockError, CryptoError) as exc:
        raise _fail(exc) from None
    await session.commit()
    return _detail(extraction, pool.name, items)


@router.get("/extractions", response_model=list[ExtractionOut])
async def list_extractions(
    store_id: int, auth: AuthDep, session: SessionDep, limit: int = Query(50, ge=1, le=200)
) -> list[ExtractionOut]:
    rows = await session.execute(
        select(StockExtraction, StockPool.name)
        .join(StockPool, StockPool.id == StockExtraction.pool_id)
        .where(StockExtraction.store_id == store_id)
        .order_by(StockExtraction.id.desc())
        .limit(limit)
    )
    return [_extraction_out(e, name) for e, name in rows]


@router.get("/extractions/{extraction_id}", response_model=ExtractionDetail)
async def view_extraction(
    store_id: int, extraction_id: int, auth: OwnerDep, platform: PlatformDep, session: SessionDep
) -> ExtractionDetail:
    try:
        extraction, items = await service.view_extraction(
            session, platform, auth.actor, store_id, extraction_id
        )
    except (service.StockError, CryptoError) as exc:
        raise _fail(exc) from None
    pool = await session.get(StockPool, extraction.pool_id)
    await session.commit()
    return _detail(extraction, pool.name if pool else "", items)


@router.post("/extractions/{extraction_id}/undo", response_model=ExtractionOut)
async def undo_extraction(
    store_id: int, extraction_id: int, auth: OwnerDep, session: SessionDep
) -> ExtractionOut:
    try:
        extraction = await service.undo_extraction(session, auth.actor, store_id, extraction_id)
    except service.StockError as exc:
        raise _fail(exc) from None
    pool = await session.get(StockPool, extraction.pool_id)
    await session.commit()
    return _extraction_out(extraction, pool.name if pool else "")


# ---- items -------------------------------------------------------------------------------


@router.post("/items/state")
async def change_state(
    store_id: int, body: StateIn, auth: OwnerDep, session: SessionDep
) -> dict[str, int]:
    try:
        changed = await service.change_state(
            session, auth.actor, store_id, body.item_ids, body.state, reason=body.reason
        )
    except service.StockError as exc:
        raise _fail(exc) from None
    await session.commit()
    return {"changed": changed}


@router.post("/items/move")
async def move_items(
    store_id: int, body: MoveIn, auth: OwnerDep, platform: PlatformDep, session: SessionDep
) -> dict[str, int]:
    try:
        result = await service.move_items(
            session,
            platform,
            auth.actor,
            store_id,
            body.item_ids,
            body.target_pool_id,
            target_store_id=body.target_store_id,
        )
    except (service.StockError, CryptoError) as exc:
        raise _fail(exc) from None
    await session.commit()
    return result


@router.get("/search", response_model=ItemOut | None)
async def search(
    store_id: int,
    auth: AuthDep,
    platform: PlatformDep,
    session: SessionDep,
    login: str = Query(min_length=1, max_length=320),
) -> ItemOut | None:
    try:
        item = await service.find_by_login(session, platform, store_id, login)
    except CryptoError as exc:
        raise _fail(exc) from None
    return _item_out(item) if item else None


@router.get("/items/{item_id}/history", response_model=list[EventOut])
async def history(
    store_id: int, item_id: int, auth: AuthDep, session: SessionDep
) -> list[EventOut]:
    item = await session.get(StockItem, item_id)
    if item is None or item.store_id != store_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Item not found.")
    return [
        EventOut(
            at=e.at,
            actor=e.actor_label,
            event=e.event,
            from_state=e.from_state,
            to_state=e.to_state,
            details=e.details,
        )
        for e in await service.item_history(session, item_id)
    ]
