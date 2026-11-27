"""Restock: vault top-up, unknown outcomes, duplicates, concurrency, shadow mode (I1, I9)."""

from __future__ import annotations

import asyncio
import re
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.base import (
    ConnectorError,
    Money,
    OfferKind,
    OfferState,
    OrderState,
    RemoteOffer,
    VaultEntry,
)
from app.connectors.fake import FakeConnector, FakeMarketplace, make_order
from app.core import jobs
from app.core.audit import Actor, AuditLog
from app.core.db import utcnow
from app.core.events import OutboxEvent
from app.core.platform import Platform
from app.listings import service as listings
from app.listings.models import DeliveryMode, Listing, VaultUpload, VaultUploadState
from app.modules.deliveries import service as deliveries
from app.modules.restock import service
from app.orders.models import Order
from app.stock import service as stock
from app.stock.models import StockItem
from app.stores import service as stores

ACTOR = Actor.system("tests")
OFFER = "acc-1"


class Vault:
    def __init__(self, platform: Platform, market: FakeMarketplace) -> None:
        self.platform, self.market = platform, market
        self.store_id = self.pool_id = self.listing_id = 0

    async def setup(
        self,
        session: AsyncSession,
        *,
        accounts: int = 5,
        target: int | None = 3,
        shadow: bool = False,
        remote_state: str = "active",
        email: bool = False,
    ) -> Vault:
        store = await stores.create_store(session, self.platform, ACTOR, name="Kawai", slug="kawai")
        store.shadow_mode = shadow
        connection = await stores.add_connection(
            session,
            self.platform,
            ACTOR,
            store,
            connector_key="fake",
            credentials={"api_key": "test-key"},
        )
        await stores.set_module(session, self.platform, ACTOR, store.id, "restock", enabled=True)
        pool = await stock.create_pool(
            session, ACTOR, store.id, name="Fortnite", low_stock_threshold=0
        )
        await session.commit()
        if accounts:
            lines = [
                f"user{i}:pass{i}:mail{i}@example.com:mailpass{i}" if email else f"user{i}:pass{i}"
                for i in range(1, accounts + 1)
            ]
            body = "\n".join(lines)
            await stock.commit_import(
                session,
                self.platform,
                ACTOR,
                store.id,
                pool.id,
                body,
                expected_sha256=stock.text_digest(body),
            )
        listing = Listing(
            store_id=store.id,
            connection_id=connection.id,
            remote_offer_id=OFFER,
            kind="account",
            title="Fortnite account",
            remote_state=remote_state,
            price_amount=Decimal("9.99"),
            currency="USD",
            pool_id=pool.id,
            delivery_mode=DeliveryMode.manual,
            restock_enabled=True,
            restock_target=target,
            include_email_details=email,
            vault_additional_info="Change the password after login.",
        )
        session.add(listing)
        await session.commit()
        self.market.add_offer(
            RemoteOffer(
                remote_id=OFFER,
                title="Fortnite account",
                state=OfferState(remote_state),
                price=Money(amount=Decimal("9.99")),
                kind=OfferKind.account,
            )
        )
        self.store_id, self.pool_id, self.listing_id = store.id, pool.id, listing.id
        return self

    def logins(self) -> list[str]:
        return [e.login for e in self.market.vault[OFFER]]

    async def run(self) -> service.ListingResult:
        return await service.restock_listing(self.platform, self.listing_id)

    async def states(self, session: AsyncSession) -> dict[str, int]:
        session.expire_all()
        rows = await session.execute(
            select(StockItem.state, func.count())
            .where(StockItem.pool_id == self.pool_id)
            .group_by(StockItem.state)
        )
        return {s.value: n for s, n in rows}

    async def uploads(self, session: AsyncSession) -> list[VaultUpload]:
        session.expire_all()
        return list(await session.scalars(select(VaultUpload).order_by(VaultUpload.id)))


@pytest.fixture
def market(platform: Platform) -> FakeMarketplace:
    m = FakeMarketplace()
    platform.connectors.override("fake", lambda creds: FakeConnector(creds, m))
    return m


@pytest.fixture
def vault(platform: Platform, market: FakeMarketplace) -> Vault:
    return Vault(platform, market)


