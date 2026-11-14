"""Sales statistics for any date range, and the daily report (exactly once per store and day).

Days are the store's local days. Valid orders are every order not cancelled, refunded or still
unpaid. Revenue is the order total the marketplace reports (the seller API does not report fees).
"""

from __future__ import annotations

import csv
import io
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import events
from app.core.db import utcnow
from app.core.platform import Platform
from app.listings.models import Listing
from app.modules.catalog import ReportsSettings
from app.modules.reports.models import DailyReport
from app.orders.models import Fulfilment, Order
from app.stock.models import ItemState, StockItem, StockPool
from app.stores.models import Store, StoreModule

DAILY_JOB = "reports.daily"
CANCELLED = frozenset({"cancelled", "refunded"})
NOT_SALES = CANCELLED | {"pending_payment"}
CATCH_UP = timedelta(hours=3)  # a report missed while the platform was down is sent this late
MAX_RANGE_DAYS = 400

TYPE_LABELS = {
    "accounts_instant": "Accounts (instant)",
    "accounts": "Accounts",
    "items": "Items",
    "topups": "Top-ups",
    "other": "Other",
}


class ReportError(Exception):
    pass


def zone(store: Store) -> ZoneInfo:
    try:
        return ZoneInfo(store.timezone)
    except Exception:
        return ZoneInfo("UTC")


def bounds(tz: ZoneInfo, start: date, end: date) -> tuple[datetime, datetime]:
    return (
        datetime.combine(start, time.min, tz),
        datetime.combine(end + timedelta(days=1), time.min, tz),
    )


def product_type(listing: Listing | None, order: Order) -> str:
    if listing is not None:
        if listing.kind == "account":
            return "accounts_instant" if listing.restock_enabled else "accounts"
        return {"item": "items", "topup": "topups"}.get(listing.kind, "other")
    category = (order.category or "").lower()
    if "top" in category:
        return "topups"
    if "account" in category:
        return "accounts"
    if "item" in category or "custom" in category:
        return "items"
    return "other"


def level(accounts: int) -> str:
    if accounts >= 300:
        return "Legend, 300+"
    if accounts >= 200:
        return "Epic, 200+"
    if accounts >= 100:
        return "Great, 100+"
    return "Standard"


def _money(value: Decimal, currency: str) -> str:
    return f"${value:,.2f} {currency}"


async def _rows(
    session: AsyncSession, store: Store, start: date, end: date
) -> list[tuple[Order, Listing | None, int | None]]:
    lo, hi = bounds(zone(store), start, end)
    result = await session.execute(
        select(Order, Listing, Fulfilment.units)
        .outerjoin(Listing, Listing.id == Order.listing_id)
        .outerjoin(Fulfilment, Fulfilment.order_id == Order.id)
        .where(
            Order.store_id == store.id,
            Order.remote_created_at >= lo,
            Order.remote_created_at < hi,
        )
        .order_by(Order.remote_created_at)
    )
    return [(o, listing, units) for o, listing, units in result]


def _units(order: Order, listing: Listing | None, fulfilment_units: int | None) -> int:
    if fulfilment_units:
        return fulfilment_units
    return order.quantity * (listing.units_per_sale if listing else 1)


async def stock_levels(session: AsyncSession, store_id: int) -> list[dict[str, Any]]:
    rows = await session.execute(
        select(StockItem.pool_id, func.count())
        .join(StockPool, StockPool.id == StockItem.pool_id)
        .where(StockPool.store_id == store_id, StockItem.state == ItemState.available)
        .group_by(StockItem.pool_id)
    )
    counts = {pool_id: n for pool_id, n in rows}
    pools = await session.scalars(
        select(StockPool)
        .where(StockPool.store_id == store_id, StockPool.archived.is_(False))
        .order_by(StockPool.name)
    )
    return [{"pool_id": p.id, "pool": p.name, "available": int(counts.get(p.id, 0))} for p in pools]


async def summarize(session: AsyncSession, store: Store, start: date, end: date) -> dict[str, Any]:
    if end < start:
        raise ReportError("The end date is before the start date.")
    if (end - start).days >= MAX_RANGE_DAYS:
        raise ReportError(f"Choose at most {MAX_RANGE_DAYS} days.")
    tz = zone(store)
    zero = Decimal("0")
    series: dict[date, dict[str, Any]] = {
        start + timedelta(days=i): {"revenue": zero, "orders": 0, "accounts": 0}
        for i in range((end - start).days + 1)
    }
    by_type: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"orders": 0, "units": 0, "revenue": zero}
    )
    by_listing: dict[str, dict[str, Any]] = {}
    revenue, orders, accounts = zero, 0, 0
    cancelled, cancelled_amount = 0, zero
    for order, listing, fulfilment_units in await _rows(session, store, start, end):
        day = order.remote_created_at.astimezone(tz).date()
        if order.state in CANCELLED:
            cancelled += 1
            cancelled_amount += order.total_amount
            continue
        if order.state in NOT_SALES:
            continue
        kind = product_type(listing, order)
        units = _units(order, listing, fulfilment_units)
        counted = 0 if kind == "topups" else units
        revenue += order.total_amount
        orders += 1
        accounts += counted
        point = series[day]
        point["revenue"] += order.total_amount
        point["orders"] += 1
        point["accounts"] += counted
        t = by_type[kind]
        t["orders"] += 1
        t["units"] += units
        t["revenue"] += order.total_amount
        key = f"l{listing.id}" if listing else f"t{order.offer_title or '?'}"
        row = by_listing.setdefault(
            key,
            {
                "listing_id": listing.id if listing else None,
                "title": (listing.title if listing else order.offer_title) or "Unknown offer",
                "type": kind,
                "orders": 0,
                "units": 0,
                "revenue": zero,
            },
        )
        row["orders"] += 1
        row["units"] += units
        row["revenue"] += order.total_amount

    def money_out(d: dict[str, Any]) -> dict[str, Any]:
        return {**d, "revenue": str(d["revenue"].quantize(Decimal("0.01")))}

    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "timezone": store.timezone,
        "currency": store.currency,
        "revenue": str(revenue.quantize(Decimal("0.01"))),
        "orders": orders,
        "accounts": accounts,
        "cancelled_orders": cancelled,
        "cancelled_amount": str(cancelled_amount.quantize(Decimal("0.01"))),
        "series": [money_out({"day": d.isoformat(), **v}) for d, v in series.items()],
        "by_type": sorted(
            (money_out({"type": k, "label": TYPE_LABELS[k], **v}) for k, v in by_type.items()),
            key=lambda r: Decimal(r["revenue"]),
            reverse=True,
        ),
        "by_listing": sorted(
            (money_out(v) for v in by_listing.values()),
            key=lambda r: (Decimal(r["revenue"]), r["orders"]),
            reverse=True,
        ),
        "stock": await stock_levels(session, store.id),
    }


