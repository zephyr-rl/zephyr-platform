"""Review reminders: candidates, filters, pacing, daily cap, never twice, shadow mode."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.base import ConnectorError, Review
from app.connectors.fake import FakeConnector, FakeMarketplace
from app.core import jobs
from app.core.audit import Actor
from app.core.db import utcnow
from app.core.platform import Platform
from app.legacy.settings import migrate_settings
from app.modules.review_reminders import service
from app.modules.review_reminders.models import ReminderState, ReviewReminder
from app.orders.models import Order
from app.stores import service as stores
from app.stores.models import Store, StoreModule

ACTOR = Actor.system("tests")
NOW = utcnow()


@pytest.fixture
def market(platform: Platform) -> FakeMarketplace:
    m = FakeMarketplace()
    platform.connectors.override("fake", lambda creds: FakeConnector(creds, m))
    return m


class Shop:
    def __init__(self, platform: Platform, market: FakeMarketplace) -> None:
        self.platform, self.market = platform, market
        self.store_id = self.connection_id = 0

    async def setup(
        self, session: AsyncSession, *, shadow: bool = False, **settings: object
    ) -> Shop:
        store = await stores.create_store(
            session, self.platform, ACTOR, name="ZephyrStore", slug="zephyr"
        )
        store.shadow_mode = shadow
        connection = await stores.add_connection(
            session,
            self.platform,
            ACTOR,
            store,
            connector_key="fake",
            credentials={"api_key": "test-key"},
        )
        await stores.set_module(
            session,
            self.platform,
            ACTOR,
            store.id,
            "review_reminders",
            enabled=True,
            settings={"min_delay_seconds": 5, "max_delay_seconds": 5, **settings},
        )
        await session.commit()
        self.store_id, self.connection_id = store.id, connection.id
        return self

    async def order(
        self,
        session: AsyncSession,
        remote_id: str,
        *,
        buyer: str = "",
        age: timedelta = timedelta(days=2),
        state: str = "completed",
    ) -> Order:
        order = Order(
            store_id=self.store_id,
            connection_id=self.connection_id,
            remote_id=remote_id,
            state=state,
            quantity=1,
            total_amount=Decimal("5"),
            buyer_name=buyer or f"buyer-{remote_id}",
            conversation_id=f"conv-{remote_id}",
            offer_title="Twitch drops",
            remote_created_at=NOW - age,
        )
        session.add(order)
        await session.commit()
        return order

    def review(self, remote_id: str, rating: int) -> None:
        self.market.reviews.append(
            Review(
                remote_id=f"r-{remote_id}",
                order_id=remote_id,
                rating=rating,
                created_at=NOW - timedelta(hours=5),
            )
        )

    async def candidates(self, session: AsyncSession) -> list[str]:
        return [
            c.remote_id
            for c in await service.candidates(session, self.platform, self.store_id, NOW)
        ]

    async def queue_all(self, session: AsyncSession) -> int:
        found = await service.candidates(session, self.platform, self.store_id)
        count = await service.queue(
            session, self.platform, ACTOR, self.store_id, [c.order_id for c in found]
        )
        await session.commit()
        return count

    async def drain(self) -> None:
        """Run the send jobs, ignoring the pause between two reminders."""
        for _ in range(50):
            await service.send_next(self.platform, self.store_id)

    def sent(self) -> dict[str, list[str]]:
        return {
            conv: [m.text for m in msgs if m.from_seller]
            for conv, msgs in self.market.messages.items()
            if msgs
        }


@pytest.fixture
def shop(platform: Platform, market: FakeMarketplace) -> Shop:
    return Shop(platform, market)


async def test_candidates_follow_the_rules(session: AsyncSession, shop: Shop) -> None:
    await shop.setup(session, delay_hours=24, lookback_days=14)
    await shop.order(session, "OK1")
    await shop.order(session, "TOO-NEW", age=timedelta(hours=3))
    await shop.order(session, "TOO-OLD", age=timedelta(days=20))
    await shop.order(session, "CANCELLED", state="cancelled")
    await shop.order(session, "NEG", buyer="sad")
    await shop.order(session, "POS", buyer="happy")
    await shop.order(session, "SAME-BUYER-1", buyer="twice", age=timedelta(days=3))
    await shop.order(session, "SAME-BUYER-2", buyer="Twice", age=timedelta(days=2))
    shop.review("NEG", 1)
    shop.review("POS", 5)
    assert await shop.candidates(session) == ["SAME-BUYER-1", "OK1", "POS"]


@pytest.mark.parametrize(
    ("review_filter", "expected"),
    [
        ("skip_negative", ["NONE", "POS"]),
        ("skip_reviewed", ["NONE"]),
        ("remind_all", ["NEG", "NONE", "POS"]),
    ],
)
async def test_review_filter(
    session: AsyncSession, shop: Shop, review_filter: str, expected: list[str]
) -> None:
    await shop.setup(session, review_filter=review_filter)
    for name in ("NEG", "NONE", "POS"):
        await shop.order(session, name)
    shop.review("NEG", 1)
    shop.review("POS", 5)
    assert sorted(await shop.candidates(session)) == expected


async def test_queue_and_send_once(session: AsyncSession, shop: Shop) -> None:
    await shop.setup(session, message_template="Hi {buyer}, how was {product}? (#{order})")
    await shop.order(session, "A1", buyer="NightOwl92")
    await shop.order(session, "A2", buyer="PixelFox")
    assert await shop.queue_all(session) == 2
    await shop.drain()
    assert shop.sent() == {
        "conv-A1": ["Hi NightOwl92, how was Twitch drops? (#A1)"],
        "conv-A2": ["Hi PixelFox, how was Twitch drops? (#A2)"],
    }
    # Never again: not a candidate any more, and queueing it again does nothing.
    assert await shop.candidates(session) == []
    assert await shop.queue_all(session) == 0
    rows = list(await session.scalars(select(ReviewReminder)))
    assert {r.state for r in rows} == {ReminderState.sent}


async def test_pacing_uses_the_job_queue(
    session: AsyncSession, shop: Shop, platform: Platform
) -> None:
    await shop.setup(session)
    await shop.order(session, "A1")
    await shop.order(session, "A2")
    await shop.queue_all(session)
    ran = 0
    while await jobs.run_one(
        platform.db.sessionmaker, platform.jobs, "t", services={"platform": platform}
    ):
        ran += 1
    # First sent now; the second waits for the pause (a job scheduled in the future).
    assert len(shop.sent()) == 1 and ran == 1
    async with platform.db.sessionmaker() as s:
        pending = await s.scalar(select(jobs.Job).where(jobs.Job.state == jobs.JobState.queued))
        assert pending is not None and pending.run_at > utcnow()


async def test_daily_cap(session: AsyncSession, shop: Shop) -> None:
    await shop.setup(session, daily_cap=2)
    for i in range(4):
        await shop.order(session, f"A{i}")
    assert await shop.queue_all(session) == 2
    await shop.drain()
    assert len(shop.sent()) == 2
    assert await service.queued_count(session, shop.store_id) == 0
    with pytest.raises(service.ReminderError, match="limit"):
        await shop.queue_all(session)


async def test_review_left_after_queueing_is_skipped(session: AsyncSession, shop: Shop) -> None:
    await shop.setup(session)
    await shop.order(session, "A1")
    await shop.queue_all(session)
    shop.review("A1", 1)
    await shop.drain()
    assert shop.sent() == {}
    row = await session.scalar(select(ReviewReminder))
    assert row is not None and row.state is ReminderState.skipped and "review" in (row.note or "")


async def test_interrupted_send_is_not_repeated(session: AsyncSession, shop: Shop) -> None:
    await shop.setup(session)
    await shop.order(session, "A1")
    await shop.queue_all(session)
    original = FakeConnector.chat_send

    async def send_then_die(self: FakeConnector, conversation_id: str, text: str):  # type: ignore[no-untyped-def]
        await original(self, conversation_id, text)
        raise RuntimeError("worker died")

    FakeConnector.chat_send = send_then_die  # type: ignore[method-assign]
    try:
        with pytest.raises(RuntimeError):
            await service.send_next(shop.platform, shop.store_id)
    finally:
        FakeConnector.chat_send = original  # type: ignore[method-assign]
    await shop.drain()
    assert shop.sent() == {"conv-A1": [shop.sent()["conv-A1"][0]]}
    session.expire_all()
    row = await session.scalar(select(ReviewReminder))
    assert row is not None and row.state is ReminderState.sent and "interrupted" in (row.note or "")


async def test_failures_retry_then_stop(session: AsyncSession, shop: Shop) -> None:
    await shop.setup(session)
    await shop.order(session, "A1")
    await shop.queue_all(session)
    for _ in range(3):
        shop.market.fail_next("chat_send", ConnectorError("busy", retryable=True))
    await shop.drain()
    row = await session.scalar(select(ReviewReminder))
    assert row is not None and row.state is ReminderState.failed and row.attempts == 3


async def test_shadow_mode_and_module_off(session: AsyncSession, shop: Shop) -> None:
    await shop.setup(session, shadow=True)
    await shop.order(session, "A1")
    assert await shop.candidates(session) == ["A1"]  # shown, never sent
    with pytest.raises(service.ReminderError, match="shadow"):
        await shop.queue_all(session)
    await session.rollback()
    store = await session.get(Store, shop.store_id)
    assert store is not None
    store.shadow_mode = False
    module = await session.scalar(
        select(StoreModule).where(StoreModule.module_key == "review_reminders")
    )
    assert module is not None
    module.enabled = False
    await session.commit()
    with pytest.raises(service.ReminderError, match="Turn on"):
        await shop.queue_all(session)


async def test_automatic_mode_queues_within_the_cap(session: AsyncSession, shop: Shop) -> None:
    await shop.setup(session, automatic=True, daily_cap=3)
    for i in range(5):
        await shop.order(session, f"A{i}")
    assert await service.run_automatic(shop.platform, shop.store_id) == 3
    assert await service.run_automatic(shop.platform, shop.store_id) == 0


async def test_manual_mode_queues_nothing_by_itself(session: AsyncSession, shop: Shop) -> None:
    await shop.setup(session)
    await shop.order(session, "A1")
    assert await service.run_automatic(shop.platform, shop.store_id) == 0


async def test_reminders_api(auth_client, session: AsyncSession, shop: Shop) -> None:  # type: ignore[no-untyped-def]
    await shop.setup(session)
    order = await shop.order(session, "A1", buyer="NightOwl92")
    order_id = order.id
    base = f"/api/v1/stores/{shop.store_id}/reminders"
    found = (await auth_client.get(f"{base}/candidates")).json()
    assert [c["remote_id"] for c in found] == ["A1"]
    assert (await auth_client.post(f"{base}/queue", json={"order_ids": [order_id]})).json() == {
        "queued": 1
    }
    status = (await auth_client.get(base)).json()
    assert status["queued"] == 1 and status["history"][0]["state"] == "queued"
    assert (await auth_client.post(f"{base}/cancel")).json() == {"cancelled": 1}
    assert (await auth_client.get(base)).json()["queued"] == 0


async def test_old_reminders_are_imported(
    session: AsyncSession, shop: Shop, tmp_path: Path
) -> None:
    await shop.setup(session)
    await shop.order(session, "OLD-1", buyer="fastbuyer")
    await shop.order(session, "NEW-1", buyer="someone")
    folder = tmp_path / "old"
    folder.mkdir()
    (folder / "buyer_reminder_config.json").write_text(
        json.dumps(
            {
                "template": "Hi {buyerUsername}! Thanks for {productName} #{orderId}",
                "daysLookback": 10,
                "minDelaySeconds": 20.0,
                "maxDelaySeconds": 50.0,
                "maxMessagesPerBatch": 100,
                "reviewFilter": "all",
                "cooldownDaysPerBuyer": 7,
                "remindedOrderIds": ["OLD-1", "OLD-2"],
            }
        ),
        encoding="utf-8",
    )
    (folder / "buyer_reminder_history.json").write_text(
        json.dumps(
            [
                {
                    "orderId": "OLD-1",
                    "buyer": "fastbuyer",
                    "timestamp": (NOW - timedelta(days=1)).isoformat(),
                }
            ]
        ),
        encoding="utf-8",
    )
    (folder / "topup_config.json").write_text(
        json.dumps(
            {
                "enabled": True,
                "defaultWelcomeMessage": "Hi {buyerUsername}! Send your login.",
                "offerSettings": {
                    "rp-pc": {
                        "enabled": True,
                        "customMessage": "Hi *{buyerUsername}*! Rocket Pass: send your Epic login.",
                    },
                    "rp-xbox": {
                        "enabled": False,
                        "customMessage": "Hi {buyerUsername}! Send your login.",
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    report = await migrate_settings(
        session, shop.platform, ACTOR, folder=folder, store_id=shop.store_id
    )
    assert report.reminders_imported == 2
    session.expire_all()
    modules = {m.module_key: m for m in await session.scalars(select(StoreModule))}
    r = modules["review_reminders"].settings
    assert r["message_template"] == "Hi {buyer}! Thanks for {product} #{order}"
    assert (r["review_filter"], r["daily_cap"], r["buyer_cooldown_days"], r["automatic"]) == (
        "skip_reviewed",
        100,
        7,
        False,
    )
    t = modules["topups"]
    assert t.enabled and t.settings["instructions"] == "Hi {buyer}! Send your login."
    assert t.settings["offer_messages"] == {
        "rp-pc": "Hi *{buyer}*! Rocket Pass: send your Epic login."
    }
    assert t.settings["skip_offers"] == ["rp-xbox"]
    # OLD-1 was reminded by the old system; NEW-1 is still a candidate.
    assert await shop.candidates(session) == ["NEW-1"]


def test_settings_validation() -> None:
    from pydantic import ValidationError

    from app.modules.catalog import ReviewRemindersSettings

    with pytest.raises(ValidationError):
        ReviewRemindersSettings(min_delay_seconds=60, max_delay_seconds=30)
    assert isinstance(NOW, datetime)


async def test_a_batch_never_goes_past_todays_limit(session: AsyncSession, shop: Shop) -> None:
    await shop.setup(session, daily_cap=2)
    for i in range(4):
        await shop.order(session, f"A{i}")
    assert await shop.queue_all(session) == 2
    with pytest.raises(service.ReminderError, match="limit"):
        await shop.queue_all(session)
