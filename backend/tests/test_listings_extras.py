"""Listings page extras: offer platform, orders waiting per listing, orders filtered by listing."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.base import OfferKind
from app.connectors.eldorado.parse import parse_offer, platform_of
from app.core.audit import Actor
from app.core.db import utcnow
from app.core.platform import Platform
from app.listings.models import Listing
from app.orders.models import Fulfilment, FulfilmentState, Order
from app.stores import service as stores

ACTOR = Actor.system("tests")


def test_platform_from_eldorado_offer() -> None:
    data = {
        "id": "o1",
        "offerTitle": "Rocket Pass",
        "offerState": "Active",
        "pricePerUnit": {"amount": 6.99, "currency": "USD"},
        "category": "TopUp",
        "tradeEnvironmentValues": [{"value": "PC"}, {"value": "PlayStation"}, {"value": "PC"}],
    }
    assert platform_of(data) == "PC, PlayStation"
    assert parse_offer(data, OfferKind.item).platform == "PC, PlayStation"
    assert platform_of({}) is None


async def test_orders_need_you_per_listing(
    auth_client, session: AsyncSession, platform: Platform
) -> None:  # type: ignore[no-untyped-def]
    store = await stores.create_store(session, platform, ACTOR, name="Zephyr", slug="zephyr")
    conn = await stores.add_connection(
        session, platform, ACTOR, store, connector_key="fake", credentials={"api_key": "test-key"}
    )
    await session.flush()
    listing = Listing(
        store_id=store.id,
        connection_id=conn.id,
        remote_offer_id="rp",
        kind="topup",
        title="Rocket Pass",
        remote_state="active",
        price_amount=Decimal("6.99"),
        currency="USD",
        platform="PC",
    )
    session.add(listing)
    await session.flush()
    for n, state in enumerate(
        [FulfilmentState.awaiting_owner, FulfilmentState.awaiting_owner, FulfilmentState.done]
    ):
        order = Order(
            store_id=store.id,
            connection_id=conn.id,
            listing_id=listing.id,
            remote_id=f"R{n}",
            state="paid",
            quantity=1,
            total_amount=Decimal("6.99"),
            remote_created_at=utcnow(),
        )
        session.add(order)
        await session.flush()
        session.add(
            Fulfilment(order_id=order.id, store_id=store.id, mode="topup", state=state, units=1)
        )
    await session.commit()

    row = (await auth_client.get(f"/api/v1/stores/{store.id}/listings")).json()[0]
    assert row["platform"] == "PC" and row["orders_need_you"] == 2
    only = (
        await auth_client.get(
            f"/api/v1/stores/{store.id}/orders",
            params={"show": "attention", "listing_id": row["id"]},
        )
    ).json()
    assert len(only) == 2
    none = await auth_client.get(f"/api/v1/stores/{store.id}/orders", params={"listing_id": 999999})
    assert none.json() == []
