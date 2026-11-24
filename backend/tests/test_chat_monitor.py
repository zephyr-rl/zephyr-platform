"""Chat monitor: the anti-spam rules of the old monitor, and the Messages API."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.base import ChatMessage
from app.connectors.fake import FakeConnector, FakeMarketplace
from app.core.audit import Actor
from app.core.db import utcnow
from app.core.events import OutboxEvent
from app.core.platform import Platform
from app.modules.chat_monitor import service
from app.modules.chat_monitor.models import ChatThread
from app.orders.models import Fulfilment, FulfilmentState, Order
from app.stores import service as stores
from app.stores.models import Store

ACTOR = Actor.system("tests")
NOW = utcnow()


@pytest.fixture
def market(platform: Platform) -> FakeMarketplace:
    m = FakeMarketplace()
    platform.connectors.override("fake", lambda creds: FakeConnector(creds, m))
    return m


class Chats:
    def __init__(self, platform: Platform, market: FakeMarketplace) -> None:
        self.platform, self.market = platform, market
        self.store_id = self.connection_id = 0

    async def setup(self, session: AsyncSession, settings: dict | None = None) -> Chats:  # type: ignore[type-arg]
        store = await stores.create_store(session, self.platform, ACTOR, name="Kawai", slug="kawai")
        connection = await stores.add_connection(
            session,
            self.platform,
            ACTOR,
            store,
            connector_key="fake",
            credentials={"api_key": "test-key"},
        )
        if settings:
            await stores.set_module(
                session, self.platform, ACTOR, store.id, "chat_monitor", settings=settings
            )
        await session.commit()
        self.store_id, self.connection_id = store.id, connection.id
        return self

    async def order(self, session: AsyncSession, remote_id: str, state: str = "delivered") -> Order:
        order = Order(
            store_id=self.store_id,
            connection_id=self.connection_id,
            remote_id=remote_id,
            state=state,
            quantity=1,
            total_amount=Decimal("9.99"),
            buyer_name="NightOwl92",
            conversation_id=f"conv-{remote_id}",
            offer_title="Fortnite account",
            remote_created_at=NOW - timedelta(hours=1),
        )
        session.add(order)
        await session.commit()
        return order

    def say(
        self,
        remote_id: str,
        text: str,
        *,
        at: datetime,
        seller: bool = False,
        author: str | None = "x",
    ) -> None:
        conv = self.market.messages[f"conv-{remote_id}"]
        conv.append(
            ChatMessage(
                remote_id=f"m{len(conv)}",
                conversation_id=f"conv-{remote_id}",
                author_id=("seller" if seller else author),
                from_seller=seller,
                text=text,
                sent_at=at,
            )
        )

    async def scan(self, now: datetime = NOW) -> service.ScanResult:
        return await service.scan_store(self.platform, self.store_id, now)

    async def alerts(self, session: AsyncSession) -> list[dict]:  # type: ignore[type-arg]
        session.expire_all()
        return [
            e.payload
            for e in await session.scalars(
                select(OutboxEvent)
                .where(OutboxEvent.type == "chat.message_received")
                .order_by(OutboxEvent.id)
            )
        ]


@pytest.fixture
def chats(platform: Platform, market: FakeMarketplace) -> Chats:
    return Chats(platform, market)


async def test_new_buyer_message_raises_one_alert(session: AsyncSession, chats: Chats) -> None:
    await chats.setup(session)
    await chats.order(session, "A1")
    chats.say("A1", "Hi, the password does not work", at=NOW - timedelta(minutes=1))
    result = await chats.scan()
    assert result.alerts == 1 and result.conversations == 1
    [alert] = await chats.alerts(session)
    assert alert["buyer"] == "NightOwl92" and alert["message"] == "Hi, the password does not work"
    assert alert["urgent"] is False
    # Same message on the next scan: nothing new.
    assert (await chats.scan(NOW + timedelta(seconds=30))).alerts == 0


async def test_alert_words_mark_it_urgent(session: AsyncSession, chats: Chats) -> None:
    await chats.setup(session)
    await chats.order(session, "A1")
    chats.say("A1", "WRONG PASSWORD!!", at=NOW - timedelta(minutes=1))
    await chats.scan()
    assert (await chats.alerts(session))[0]["urgent"] is True


async def test_stale_messages_are_ignored(session: AsyncSession, chats: Chats) -> None:
    await chats.setup(session)
    await chats.order(session, "A1")
    chats.say("A1", "hello?", at=NOW - timedelta(minutes=6))
    assert (await chats.scan()).alerts == 0


async def test_answered_or_active_seller_is_not_alerted(
    session: AsyncSession, chats: Chats
) -> None:
    await chats.setup(session)
    await chats.order(session, "A1")
    await chats.order(session, "A2")
    chats.say("A1", "hello?", at=NOW - timedelta(minutes=2))
    chats.say("A1", "Hi! Checking it now", at=NOW - timedelta(minutes=1), seller=True)
    chats.say("A2", "Here you go", at=NOW - timedelta(minutes=2), seller=True)
    chats.say("A2", "thanks, one more question", at=NOW - timedelta(minutes=1))
    assert (await chats.scan()).alerts == 0  # A1 answered; A2 seller active 2 min ago


async def test_automatic_messages_do_not_count_as_the_seller(
    session: AsyncSession, chats: Chats
) -> None:
    await chats.setup(session)
    await chats.order(session, "A1")
    chats.say(
        "A1",
        "Thank you for your purchase!\n\n🟢Account 1",
        at=NOW - timedelta(minutes=2),
        seller=True,
    )
    chats.say("A1", "it says wrong password", at=NOW - timedelta(minutes=1))
    assert (await chats.scan()).alerts == 1


async def test_cooldown_between_alerts(session: AsyncSession, chats: Chats) -> None:
    await chats.setup(session, {"cooldown_minutes": 20})
    await chats.order(session, "A1")
    chats.say("A1", "hello?", at=NOW - timedelta(minutes=1))
    assert (await chats.scan()).alerts == 1
    chats.say("A1", "anyone??", at=NOW + timedelta(minutes=5))
    assert (await chats.scan(NOW + timedelta(minutes=6))).alerts == 0
    chats.say("A1", "please", at=NOW + timedelta(minutes=21))
    assert (await chats.scan(NOW + timedelta(minutes=22))).alerts == 1


async def test_courtesy_replies_and_system_messages(session: AsyncSession, chats: Chats) -> None:
    await chats.setup(session)
    await chats.order(session, "A1")
    await chats.order(session, "A2", state="paid")
    chats.say("A1", "Thanks!", at=NOW - timedelta(minutes=1))
    chats.say("A1", "Order delivered", at=NOW - timedelta(seconds=30), author=None)  # system
    chats.say("A2", "thanks", at=NOW - timedelta(minutes=1))  # not delivered yet: alerted
    await chats.scan()
    assert [a["order"] for a in await chats.alerts(session)] == ["A2"]


async def test_closed_orders_are_not_read(
    session: AsyncSession, chats: Chats, market: FakeMarketplace
) -> None:
    await chats.setup(session)
    await chats.order(session, "A1", state="completed")
    chats.say("A1", "hello?", at=NOW - timedelta(minutes=1))
    result = await chats.scan()
    assert result.conversations == 0 and result.alerts == 0
    assert not [c for c in market.calls if c[0] == "chat_list_messages"]


async def test_confirmation_replies_are_left_to_deliveries(
    session: AsyncSession, chats: Chats
) -> None:
    await chats.setup(session)
    order = await chats.order(session, "A1", state="paid")
    session.add(
        Fulfilment(
            order_id=order.id,
            store_id=chats.store_id,
            mode="auto",
            require_confirmation=True,
            state=FulfilmentState.sent,
            units=1,
        )
    )
    await session.commit()
    chats.say("A1", "CONFIRM", at=NOW - timedelta(minutes=1))
    assert (await chats.scan()).alerts == 0
    chats.say("A1", "it doesn't work", at=NOW + timedelta(minutes=1))
    assert (await chats.scan(NOW + timedelta(minutes=2))).alerts == 1


async def test_messages_api(auth_client, session: AsyncSession, chats: Chats) -> None:  # type: ignore[no-untyped-def]
    await chats.setup(session)
    await chats.order(session, "A1")
    chats.say("A1", "hello?", at=NOW - timedelta(minutes=1))
    await chats.scan()
    base = f"/api/v1/stores/{chats.store_id}/messages"
    rows = (await auth_client.get(base)).json()
    assert len(rows) == 1 and rows[0]["needs_reply"] and rows[0]["last_buyer_text"] == "hello?"
    assert (await auth_client.get(f"{base}/summary")).json() == {"needs_reply": 1, "urgent": 0}
    handled = await auth_client.post(f"{base}/{rows[0]['id']}/handled")
    assert handled.json()["needs_reply"] is False and handled.json()["handled_by"]
    assert (await auth_client.get(base)).json() == []
    assert len((await auth_client.get(base, params={"show": "all"})).json()) == 1
    assert (await auth_client.post(f"{base}/scan")).status_code == 202


async def test_thread_state_is_kept(session: AsyncSession, chats: Chats) -> None:
    await chats.setup(session)
    await chats.order(session, "A1")
    chats.say("A1", "hello?", at=NOW - timedelta(minutes=1))
    await chats.scan()
    session.expire_all()
    thread = await session.scalar(select(ChatThread))
    assert thread is not None and thread.needs_reply and thread.last_alert_at is not None
    store = await session.get(Store, chats.store_id)
    assert store is not None


async def test_confirm_on_a_delivered_order_is_not_waiting(
    session: AsyncSession, chats: Chats
) -> None:
    """Seen on the server: CONFIRM replies to the old system, answered by its automatic message."""
    await chats.setup(session)
    await chats.order(session, "A1")  # delivered by the old system: no fulfilment here
    await chats.order(session, "A2")
    await chats.order(session, "A3")
    chats.say("A1", "confirm", at=NOW - timedelta(minutes=3))
    chats.say("A1", "✅ Delivery confirmed, enjoy!", at=NOW - timedelta(minutes=2), seller=True)
    chats.say("A2", "Thank you", at=NOW - timedelta(minutes=2))
    chats.say("A3", "the password is wrong", at=NOW - timedelta(minutes=2))
    await chats.scan()
    assert [a["order"] for a in await chats.alerts(session)] == ["A3"]
    session.expire_all()
    waiting = {t.conversation_id: t.needs_reply for t in await session.scalars(select(ChatThread))}
    assert waiting == {"conv-A1": False, "conv-A2": False, "conv-A3": True}


async def test_threads_of_closed_orders_are_settled(session: AsyncSession, chats: Chats) -> None:
    await chats.setup(session)
    await chats.order(session, "A1", state="paid")
    chats.say("A1", "si", at=NOW - timedelta(minutes=2))  # Spanish CONFIRM
    await chats.order(session, "A2", state="paid")
    chats.say("A2", "gracias", at=NOW - timedelta(minutes=2))  # thanks before delivery: waiting
    await chats.scan()
    session.expire_all()
    rows = {t.conversation_id: t for t in await session.scalars(select(ChatThread))}
    assert rows["conv-A1"].needs_reply is False and rows["conv-A2"].needs_reply is True
    a2_id = rows["conv-A2"].id
    # The order is completed afterwards (no longer scanned): a later scan settles the thread.
    for o in await session.scalars(select(Order).where(Order.remote_id.in_(("A1", "A2")))):
        o.state = "completed"
    await session.commit()
    await chats.scan(NOW + timedelta(minutes=5))
    session.expire_all()
    assert (await session.get(ChatThread, a2_id)).needs_reply is False  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("text", "closing"),
    [
        ("thanks bro", True),
        ("Np, Thanks brother", True),
        ("thx u man", True),
        ("thanks bro appreciate it❤️", True),
        ("ima check", True),
        ("lemme check pls", True),
        ("todo bien", True),
        ("Esta bien", True),
        ("cancle i figure it out", True),
        ("tysm", True),
        ("just i conecct twitch in epic?", False),
        ("Do i need to claim rewards on the twitch account?", False),
        ("the account does not work, I already linked it to my epic games", False),
        ("hello", False),
        ("ok but it says the account is banned now, can you help me with this", False),
    ],
)
def test_closing_messages(text: str, closing: bool) -> None:
    assert service.is_courtesy(text) is closing


def test_waiting_stops_after_a_day() -> None:
    now = utcnow()
    fresh = ChatThread(last_buyer_at=now - timedelta(hours=23), no_reply_needed=False)
    old = ChatThread(last_buyer_at=now - timedelta(hours=25), no_reply_needed=False)
    assert fresh.needs_reply and not old.needs_reply


async def test_mark_all_as_read(auth_client, session: AsyncSession, chats: Chats) -> None:  # type: ignore[no-untyped-def]
    await chats.setup(session)
    for ref in ("B1", "B2"):
        await chats.order(session, ref, state="paid")
        chats.say(ref, "is it ready?", at=NOW - timedelta(minutes=1))
    await chats.scan()
    base = f"/api/v1/stores/{chats.store_id}/messages"
    assert (await auth_client.get(f"{base}/summary")).json()["needs_reply"] == 2
    r = await auth_client.post(f"{base}/handled-all")
    assert r.json() == {"marked": 2}
    assert (await auth_client.get(f"{base}/summary")).json()["needs_reply"] == 0