async def test_tops_up_the_vault_to_the_target(session: AsyncSession, vault: Vault) -> None:
    await vault.setup(session)
    result = await vault.run()
    assert result.uploaded == 3 and result.vault_after == 3
    assert vault.logins() == ["user1", "user2", "user3"]
    assert vault.market.vault[OFFER][0].additional_info == "Change the password after login."
    assert vault.market.vault[OFFER][0].email is None
    assert await vault.states(session) == {"delivered": 3, "available": 2}
    assert [u.state for u in await vault.uploads(session)] == [VaultUploadState.uploaded] * 3

    listing = await session.get(Listing, vault.listing_id)
    assert listing is not None and listing.vault_count == 3
    assert "uploaded 3" in (listing.last_restock_note or "")
    event = await session.scalar(select(OutboxEvent).where(OutboxEvent.type == "offer.restocked"))
    assert event is not None and event.payload["uploaded"] == 3

    # Nothing to do the second time.
    again = await vault.run()
    assert again.uploaded == 0 and len(vault.logins()) == 3


async def test_refills_only_what_was_sold(session: AsyncSession, vault: Vault) -> None:
    await vault.setup(session)
    await vault.run()
    vault.market.vault[OFFER].pop(0)  # the marketplace delivered one to a buyer
    result = await vault.run()
    assert result.vault_before == 2 and result.uploaded == 1
    assert vault.logins() == ["user2", "user3", "user4"]


async def test_vault_read_error_uploads_nothing(session: AsyncSession, vault: Vault) -> None:
    """A failed read (429, 5xx, timeout, odd answer) is not an empty vault: nothing is uploaded."""
    await vault.setup(session, target=3)
    await vault.run()
    vault.market.fail_next("vault_list", ConnectorError("Eldorado HTTP 429", retryable=True))
    result = await vault.run()
    assert result.uploaded == 0 and len(vault.logins()) == 3
    assert "Marketplace error" in result.summary
    assert await vault.states(session) == {"delivered": 3, "available": 2}
    listing = await session.get(Listing, vault.listing_id)
    assert listing is not None and listing.vault_count == 3  # the last known count is kept


async def test_email_details_are_sent_only_when_asked(session: AsyncSession, vault: Vault) -> None:
    await vault.setup(session, email=True, target=1)
    await vault.run()
    entry = vault.market.vault[OFFER][0]
    assert entry.email == "mail1@example.com" and entry.email_password == "mailpass1"


async def test_listing_without_target_keeps_one(
    session: AsyncSession, vault: Vault, platform: Platform
) -> None:
    # There is no store default any more: a listing without its own target keeps 1.
    await vault.setup(session, target=None)
    result = await vault.run()
    assert result.uploaded == 1 and result.target == 1
    with pytest.raises(stores.StoreError):  # the old setting is refused
        await stores.set_module(
            session, platform, ACTOR, vault.store_id, "restock", settings={"target_quantity": 2}
        )


async def test_empty_pool_is_reported(session: AsyncSession, vault: Vault) -> None:
    await vault.setup(session, accounts=1)
    result = await vault.run()
    assert result.uploaded == 1
    assert "has no available accounts" in result.summary


async def test_account_already_in_the_vault_is_quarantined(
    session: AsyncSession, vault: Vault
) -> None:
    await vault.setup(session, target=2)
    vault.market.vault[OFFER].append(VaultEntry(remote_id="old", login="USER1", password="x"))
    result = await vault.run()
    assert result.quarantined == 1 and result.uploaded == 1
    assert vault.logins() == ["USER1", "user2"]
    assert await vault.states(session) == {"quarantined": 1, "delivered": 1, "available": 3}


async def test_refused_upload_returns_the_account(session: AsyncSession, vault: Vault) -> None:
    await vault.setup(session)
    vault.market.fail_next("vault_add", ConnectorError("bad request", retryable=False, status=400))
    result = await vault.run()
    assert result.uploaded == 0 and "Marketplace error" in result.summary
    assert await vault.states(session) == {"available": 5}
    [upload] = await vault.uploads(session)
    assert upload.state is VaultUploadState.failed and upload.error == "bad request"


