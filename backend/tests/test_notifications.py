"""Notifications: templates, rules, Discord delivery, retries, shadow mode, instant sales."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.base import OrderState
from app.connectors.fake import FakeConnector, FakeMarketplace, make_order
from app.core import events, jobs
from app.core.audit import Actor
from app.core.db import utcnow
from app.core.events import OutboxEvent
from app.core.platform import Platform
from app.listings.models import DeliveryMode, Listing
from app.modules.deliveries import service as deliveries
from app.notifications import catalog, discord, render, service
from app.notifications.models import DeliveryState, NotificationChannel, NotificationDelivery
from app.stock import service as stock
from app.stores import service as stores
from app.stores.models import Store

ACTOR = Actor.system("tests")
URL = "https://discord.com/api/webhooks/123456789012345678/" + "T0ken-" * 12
OTHER_URL = "https://discord.com/api/webhooks/876543210987654321/" + "x" * 68


class DiscordStub:
    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []
        self.responses: list[httpx.Response] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.responses:
            return self.responses.pop(0)
        self.posts.append({"url": str(request.url), "body": json.loads(request.content)})
        return httpx.Response(200, json={"id": "1"})

    @property
    def embeds(self) -> list[dict[str, Any]]:
        return [p["body"]["embeds"][0] for p in self.posts]


@pytest.fixture
def stub() -> Iterator[DiscordStub]:
    s = DiscordStub()
    discord.transport = httpx.MockTransport(s.handler)
    yield s
    discord.transport = None


@pytest.fixture
def market(platform: Platform) -> FakeMarketplace:
    m = FakeMarketplace()
    platform.connectors.override("fake", lambda creds: FakeConnector(creds, m))
    return m


async def make_store(session: AsyncSession, platform: Platform, *, shadow: bool = False) -> Store:
    store = await stores.create_store(session, platform, ACTOR, name="ZephyrStore", slug="zephyr")
    store.shadow_mode = shadow
    await session.commit()
    return store


async def make_channel(session: AsyncSession, platform: Platform, **kw: Any) -> NotificationChannel:
    channel = await service.create_channel(
        session, platform, ACTOR, name=kw.pop("name", "Sales"), url=kw.pop("url", URL), **kw
    )
    await session.commit()
    return channel


async def add_rule(
    session: AsyncSession, channel: NotificationChannel, preset: str = "sale", **kw: Any
) -> int:
    p = catalog.PRESETS_BY_KEY[preset]
    rule = await service.create_rule(
        session,
        ACTOR,
        name=p.name,
        event_type=p.event_type,
        channel_id=channel.id,
        store_id=kw.get("store_id"),
        template=p.template,
        filters=kw.get("filters"),
    )
    await session.commit()
    return rule.id


async def flush(platform: Platform) -> None:
    """Dispatch pending events, then run the queued jobs."""
    await platform.events.dispatch_pending(platform.db.sessionmaker)
    while await jobs.run_one(
        platform.db.sessionmaker, platform.jobs, "t", services={"platform": platform}
    ):
        await platform.events.dispatch_pending(platform.db.sessionmaker)


async def publish(
    session: AsyncSession, event_type: str, payload: dict[str, Any], store_id: int | None
) -> None:
    events.publish(session, event_type, payload, store_id=store_id)
    await session.commit()


async def deliveries_rows(session: AsyncSession) -> list[NotificationDelivery]:
    session.expire_all()
    return list(
        await session.scalars(select(NotificationDelivery).order_by(NotificationDelivery.id))
    )


# ---- templates -----------------------------------------------------------------------------


def test_every_preset_is_valid_for_its_event() -> None:
    for preset in catalog.PRESETS:
        embed = service.preview(preset.event_type, preset.template)
        assert embed.get("title")
        assert not re.search(r"\{[a-z_]+\}", json.dumps(embed)), preset.key  # all have samples


def test_render_fills_drops_empty_fields_and_respects_limits() -> None:
    template = render.Template(
        title="Sale {short_order}",
        description="{message}",
        fields=[
            render.TemplateField(name="Buyer", value="**{buyer}**"),
            render.TemplateField(name="Bundle", value="`{bundle}`"),
            render.TemplateField(name="Typo", value="{buyerr}"),
        ],
    )
    values = {"short_order": "8f3c2a1e", "buyer": "Ann", "bundle": "", "message": "x" * 5000}
    embed = render.render(template, values)
    assert embed["title"] == "Sale 8f3c2a1e"
    assert len(embed["description"]) == 4096 and embed["description"].endswith("…")
    names = [f["name"] for f in embed["fields"]]
    assert names == ["Buyer", "Typo"]  # empty bundle left out; a typo stays visible
    assert embed["fields"][1]["value"] == "{buyerr}"


def test_unknown_variables_are_refused() -> None:
    template = {"title": "Sale by {buyer_name}", "fields": []}
    with pytest.raises(service.NotificationError, match=r"\{buyer_name\}"):
        service.check_template("order.delivered", template)
    with pytest.raises(service.NotificationError, match="Unknown event"):
        service.check_template("order.teleported", {"title": "x"})


def test_webhook_urls_are_checked() -> None:
    assert discord.check_url(URL + " ") == URL
    for bad in (
        "https://example.com/api/webhooks/1/2",
        "http://discord.com/api/webhooks/1/x",
        "hi",
    ):
        with pytest.raises(ValueError):
            discord.check_url(bad)
    assert discord.url_hint(URL).endswith("5678")


# ---- channels and delivery -----------------------------------------------------------------


async def test_channel_url_is_encrypted(session: AsyncSession, platform: Platform) -> None:
    channel = await make_channel(session, platform)
    assert "T0ken" not in channel.target_encrypted and "T0ken" not in channel.target_hint
    assert service.channel_url(platform, channel) == URL
    with pytest.raises(service.NotificationError, match="already exists"):
        await make_channel(session, platform)


async def test_event_becomes_one_discord_message(
    session: AsyncSession, platform: Platform, stub: DiscordStub
) -> None:
    store = await make_store(session, platform)
    channel = await make_channel(session, platform, sender_name="The seller")
    await add_rule(session, channel, "low_stock")
    await publish(session, "stock.low", {"pool": "Fortnite accounts", "available": 3}, store.id)
    await flush(platform)

    assert len(stub.posts) == 1
    post = stub.posts[0]
    assert post["url"].startswith(URL) and post["body"]["username"] == "The seller"
    assert post["body"]["allowed_mentions"] == {"parse": []}
    assert stub.embeds[0]["title"] == "📉 Fortnite accounts is running low"
    assert "Only **3** accounts left in ZephyrStore" in stub.embeds[0]["description"]
    [row] = await deliveries_rows(session)
    assert row.state is DeliveryState.sent and row.sent_at is not None


async def test_redispatched_event_is_not_sent_twice(
    session: AsyncSession, platform: Platform, stub: DiscordStub
) -> None:
    store = await make_store(session, platform)
    await add_rule(session, await make_channel(session, platform), "low_stock")
    await publish(session, "stock.low", {"pool": "P", "available": 1}, store.id)
    event_id = int(await session.scalar(select(OutboxEvent.id)) or 0)
    event = events.Event(event_id, "stock.low", store.id, {"pool": "P", "available": 1}, utcnow())
    assert await service.handle_event(platform, event) == 1
    assert await service.handle_event(platform, event) == 0  # same event again
    await flush(platform)
    assert len(stub.posts) == 1


async def test_rules_filter_by_store_and_listing(
    session: AsyncSession, platform: Platform, stub: DiscordStub
) -> None:
    store = await make_store(session, platform)
    other = await stores.create_store(session, platform, ACTOR, name="Kawai", slug="kawai")
    other.shadow_mode = False
    await session.commit()
    channel = await make_channel(session, platform)
    await add_rule(session, channel, "restocked", store_id=store.id, filters={"listing_ids": [7]})
    await publish(
        session, "offer.restocked", {"listing_id": 7, "uploaded": 1, "vault": 3}, other.id
    )
    await publish(
        session, "offer.restocked", {"listing_id": 8, "uploaded": 1, "vault": 3}, store.id
    )
    await publish(
        session, "offer.restocked", {"listing_id": 7, "uploaded": 2, "vault": 3}, store.id
    )
    await flush(platform)
    assert len(stub.posts) == 1 and "**2**" in stub.embeds[0]["description"]


async def test_shadow_stores_are_not_announced_unless_asked(
    session: AsyncSession, platform: Platform, stub: DiscordStub
) -> None:
    store = await make_store(session, platform, shadow=True)
    quiet = await make_channel(session, platform)
    loud = await make_channel(session, platform, name="Testing", url=OTHER_URL, include_shadow=True)
    await add_rule(session, quiet, "low_stock")
    await add_rule(session, loud, "low_stock")
    await publish(session, "stock.low", {"pool": "P", "available": 1}, store.id)
    await flush(platform)
    rows = await deliveries_rows(session)
    assert [r.state for r in rows] == [DeliveryState.skipped, DeliveryState.sent]
    assert rows[0].last_error == "Store in shadow mode."
    assert len(stub.posts) == 1 and stub.posts[0]["url"].startswith(OTHER_URL)


async def test_rate_limit_and_outage_are_retried(
    session: AsyncSession, platform: Platform, stub: DiscordStub
) -> None:
    store = await make_store(session, platform)
    await add_rule(session, await make_channel(session, platform), "low_stock")
    stub.responses = [httpx.Response(429, json={"retry_after": 2.5})]
    await publish(session, "stock.low", {"pool": "P", "available": 1}, store.id)
    await platform.events.dispatch_pending(platform.db.sessionmaker)
    [row] = await deliveries_rows(session)
    with pytest.raises(jobs.RetryLater) as raised:
        await service.send(platform, row.id)
    assert raised.value.delay == timedelta(seconds=2.5)
    stub.responses = [httpx.Response(502)]
    with pytest.raises(jobs.RetryLater):
        await service.send(platform, row.id)
    assert await service.send(platform, row.id) is DeliveryState.sent
    assert (await deliveries_rows(session))[0].attempts == 3


async def test_deleted_webhook_fails_and_is_reported(
    session: AsyncSession, platform: Platform, stub: DiscordStub
) -> None:
    store = await make_store(session, platform)
    channel = await make_channel(session, platform)
    await add_rule(session, channel, "low_stock")
    stub.responses = [httpx.Response(404, json={"message": "Unknown Webhook"})]
    await publish(session, "stock.low", {"pool": "P", "available": 1}, store.id)
    await flush(platform)
    [row] = await deliveries_rows(session)
    assert row.state is DeliveryState.failed and "deleted" in (row.last_error or "")
    await session.refresh(channel)
    assert channel.last_error and "404" in channel.last_error


async def test_disabled_channel_skips(
    session: AsyncSession, platform: Platform, stub: DiscordStub
) -> None:
    store = await make_store(session, platform)
    channel = await make_channel(session, platform)
    await add_rule(session, channel, "low_stock")
    await service.update_channel(session, platform, ACTOR, channel, {"enabled": False})
    await session.commit()
    await publish(session, "stock.low", {"pool": "P", "available": 1}, store.id)
    await flush(platform)
    assert stub.posts == [] and (await deliveries_rows(session))[0].state is DeliveryState.skipped


# ---- orders end to end ---------------------------------------------------------------------


async def _shop(session: AsyncSession, platform: Platform) -> tuple[Store, int, int]:
    store = await make_store(session, platform)
    connection = await stores.add_connection(
        session, platform, ACTOR, store, connector_key="fake", credentials={"api_key": "test-key"}
    )
    pool = await stock.create_pool(
        session, ACTOR, store.id, name="Twitch drops", low_stock_threshold=0
    )
    await session.commit()
    body = "\n".join(f"drop{i}:pass{i}" for i in range(1, 6))
    await stock.commit_import(
        session, platform, ACTOR, store.id, pool.id, body, expected_sha256=stock.text_digest(body)
    )
    session.add_all(
        [
            Listing(
                store_id=store.id,
                connection_id=connection.id,
                remote_offer_id="offer-1",
                kind="item",
                title="Twitch drops",
                remote_state="active",
                price_amount=Decimal("4.99"),
                pool_id=pool.id,
                delivery_mode=DeliveryMode.auto,
            ),
            Listing(
                store_id=store.id,
                connection_id=connection.id,
                remote_offer_id="acc-1",
                kind="account",
                title="Fortnite account",
                remote_state="active",
                price_amount=Decimal("9.99"),
                pool_id=pool.id,
                delivery_mode=DeliveryMode.manual,
                restock_enabled=True,
            ),
        ]
    )
    await session.commit()
    return store, connection.id, pool.id


async def test_sale_delivered_message_has_the_details(
    session: AsyncSession, platform: Platform, stub: DiscordStub, market: FakeMarketplace
) -> None:
    store, _, _ = await _shop(session, platform)
    await add_rule(session, await make_channel(session, platform), "sale")
    order = make_order(
        "8f3c2a1e-0000-4000-8000-000000000001", state=OrderState.paid, amount="9.98", quantity=2
    )
    market.add_order(order.model_copy(update={"buyer_name": "NightOwl92"}))
    await deliveries.poll_store(platform, store.id)
    await flush(platform)

    [embed] = stub.embeds
    assert embed["title"] == "🛒 New sale delivered!"
    fields = {f["name"]: f["value"] for f in embed["fields"]}
    assert fields["👤 Buyer"] == "**NightOwl92**"
    assert fields["💰 Price"] == "**$9.98 USD**"
    assert fields["⚡ Delivery"] == "Automatic (chat)"
    assert fields["🔢 Accounts"].startswith("**2**")
    assert fields["📊 Stock left"] == "**3** accounts"
    assert "pass1" not in json.dumps(stub.posts)  # never the credentials


async def test_instant_sales_are_announced_once_and_only_when_recent(
    session: AsyncSession, platform: Platform, stub: DiscordStub, market: FakeMarketplace
) -> None:
    store, _, _ = await _shop(session, platform)
    await add_rule(session, await make_channel(session, platform), "sale")
    market.add_order(make_order("NEW-1", offer_id="acc-1", state=OrderState.delivered))
    old = make_order("OLD-1", offer_id="acc-1", state=OrderState.delivered)
    market.add_order(old.model_copy(update={"created_at": utcnow() - timedelta(hours=5)}))
    await deliveries.poll_store(platform, store.id)
    await deliveries.poll_store(platform, store.id)
    await flush(platform)
    [embed] = stub.embeds
    fields = {f["name"]: f["value"] for f in embed["fields"]}
    assert fields["⚡ Delivery"] == "Instant (vault)" and "#NEW-1" in fields["🆔 Order"]


# ---- API -----------------------------------------------------------------------------------


async def test_notifications_api(
    auth_client, session: AsyncSession, platform: Platform, stub: DiscordStub
) -> None:  # type: ignore[no-untyped-def]
    store = await make_store(session, platform)
    cat = (await auth_client.get("/api/v1/notifications/catalog")).json()
    assert {e["type"] for e in cat["events"]} >= {"order.delivered", "report.daily_ready"}

    bad = await auth_client.post(
        "/api/v1/notifications/channels", json={"name": "S", "url": "nope"}
    )
    assert bad.status_code == 422
    made = await auth_client.post(
        "/api/v1/notifications/channels",
        json={"name": "Sales", "url": URL, "sender_name": "Seller"},
    )
    assert made.status_code == 201 and "T0ken" not in made.text
    channel = made.json()

    tested = await auth_client.post(f"/api/v1/notifications/channels/{channel['id']}/test")
    assert tested.json() == {"ok": True, "error": None}
    assert stub.embeds[-1]["title"] == "✅ Test message"

    preset = cat["presets"][0]
    preview = await auth_client.post(
        "/api/v1/notifications/preview",
        json={"event_type": preset["event_type"], "template": preset["template"]},
    )
    assert preview.status_code == 200 and preview.json()["title"]

    wrong = await auth_client.post(
        "/api/v1/notifications/rules",
        json={
            "event_type": "order.delivered",
            "channel_id": channel["id"],
            "template": {"title": "{nope}"},
        },
    )
    assert wrong.status_code == 422 and "{nope}" in wrong.json()["detail"]
    rule = (
        await auth_client.post(
            "/api/v1/notifications/rules",
            json={
                "event_type": preset["event_type"],
                "channel_id": channel["id"],
                "store_id": store.id,
                "template": preset["template"],
            },
        )
    ).json()
    assert rule["name"] == "Sale delivered"
    test = await auth_client.post(f"/api/v1/notifications/rules/{rule['id']}/test")
    assert test.json()["ok"] and stub.embeds[-1]["title"].startswith("[Test]")

    patched = await auth_client.patch(
        f"/api/v1/notifications/rules/{rule['id']}", json={"enabled": False, "all_stores": True}
    )
    assert patched.json()["enabled"] is False and patched.json()["store_id"] is None
    in_use = await auth_client.delete(f"/api/v1/notifications/channels/{channel['id']}")
    assert in_use.status_code == 422
    log = (await auth_client.get("/api/v1/notifications/deliveries")).json()
    assert [d["state"] for d in log] == ["sent", "sent"] and "T0ken" not in str(log)
    assert (
        await auth_client.delete(f"/api/v1/notifications/rules/{rule['id']}")
    ).status_code == 204
    assert (
        await auth_client.delete(f"/api/v1/notifications/channels/{channel['id']}")
    ).status_code == 204


async def test_failed_message_can_be_retried(
    auth_client, session: AsyncSession, platform: Platform, stub: DiscordStub
) -> None:  # type: ignore[no-untyped-def]
    store = await make_store(session, platform)
    await add_rule(session, await make_channel(session, platform), "low_stock")
    stub.responses = [httpx.Response(400, json={"message": "Invalid Form Body"})]
    await publish(session, "stock.low", {"pool": "P", "available": 1}, store.id)
    await flush(platform)
    [row] = await deliveries_rows(session)
    assert row.state is DeliveryState.failed
    assert (
        await auth_client.post(f"/api/v1/notifications/deliveries/{row.id}/retry")
    ).status_code == 202
    await flush(platform)
    assert (await deliveries_rows(session))[0].state is DeliveryState.sent
