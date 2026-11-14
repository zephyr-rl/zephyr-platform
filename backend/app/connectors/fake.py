"""In-memory marketplace used by tests and local development.

It behaves like a real marketplace for the parts the core relies on: delivering an order that is
not awaiting delivery fails, delivering twice fails, and failures can be scripted to simulate
timeouts and errors.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.connectors.base import (
    Capability,
    ChatMessage,
    ConnectionStatus,
    ConnectorError,
    ConnectorInfo,
    DeliveryPayload,
    Money,
    OfferState,
    OrderState,
    RemoteOffer,
    RemoteOrder,
    RemoteResult,
    Review,
    VaultEntry,
)
from app.core.db import utcnow

INFO = ConnectorInfo(
    key="fake",
    name="Fake marketplace (testing)",
    capabilities=frozenset(Capability),
    credential_fields=("api_key",),
)


@dataclass
class FakeMarketplace:
    """Shared state, so several connector instances can see the same 'remote' data."""

    orders: dict[str, RemoteOrder] = field(default_factory=dict)
    offers: dict[str, RemoteOffer] = field(default_factory=dict)
    vault: dict[str, list[VaultEntry]] = field(default_factory=lambda: defaultdict(list))
    messages: dict[str, list[ChatMessage]] = field(default_factory=lambda: defaultdict(list))
    reviews: list[Review] = field(default_factory=list)
    deliveries: list[tuple[str, DeliveryPayload]] = field(default_factory=list)
    calls: list[tuple[str, tuple[Any, ...]]] = field(default_factory=list)
    # method name -> list of exceptions to raise on the next calls (consumed in order)
    failures: dict[str, list[Exception]] = field(default_factory=lambda: defaultdict(list))
    valid_api_key: str = "test-key"

    def add_order(self, order: RemoteOrder) -> None:
        self.orders[order.remote_id] = order

    def add_offer(self, offer: RemoteOffer) -> None:
        self.offers[offer.remote_id] = offer

    def fail_next(self, method: str, error: Exception) -> None:
        self.failures[method].append(error)


class FakeConnector:
    key = INFO.key
    capabilities = INFO.capabilities

    def __init__(self, credentials: dict[str, Any], market: FakeMarketplace | None = None) -> None:
        self._credentials = credentials
        self.market = market or FakeMarketplace()

    def _enter(self, method: str, *args: Any) -> None:
        self.market.calls.append((method, args))
        pending = self.market.failures.get(method)
        if pending:
            raise pending.pop(0)

    async def test_credentials(self) -> ConnectionStatus:
        self._enter("test_credentials")
        if self._credentials.get("api_key") == self.market.valid_api_key:
            return ConnectionStatus(ok=True, account_name="fake-seller")
        return ConnectionStatus(ok=False, detail="Invalid API key")

    async def list_orders(self, since: datetime, states: set[OrderState]) -> list[RemoteOrder]:
        self._enter("list_orders", since, states)
        return [
            o
            for o in self.market.orders.values()
            if o.created_at >= since and (not states or o.state in states)
        ]

    async def get_order(self, remote_id: str) -> RemoteOrder:
        self._enter("get_order", remote_id)
        try:
            return self.market.orders[remote_id]
        except KeyError:
            raise ConnectorError(
                f"Order {remote_id} not found", retryable=False, status=404
            ) from None

    async def mark_delivered(self, remote_id: str, payload: DeliveryPayload) -> RemoteResult:
        self._enter("mark_delivered", remote_id)
        order = await self.get_order(remote_id)
        if order.state not in {OrderState.paid, OrderState.awaiting_delivery}:
            raise ConnectorError(
                f"Order {remote_id} is {order.state}, cannot deliver", retryable=False, status=409
            )
        self.market.orders[remote_id] = order.model_copy(update={"state": OrderState.delivered})
        self.market.deliveries.append((remote_id, payload))
        return RemoteResult(ok=True, remote_state=OrderState.delivered)

    async def cancel_order(self, remote_id: str, reason: str) -> RemoteResult:
        self._enter("cancel_order", remote_id, reason)
        order = await self.get_order(remote_id)
        self.market.orders[remote_id] = order.model_copy(update={"state": OrderState.cancelled})
        return RemoteResult(ok=True, remote_state=OrderState.cancelled)

    async def list_offers(self) -> list[RemoteOffer]:
        self._enter("list_offers")
        return list(self.market.offers.values())

    async def _update_offer(self, remote_id: str, **changes: Any) -> RemoteResult:
        try:
            offer = self.market.offers[remote_id]
        except KeyError:
            raise ConnectorError(
                f"Offer {remote_id} not found", retryable=False, status=404
            ) from None
        self.market.offers[remote_id] = offer.model_copy(update=changes)
        return RemoteResult(ok=True)

    async def set_offer_price(self, remote_id: str, price: Money) -> RemoteResult:
        self._enter("set_offer_price", remote_id, price)
        return await self._update_offer(remote_id, price=price)

    async def set_offer_state(self, remote_id: str, state: OfferState) -> RemoteResult:
        self._enter("set_offer_state", remote_id, state)
        return await self._update_offer(remote_id, state=state)

    async def vault_list(self, offer_id: str) -> list[VaultEntry]:
        self._enter("vault_list", offer_id)
        return list(self.market.vault[offer_id])

    async def vault_add(self, offer_id: str, entry: VaultEntry) -> RemoteResult:
        self._enter("vault_add", offer_id)
        stored = entry.model_copy(update={"remote_id": f"v{len(self.market.vault[offer_id]) + 1}"})
        self.market.vault[offer_id].append(stored)
        return RemoteResult(ok=True, remote_state=stored.remote_id)

    async def chat_list_messages(self, conversation_id: str, since: datetime) -> list[ChatMessage]:
        self._enter("chat_list_messages", conversation_id, since)
        return [m for m in self.market.messages[conversation_id] if m.sent_at >= since]

    async def chat_send(self, conversation_id: str, text: str) -> RemoteResult:
        self._enter("chat_send", conversation_id)
        msgs = self.market.messages[conversation_id]
        msgs.append(
            ChatMessage(
                remote_id=f"m{len(msgs) + 1}",
                conversation_id=conversation_id,
                author_id="seller",
                from_seller=True,
                text=text,
                sent_at=utcnow(),
            )
        )
        return RemoteResult(ok=True)

    async def list_reviews(self, since: datetime) -> list[Review]:
        self._enter("list_reviews", since)
        return [r for r in self.market.reviews if r.created_at >= since]

    async def aclose(self) -> None:
        return None


def make_order(
    remote_id: str,
    *,
    offer_id: str = "offer-1",
    state: OrderState = OrderState.awaiting_delivery,
    amount: str = "4.99",
    quantity: int = 1,
) -> RemoteOrder:
    return RemoteOrder(
        remote_id=remote_id,
        offer_id=offer_id,
        state=state,
        quantity=quantity,
        total=Money(amount=Decimal(amount)),
        buyer_name="buyer",
        conversation_id=f"conv-{remote_id}",
        created_at=utcnow(),
    )