async def test_unknown_outcome_stored_is_confirmed_next_pass(
    session: AsyncSession, vault: Vault
) -> None:
    await vault.setup(session, target=1)

    # The marketplace stores it, but the answer is lost (timeout).
    original = FakeConnector.vault_add

    async def store_then_fail(self: FakeConnector, offer_id: str, entry: VaultEntry):  # type: ignore[no-untyped-def]
        await original(self, offer_id, entry)
        raise ConnectorError("timeout", retryable=True)

    FakeConnector.vault_add = store_then_fail  # type: ignore[method-assign]
    try:
        first = await vault.run()
    finally:
        FakeConnector.vault_add = original  # type: ignore[method-assign]
    assert first.uploaded == 0
    assert await vault.states(session) == {"reserved": 1, "available": 4}
    [upload] = await vault.uploads(session)
    assert upload.state is VaultUploadState.pending

    second = await vault.run()
    assert second.resolved == 1 and second.uploaded == 0  # already at the target
    assert await vault.states(session) == {"delivered": 1, "available": 4}
    assert vault.logins() == ["user1"]  # not uploaded twice


async def test_unknown_outcome_missing_from_the_vault_is_quarantined(
    session: AsyncSession, vault: Vault
) -> None:
    await vault.setup(session, target=1)
    vault.market.fail_next("vault_add", ConnectorError("timeout", retryable=True))
    await vault.run()
    assert await vault.states(session) == {"reserved": 1, "available": 4}

    second = await vault.run()
    # Not in the vault: it may have been stored and sold, so it is never put back on sale.
    assert second.quarantined == 1 and second.uploaded == 1
    assert await vault.states(session) == {"quarantined": 1, "delivered": 1, "available": 3}
    assert vault.logins() == ["user2"]


async def test_paused_offer_is_resumed_after_restocking(
    session: AsyncSession, vault: Vault
) -> None:
    await vault.setup(session, remote_state="paused")
    result = await vault.run()
    assert result.resumed
    assert vault.market.offers[OFFER].state is OfferState.active
    session.expire_all()
    listing = await session.get(Listing, vault.listing_id)
    assert listing is not None and listing.remote_state == "active"
    assert await session.scalar(select(AuditLog).where(AuditLog.action == "listing.resumed"))


async def test_paused_offer_with_a_full_vault_stays_paused(
    session: AsyncSession, vault: Vault
) -> None:
    await vault.setup(session, remote_state="paused", target=1)
    vault.market.vault[OFFER].append(VaultEntry(login="someone", password="x"))
    result = await vault.run()
    assert not result.resumed and vault.market.offers[OFFER].state is OfferState.paused


async def test_paused_offer_with_accounts_below_target_is_left_alone(
    session: AsyncSession, vault: Vault
) -> None:
    """Paused by the owner (it still has accounts): neither topped up nor resumed."""
    await vault.setup(session, remote_state="paused", target=5)
    vault.market.vault[OFFER].append(VaultEntry(login="someone", password="x"))
    result = await vault.run()
    assert result.uploaded == 0 and not result.resumed
    assert vault.market.offers[OFFER].state is OfferState.paused
    assert len(vault.market.vault[OFFER]) == 1
    assert "left as it is" in result.summary


async def test_shadow_mode_records_and_touches_no_stock(
    session: AsyncSession, vault: Vault
) -> None:
    await vault.setup(session, shadow=True, remote_state="paused")
    result = await vault.run()
    assert result.shadow and result.uploaded == 3 and result.resumed
    assert vault.logins() == [] and vault.market.offers[OFFER].state is OfferState.paused
    assert await vault.states(session) == {"available": 5}
    assert await vault.uploads(session) == []
    actions = list(
        await session.scalars(select(AuditLog.action).where(AuditLog.action.like("shadow.%")))
    )
    assert actions.count("shadow.vault_add") == 3 and "shadow.set_offer_state" in actions
    assert "pass" not in str(await session.scalars(select(AuditLog.after)))


async def test_concurrent_passes_upload_once(session: AsyncSession, vault: Vault) -> None:
    await vault.setup(session)
    results = await asyncio.gather(*(vault.run() for _ in range(4)))
    assert sum(r.uploaded for r in results) == 3
    assert sorted(vault.logins()) == ["user1", "user2", "user3"]
    assert any(r.skipped for r in results)


