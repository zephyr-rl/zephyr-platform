"""Offer schedule: paused at one time every day, turned back on at another."""

from __future__ import annotations

from datetime import UTC, datetime, time

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.base import OfferState
from app.connectors.fake import FakeConnector, FakeMarketplace
from app.core.platform import Platform
from app.listings import schedule
from app.listings.models import Listing
from app.stores.models import Store
from tests.test_restock import ACTOR, OFFER, Vault


@pytest.fixture
def vault(platform: Platform) -> Vault:
    m = FakeMarketplace()
    platform.connectors.override("fake", lambda creds: FakeConnector(creds, m))
    return Vault(platform, m)


def test_wanted_window_inside_a_day_and_across_midnight() -> None:
    w = schedule.wanted
    assert w(time(1), time(9), time(0, 59)) == "on"
    assert w(time(1), time(9), time(1)) == "off"
    assert w(time(1), time(9), time(8, 59)) == "off"
    assert w(time(1), time(9), time(9)) == "on"
    assert w(time(23, 30), time(8), time(23, 30)) == "off"
    assert w(time(23, 30), time(8), time(3)) == "off"
    assert w(time(23, 30), time(8), time(8)) == "on"
    assert w(time(23, 30), time(8), time(12)) == "on"
    with pytest.raises(schedule.ScheduleError):
        w(time(1), time(1), time(1))


def at(hhmm: str) -> datetime:
    """A UTC instant whose Buenos Aires (UTC-3) time is hhmm on 2026-11-27."""
    h, m = map(int, hhmm.split(":"))
    return datetime(2026, 11, 27, (h + 3) % 24, m, tzinfo=UTC)


async def _setup(session: AsyncSession, vault: Vault, *, shadow: bool = False) -> Listing:
    await vault.setup(session, shadow=shadow)
    store = await session.get(Store, vault.store_id)
    assert store is not None
    store.timezone = "America/Argentina/Buenos_Aires"
    listing = await session.get(Listing, vault.listing_id)
    assert listing is not None
    await session.commit()
    return listing


async def _state(session: AsyncSession, vault: Vault) -> str:
    session.expire_all()
    listing = await session.get(Listing, vault.listing_id)
    assert listing is not None
    return listing.remote_state


async def test_pauses_and_turns_on_at_the_set_times(
    session: AsyncSession, platform: Platform, vault: Vault
) -> None:
    listing = await _setup(session, vault)
    schedule.set_schedule(session, ACTOR, listing, time(1), time(9))
    await session.commit()
    assert await schedule.run_store(platform, vault.store_id, at("23:00")) == 0  # on: nothing
    assert vault.market.offers[OFFER].state is OfferState.active
    assert await schedule.run_store(platform, vault.store_id, at("01:00")) == 1
    assert vault.market.offers[OFFER].state is OfferState.paused
    assert await _state(session, vault) == "paused"
    assert await schedule.run_store(platform, vault.store_id, at("05:00")) == 0  # once only
    assert await schedule.run_store(platform, vault.store_id, at("09:00")) == 1
    assert vault.market.offers[OFFER].state is OfferState.active


async def test_saved_inside_the_off_window_pauses_at_once(
    session: AsyncSession, platform: Platform, vault: Vault
) -> None:
    listing = await _setup(session, vault)
    schedule.set_schedule(session, ACTOR, listing, time(23, 30), time(8))
    await session.commit()
    assert await schedule.run_store(platform, vault.store_id, at("02:00")) == 1
    assert vault.market.offers[OFFER].state is OfferState.paused


async def test_a_change_by_hand_is_kept_until_the_next_time(
    session: AsyncSession, platform: Platform, vault: Vault
) -> None:
    listing = await _setup(session, vault)
    schedule.set_schedule(session, ACTOR, listing, time(1), time(9))
    await session.commit()
    await schedule.run_store(platform, vault.store_id, at("01:00"))
    # The owner turns it on at 03:00 by hand; the schedule does not pause it again.
    o = vault.market.offers[OFFER]
    vault.market.offers[OFFER] = o.model_copy(update={"state": OfferState.active})
    listing = await session.get(Listing, vault.listing_id)
    assert listing is not None
    listing.remote_state = "active"
    await session.commit()
    assert await schedule.run_store(platform, vault.store_id, at("04:00")) == 0
    assert vault.market.offers[OFFER].state is OfferState.active


async def test_shadow_mode_changes_nothing(
    session: AsyncSession, platform: Platform, vault: Vault
) -> None:
    listing = await _setup(session, vault, shadow=True)
    schedule.set_schedule(session, ACTOR, listing, time(1), time(9))
    await session.commit()
    assert await schedule.run_store(platform, vault.store_id, at("01:00")) == 1
    assert vault.market.offers[OFFER].state is OfferState.active
    assert await _state(session, vault) == "active"


async def test_api_set_and_remove(auth_client, session: AsyncSession, vault: Vault) -> None:  # type: ignore[no-untyped-def]
    await _setup(session, vault)
    url = f"/api/v1/stores/{vault.store_id}/listings/{vault.listing_id}/schedule"
    r = await auth_client.put(url, json={"off": "01:00", "on": "09:30"})
    assert r.status_code == 200, r.text
    assert (r.json()["schedule_off"], r.json()["schedule_on"]) == ("01:00", "09:30")
    assert (await auth_client.put(url, json={"off": "01:00"})).status_code == 409
    assert (await auth_client.put(url, json={"off": "01:00", "on": "01:00"})).status_code == 409
    r = await auth_client.put(url, json={})
    assert r.status_code == 200 and r.json()["schedule_off"] is None
