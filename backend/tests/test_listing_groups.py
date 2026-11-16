"""Cross-platform cards: one pool for the PC / PlayStation / Xbox offers, and the legacy import."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.base import Money, OfferState, RemoteOffer
from app.connectors.fake import FakeConnector, FakeMarketplace
from app.core.audit import Actor
from app.core.platform import Platform
from app.legacy.restock import migrate_restock
from app.listings import groups
from app.listings.models import Listing, ListingGroup
from app.stock import service as stock
from app.stock.models import ItemState, StockItem, StockPool
from app.stores import service as stores
from app.stores.models import StoreModule

ACTOR = Actor.system("tests")
OFFERS = {"pc": "off-pc", "playstation": "off-ps", "xbox": "off-xb"}


@pytest.fixture
def market(platform: Platform) -> FakeMarketplace:
    m = FakeMarketplace()
    platform.connectors.override("fake", lambda creds: FakeConnector(creds, m))
    return m


async def _setup(
    session: AsyncSession, platform: Platform, market: FakeMarketplace, *, accounts: int = 8
) -> tuple[int, int, int, dict[str, int]]:
    store = await stores.create_store(session, platform, ACTOR, name="Zephyr", slug="zephyr")
    store.shadow_mode = False
    connection = await stores.add_connection(
        session, platform, ACTOR, store, connector_key="fake", credentials={"api_key": "test-key"}
    )
    await stores.set_module(session, platform, ACTOR, store.id, "restock", enabled=True)
    pool = await stock.create_pool(
        session, ACTOR, store.id, name="Neural 6 drops", low_stock_threshold=0
    )
    pool.legacy_id = "cat-neural"
    await session.commit()
    body = "\n".join(f"user{i}:pass{i}" for i in range(1, accounts + 1))
    await stock.commit_import(
        session, platform, ACTOR, store.id, pool.id, body, expected_sha256=stock.text_digest(body)
    )
    ids = {}
    for device, offer in OFFERS.items():
        listing = Listing(
            store_id=store.id,
            connection_id=connection.id,
            remote_offer_id=offer,
            kind="account",
            title=f"Neural Network & 6 cases [{device}]",
            remote_state="active",
            price_amount=Decimal("4.99"),
            currency="USD",
        )
        session.add(listing)
        await session.flush()
        ids[device] = listing.id
        market.add_offer(
            RemoteOffer(
                remote_id=offer,
                title=listing.title,
                state=OfferState.active,
                price=Money(amount=Decimal("4.99"), currency="USD"),
            )
        )
    await session.commit()
    return store.id, connection.id, pool.id, ids


async def test_card_fills_each_platform_from_one_pool(
    auth_client,
    session: AsyncSession,
    platform: Platform,
    market: FakeMarketplace,  # type: ignore[no-untyped-def]
) -> None:
    store_id, _, pool_id, ids = await _setup(session, platform, market)
    base = f"/api/v1/stores/{store_id}/listing-groups"
    created = await auth_client.post(
        base,
        json={
            "title": "Neural 6 drops",
            "pool_id": pool_id,
            "restock_enabled": True,
            "restock_target": 2,
            "vault_additional_info": "Check the Twitch connection first.",
            "devices": ids,
        },
    )
    assert created.status_code == 201, created.text
    card = created.json()
    assert [d["listing_id"] for d in card["devices"]] == [
        ids["pc"],
        ids["playstation"],
        ids["xbox"],
    ]
    assert card["pool_available"] == 8 and card["effective_target"] == 2

    # The members carry the card's settings and refuse changes of their own.
    session.expire_all()
    pc = await session.get(Listing, ids["pc"])
    assert pc is not None and pc.pool_id == pool_id and pc.restock_enabled and pc.device == "pc"
    assert pc.vault_additional_info == "Check the Twitch connection first."
    refused = await auth_client.patch(
        f"/api/v1/stores/{store_id}/listings/{ids['pc']}", json={"restock_target": 5}
    )
    assert refused.status_code == 409 and "cross-platform card" in refused.json()["detail"]

    # Restock now: 2 per platform, all from the same pool.
    result = await auth_client.post(f"{base}/{card['id']}/restock")
    assert result.status_code == 200, result.text
    assert [(r["label"], r["uploaded"]) for r in result.json()["results"]] == [
        ("PC", 2),
        ("PlayStation", 2),
        ("Xbox", 2),
    ]
    assert {o: len(v) for o, v in market.vault.items()} == {"off-pc": 2, "off-ps": 2, "off-xb": 2}
    logins = [e.login for v in market.vault.values() for e in v]
    assert len(set(logins)) == 6  # never the same account twice
    left = await session.scalar(
        select(func.count()).where(
            StockItem.pool_id == pool_id, StockItem.state == ItemState.available
        )
    )
    assert left == 2

    # Off: every platform stops; deleting releases the offers.
    off = await auth_client.post(f"{base}/{card['id']}/enabled", json={"enabled": False})
    assert off.json()["restock_enabled"] is False
    session.expire_all()
    assert not (await session.get(Listing, ids["xbox"])).restock_enabled  # type: ignore[union-attr]
    assert (await auth_client.delete(f"{base}/{card['id']}")).status_code == 204
    session.expire_all()
    xbox = await session.get(Listing, ids["xbox"])
    assert xbox is not None and xbox.group_id is None and xbox.device is None
    ok = await auth_client.patch(
        f"/api/v1/stores/{store_id}/listings/{ids['xbox']}", json={"restock_target": 5}
    )
    assert ok.status_code == 200


async def test_card_validation(
    auth_client, session: AsyncSession, platform: Platform, market: FakeMarketplace
) -> None:  # type: ignore[no-untyped-def]
    store_id, _, pool_id, ids = await _setup(session, platform, market, accounts=1)
    base = f"/api/v1/stores/{store_id}/listing-groups"
    same = await auth_client.post(
        base, json={"title": "X", "devices": {"pc": ids["pc"], "xbox": ids["pc"]}}
    )
    assert same.status_code == 409
    no_pool = await auth_client.post(
        base, json={"title": "X", "restock_enabled": True, "devices": {"pc": ids["pc"]}}
    )
    assert no_pool.status_code == 409
    first = await auth_client.post(
        base, json={"title": "A", "pool_id": pool_id, "devices": {"pc": ids["pc"]}}
    )
    assert first.status_code == 201
    taken = await auth_client.post(
        base, json={"title": "B", "pool_id": pool_id, "devices": {"pc": ids["pc"]}}
    )
    assert taken.status_code == 409 and "already in the card" in taken.json()["detail"]


async def test_legacy_restock_import(
    tmp_path: Path, session: AsyncSession, platform: Platform, market: FakeMarketplace
) -> None:
    store_id, connection_id, pool_id, ids = await _setup(session, platform, market)
    # The Xbox offer is not synced yet.
    xbox = await session.get(Listing, ids["xbox"])
    await session.delete(xbox)
    await session.commit()
    config = {
        "enabled": True,
        "checkInterval": 45,
        "targetStock": 1,
        "offerMappings": {
            "manual-1": {"enabled": False, "categoryId": "", "categoryName": "None (Manual Only)"},
        },
        "crossPlatformGroups": {
            "neural-bundle": {
                "title": "🔥Black Neural Network & 6 Cases",
                "categoryId": "cat-neural",
                "categoryName": "Neural 6 drops",
                "targetStock": 5,
                "enabled": True,
                "additionalInfo": "BEFORE LINKING, CHECK THE TWITCH CONNECTION",
                "devices": {
                    "pc": {"offerId": "off-pc"},
                    "playstation": {"offerId": "off-ps"},
                    "xbox": {"offerId": "off-xb"},
                },
            }
        },
        "additionalInfoPresets": {"epic": {"title": "Twitch", "text": "..."}},
    }
    (tmp_path / "restock_config.json").write_text(json.dumps(config), encoding="utf-8")

    dry = await migrate_restock(
        session, platform, ACTOR, folder=tmp_path, store_id=store_id, dry_run=True
    )
    assert dry.cards_created == ["🔥Black Neural Network & 6 Cases"]
    assert await session.scalar(select(func.count()).select_from(ListingGroup)) == 0

    report = await migrate_restock(session, platform, ACTOR, folder=tmp_path, store_id=store_id)
    assert report.waiting_offers == ["🔥Black Neural Network & 6 Cases / Xbox"]
    assert report.manual_only == 1 and report.presets_skipped == 1
    assert report.module == "on, every 1 min"
    card = await session.scalar(select(ListingGroup))
    assert card is not None and card.pool_id == pool_id and card.restock_target == 5
    assert card.devices == {"pc": "off-pc", "playstation": "off-ps", "xbox": "off-xb"}
    module = await session.scalar(select(StoreModule).where(StoreModule.module_key == "restock"))
    assert module is not None and module.settings["interval_minutes"] == 1

    # "Read offers from marketplace" brings the Xbox offer: the card attaches it.
    session.add(
        Listing(
            store_id=store_id,
            connection_id=connection_id,
            remote_offer_id="off-xb",
            kind="account",
            title="Neural [xbox]",
            remote_state="active",
            price_amount=Decimal("4.99"),
            currency="USD",
        )
    )
    await session.flush()
    assert await groups.link_pending(session, ACTOR, store_id) == 1
    await session.commit()
    new_xbox = await session.scalar(select(Listing).where(Listing.remote_offer_id == "off-xb"))
    assert new_xbox is not None and new_xbox.device == "xbox" and new_xbox.restock_target == 5

    # Running it again updates the same card.
    again = await migrate_restock(session, platform, ACTOR, folder=tmp_path, store_id=store_id)
    assert again.cards_updated == ["🔥Black Neural Network & 6 Cases"] and not again.waiting_offers
    assert await session.scalar(select(func.count()).select_from(ListingGroup)) == 1
    assert await session.scalar(select(func.count()).select_from(StockPool)) == 1


async def test_module_flags_and_deliveries_always_on(
    auth_client, session: AsyncSession, platform: Platform, market: FakeMarketplace
) -> None:  # type: ignore[no-untyped-def]
    store_id, *_ = await _setup(session, platform, market, accounts=1)
    mods = {
        m["key"]: m for m in (await auth_client.get(f"/api/v1/stores/{store_id}/modules")).json()
    }
    assert "account_check" not in mods
    assert mods["deliveries"]["always_on"] and mods["restock"]["managed_in"] == "listings"
    assert mods["topups"]["managed_in"] == "listings" and mods["reports"]["managed_in"] is None
    assert mods["deliveries"]["settings_schema"]["properties"]["auto_deliver"]["ui"] == "hidden"
    off = await auth_client.put(
        f"/api/v1/stores/{store_id}/modules/deliveries", json={"enabled": False}
    )
    assert off.status_code == 409 and "Automatic deliveries" in off.json()["detail"]
    hold = await auth_client.put(
        f"/api/v1/stores/{store_id}/modules/deliveries", json={"settings": {"auto_deliver": False}}
    )
    assert hold.status_code == 200 and hold.json()["settings"]["auto_deliver"] is False
