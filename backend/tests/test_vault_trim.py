"""Pull back extras: vaults trimmed down to a number, accounts back in the pool."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.base import ConnectorError, VaultEntry
from app.connectors.fake import FakeConnector, FakeMarketplace
from app.core.platform import Platform
from app.listings.models import Listing, ListingGroup
from app.modules.restock import trim
from app.stock import service as stock
from app.stock.models import ItemState, StockItem
from tests.test_restock import ACTOR, OFFER, Vault


@pytest.fixture
def market(platform: Platform) -> FakeMarketplace:
    m = FakeMarketplace()
    platform.connectors.override("fake", lambda creds: FakeConnector(creds, m))
    return m


async def _card(
    session: AsyncSession, platform: Platform, market: FakeMarketplace
) -> tuple[Vault, ListingGroup]:
    v = await Vault(platform, market).setup(session, accounts=5, target=1)
    await v.run()  # the platform uploads user1 ("delivered": it stays in the vault)
    # Two accounts the old system uploaded: in the platform as taken out by hand.
    items = list(await session.scalars(select(StockItem).where(StockItem.pool_id == v.pool_id)))
    for item in [i for i in items if i.state is ItemState.available][:2]:
        revealed = (await stock.reveal(session, platform, [item]))[0]
        item.state = ItemState.extracted
        market.vault[OFFER].append(
            VaultEntry(remote_id=f"k{item.id}", login=revealed.login, password=revealed.password)
        )
    listing = await session.get(Listing, v.listing_id)
    assert listing is not None
    group = ListingGroup(store_id=v.store_id, title="Card", pool_id=v.pool_id, restock_target=2)
    session.add(group)
    await session.flush()
    listing.group_id, listing.device = group.id, "playstation"
    # The old system uploaded 3 more the platform never saw.
    for i in range(10, 13):
        market.vault[OFFER].append(VaultEntry(remote_id=f"x{i}", login=f"old{i}", password="pw"))
    await session.commit()
    return v, group


async def _states(session: AsyncSession, pool_id: int) -> dict[str, int]:
    rows = await session.scalars(select(StockItem).where(StockItem.pool_id == pool_id))
    out: dict[str, int] = {}
    for item in rows:
        out[item.state.value] = out.get(item.state.value, 0) + 1
    return out


async def test_plan_changes_nothing(
    session: AsyncSession, platform: Platform, market: FakeMarketplace
) -> None:
    _, group = await _card(session, platform, market)
    result = await trim.trim(session, platform, ACTOR, group, {"playstation": 1}, apply=False)
    assert result.to_remove == 5
    assert result.platforms[0].known == 2  # accounts taken out by hand go first
    assert len(market.vault[OFFER]) == 6
    assert not any(c[0] == "vault_remove" for c in market.calls)


async def test_pulls_back_down_to_keep_and_returns_accounts(
    session: AsyncSession, platform: Platform, market: FakeMarketplace
) -> None:
    v, group = await _card(session, platform, market)
    before = await _states(session, v.pool_id)
    result = await trim.trim(session, platform, ACTOR, group, {"playstation": 1}, apply=True)
    p = result.platforms[0]
    assert (p.removed, p.returned, p.quarantined, p.errors) == (5, 5, 0, [])
    assert [e.login for e in market.vault[OFFER]] == ["user1"]  # the platform's own upload
    after = await _states(session, v.pool_id)
    assert after.get("available", 0) == before.get("available", 0) + 5
    # Known accounts came back as the same rows; the 3 unknown ones were added.
    assert after.get("extracted", 0) == before.get("extracted", 0) - 2
    assert sum(after.values()) == sum(before.values()) + 3


async def test_never_below_one(
    session: AsyncSession, platform: Platform, market: FakeMarketplace
) -> None:
    _, group = await _card(session, platform, market)
    with pytest.raises(trim.TrimError):
        await trim.trim(session, platform, ACTOR, group, {"playstation": 0}, apply=True)


async def test_unconfirmed_removal_goes_to_quarantine(
    session: AsyncSession, platform: Platform, market: FakeMarketplace
) -> None:
    v, group = await _card(session, platform, market)

    class Flaky(FakeConnector):
        async def vault_remove(self, offer_id: str, entry_id: str):  # type: ignore[no-untyped-def]
            await super().vault_remove(offer_id, entry_id)
            raise ConnectorError("timed out", retryable=True)

    platform.connectors.override("fake", lambda creds: Flaky(creds, market))
    result = await trim.trim(session, platform, ACTOR, group, {"playstation": 5}, apply=True)
    p = result.platforms[0]
    assert (p.removed, p.returned, p.quarantined) == (1, 0, 1)
    assert (await _states(session, v.pool_id)).get(ItemState.quarantined.value) == 1


async def test_api_plan_and_apply(
    auth_client, session: AsyncSession, platform: Platform, market: FakeMarketplace
) -> None:  # type: ignore[no-untyped-def]
    v, group = await _card(session, platform, market)
    url = f"/api/v1/stores/{v.store_id}/listing-groups/{group.id}/trim"
    r = await auth_client.post(url, json={"keep": {"playstation": 2}})
    assert r.status_code == 200, r.text
    assert r.json()["to_remove"] == 4 and r.json()["done"] is False
    r = await auth_client.post(url, json={"keep": {"playstation": 2}, "apply": True})
    assert r.status_code == 200 and r.json()["platforms"][0]["returned"] == 4
    assert len(market.vault[OFFER]) == 2
    r = await auth_client.post(url, json={"keep": {"playstation": 0}})
    assert r.status_code == 409


async def test_delete_that_does_nothing_returns_nothing(
    session: AsyncSession, platform: Platform, market: FakeMarketplace
) -> None:
    v, group = await _card(session, platform, market)

    class Ignores(FakeConnector):
        async def vault_remove(self, offer_id: str, entry_id: str):  # type: ignore[no-untyped-def]
            from app.connectors.base import RemoteResult

            return RemoteResult(ok=True)  # says OK, removes nothing

    platform.connectors.override("fake", lambda creds: Ignores(creds, market))
    before = await _states(session, v.pool_id)
    result = await trim.trim(session, platform, ACTOR, group, {"playstation": 1}, apply=True)
    p = result.platforms[0]
    assert (p.removed, p.returned) == (0, 0) and len(p.errors) == 5
    assert await _states(session, v.pool_id) == before
