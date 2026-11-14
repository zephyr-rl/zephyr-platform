"""Top-ups: instructions are sent once when a top-up is paid; the platform never marks it done."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.base import ConnectorError, OrderState
from app.connectors.fake import FakeConnector, FakeMarketplace, make_order
from app.core import jobs
from app.core.audit import Actor
from app.core.platform import Platform
from app.listings.models import DeliveryMode, Listing
from app.modules.deliveries import service as deliveries
from app.orders.models import Fulfilment, FulfilmentState, Order
from app.stores import service as stores

ACTOR = Actor.system("tests")


@pytest.fixture
def market(platform: Platform) -> FakeMarketplace:
    m = FakeMarketplace()
    platform.connectors.override("fake", lambda creds: FakeConnector(creds, m))
    return m


async def setup(
    session: AsyncSession,
    platform: Platform,
    *,
    module: bool = True,
    settings: dict | None = None,  # type: ignore[type-arg]
    template: str | None = None,
    shadow: bool = False,
) -> int:
    store = await stores.create_store(session, platform, ACTOR, name="ZephyrStore", slug="zephyr")
    store.shadow_mode = shadow
    connection = await stores.add_connection(
        session, platform, ACTOR, store, connector_key="fake", credentials={"api_key": "test-key"}
    )
    if module or settings:
        await stores.set_module(
            session, platform, ACTOR, store.id, "topups", enabled=module, settings=settings or {}
        )
    session.add(
        Listing(
            store_id=store.id,
            connection_id=connection.id,
            remote_offer_id="rp-pc",
            kind="topup",
            title="Rocket Pass Premium (PC)",
            remote_state="active",
            price_amount=Decimal("6.99"),
            delivery_mode=DeliveryMode.manual,
            delivery_template=template,
        )
    )
    await session.commit()
    return store.id


def sell(market: FakeMarketplace, remote_id: str = "T1", offer: str = "rp-pc") -> None:
    order = make_order(remote_id, offer_id=offer, state=OrderState.paid)
    market.add_order(
        order.model_copy(update={"buyer_name": "NightOwl92", "offer_title": "Rocket Pass Premium"})
    )


async def run(platform: Platform, store_id: int) -> None:
    await deliveries.poll_store(platform, store_id)
    while await jobs.run_one(
        platform.db.sessionmaker, platform.jobs, "t", services={"platform": platform}
    ):
        pass


async def fulfilment(session: AsyncSession, remote_id: str = "T1") -> Fulfilment:
    session.expire_all()
    f = await session.scalar(
        select(Fulfilment)
        .join(Order, Order.id == Fulfilment.order_id)
        .where(Order.remote_id == remote_id)
    )
    assert f is not None
    return f


def sent(market: FakeMarketplace, remote_id: str = "T1") -> list[str]:
    return [m.text for m in market.messages[f"conv-{remote_id}"] if m.from_seller]


async def test_instructions_then_waiting_for_the_owner(
    session: AsyncSession, platform: Platform, market: FakeMarketplace
) -> None:
    store_id = await setup(session, platform)
    sell(market)
    await run(platform, store_id)
    [text] = sent(market)
    assert text.startswith("Hi NightOwl92! Thank you for your purchase.")
    f = await fulfilment(session)
    assert f.mode == "topup" and f.state is FulfilmentState.awaiting_owner
    assert not [c for c in market.calls if c[0] == "mark_delivered"]  # never marked by the platform

    # The owner does the top-up and marks it delivered on the marketplace.
    market.orders["T1"] = market.orders["T1"].model_copy(update={"state": OrderState.delivered})
    await run(platform, store_id)
    assert (await fulfilment(session)).state is FulfilmentState.done
    assert len(sent(market)) == 1


async def test_message_priority_listing_then_offer_then_default(
    session: AsyncSession, platform: Platform, market: FakeMarketplace
) -> None:
    store_id = await setup(
        session,
        platform,
        settings={
            "offer_messages": {"rp-pc": "Offer message for {buyer}: {product} x{quantity} #{order}"}
        },
    )
    sell(market)
    await run(platform, store_id)
    assert sent(market) == ["Offer message for NightOwl92: Rocket Pass Premium x1 #T1"]


async def test_listing_message_wins(
    session: AsyncSession, platform: Platform, market: FakeMarketplace
) -> None:
    store_id = await setup(
        session,
        platform,
        template="Listing message, {buyer}",
        settings={"offer_messages": {"rp-pc": "x"}},
    )
    sell(market)
    await run(platform, store_id)
    assert sent(market) == ["Listing message, NightOwl92"]


@pytest.mark.parametrize("case", ["module_off", "offer_skipped"])
async def test_nothing_is_sent_when_off(
    session: AsyncSession, platform: Platform, market: FakeMarketplace, case: str
) -> None:
    if case == "module_off":
        store_id = await setup(session, platform, module=False)
    else:
        store_id = await setup(session, platform, settings={"skip_offers": ["rp-pc"]})
    sell(market)
    await run(platform, store_id)
    assert sent(market) == []
    assert (await fulfilment(session)).state is FulfilmentState.manual


async def test_shadow_mode_sends_nothing(
    session: AsyncSession, platform: Platform, market: FakeMarketplace
) -> None:
    store_id = await setup(session, platform, shadow=True)
    sell(market)
    await run(platform, store_id)
    assert sent(market) == []
    f = await fulfilment(session)
    assert f.state is FulfilmentState.simulated


async def test_lost_answer_is_not_sent_twice(
    session: AsyncSession, platform: Platform, market: FakeMarketplace
) -> None:
    store_id = await setup(session, platform)
    sell(market)
    original = FakeConnector.chat_send

    async def send_then_fail(self: FakeConnector, conversation_id: str, text: str):  # type: ignore[no-untyped-def]
        await original(self, conversation_id, text)
        raise ConnectorError("timeout", retryable=True)

    FakeConnector.chat_send = send_then_fail  # type: ignore[method-assign]
    try:
        await deliveries.poll_store(platform, store_id)
        while await jobs.run_one(
            platform.db.sessionmaker, platform.jobs, "t", services={"platform": platform}
        ):
            pass
    finally:
        FakeConnector.chat_send = original  # type: ignore[method-assign]
    f = await fulfilment(session)
    assert f.attempts == 1 and f.state is FulfilmentState.pending
    await deliveries.process(platform, f.id)  # the retry reads the chat first
    assert len(sent(market)) == 1
    assert (await fulfilment(session)).state is FulfilmentState.awaiting_owner


async def test_buyer_cancels_before_the_top_up(
    session: AsyncSession, platform: Platform, market: FakeMarketplace
) -> None:
    store_id = await setup(session, platform)
    sell(market)
    await run(platform, store_id)
    market.orders["T1"] = market.orders["T1"].model_copy(update={"state": OrderState.cancelled})
    await run(platform, store_id)
    assert (await fulfilment(session)).state is FulfilmentState.cancelled


async def test_top_ups_show_in_needs_you(
    auth_client, session: AsyncSession, platform: Platform, market: FakeMarketplace
) -> None:  # type: ignore[no-untyped-def]
    store_id = await setup(session, platform)
    sell(market)
    await run(platform, store_id)
    rows = (
        await auth_client.get(f"/api/v1/stores/{store_id}/orders", params={"show": "attention"})
    ).json()
    assert [r["fulfilment_state"] for r in rows] == ["awaiting_owner"]
    assert (await auth_client.get(f"/api/v1/stores/{store_id}/orders/summary")).json()[
        "attention"
    ] == 1
