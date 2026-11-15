"""All stores together: the combined Overview and Reports.

Each store keeps its own days (its own time zone): "today" is each store's today, and a date
range is read in each store's time zone, then added up.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func, select

from app.api.deps import AuthDep, OwnerDep, SessionDep
from app.api.v1.orders import ATTENTION
from app.core.audit import AuditLog
from app.modules.chat_monitor.models import ChatThread
from app.modules.reports import service as reports
from app.orders.models import Fulfilment
from app.stock.models import ItemState, StockItem, StockPool
from app.stores.models import Store

router = APIRouter(prefix="/all", tags=["all stores"])


async def _stores(session: SessionDep) -> list[Store]:
    return list(
        await session.scalars(select(Store).where(Store.is_active.is_(True)).order_by(Store.id))
    )


def _today(store: Store) -> date:
    return datetime.now(reports.zone(store)).date()


def merge(per_store: list[tuple[Store, dict[str, Any]]]) -> dict[str, Any]:
    """Add up several stores' summaries (same shape as one store's)."""
    zero = Decimal("0")
    series: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"revenue": zero, "orders": 0, "accounts": 0}
    )
    types: dict[str, dict[str, Any]] = {}
    listings: list[dict[str, Any]] = []
    stock: list[dict[str, Any]] = []
    revenue = cancelled_amount = zero
    orders = accounts = cancelled = 0
    for store, s in per_store:
        revenue += Decimal(s["revenue"])
        orders += s["orders"]
        accounts += s["accounts"]
        cancelled += s["cancelled_orders"]
        cancelled_amount += Decimal(s["cancelled_amount"])
        for p in s["series"]:
            point = series[p["day"]]
            point["revenue"] += Decimal(p["revenue"])
            point["orders"] += p["orders"]
            point["accounts"] += p["accounts"]
        for t in s["by_type"]:
            row = types.setdefault(
                t["type"],
                {"type": t["type"], "label": t["label"], "orders": 0, "units": 0, "revenue": zero},
            )
            row["orders"] += t["orders"]
            row["units"] += t["units"]
            row["revenue"] += Decimal(t["revenue"])
        listings += [{**r, "store": store.name} for r in s["by_listing"]]
        stock += [{**p, "store": store.name} for p in s["stock"]]
    cents = Decimal("0.01")
    return {
        "revenue": str(revenue.quantize(cents)),
        "orders": orders,
        "accounts": accounts,
        "cancelled_orders": cancelled,
        "cancelled_amount": str(cancelled_amount.quantize(cents)),
        "series": [
            {"day": d, **{**v, "revenue": str(v["revenue"].quantize(cents))}}
            for d, v in sorted(series.items())
        ],
        "by_type": sorted(
            ({**t, "revenue": str(t["revenue"].quantize(cents))} for t in types.values()),
            key=lambda r: Decimal(r["revenue"]),
            reverse=True,
        ),
        "by_listing": sorted(
            listings, key=lambda r: (Decimal(r["revenue"]), r["orders"]), reverse=True
        ),
        "stock": stock,
        "by_store": [
            {
                "store_id": st.id,
                "store": st.name,
                "revenue": s["revenue"],
                "orders": s["orders"],
                "accounts": s["accounts"],
            }
            for st, s in per_store
        ],
    }


class StoreFigures(BaseModel):
    store_id: int
    store: str
    revenue: Decimal
    orders: int
    accounts: int


class CombinedSummary(BaseModel):
    start: date | None
    end: date | None
    currency: str
    revenue: Decimal
    orders: int
    accounts: int
    cancelled_orders: int
    cancelled_amount: Decimal
    series: list[dict[str, Any]]
    by_type: list[dict[str, Any]]
    by_listing: list[dict[str, Any]]
    stock: list[dict[str, Any]]
    by_store: list[StoreFigures]


@router.get("/summary", response_model=CombinedSummary)
async def combined_summary(
    auth: AuthDep,
    session: SessionDep,
    start: date | None = Query(None, description="First day. Default: each store's today."),
    end: date | None = Query(None, description="Last day, included. Default: start."),
) -> CombinedSummary:
    per_store = []
    for store in await _stores(session):
        first = start or _today(store)
        try:
            per_store.append((store, await reports.summarize(session, store, first, end or first)))
        except reports.ReportError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from None
    currencies = {s.currency for s, _ in per_store} or {"USD"}
    if len(currencies) > 1:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The stores use different currencies: totals cannot be added up.",
        )
    return CombinedSummary(
        start=start, end=end or start, currency=currencies.pop(), **merge(per_store)
    )


class StoreStatus(BaseModel):
    store_id: int
    store: str
    shadow_mode: bool
    revenue_today: Decimal
    orders_today: int
    accounts_today: int
    orders_need_you: int
    buyers_waiting: int
    urgent_messages: int
    low_pools: int


class ActivityRow(BaseModel):
    id: int
    at: datetime
    store: str | None
    actor: str
    action: str


class CombinedOverview(BaseModel):
    currency: str
    stores: list[StoreStatus]
    activity: list[ActivityRow]


@router.get("/overview", response_model=CombinedOverview)
async def combined_overview(auth: OwnerDep, session: SessionDep) -> CombinedOverview:
    stores = await _stores(session)
    out: list[StoreStatus] = []
    for store in stores:
        today = await reports.summarize(session, store, _today(store), _today(store))
        attention = int(
            await session.scalar(
                select(func.count()).where(
                    Fulfilment.store_id == store.id, Fulfilment.state.in_(ATTENTION)
                )
            )
            or 0
        )
        threads = list(
            await session.scalars(
                select(ChatThread)
                .where(ChatThread.store_id == store.id, ChatThread.last_buyer_at.is_not(None))
                .order_by(ChatThread.last_buyer_at.desc())
                .limit(300)
            )
        )
        waiting = [t for t in threads if t.needs_reply]
        counts = dict(
            (pid, n)
            for pid, n in await session.execute(
                select(StockItem.pool_id, func.count())
                .join(StockPool, StockPool.id == StockItem.pool_id)
                .where(StockPool.store_id == store.id, StockItem.state == ItemState.available)
                .group_by(StockItem.pool_id)
            )
        )
        pools = await session.scalars(
            select(StockPool).where(StockPool.store_id == store.id, StockPool.archived.is_(False))
        )
        low = sum(
            1
            for p in pools
            if p.low_stock_threshold and counts.get(p.id, 0) <= p.low_stock_threshold
        )
        out.append(
            StoreStatus(
                store_id=store.id,
                store=store.name,
                shadow_mode=store.shadow_mode,
                revenue_today=Decimal(today["revenue"]),
                orders_today=today["orders"],
                accounts_today=today["accounts"],
                orders_need_you=attention,
                buyers_waiting=len(waiting),
                urgent_messages=sum(1 for t in waiting if t.urgent),
                low_pools=low,
            )
        )
    names = {s.id: s.name for s in stores}
    rows = await session.scalars(
        select(AuditLog)
        .where(AuditLog.store_id.is_not(None))
        .order_by(AuditLog.id.desc())
        .limit(15)
    )
    activity = [
        ActivityRow(
            id=a.id, at=a.at, store=names.get(a.store_id or 0), actor=a.actor_label, action=a.action
        )
        for a in rows
    ]
    currencies = {s.currency for s in stores} or {"USD"}
    return CombinedOverview(currency=sorted(currencies)[0], stores=out, activity=activity)
