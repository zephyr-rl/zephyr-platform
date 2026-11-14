"""Reports: numbers for a date range, the daily report exactly once, CSV export."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import Actor
from app.core.events import OutboxEvent
from app.core.platform import Platform
from app.listings.models import DeliveryMode, Listing
from app.modules.reports import service
from app.modules.reports.models import DailyReport
from app.orders.models import Order
from app.stock import service as stock
from app.stores import service as stores
from app.stores.models import Store

ACTOR = Actor.system("tests")
BA = ZoneInfo("America/Argentina/Buenos_Aires")
DAY = date(2026, 11, 14)


def at(hour: int, minute: int = 0, day: date = DAY) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=BA)


class Book:
    def __init__(self, platform: Platform) -> None:
        self.platform = platform
        self.store_id = self.connection_id = 0
        self.listings: dict[str, int] = {}
        self.n = 0

    async def setup(self, session: AsyncSession) -> Book:
        store = await stores.create_store(
            session, self.platform, ACTOR, name="ZephyrStore", slug="zephyr"
        )
        store.timezone = "America/Argentina/Buenos_Aires"
        connection = await stores.add_connection(
            session,
            self.platform,
            ACTOR,
            store,
            connector_key="fake",
            credentials={"api_key": "test-key"},
        )
        pool = await stock.create_pool(session, ACTOR, store.id, name="Fortnite accounts")
        await session.commit()
        body = "\n".join(f"u{i}:p{i}" for i in range(7))
        await stock.commit_import(
            session,
            self.platform,
            ACTOR,
            store.id,
            pool.id,
            body,
            expected_sha256=stock.text_digest(body),
        )
        for key, kind, title, units, restock in (
            ("vault", "account", "Fortnite account", 1, True),
            ("drops", "item", "Twitch drops bundle", 2, False),
            ("pass", "topup", "Rocket Pass", 1, False),
        ):
            listing = Listing(
                store_id=store.id,
                connection_id=connection.id,
                remote_offer_id=key,
                kind=kind,
                title=title,
                remote_state="active",
                price_amount=Decimal("1"),
                pool_id=pool.id,
                delivery_mode=DeliveryMode.manual,
                units_per_sale=units,
                restock_enabled=restock,
            )
            session.add(listing)
            await session.flush()
            self.listings[key] = listing.id
        await session.commit()
        self.store_id, self.connection_id = store.id, connection.id
        return self

    def sell(
        self,
        session: AsyncSession,
        listing: str | None,
        amount: str,
        when: datetime,
        *,
        state: str = "delivered",
        qty: int = 1,
        title: str | None = None,
        buyer: str = "buyer",
    ) -> None:
        self.n += 1
        session.add(
            Order(
                store_id=self.store_id,
                connection_id=self.connection_id,
                listing_id=self.listings[listing] if listing else None,
                remote_id=f"O{self.n}",
                state=state,
                quantity=qty,
                total_amount=Decimal(amount),
                buyer_name=buyer,
                offer_title=title,
                category="Account" if listing is None else None,
                remote_created_at=when,
            )
        )

    async def store(self, session: AsyncSession) -> Store:
        store = await session.get(Store, self.store_id)
        assert store is not None
        return store


@pytest.fixture
def book(platform: Platform) -> Book:
    return Book(platform)


async def _day_sales(session: AsyncSession, book: Book) -> None:
    book.sell(session, "vault", "10.00", at(9))
    book.sell(session, "vault", "10.00", at(23, 58))
    book.sell(session, "drops", "8.50", at(12), qty=3)  # bundle: 3 units x 2 accounts
    book.sell(session, "pass", "5.00", at(13))
    book.sell(session, None, "4.00", at(14), title="Old offer")
    book.sell(session, "vault", "10.00", at(15), state="refunded")
    book.sell(session, "vault", "10.00", at(16), state="pending_payment")
    book.sell(session, "vault", "99.00", at(0, 30, DAY + timedelta(days=1)))  # next local day
    book.sell(session, "vault", "99.00", at(23, 59, DAY - timedelta(days=1)))  # previous local day
    await session.commit()


async def test_day_summary(session: AsyncSession, book: Book) -> None:
    await book.setup(session)
    await _day_sales(session, book)
    s = await service.summarize(session, await book.store(session), DAY, DAY)
    assert s["revenue"] == "37.50" and s["orders"] == 5
    assert s["accounts"] == 2 + 6 + 1  # vault 2, bundle 3x2, unmatched 1; top-ups not counted
    assert (s["cancelled_orders"], s["cancelled_amount"]) == (1, "10.00")
    types = {t["type"]: (t["orders"], t["units"], t["revenue"]) for t in s["by_type"]}
    assert types == {
        "accounts_instant": (2, 2, "20.00"),
        "items": (1, 6, "8.50"),
        "topups": (1, 1, "5.00"),
        "accounts": (1, 1, "4.00"),
    }
    assert [r["title"] for r in s["by_listing"]] == [
        "Fortnite account",
        "Twitch drops bundle",
        "Rocket Pass",
        "Old offer",
    ]
    assert s["stock"] == [
        {"pool_id": s["stock"][0]["pool_id"], "pool": "Fortnite accounts", "available": 7}
    ]


async def test_range_series_has_every_day(session: AsyncSession, book: Book) -> None:
    await book.setup(session)
    await _day_sales(session, book)
    s = await service.summarize(
        session, await book.store(session), DAY - timedelta(days=1), DAY + timedelta(days=2)
    )
    assert [p["day"] for p in s["series"]] == [
        (DAY + timedelta(days=i)).isoformat() for i in range(-1, 3)
    ]
    assert [p["revenue"] for p in s["series"]] == ["99.00", "37.50", "99.00", "0.00"]
    with pytest.raises(service.ReportError):
        await service.summarize(session, await book.store(session), DAY, DAY - timedelta(days=1))


def test_due_day() -> None:
    assert service.due_day(at(23, 58), "23:59") is None
    assert service.due_day(at(23, 59), "23:59") == DAY
    # Platform down at 23:59, back at 01:30: yesterday's report is still sent.
    assert service.due_day(at(1, 30, DAY + timedelta(days=1)), "23:59") == DAY
    assert service.due_day(at(4, 0, DAY + timedelta(days=1)), "23:59") is None
    assert service.due_day(at(9, 0), "08:00") == DAY


async def test_daily_report_is_produced_exactly_once(
    session: AsyncSession, book: Book, platform: Platform
) -> None:
    await book.setup(session)
    await stores.set_module(
        session, platform, ACTOR, book.store_id, "reports", settings={"daily_report_time": "23:59"}
    )
    await session.commit()
    await _day_sales(session, book)
    assert await service.run_daily(platform, book.store_id, at(23, 30)) is None
    results = await asyncio.gather(
        *(service.run_daily(platform, book.store_id, at(23, 59)) for _ in range(4))
    )
    assert [r for r in results if r] == [DAY]
    assert (
        await service.run_daily(platform, book.store_id, at(0, 30, DAY + timedelta(days=1))) is None
    )

    events = list(
        await session.scalars(select(OutboxEvent).where(OutboxEvent.type == "report.daily_ready"))
    )
    assert len(events) == 1
    p = events[0].payload
    assert p["revenue"] == "$37.50 USD" and p["orders"] == "5" and p["accounts"] == "9"
    assert p["date"] == "Nov 14, 2026" and p["cancelled"] == "1 ($10.00 USD)"
    assert "Twitch drops bundle: 6 accounts in 1 orders, $8.50 USD" in p["by_listing"]
    assert "Rocket Pass: 1 top-ups in 1 orders" in p["by_listing"]
    assert p["stock"] == "Fortnite accounts: 7"

    with pytest.raises(DBAPIError):
        await session.execute(update(DailyReport).values(summary={}))
    await session.rollback()


async def test_reports_api_and_csv(auth_client, session: AsyncSession, book: Book) -> None:  # type: ignore[no-untyped-def]
    await book.setup(session)
    await _day_sales(session, book)
    book.sell(session, "vault", "1.00", at(10), buyer="=HYPERLINK(1)")
    await session.commit()
    base = f"/api/v1/stores/{book.store_id}/reports"
    s = (await auth_client.get(f"{base}/summary", params={"start": DAY.isoformat()})).json()
    assert s["revenue"] == "38.50" and s["orders"] == 6 and len(s["series"]) == 1
    bad = await auth_client.get(
        f"{base}/summary", params={"start": "2026-11-14", "end": "2026-11-01"}
    )
    assert bad.status_code == 422

    csv = await auth_client.get(
        f"{base}/export.csv", params={"start": DAY.isoformat(), "end": DAY.isoformat()}
    )
    assert csv.status_code == 200 and "attachment" in csv.headers["content-disposition"]
    lines = csv.text.strip().splitlines()
    assert lines[0] == ",".join(service.CSV_COLUMNS) and len(lines) == 1 + 8
    assert "'=HYPERLINK(1)" in csv.text  # formula injection neutralised
    assert (await auth_client.get(f"{base}/daily")).json() == []


async def test_daily_reports_table_rejects_changes(session: AsyncSession, book: Book) -> None:
    await book.setup(session)
    session.add(DailyReport(store_id=book.store_id, day=DAY, summary={"revenue": "0"}))
    await session.commit()
    with pytest.raises(DBAPIError):
        await session.execute(text("UPDATE daily_reports SET day = day + 1"))
    await session.rollback()
