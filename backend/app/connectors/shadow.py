"""Shadow (dry-run) mode, invariant I9.

``ShadowConnector`` wraps a real connector: reads go through untouched, every write is replaced by
a recorded "would do" decision and a successful ``RemoteResult(dry_run=True)``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.connectors.base import (
    ChatMessage,
    ConnectionStatus,
    DeliveryPayload,
    MarketplaceConnector,
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


@dataclass(frozen=True)
class ShadowDecision:
    connector: str
    action: str
    target: str
    details: dict[str, Any]
    at: datetime


Recorder = Callable[[ShadowDecision], Awaitable[None]]


class ShadowConnector:
    def __init__(self, inner: MarketplaceConnector, recorder: Recorder) -> None:
        self._inner = inner
        self._record = recorder
        self.key = inner.key
        self.capabilities = inner.capabilities

    async def _would(self, action: str, target: str, **details: Any) -> RemoteResult:
        await self._record(ShadowDecision(self.key, action, target, details, utcnow()))
        return RemoteResult(ok=True, dry_run=True, detail=f"shadow: would {action}")

    # Reads pass through.
    async def test_credentials(self) -> ConnectionStatus:
        return await self._inner.test_credentials()

    async def list_orders(self, since: datetime, states: set[OrderState]) -> list[RemoteOrder]:
        return await self._inner.list_orders(since, states)

    async def get_order(self, remote_id: str) -> RemoteOrder:
        return await self._inner.get_order(remote_id)

    async def list_offers(self) -> list[RemoteOffer]:
        return await self._inner.list_offers()

    async def vault_list(self, offer_id: str) -> list[VaultEntry]:
        return await self._inner.vault_list(offer_id)

    async def chat_list_messages(self, conversation_id: str, since: datetime) -> list[ChatMessage]:
        return await self._inner.chat_list_messages(conversation_id, since)

    async def list_reviews(self, since: datetime) -> list[Review]:
        return await self._inner.list_reviews(since)

    # Writes are recorded, never sent. Secrets (delivered items, vault content) are not recorded.
    async def mark_delivered(self, remote_id: str, payload: DeliveryPayload) -> RemoteResult:
        return await self._would("deliver", remote_id, items=len(payload.items))

    async def cancel_order(self, remote_id: str, reason: str) -> RemoteResult:
        return await self._would("cancel", remote_id, reason=reason)

    async def set_offer_price(self, remote_id: str, price: Money) -> RemoteResult:
        return await self._would(
            "set_price", remote_id, amount=str(price.amount), currency=price.currency
        )

    async def set_offer_state(self, remote_id: str, state: OfferState) -> RemoteResult:
        return await self._would("set_offer_state", remote_id, state=str(state))

    async def vault_add(self, offer_id: str, entry: VaultEntry) -> RemoteResult:
        return await self._would("vault_add", offer_id)

    async def chat_send(self, conversation_id: str, text: str) -> RemoteResult:
        return await self._would("chat_send", conversation_id, length=len(text))

    async def aclose(self) -> None:
        await self._inner.aclose()
