"""Listings: read-only sync from Eldorado, fulfilment settings and shadow mode at run time."""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.base import DeliveryPayload
from app.connectors.eldorado.connector import EldoradoConnector
from app.connectors.runtime import open_connector
from app.core.audit import Actor, AuditLog
from app.core.platform import Platform
from app.listings import service
from app.listings.models import DeliveryMode, Listing
from app.stock import service as stock
from app.stores import service as stores
from app.stores.models import MarketplaceConnection, Store
from tests.eldorado_server import EldoradoServer

ACTOR = Actor.system("tests")
CREDS = {"client_id": "client-1", "client_secret": "secret-1"}


@pytest.fixture
def server(platform: Platform) -> EldoradoServer:
    srv = EldoradoServer()
    platform.connectors.override(
        "eldorado",
        lambda creds: EldoradoConnector(
            creds, transport=srv.transport, requests_per_second=0, retry_base_delay=0
        ),
    )
    return srv


async def _store_with_eldorado(
    session: AsyncSession, platform: Platform, slug: str = "zephyr"
) -> tuple[int, int]:
    store = await stores.create_store(session, platform, ACTOR, name=slug.title(), slug=slug)
    connection = await stores.add_connection(
        session, platform, ACTOR, store, connector_key="eldorado", credentials=CREDS
    )
    await session.commit()
    return store.id, connection.id


async def _listing(session: AsyncSession, remote_id: str) -> Listing:
    listing = await session.scalar(select(Listing).where(Listing.remote_offer_id == remote_id))
    assert listing is not None
    return listing


async def test_sync_creates_listings_as_manual_and_only_reads(
    session: AsyncSession, platform: Platform, server: EldoradoServer
) -> None:
    store_id, _ = await _store_with_eldorado(session, platform)
    result = await service.sync_listings(session, platform, ACTOR, store_id)
    await session.commit()
    assert (result.created, result.updated, result.removed, result.errors) == (4, 0, 0, [])
    listings = list(await session.scalars(select(Listing).order_by(Listing.remote_offer_id)))
    assert [(x.remote_offer_id, x.kind, x.remote_state) for x in listings] == [
        ("acc-offer-1", "account", "active"),
        ("acc-offer-2", "account", "paused"),
        ("item-offer-1", "item", "active"),
        ("topup-1", "topup", "active"),
    ]
    assert all(x.delivery_mode is DeliveryMode.manual and x.pool_id is None for x in listings)
    writes = [
        r
        for r in server.requests
        if r.method != "GET" and r.url.path != "/api/authentication/seller/token"
    ]
    assert writes == []


async def test_resync_updates_and_flags_removed_offers(
    session: AsyncSession, platform: Platform, server: EldoradoServer
) -> None:
    store_id, _ = await _store_with_eldorado(session, platform)
    await service.sync_listings(session, platform, ACTOR, store_id)
    await session.commit()
    server.script(
        "GET",
        "/api/flexibleOffers/me/search",
        httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": "acc-offer-1",
                        "offerTitle": "Renamed",
                        "offerState": "Paused",
                        "quantity": 0,
                        "pricePerUnit": {"amount": 5, "currency": "USD"},
                    }
                ],
                "recordCount": 1,
            },
        ),
    )
    result = await service.sync_listings(session, platform, ACTOR, store_id)
    await session.commit()
    assert (result.created, result.updated, result.removed) == (0, 3, 1)
    renamed = await _listing(session, "acc-offer-1")
    assert (renamed.title, renamed.remote_state, renamed.remote_quantity) == (
        "Renamed",
        "paused",
        0,
    )
    assert (await _listing(session, "acc-offer-2")).removed_remotely


async def test_sync_reports_connection_errors(
    session: AsyncSession, platform: Platform, server: EldoradoServer
) -> None:
    store_id, connection_id = await _store_with_eldorado(session, platform)
    server.script(
        "POST",
        "/api/authentication/seller/token",
        httpx.Response(401, json={"message": "Invalid client"}),
    )
    result = await service.sync_listings(session, platform, ACTOR, store_id)
    await session.commit()
    assert result.created == 0 and "Invalid client" in result.errors[0]
    connection = await session.get(MarketplaceConnection, connection_id)
    assert connection is not None and connection.status == "error"


