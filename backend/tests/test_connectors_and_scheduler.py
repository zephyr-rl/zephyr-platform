"""Fake connector behaviour, shadow mode (I9) and the scheduler."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.base import (
    ConnectorError,
    DeliveryPayload,
    MarketplaceConnector,
    Money,
    OfferState,
    OrderState,
    RemoteOffer,
)
from app.connectors.fake import FakeConnector, FakeMarketplace, make_order
from app.connectors.shadow import ShadowConnector, ShadowDecision
from app.core.audit import Actor
from app.core.db import utcnow
from app.core.jobs import Job
from app.core.modules import PeriodicTask
from app.core.platform import Platform
from app.stores import service as stores
from app.workers.scheduler import enqueue_due


def test_fake_connector_satisfies_the_protocol() -> None:
    assert isinstance(FakeConnector({}), MarketplaceConnector)


async def test_fake_marketplace_refuses_double_delivery() -> None:
    market = FakeMarketplace()
    market.add_order(make_order("A1"))
    connector = FakeConnector({"api_key": "test-key"}, market)
    result = await connector.mark_delivered("A1", DeliveryPayload(message="hi", items=["u:p"]))
    assert result.ok and result.remote_state == OrderState.delivered
    with pytest.raises(ConnectorError) as err:
        await connector.mark_delivered("A1", DeliveryPayload(message="hi", items=["u:p"]))
    assert err.value.status == 409 and err.value.retryable is False
    assert len(market.deliveries) == 1


async def test_fake_marketplace_can_script_failures() -> None:
    market = FakeMarketplace()
    market.add_order(make_order("A1"))
    market.fail_next("mark_delivered", ConnectorError("timeout", retryable=True))
    connector = FakeConnector({}, market)
    with pytest.raises(ConnectorError, match="timeout"):
        await connector.mark_delivered("A1", DeliveryPayload(message="x"))
    assert (await connector.get_order("A1")).state == OrderState.awaiting_delivery


async def test_shadow_mode_records_writes_and_changes_nothing() -> None:
    market = FakeMarketplace()
    market.add_order(make_order("A1"))
    market.add_offer(
        RemoteOffer(
            remote_id="o1", title="Rust bundle", state=OfferState.active, price=Money(amount="4.99")
        )  # type: ignore[arg-type]
    )
    decisions: list[ShadowDecision] = []

    async def record(decision: ShadowDecision) -> None:
        decisions.append(decision)

    shadow = ShadowConnector(FakeConnector({}, market), record)
    assert (await shadow.get_order("A1")).remote_id == "A1"  # reads still work
    result = await shadow.mark_delivered("A1", DeliveryPayload(message="m", items=["user:secret"]))
    await shadow.set_offer_price("o1", Money(amount="3.99"))  # type: ignore[arg-type]
    await shadow.chat_send("conv-A1", "hello")

    assert result.ok and result.dry_run
    assert [d.action for d in decisions] == ["deliver", "set_price", "chat_send"]
    assert "secret" not in repr(decisions)
    assert market.orders["A1"].state == OrderState.awaiting_delivery
    assert market.offers["o1"].price.amount == Money(amount="4.99").amount  # type: ignore[arg-type]
    assert market.deliveries == [] and market.messages["conv-A1"] == []


async def test_scheduler_enqueues_each_run_once_for_enabled_stores(
    session: AsyncSession, platform: Platform
) -> None:
    actor = Actor.system("tests")
    on = await stores.create_store(session, platform, actor, name="On", slug="on")
    off = await stores.create_store(session, platform, actor, name="Off", slug="off")
    await session.commit()
    for store in (on, off):
        await stores.add_connection(
            session, platform, actor, store, connector_key="fake", credentials={"api_key": "k"}
        )
    await stores.set_module(session, platform, actor, on.id, "restock", enabled=True)
    await session.commit()

    platform.module_context.periodic[:] = [
        PeriodicTask("restock-check", "restock.run", timedelta(minutes=10), module_key="restock")
    ]
    now = utcnow()
    assert await enqueue_due(platform, now) == 1
    assert await enqueue_due(platform, now) == 0  # same time bucket: nothing new
    assert await enqueue_due(platform, now + timedelta(minutes=10)) == 1
    store_ids = (await session.scalars(select(Job.store_id))).all()
    assert set(store_ids) == {on.id}
