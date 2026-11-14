"""The values an event offers to templates, read from the event and the database."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import utcnow
from app.listings.models import Listing
from app.orders.models import Fulfilment, Order
from app.stock.models import ItemState, StockItem, StockPool
from app.stores.models import MarketplaceConnection, Store

DELIVERY_LABEL = {
    "auto": "Automatic (chat)",
    "assisted": "Approved by you (chat)",
    "text": "Message only",
    "manual": "By hand",
    "instant": "Instant (vault)",
}

ORDER_URLS = {"eldorado": "https://www.eldorado.gg/order/{id}"}


def money(amount: Decimal | float | str | None, currency: str = "USD") -> str:
    if amount is None:
        return ""
    return f"${Decimal(str(amount)):,.2f} {currency}"


def when(at: datetime, tz: str) -> str:
    try:
        zone = ZoneInfo(tz)
    except Exception:
        zone = ZoneInfo("UTC")
    return at.astimezone(zone).strftime("%b %d, %Y %H:%M")


async def available(session: AsyncSession, pool_id: int) -> int:
    return int(
        await session.scalar(
            select(func.count()).where(
                StockItem.pool_id == pool_id, StockItem.state == ItemState.available
            )
        )
        or 0
    )


def _plain(payload: dict[str, Any]) -> dict[str, str]:
    out = {}
    for key, value in payload.items():
        if isinstance(value, bool):
            out[key] = "Yes" if value else ""
        elif value is None:
            out[key] = ""
        elif isinstance(value, (str, int, float, Decimal)):
            out[key] = str(value)
    return out


@dataclass
class EventContext:
    values: dict[str, str]
    listing_id: int | None = None
    offer_id: str | None = None  # the marketplace offer the event is about


async def build(
    session: AsyncSession,
    event_type: str,
    store_id: int | None,
    payload: dict[str, Any],
    at: datetime | None = None,
) -> EventContext:
    values = _plain(payload)
    store = await session.get(Store, store_id) if store_id else None
    tz = store.timezone if store else "UTC"
    values["store"] = store.name if store else "All stores"
    values["time"] = when(at or utcnow(), tz)
    listing_id: int | None = payload.get("listing_id")

    offer_id: str | None = None
    if event_type.startswith("order.") or event_type == "chat.message_received":
        found = await _order(session, store, payload, values)
        if found is not None:
            listing_id = found.listing_id or listing_id
            offer_id = found.remote_offer_id
    if listing_id:
        listing = await session.get(Listing, listing_id)
        if listing is not None:
            values.setdefault("listing", listing.title)
            offer_id = offer_id or listing.remote_offer_id
    if event_type == "offer.restocked":
        values["resumed"] = "Offer resumed" if payload.get("resumed") else ""
        values.setdefault("listing", str(payload.get("title", "")))
    if event_type == "chat.message_received":
        values["urgent"] = "Urgent" if payload.get("urgent") else ""
        values["attachment"] = "Yes" if payload.get("attachment") else ""
    return EventContext(values, listing_id, offer_id)


async def _order(
    session: AsyncSession, store: Store | None, payload: dict[str, Any], values: dict[str, str]
) -> Order | None:
    remote_id = str(payload.get("order") or "")
    values["order"] = remote_id
    values["short_order"] = remote_id[:8]
    if not remote_id or store is None:
        return None
    order = await session.scalar(
        select(Order)
        .where(Order.store_id == store.id, Order.remote_id == remote_id)
        .order_by(Order.id)
        .limit(1)
    )
    if order is None:
        return None
    connection = await session.get(MarketplaceConnection, order.connection_id)
    url = ORDER_URLS.get(connection.connector_key if connection else "", "")
    values.update(
        order_url=url.format(id=remote_id) if url else "",
        buyer=values.get("buyer") or order.buyer_name or "Unknown buyer",
        title=values.get("title") or order.offer_title or "",
        quantity=str(order.quantity),
        total=money(order.total_amount, order.currency),
        amount=f"{order.total_amount:.2f}",
        currency=order.currency,
    )
    listing = await session.get(Listing, order.listing_id) if order.listing_id else None
    if listing is not None:
        values["listing"] = listing.title
    fulfilment = await session.scalar(select(Fulfilment).where(Fulfilment.order_id == order.id))
    per_unit = listing.units_per_sale if listing else 1
    accounts = fulfilment.units if fulfilment else order.quantity * per_unit
    values["accounts"] = str(payload.get("units") or accounts)
    if per_unit > 1:
        units = "unit" if order.quantity == 1 else "units"
        values["bundle"] = f"({order.quantity} {units} x {per_unit} accounts)"
    else:
        values.setdefault("bundle", "")
    mode = payload.get("delivery") or (fulfilment.mode if fulfilment else None)
    if mode == "instant" or (listing is not None and listing.restock_enabled and not fulfilment):
        mode = "instant"
    values["delivery"] = DELIVERY_LABEL.get(str(mode), "")
    pool_id = (fulfilment.pool_id if fulfilment else None) or (listing.pool_id if listing else None)
    if pool_id:
        pool = await session.get(StockPool, pool_id)
        values["pool"] = pool.name if pool else ""
        values["stock_left"] = str(await available(session, pool_id))
    else:
        values.setdefault("pool", "")
        values.setdefault("stock_left", "")
    return order
