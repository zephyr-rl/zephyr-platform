from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Response, status
from pydantic import BaseModel
from sqlalchemy import select

from app.api.deps import AuthDep, SessionDep
from app.modules.reports import service
from app.modules.reports.models import DailyReport
from app.stores.models import Store
from app.stores.service import NotFound as StoreNotFound
from app.stores.service import get_store

router = APIRouter(prefix="/stores/{store_id}/reports", tags=["reports"])


class DayPoint(BaseModel):
    day: date
    revenue: Decimal
    orders: int
    accounts: int


class TypeRow(BaseModel):
    type: str
    label: str
    orders: int
    units: int
    revenue: Decimal


class ListingRow(BaseModel):
    listing_id: int | None
    title: str
    type: str
    orders: int
    units: int
    revenue: Decimal


class StockRow(BaseModel):
    pool_id: int
    pool: str
    available: int


class SummaryOut(BaseModel):
    start: date
    end: date
    timezone: str
    currency: str
    revenue: Decimal
    orders: int
    accounts: int
    cancelled_orders: int
    cancelled_amount: Decimal
    series: list[DayPoint]
    by_type: list[TypeRow]
    by_listing: list[ListingRow]
    stock: list[StockRow]
    today: date


class DailyOut(BaseModel):
    id: int
    day: date
    created_at: datetime
    revenue: Decimal
    orders: int
    accounts: int


async def _store(session: SessionDep, store_id: int) -> Store:
    try:
        return await get_store(session, store_id)
    except StoreNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from None


def _today(store: Store) -> date:
    return datetime.now(service.zone(store)).date()


async def _summary(session: SessionDep, store: Store, start: date, end: date) -> dict[str, Any]:
    try:
        return await service.summarize(session, store, start, end)
    except service.ReportError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from None


@router.get("/summary", response_model=SummaryOut)
async def summary(
    store_id: int,
    auth: AuthDep,
    session: SessionDep,
    start: date | None = Query(None, description="First day (store time). Default: today."),
    end: date | None = Query(None, description="Last day, included. Default: start."),
) -> SummaryOut:
    store = await _store(session, store_id)
    today = _today(store)
    start = start or today
    data = await _summary(session, store, start, end or start)
    return SummaryOut(**data, today=today)


@router.get("/export.csv")
async def export_csv(
    store_id: int,
    auth: AuthDep,
    session: SessionDep,
    start: date = Query(...),
    end: date = Query(...),
) -> Response:
    store = await _store(session, store_id)
    await _summary(session, store, start, end)  # validates the range
    body = await service.orders_csv(session, store, start, end)
    name = f"{store.slug}-orders-{start.isoformat()}-to-{end.isoformat()}.csv"
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@router.get("/daily", response_model=list[DailyOut])
async def daily_reports(
    store_id: int, auth: AuthDep, session: SessionDep, limit: int = Query(30, ge=1, le=366)
) -> list[DailyOut]:
    await _store(session, store_id)
    rows = await session.scalars(
        select(DailyReport)
        .where(DailyReport.store_id == store_id)
        .order_by(DailyReport.day.desc())
        .limit(limit)
    )
    return [
        DailyOut(
            id=r.id,
            day=r.day,
            created_at=r.created_at,
            revenue=Decimal(r.summary["revenue"]),
            orders=r.summary["orders"],
            accounts=r.summary["accounts"],
        )
        for r in rows
    ]
