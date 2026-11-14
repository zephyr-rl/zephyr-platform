"""Contract tests for the Eldorado connector. No network: see tests/eldorado_server.py."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from app.connectors.base import (
    ConnectorError,
    DeliveryPayload,
    MarketplaceConnector,
    Money,
    OfferKind,
    OfferState,
    OrderState,
    VaultEntry,
)
from app.connectors.eldorado.connector import EldoradoConnector
from app.connectors.eldorado.http import RateLimiter
from app.connectors.eldorado.parse import parse_order
from app.connectors.shadow import ShadowConnector, ShadowDecision
from tests.eldorado_server import CHAT_APP, CHAT_SELLER, EldoradoServer

CREDS = {"client_id": "client-1", "client_secret": "secret-1"}
LONG_AGO = datetime(2020, 1, 1, tzinfo=UTC)


@pytest.fixture
def server() -> EldoradoServer:
    return EldoradoServer()


@pytest.fixture
async def eldorado(server: EldoradoServer):
    connector = EldoradoConnector(
        CREDS, transport=server.transport, requests_per_second=0, retry_base_delay=0
    )
    yield connector
    await connector.aclose()


def test_satisfies_the_connector_protocol() -> None:
    assert isinstance(EldoradoConnector(CREDS), MarketplaceConnector)


def test_requires_credentials() -> None:
    with pytest.raises(ConnectorError, match="required"):
        EldoradoConnector({"client_id": "x"})


# ---- authentication ----------------------------------------------------------------------


async def test_token_is_requested_once_and_reused(eldorado, server) -> None:
    await eldorado.list_offers()
    await eldorado.list_orders(LONG_AGO, {OrderState.paid})
    assert len(server.calls("POST", "/api/authentication/seller/token")) == 1
    authorised = [r for r in server.requests if r.url.path != "/api/authentication/seller/token"]
    assert all(r.headers["authorization"] == "Bearer access-1" for r in authorised)


async def test_expired_token_is_refreshed_once_on_401(eldorado, server) -> None:
    server.script("GET", "/api/users/me", httpx.Response(401, json={"message": "expired"}))
    status = await eldorado.test_credentials()
    assert status.ok and status.account_name == "ZephyrStore"
    assert server.tokens_issued == 2  # forced by test_credentials, then refreshed after the 401
    assert len(server.calls("GET", "/api/users/me")) == 2


async def test_wrong_credentials_are_reported_not_raised(server) -> None:
    connector = EldoradoConnector(
        {"client_id": "client-1", "client_secret": "wrong"},
        transport=server.transport,
        requests_per_second=0,
    )
    status = await connector.test_credentials()
    assert not status.ok and "Invalid client credentials" in (status.detail or "")
    await connector.aclose()


# ---- orders ------------------------------------------------------------------------------


async def test_orders_are_mapped_and_queried_per_state(eldorado, server) -> None:
    orders = await eldorado.list_orders(LONG_AGO, {OrderState.paid, OrderState.completed})
    assert [(o.remote_id, o.state) for o in orders] == [
        ("order-received", OrderState.completed),
        ("order-paid-item", OrderState.paid),
    ]
    paid = orders[1]
    assert paid.quantity == 2 and paid.total == Money(amount=Decimal("9.98"), currency="USD")
    assert (paid.buyer_name, paid.conversation_id, paid.offer_id) == (
        "buyer_one",
        "conv-paid-item",
        "item-offer-1",
    )
    assert (paid.category, paid.remote_state) == ("CustomItem", "Paid")
    queried = sorted(
        r.url.params["orderState"] for r in server.calls("GET", "/api/orders/me/seller/orders")
    )
    assert queried == ["Completed", "Paid", "Received"]
    assert all(
        r.url.params["pageSize"] == "50"
        for r in server.calls("GET", "/api/orders/me/seller/orders")
    )


async def test_orders_before_since_are_left_out(eldorado) -> None:
    since = datetime(2026, 11, 13, 20, 0, tzinfo=UTC)
    orders = await eldorado.list_orders(since, set())
    assert [o.remote_id for o in orders] == ["order-paid-item"]


def test_unknown_states_are_never_mistaken_for_known_ones() -> None:
    order = parse_order(
        {"id": "x", "state": {"state": "Escrowed"}, "createdDate": "2026-11-13T00:00:00Z"}
    )
    assert order.state is OrderState.unknown and order.remote_state == "Escrowed"


async def test_get_order_only_returns_an_exact_match(eldorado, server) -> None:
    assert (await eldorado.get_order("order-delivered-account")).state is OrderState.delivered
    # The search is fuzzy and would return another order: the connector must not accept it.
    with pytest.raises(ConnectorError) as err:
        await eldorado.get_order("missing-order")
    assert err.value.status == 404 and not err.value.retryable


async def test_deliver_sends_an_empty_body(eldorado, server) -> None:
    result = await eldorado.mark_delivered(
        "order-paid-item", DeliveryPayload(message="m", items=["a:b"])
    )
    assert result.ok and result.remote_state == "Delivered"
    [put] = server.calls("PUT", "/api/orders/me/order-paid-item/deliver")
    assert json.loads(put.content) == {}


async def test_deliver_failure_rereads_and_accepts_an_order_already_delivered(
    eldorado, server
) -> None:
    """I4: a 500 after Eldorado stored the delivery must not be reported as a failure."""
    server.orders[0]["state"] = {"state": "Delivered"}
    server.script("PUT", "/api/orders/me/order-paid-item/deliver", httpx.Response(500))
    result = await eldorado.mark_delivered("order-paid-item", DeliveryPayload(message="m"))
    assert result.ok and result.detail == "already delivered"


async def test_deliver_is_never_retried_blindly(eldorado, server) -> None:
    server.script("PUT", "/api/orders/me/order-paid-item/deliver", httpx.Response(503))
    with pytest.raises(ConnectorError) as err:
        await eldorado.mark_delivered("order-paid-item", DeliveryPayload(message="m"))
    assert err.value.retryable  # still Paid: the job may try again later
    assert len(server.calls("PUT", "/api/orders/me/order-paid-item/deliver")) == 1


async def test_deliver_of_a_cancelled_order_is_not_retryable(eldorado, server) -> None:
    with pytest.raises(ConnectorError) as err:
        await eldorado.mark_delivered("order-canceled", DeliveryPayload(message="m"))
    assert not err.value.retryable and "Canceled" in str(err.value)


async def test_cancel_uses_a_valid_reason(eldorado, server) -> None:
    result = await eldorado.cancel_order("order-paid-item", "something else")
    assert result.ok
    [post] = server.calls("POST", "/api/orders/me/order-paid-item/cancel")
    assert json.loads(post.content) == {"reason": "Buyer_Does_Not_Meet_Criteria_For_Order"}


# ---- reads: retries and rate limit ---------------------------------------------------------


async def test_reads_are_retried_on_429_and_5xx(eldorado, server) -> None:
    server.script(
        "GET",
        "/api/flexibleOffers/me/search",
        httpx.Response(503),
        httpx.Response(429, headers={"retry-after": "0"}),
    )
    offers = await eldorado.list_offers()
    assert len(offers) == 4
    assert len(server.calls("GET", "/api/flexibleOffers/me/search")) == 3


async def test_reads_give_up_with_a_retryable_error(eldorado, server) -> None:
    server.script("GET", "/api/users/me", *[httpx.Response(502)] * 3)
    with pytest.raises(ConnectorError) as err:
        await eldorado.test_credentials()
    assert err.value.retryable and err.value.status == 502


async def test_rate_limiter_spaces_requests() -> None:
    limiter = RateLimiter(per_second=50)
    start = time.monotonic()
    for _ in range(6):
        await limiter.wait()
    assert time.monotonic() - start >= 0.09


# ---- offers ------------------------------------------------------------------------------


async def test_offers_of_every_kind(eldorado) -> None:
    offers = {o.remote_id: o for o in await eldorado.list_offers()}
    assert {k: (o.kind, o.state) for k, o in offers.items()} == {
        "acc-offer-1": (OfferKind.account, OfferState.active),
        "acc-offer-2": (OfferKind.account, OfferState.paused),
        "item-offer-1": (OfferKind.item, OfferState.active),
        "topup-1": (OfferKind.topup, OfferState.active),
    }
    assert offers["acc-offer-1"].quantity == 3
    assert offers["acc-offer-1"].price == Money(amount=Decimal("4.99"), currency="USD")


async def test_price_and_state_use_the_endpoint_of_each_kind(eldorado, server) -> None:
    await eldorado.set_offer_price("acc-offer-1", Money(amount=Decimal("5.49")))
    await eldorado.set_offer_price("item-offer-1", Money(amount=Decimal("3.00")))
    await eldorado.set_offer_state("acc-offer-2", OfferState.active)
    await eldorado.set_offer_state("item-offer-1", OfferState.paused)
    writes = [(r.method, r.url.path) for r in server.requests if r.method != "GET"][
        1:
    ]  # skip the token
    assert writes == [
        ("PUT", "/api/flexibleOffersUser/me/acc-offer-1/changePrice"),
        ("PUT", "/api/v1/item-management/me/offers/item-offer-1/price"),
        ("PUT", "/api/flexibleOffersUser/me/acc-offer-2/resume"),
        ("POST", "/api/v1/item-management/me/offers/item-offer-1/pause"),
    ]
    price_body = json.loads(
        server.calls("PUT", "/api/flexibleOffersUser/me/acc-offer-1/changePrice")[0].content
    )
    assert price_body == {"amount": 5.49, "currency": "USD"}
    with pytest.raises(ConnectorError, match="top-up"):
        await eldorado.set_offer_price("topup-1", Money(amount=Decimal(1)))


# ---- vault -------------------------------------------------------------------------------


async def test_vault_entries_are_parsed(eldorado) -> None:
    entries = await eldorado.vault_list("acc-offer-1")
    assert [(e.remote_id, e.login, e.email) for e in entries] == [
        ("vault-1", "VaultUser1", None),
        ("vault-2", "vaultuser2", "mail2@inbox.lv"),
    ]
    assert "vault-pass" not in repr(entries)


async def test_vault_add_payload(eldorado, server) -> None:
    entry = VaultEntry(
        login=" newuser ",
        password="pw",
        email="new@inbox.lv",
        email_password="mp",
        additional_info="Epic login",
    )
    result = await eldorado.vault_add("acc-offer-1", entry)
    assert result.ok
    body = json.loads(
        server.calls("POST", "/api/flexibleOffersUser/me/offers/acc-offer-1/secretDetails")[
            0
        ].content
    )
    assert body == {
        "structuredDetails": {
            "accountDetails": {"accountLogin": "newuser", "accountPassword": "pw"},
            "additionalInfo": "Epic login",
            "emailDetails": {
                "emailLogin": "new@inbox.lv",
                "emailPassword": "mp",
                "emailProviderUrl": "https://inbox.lv",
            },
        }
    }


async def test_vault_add_conflict_checks_whether_it_was_stored(eldorado, server) -> None:
    path = "/api/flexibleOffersUser/me/offers/acc-offer-1/secretDetails"
    server.script("POST", path, httpx.Response(409, json={"message": "Concurrency conflict"}))
    stored = await eldorado.vault_add("acc-offer-1", VaultEntry(login="vaultuser1", password="x"))
    assert stored.ok and stored.detail == "already in the vault"

    server.script("POST", path, httpx.Response(409))
    with pytest.raises(ConnectorError) as err:
        await eldorado.vault_add("acc-offer-1", VaultEntry(login="not-there", password="x"))
    assert err.value.retryable
    assert len(server.calls("POST", path)) == 2  # never re-posted automatically


# ---- chat and reviews ----------------------------------------------------------------------


async def test_chat_messages_and_sending(eldorado, server) -> None:
    messages = await eldorado.chat_list_messages("conv-1", LONG_AGO)
    assert [(m.remote_id, m.from_seller) for m in messages] == [("msg-1", False), ("msg-2", True)]
    await eldorado.chat_send("conv-1", "Here is your account")
    [post] = server.calls("POST", f"/v1/{CHAT_APP}/conversations/conv-1/messages")
    assert json.loads(post.content) == {"text": "Here is your account", "sender": CHAT_SELLER}
    assert len(server.calls("GET", "/api/conversations/me/authorize")) == 1  # chat token cached


async def test_chat_token_is_renewed_on_401(eldorado, server) -> None:
    server.script("GET", f"/v1/{CHAT_APP}/conversations/conv-1/messages", httpx.Response(401))
    assert len(await eldorado.chat_list_messages("conv-1", LONG_AGO)) == 2
    assert len(server.calls("GET", "/api/conversations/me/authorize")) == 2


async def test_reviews(eldorado) -> None:
    reviews = await eldorado.list_reviews(datetime(2026, 11, 10, tzinfo=UTC))
    assert [(r.order_id, r.rating) for r in reviews] == [("order-received", 5), ("order-old", 1)]
    assert reviews[0].text == "Fast delivery"


# ---- shadow mode (I9) ----------------------------------------------------------------------


async def test_shadow_mode_never_sends_a_write_to_eldorado(eldorado, server) -> None:
    decisions: list[ShadowDecision] = []

    async def record(decision: ShadowDecision) -> None:
        decisions.append(decision)

    shadow = ShadowConnector(eldorado, record)
    assert len(await shadow.list_offers()) == 4
    await shadow.mark_delivered("order-paid-item", DeliveryPayload(message="m", items=["u:p"]))
    await shadow.cancel_order("order-paid-item", "Out_Of_Stock")
    await shadow.set_offer_price("acc-offer-1", Money(amount=Decimal(1)))
    await shadow.set_offer_state("acc-offer-1", OfferState.paused)
    await shadow.vault_add("acc-offer-1", VaultEntry(login="u", password="p"))
    await shadow.chat_send("conv-1", "hello")

    writes = [
        r
        for r in server.requests
        if r.method != "GET" and r.url.path != "/api/authentication/seller/token"
    ]
    assert writes == []
    assert len(decisions) == 6
    assert server.orders[0]["state"] == {"state": "Paid"}


def test_recorder_redacts_secrets_consistently() -> None:
    from app.connectors.eldorado.record import redact

    data = {
        "results": [
            {"id": "o1", "buyerUsername": "alice", "totalPrice": {"amount": 1}},
            {"id": "o2", "buyerUsername": "alice"},
        ],
        "structuredDetails": {"accountDetails": {"accountLogin": "u1", "accountPassword": "p1"}},
    }
    out = redact(data)
    assert "alice" not in str(out) and "p1" not in str(out) and "u1" not in str(out)
    assert out["results"][0]["buyerUsername"] == out["results"][1]["buyerUsername"]
    assert out["results"][0]["totalPrice"] == {"amount": 1}