def report_variables(store: Store, day: date, summary: dict[str, Any]) -> dict[str, Any]:
    """The values of the daily report template."""
    cur = summary["currency"]

    def line(r: dict[str, Any], name: str) -> str:
        noun = "top-ups" if r.get("type") == "topups" else "accounts"
        amount = _money(Decimal(r["revenue"]), cur)
        return f"{name}: {r['units']} {noun} in {r['orders']} orders, {amount}"

    listings = [line(r, r["title"]) for r in summary["by_listing"]]
    if len(listings) > 15:
        listings = [*listings[:15], f"… and {len(listings) - 15} more"]
    cancelled = summary["cancelled_orders"]
    return {
        "day": day.isoformat(),
        "date": day.strftime("%b %d, %Y"),
        "timezone": store.timezone,
        "revenue": _money(Decimal(summary["revenue"]), cur),
        "orders": str(summary["orders"]),
        "accounts": str(summary["accounts"]),
        "level": level(summary["accounts"]),
        "by_type": "\n".join(line(r, r["label"]) for r in summary["by_type"]) or "No sales",
        "by_listing": "\n".join(listings) or "No sales",
        "stock": "\n".join(f"{s['pool']}: {s['available']}" for s in summary["stock"])
        or "No pools",
        "cancelled": (
            f"{cancelled} ({_money(Decimal(summary['cancelled_amount']), cur)})"
            if cancelled
            else "None"
        ),
    }


async def _settings(session: AsyncSession, store_id: int) -> ReportsSettings:
    row = await session.scalar(
        select(StoreModule).where(
            StoreModule.store_id == store_id, StoreModule.module_key == "reports"
        )
    )
    return ReportsSettings.model_validate(row.settings if row else {})


def due_day(local_now: datetime, report_time: str) -> date | None:
    """The day whose report is due now: today after the report time, or yesterday's if it was
    missed by at most ``CATCH_UP`` (the platform was down at the report time)."""
    hh, mm = (int(x) for x in report_time.split(":"))
    today_at = datetime.combine(local_now.date(), time(hh, mm), local_now.tzinfo)
    if local_now >= today_at:
        return local_now.date()
    if local_now - (today_at - timedelta(days=1)) <= CATCH_UP:
        return local_now.date() - timedelta(days=1)
    return None


async def run_daily(platform: Platform, store_id: int, now: datetime | None = None) -> date | None:
    """Produce the due daily report once. Returns the day reported, or None."""
    async with platform.db.sessionmaker() as session:
        store = await session.get(Store, store_id)
        if store is None:
            return None
        settings = await _settings(session, store_id)
        day = due_day((now or utcnow()).astimezone(zone(store)), settings.daily_report_time)
        if day is None:
            return None
        exists = await session.scalar(
            select(DailyReport.id).where(DailyReport.store_id == store_id, DailyReport.day == day)
        )
        if exists:
            return None
        summary = await summarize(session, store, day, day)
        report_id = await session.scalar(
            insert(DailyReport)
            .values(store_id=store_id, day=day, summary=summary)
            .on_conflict_do_nothing(index_elements=["store_id", "day"])
            .returning(DailyReport.id)
        )
        if report_id is None:
            return None  # another worker made it
        events.publish(
            session, "report.daily_ready", report_variables(store, day, summary), store_id=store_id
        )
        await session.commit()
        return day


CSV_COLUMNS = [
    "date",
    "order",
    "buyer",
    "offer",
    "type",
    "quantity",
    "accounts",
    "total",
    "currency",
    "state",
]


async def orders_csv(session: AsyncSession, store: Store, start: date, end: date) -> str:
    tz = zone(store)
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(CSV_COLUMNS)
    for order, listing, units in await _rows(session, store, start, end):
        writer.writerow(
            [
                order.remote_created_at.astimezone(tz).strftime("%Y-%m-%d %H:%M"),
                order.remote_id,
                _safe(order.buyer_name or ""),
                _safe((listing.title if listing else order.offer_title) or ""),
                TYPE_LABELS[product_type(listing, order)],
                order.quantity,
                _units(order, listing, units),
                f"{order.total_amount:.2f}",
                order.currency,
                order.state,
            ]
        )
    return out.getvalue()


def _safe(value: str) -> str:
    """Stop spreadsheet formula injection from buyer names and titles."""
    return "'" + value if value[:1] in ("=", "+", "-", "@") else value
