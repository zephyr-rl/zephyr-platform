"""Marketplace connector contract and the core types every connector normalises into.

The core never imports platform-specific code: it talks to a ``MarketplaceConnector``.
"""

from __future__ import annotations

import enum
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


class Capability(enum.StrEnum):
    ORDERS = "orders"
    DELIVER = "deliver"
    CHAT = "chat"
    OFFERS = "offers"
    PRICE = "price"
    VAULT = "vault"
    REVIEWS = "reviews"
    CANCEL = "cancel"


class OrderState(enum.StrEnum):
    pending_payment = "pending_payment"
    paid = "paid"
    awaiting_delivery = "awaiting_delivery"
    delivered = "delivered"
    awaiting_confirmation = "awaiting_confirmation"
    completed = "completed"
    cancelled = "cancelled"
    refunded = "refunded"
    disputed = "disputed"
    unknown = "unknown"  # a state this connector does not know yet: never acted on


class OfferKind(enum.StrEnum):
    account = "account"  # instant delivery from the marketplace vault
    item = "item"  # delivered by the seller (chat)
    topup = "topup"  # fulfilled by hand


class OfferState(enum.StrEnum):
    active = "active"
    paused = "paused"
    closed = "closed"


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True)


class Money(_Model):
    amount: Decimal
    currency: str = "USD"


class ConnectionStatus(_Model):
    ok: bool
    account_name: str | None = None
    detail: str | None = None


class RemoteOrder(_Model):
    remote_id: str
    offer_id: str | None
    state: OrderState
    quantity: int = 1
    total: Money
    buyer_name: str | None = None
    buyer_id: str | None = None
    conversation_id: str | None = None
    created_at: datetime
    remote_state: str | None = None  # the marketplace's own word for the state
    offer_title: str | None = None
    category: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict, repr=False)


class RemoteOffer(_Model):
    remote_id: str
    title: str
    state: OfferState
    price: Money
    quantity: int | None = None
    kind: OfferKind = OfferKind.account
    raw: dict[str, Any] = Field(default_factory=dict, repr=False)


class DeliveryPayload(_Model):
    message: str
    items: list[str] = Field(default_factory=list, repr=False)


class RemoteResult(_Model):
    ok: bool
    remote_state: str | None = None
    detail: str | None = None
    dry_run: bool = False


class VaultEntry(_Model):
    """Credentials stored in a marketplace offer for instant delivery."""

    remote_id: str | None = None
    login: str
    password: str = Field(repr=False)
    email: str | None = None
    email_password: str | None = Field(None, repr=False)
    email_provider_url: str | None = None
    additional_info: str | None = None


class ChatMessage(_Model):
    remote_id: str
    conversation_id: str
    author_id: str | None
    from_seller: bool
    text: str
    sent_at: datetime


class Review(_Model):
    remote_id: str
    order_id: str | None
    rating: int
    text: str | None = None
    created_at: datetime


class ConnectorError(Exception):
    """A marketplace call failed. ``retryable`` says whether trying again may help."""

    def __init__(self, message: str, *, retryable: bool = True, status: int | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status = status


class CapabilityNotSupported(ConnectorError):
    def __init__(self, connector: str, capability: Capability) -> None:
        super().__init__(f"{connector} does not support {capability}", retryable=False)


@runtime_checkable
class MarketplaceConnector(Protocol):
    key: str
    capabilities: frozenset[Capability]

    async def test_credentials(self) -> ConnectionStatus: ...
    async def list_orders(self, since: datetime, states: set[OrderState]) -> list[RemoteOrder]: ...
    async def get_order(self, remote_id: str) -> RemoteOrder: ...
    async def mark_delivered(self, remote_id: str, payload: DeliveryPayload) -> RemoteResult: ...
    async def cancel_order(self, remote_id: str, reason: str) -> RemoteResult: ...
    async def list_offers(self) -> list[RemoteOffer]: ...
    async def set_offer_price(self, remote_id: str, price: Money) -> RemoteResult: ...
    async def set_offer_state(self, remote_id: str, state: OfferState) -> RemoteResult: ...
    async def vault_list(self, offer_id: str) -> list[VaultEntry]: ...
    async def vault_add(self, offer_id: str, entry: VaultEntry) -> RemoteResult: ...
    async def chat_list_messages(
        self, conversation_id: str, since: datetime
    ) -> list[ChatMessage]: ...
    async def chat_send(self, conversation_id: str, text: str) -> RemoteResult: ...
    async def list_reviews(self, since: datetime) -> list[Review]: ...
    async def aclose(self) -> None: ...


class ConnectorInfo(_Model):
    key: str
    name: str
    capabilities: frozenset[Capability]
    credential_fields: tuple[str, ...]


ConnectorFactory = Callable[[dict[str, Any]], MarketplaceConnector]


class ConnectorRegistry:
    def __init__(self) -> None:
        self._entries: dict[str, tuple[ConnectorInfo, ConnectorFactory]] = {}

    def register(self, info: ConnectorInfo, factory: ConnectorFactory) -> None:
        if info.key in self._entries:
            raise ValueError(f"Connector {info.key!r} already registered")
        self._entries[info.key] = (info, factory)

    def info(self, key: str) -> ConnectorInfo:
        try:
            return self._entries[key][0]
        except KeyError:
            raise KeyError(f"Unknown connector {key!r}") from None

    def create(self, key: str, credentials: dict[str, Any]) -> MarketplaceConnector:
        self.info(key)
        return self._entries[key][1](credentials)

    def all(self) -> list[ConnectorInfo]:
        return [info for info, _ in self._entries.values()]