async def test_fulfilment_settings_are_validated(
    session: AsyncSession, platform: Platform, server: EldoradoServer
) -> None:
    store_id, _ = await _store_with_eldorado(session, platform)
    other_store, _ = await _store_with_eldorado(session, platform, "other")
    await service.sync_listings(session, platform, ACTOR, store_id)
    pool = await stock.create_pool(session, ACTOR, store_id, name="RLCS x6")
    foreign_pool = await stock.create_pool(session, ACTOR, other_store, name="Elsewhere")
    await session.commit()
    account = await _listing(session, "acc-offer-1")
    topup = await _listing(session, "topup-1")

    with pytest.raises(service.ListingError, match="needs a stock pool"):
        await service.update_listing(session, ACTOR, account, {"delivery_mode": "auto"})
    with pytest.raises(service.ListingError, match="does not belong"):
        await service.update_listing(session, ACTOR, account, {"pool_id": foreign_pool.id})
    with pytest.raises(service.ListingError, match="Top-up"):
        await service.update_listing(
            session, ACTOR, topup, {"delivery_mode": "assisted", "pool_id": pool.id}
        )
    with pytest.raises(service.ListingError, match="message to send"):
        await service.update_listing(session, ACTOR, topup, {"delivery_mode": "text"})

    await service.update_listing(
        session,
        ACTOR,
        account,
        {
            "pool_id": pool.id,
            "delivery_mode": "auto",
            "units_per_sale": 2,
            "require_confirmation": True,
        },
    )
    await session.commit()
    entry = await session.scalar(select(AuditLog).where(AuditLog.action == "listing.updated"))
    assert entry is not None
    assert entry.before["delivery_mode"] == "manual" and entry.after["delivery_mode"] == "auto"
    assert entry.after["units_per_sale"] == 2


async def test_shadow_mode_is_applied_when_a_connector_is_opened(
    session: AsyncSession, platform: Platform, server: EldoradoServer
) -> None:
    store_id, connection_id = await _store_with_eldorado(session, platform)
    connection = await session.get(MarketplaceConnection, connection_id)
    assert connection is not None
    connector = await open_connector(session, platform, connection)
    result = await connector.mark_delivered(
        "order-paid-item", DeliveryPayload(message="m", items=["u:p"])
    )
    await connector.aclose()
    assert result.dry_run
    assert server.calls("PUT") == []
    entry = await session.scalar(select(AuditLog).where(AuditLog.action == "shadow.deliver"))
    assert entry is not None and entry.entity_id == "order-paid-item" and entry.store_id == store_id

    store = await session.get(Store, store_id)
    assert store is not None
    store.shadow_mode = False
    await session.commit()
    live = await open_connector(session, platform, connection)
    assert (
        await live.mark_delivered("order-paid-item", DeliveryPayload(message="m"))
    ).dry_run is False
    await live.aclose()
    assert len(server.calls("PUT")) == 1


async def test_listings_api(
    auth_client: httpx.AsyncClient, platform: Platform, server: EldoradoServer
) -> None:
    store = (
        await auth_client.post("/api/v1/stores", json={"name": "Zephyr", "slug": "zephyr"})
    ).json()
    base = f"/api/v1/stores/{store['id']}"
    assert (await auth_client.post(f"{base}/listings/sync")).status_code == 409  # no connection yet
    connection = await auth_client.post(
        f"{base}/connections", json={"connector": "eldorado", "credentials": CREDS}
    )
    assert connection.json()["status"] == "connected"
    assert "secret-1" not in connection.text

    synced = (await auth_client.post(f"{base}/listings/sync")).json()
    assert synced["created"] == 4
    listings = (await auth_client.get(f"{base}/listings")).json()
    item = next(x for x in listings if x["remote_offer_id"] == "item-offer-1")
    assert item["delivery_mode"] == "manual" and item["connector"] == "eldorado"

    pool = (await auth_client.post(f"{base}/stock/pools", json={"name": "Twitch drops"})).json()
    updated = await auth_client.patch(
        f"{base}/listings/{item['id']}",
        json={"pool_id": pool["id"], "delivery_mode": "assisted", "require_confirmation": True},
    )
    assert updated.status_code == 200
    assert (updated.json()["pool_name"], updated.json()["delivery_mode"]) == (
        "Twitch drops",
        "assisted",
    )
    bad = await auth_client.patch(f"{base}/listings/{item['id']}", json={"clear_pool": True})
    assert bad.status_code == 409 and "needs a stock pool" in bad.text
