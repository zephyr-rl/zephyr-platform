"""Importing the old Item Delivery setup (item_delivery_config.json)."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.base import Money, OfferKind, OfferState, RemoteOffer
from app.connectors.fake import FakeConnector, FakeMarketplace
from app.core.audit import Actor
from app.core.platform import Platform
from app.legacy.items import migrate_items
from app.listings.models import DeliveryMode, Listing
from app.stock import service as stock
from app.stores import service as stores
from app.stores.models import StoreModule

ACTOR = Actor.system("tests")
GUIDE = "📖 Follow this guide to link your twitch accounts:\n- https://example.com/guide"


@pytest.fixture
def market(platform: Platform) -> FakeMarketplace:
    m = FakeMarketplace()
    platform.connectors.override("fake", lambda creds: FakeConnector(creds, m))
    return m


def _offer(remote_id: str, title: str) -> RemoteOffer:
    return RemoteOffer(
        remote_id=remote_id,
        title=title,
        state=OfferState.active,
        price=Money(amount=Decimal("2.99"), currency="USD"),
        kind=OfferKind.item,
    )


def _config(tmp_path: Path, *, enabled: bool = True, confirm: bool = True) -> Path:
    cfg = {
        "enabled": enabled,
        "checkInterval": 60,
        "defaultGuideText": GUIDE,
        "dbPath": "account_manager/accounts.sqlite3",
        "twitchConfirmationFilter": {
            "enabled": confirm,
            "singleAccountTemplate": "Reply CONFIRM for 1 account",
            "multiAccountTemplate": "Reply CONFIRM for {quantity} accounts",
            "clarificationTemplate": "Please reply CONFIRM or CANCEL",
            "cancellationTemplate": "Order cancelled",
        },
        "offerMappings": {
            "off-1": {
                "categoryId": "cat-drops",
                "autoDeliver": True,
                "accountsPerUnit": 1,
                "customGuideText": GUIDE,
                "requireTwitchConfirmation": True,
            },
            "off-5": {
                "categoryId": "cat-drops",
                "autoDeliver": False,
                "accountsPerUnit": 5,
                "customGuideText": "Own guide",
                "requireTwitchConfirmation": False,
            },
            "off-tip": {
                "categoryId": "__tip__",
                "deliveryType": "tip",
                "customGuideText": "Thanks for the tip",
            },
            "off-nopool": {"categoryId": "cat-unknown", "autoDeliver": True},
            "off-gone": {"categoryId": "cat-drops", "autoDeliver": True},
        },
    }
    (tmp_path / "item_delivery_config.json").write_text(json.dumps(cfg), encoding="utf-8")
    return tmp_path


async def _store(
    session: AsyncSession, platform: Platform, market: FakeMarketplace
) -> tuple[int, int]:
    store = await stores.create_store(session, platform, ACTOR, name="Kawai", slug="kawai")
    await stores.add_connection(
        session, platform, ACTOR, store, connector_key="fake", credentials={"api_key": "k"}
    )
    pool = await stock.create_pool(
        session, ACTOR, store.id, name="Twitch drops", low_stock_threshold=0
    )
    pool.legacy_id = "cat-drops"
    await session.commit()
    for remote_id, title in (
        ("off-1", "Twitch account x1"),
        ("off-5", "Twitch accounts x5"),
        ("off-tip", "Tip"),
        ("off-nopool", "Something else"),
    ):
        market.add_offer(_offer(remote_id, title))
    return store.id, pool.id


async def test_items_import_maps_offers_and_messages(
    session: AsyncSession, platform: Platform, market: FakeMarketplace, tmp_path: Path
) -> None:
    store_id, pool_id = await _store(session, platform, market)
    report = await migrate_items(
        session, platform, ACTOR, folder=_config(tmp_path), store_id=store_id, sync_first=True
    )
    assert report.synced.startswith("4 new")
    assert report.not_synced == ["off-gone"]
    assert report.no_pool == ["Something else"]
    assert report.text_offers == ["Tip"]

    rows = {
        listing.remote_offer_id: listing
        for listing in await session.scalars(select(Listing).where(Listing.store_id == store_id))
    }
    one, five, tip, other = rows["off-1"], rows["off-5"], rows["off-tip"], rows["off-nopool"]
    assert (one.pool_id, one.delivery_mode, one.units_per_sale) == (pool_id, DeliveryMode.auto, 1)
    assert one.require_confirmation and one.delivery_template is None  # the store's message
    assert (five.delivery_mode, five.units_per_sale) == (DeliveryMode.assisted, 5)
    assert not five.require_confirmation
    assert five.delivery_template == "{items}\n\nOwn guide"
    assert (tip.delivery_mode, tip.delivery_template, tip.pool_id) == (
        DeliveryMode.text,
        "Thanks for the tip",
        None,
    )
    assert other.delivery_mode == DeliveryMode.manual  # untouched

    module = await session.scalar(
        select(StoreModule).where(
            StoreModule.store_id == store_id, StoreModule.module_key == "deliveries"
        )
    )
    assert module is not None
    s = module.settings
    assert s["message_template"] == "{items}\n\n" + GUIDE
    assert s["item_label"] == "Twitch Account"
    assert s["confirmation_multi"] == "Reply CONFIRM for {quantity} accounts"
    assert s["cancellation"] == "Order cancelled"
    assert s["auto_deliver"] is True
    assert "reply_languages" in s  # other settings kept

    # Running it again changes nothing.
    again = await migrate_items(session, platform, ACTOR, folder=tmp_path, store_id=store_id)
    assert len(again.offers_set) == 2


async def test_items_import_respects_the_old_switches(
    session: AsyncSession, platform: Platform, market: FakeMarketplace, tmp_path: Path
) -> None:
    store_id, _ = await _store(session, platform, market)
    folder = _config(tmp_path, enabled=False, confirm=False)
    report = await migrate_items(
        session, platform, ACTOR, folder=folder, store_id=store_id, sync_first=True, dry_run=True
    )
    assert not report.automatic and not report.confirmation
    assert (
        await session.scalar(select(Listing).where(Listing.store_id == store_id)) is None
    )  # dry run

    await migrate_items(session, platform, ACTOR, folder=folder, store_id=store_id, sync_first=True)
    one = await session.scalar(
        select(Listing).where(Listing.store_id == store_id, Listing.remote_offer_id == "off-1")
    )
    assert one is not None and not one.require_confirmation
    module = await session.scalar(
        select(StoreModule).where(
            StoreModule.store_id == store_id, StoreModule.module_key == "deliveries"
        )
    )
    assert module is not None and module.settings["auto_deliver"] is False