async def test_restock_and_deliveries_never_share_an_account(
    session: AsyncSession, vault: Vault, platform: Platform
) -> None:
    await vault.setup(session, accounts=4, target=4)
    # A second listing delivering from the same pool by chat.
    other = Listing(
        store_id=vault.store_id,
        connection_id=(await session.get(Listing, vault.listing_id)).connection_id,  # type: ignore[union-attr]
        remote_offer_id="item-9",
        kind="item",
        title="Same accounts, chat delivery",
        remote_state="active",
        price_amount=Decimal("9.99"),
        pool_id=vault.pool_id,
        delivery_mode=DeliveryMode.auto,
    )
    session.add(other)
    await session.commit()
    vault.market.add_order(make_order("B1", offer_id="item-9", state=OrderState.paid, quantity=2))
    await deliveries.poll_store(platform, vault.store_id)

    async def deliver() -> None:
        while await jobs.run_one(
            platform.db.sessionmaker, platform.jobs, "w", services={"platform": platform}
        ):
            pass

    await asyncio.gather(vault.run(), deliver())
    assert await vault.states(session) == {"delivered": 4}
    sent = {m.text for m in vault.market.messages["conv-B1"] if m.from_seller}
    in_chat = {f"user{i}" for i in range(1, 5) if any(re.search(rf"\buser{i}\b", t) for t in sent)}
    assert in_chat.isdisjoint(vault.logins())
    assert len(in_chat) + len(vault.logins()) == 4


async def test_paid_orders_on_restocked_listings_are_left_to_the_marketplace(
    session: AsyncSession, vault: Vault, platform: Platform
) -> None:
    await vault.setup(session)
    vault.market.add_order(make_order("C1", offer_id=OFFER, state=OrderState.paid))
    result = await deliveries.poll_store(platform, vault.store_id)
    assert result.new_paid == 0
    assert await session.scalar(select(Order).where(Order.remote_id == "C1")) is not None


async def test_vault_uploads_are_history(session: AsyncSession, vault: Vault) -> None:
    await vault.setup(session, target=1)
    await vault.run()
    with pytest.raises(DBAPIError):
        await session.execute(text("DELETE FROM vault_uploads"))
    await session.rollback()
    with pytest.raises(DBAPIError):
        await session.execute(update(VaultUpload).values(state=VaultUploadState.failed))
    await session.rollback()


async def test_settings_validation(session: AsyncSession, vault: Vault) -> None:
    await vault.setup(session)
    listing = await session.get(Listing, vault.listing_id)
    assert listing is not None
    with pytest.raises(listings.ListingError, match="Manual"):
        await listings.update_listing(session, ACTOR, listing, {"delivery_mode": "auto"})
    with pytest.raises(listings.ListingError, match="stock pool"):
        await listings.update_listing(session, ACTOR, listing, {"pool_id": None})
    with pytest.raises(listings.ListingError, match="between 1 and 100"):
        await listings.update_listing(session, ACTOR, listing, {"restock_target": 0})
    await listings.update_listing(session, ACTOR, listing, {"restock_enabled": False})
    await session.commit()
    assert (await vault.run()).skipped == "Restock is off for this listing."


async def test_periodic_job_respects_the_interval(
    session: AsyncSession, vault: Vault, platform: Platform
) -> None:
    await vault.setup(session, target=1)
    assert len(await service.run_store(platform, vault.store_id)) == 1
    assert await service.run_store(platform, vault.store_id) == []  # not due yet
    await session.execute(update(Listing).values(last_restock_at=utcnow() - timedelta(minutes=2)))
    await session.commit()
    assert len(await service.run_store(platform, vault.store_id)) == 1


async def test_restock_api(auth_client, session: AsyncSession, vault: Vault) -> None:  # type: ignore[no-untyped-def]
    await vault.setup(session, target=2)
    base = f"/api/v1/stores/{vault.store_id}/listings"
    run = await auth_client.post(f"{base}/{vault.listing_id}/restock")
    assert run.status_code == 200, run.text
    assert run.json()["uploaded"] == 2 and run.json()["vault_count"] == 2

    rows = (await auth_client.get(f"{base}/{vault.listing_id}/uploads")).json()
    assert [r["state"] for r in rows] == ["uploaded", "uploaded"] and "pass" not in str(rows)

    listed = (await auth_client.get(base)).json()[0]
    assert listed["restock_enabled"] and listed["vault_count"] == 2

    patched = await auth_client.patch(
        f"{base}/{vault.listing_id}", json={"clear_restock_target": True, "auto_resume": False}
    )
    assert patched.status_code == 200
    assert patched.json()["restock_target"] is None and patched.json()["auto_resume"] is False

    bad = await auth_client.patch(f"{base}/{vault.listing_id}", json={"delivery_mode": "auto"})
    assert bad.status_code == 409

    await auth_client.patch(f"{base}/{vault.listing_id}", json={"restock_enabled": False})
    off = await auth_client.post(f"{base}/{vault.listing_id}/restock")
    assert off.status_code == 409
