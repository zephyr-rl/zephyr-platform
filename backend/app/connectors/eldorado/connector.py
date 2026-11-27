"""Eldorado.gg marketplace connector (seller API + TalkJS buyer chat)."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

import httpx

from app.connectors.base import (
    Capability,
    ChatMessage,
    ConnectionStatus,
    ConnectorError,
    ConnectorInfo,
    DeliveryPayload,
    Money,
    OfferKind,
    OfferState,
    OrderState,
    RemoteOffer,
    RemoteOrder,
    RemoteResult,
    Review,
    VaultEntry,
)
from app.connectors.eldorado import parse
from app.connectors.eldorado.http import DEFAULT_BASE_URL, EldoradoHttp

log = logging.getLogger(__name__)

INFO = ConnectorInfo(
    key="eldorado",
    name="Eldorado.gg",
    capabilities=frozenset(Capability),
    credential_fields=("client_id", "client_secret"),
)

PAGE_SIZE = 50  # the largest page the order endpoints return
MAX_PAGES = 20
ORDER_PAGES = 10  # up to 500 recent orders per state
DELIVERED_OR_LATER = {OrderState.delivered, OrderState.completed}


class EldoradoConnector:
    key = INFO.key
    capabilities = INFO.capabilities

    def __init__(
        self,
        credentials: dict[str, Any],
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        requests_per_second: float = 4.0,
        retry_base_delay: float = 0.5,
    ) -> None:
        self._http = EldoradoHttp(
            str(credentials.get("client_id") or "").strip(),
            str(credentials.get("client_secret") or "").strip(),
            base_url=str(credentials.get("base_url") or DEFAULT_BASE_URL),
            transport=transport,
            requests_per_second=requests_per_second,
            retry_base_delay=retry_base_delay,
        )
        self._offer_kinds: dict[str, OfferKind] = {}
        self._seller_id: str | None = None

    async def aclose(self) -> None:
        await self._http.aclose()

    # ---- account ---------------------------------------------------------------------

    async def _profile(self) -> dict[str, Any]:
        body = await self._http.request("GET", "api/users/me")
        return body if isinstance(body, dict) else {}

    async def _seller_user_id(self) -> str:
        if self._seller_id is None:
            profile = await self._profile()
            user_id = parse.first(profile, "id", "userId")
            if not user_id:
                raise ConnectorError("Eldorado did not return the seller id.", retryable=True)
            self._seller_id = str(user_id)
        return self._seller_id

    async def test_credentials(self) -> ConnectionStatus:
        try:
            await self._http.token(force=True)
            profile = await self._profile()
        except ConnectorError as exc:
            if exc.status in (400, 401, 403):
                return ConnectionStatus(ok=False, detail=str(exc))
            raise
        name = parse.first(profile, "username", "userName", "displayName", "name")
        return ConnectionStatus(ok=True, account_name=str(name) if name else None)

    # ---- orders ----------------------------------------------------------------------

    async def _orders(
        self, *, since: datetime | None = None, max_pages: int = ORDER_PAGES, **params: Any
    ) -> AsyncIterator[RemoteOrder]:
        """Seller orders, newest first, following the page cursor."""
        cursor: str | None = None
        for _ in range(max_pages):
            body = await self._http.request(
                "GET",
                "api/orders/me/seller/orders",
                params={"pageSize": PAGE_SIZE, "pageDirection": "Next", "cursor": cursor, **params},
            )
            page = [
                parse.parse_order(o) for o in parse.results(body) if parse.first(o, "id", "orderId")
            ]
            for order in page:
                yield order
            cursor = parse.next_cursor(body)
            if not cursor or not page or (since and min(o.created_at for o in page) < since):
                return

    async def list_orders(self, since: datetime, states: set[OrderState]) -> list[RemoteOrder]:
        """Orders created since ``since`` in the given states (all states when empty)."""
        names = sorted({n for state in states for n in parse.REMOTE_STATE_FILTER.get(state, [])})
        filters: list[str | None] = list(names) if names else [None]
        found: dict[str, RemoteOrder] = {}
        for remote_state in filters:
            async for order in self._orders(since=since, orderState=remote_state):
                if order.created_at >= since and (not states or order.state in states):
                    found.setdefault(order.remote_id, order)
        return sorted(found.values(), key=lambda o: o.created_at)

    async def get_order(self, remote_id: str) -> RemoteOrder:
        """One order by id. The seller API has no "get order" endpoint (it answers 403), so the
        order list is searched; only an exact id match is accepted, never "the first result"."""
        remote_id = remote_id.strip()
        async for order in self._orders(max_pages=1, query=remote_id):
            if order.remote_id == remote_id:
                return order
        async for order in self._orders():
            if order.remote_id == remote_id:
                return order
        raise ConnectorError(f"Order {remote_id} not found", retryable=False, status=404)

    async def mark_delivered(self, remote_id: str, payload: DeliveryPayload) -> RemoteResult:
        """Marks the order delivered. Eldorado takes no body here: the items reach the buyer
        through the chat (``chat_send``) or the offer vault, before this call."""
        try:
            await self._http.request("PUT", f"api/orders/me/{remote_id}/deliver", json_body={})
        except ConnectorError as exc:
            # Invariant I4: the call may have worked even if we did not get the answer.
            current = await self._reread(remote_id, exc)
            if current.state in DELIVERED_OR_LATER:
                return RemoteResult(
                    ok=True, remote_state=current.remote_state, detail="already delivered"
                )
            raise ConnectorError(
                f"{exc} (order is {current.remote_state or current.state})",
                retryable=exc.retryable and current.state is OrderState.paid,
                status=exc.status,
            ) from exc
        return RemoteResult(ok=True, remote_state="Delivered")

    async def cancel_order(self, remote_id: str, reason: str) -> RemoteResult:
        enum_reason = (
            reason if reason in parse.CANCEL_REASONS else "Buyer_Does_Not_Meet_Criteria_For_Order"
        )
        try:
            await self._http.request(
                "POST", f"api/orders/me/{remote_id}/cancel", json_body={"reason": enum_reason}
            )
        except ConnectorError as exc:
            current = await self._reread(remote_id, exc)
            if current.state in {OrderState.cancelled, OrderState.refunded}:
                return RemoteResult(
                    ok=True, remote_state=current.remote_state, detail="already cancelled"
                )
            raise
        return RemoteResult(ok=True, remote_state="Canceled")

    async def _reread(self, remote_id: str, original: ConnectorError) -> RemoteOrder:
        try:
            return await self.get_order(remote_id)
        except ConnectorError:
            raise original from None

    # ---- offers ----------------------------------------------------------------------

    async def _paged(self, path: str, kind: OfferKind) -> list[RemoteOffer]:
        offers: list[RemoteOffer] = []
        for page in range(1, MAX_PAGES + 1):
            body = await self._http.request(
                "GET", path, params={"pageSize": PAGE_SIZE, "pageIndex": page}
            )
            rows = parse.results(body)
            offers.extend(
                parse.parse_offer(r, kind) for r in rows if parse.first(r, "id", "offerId")
            )
            total = body.get("recordCount") if isinstance(body, dict) else None
            if len(rows) < PAGE_SIZE or (isinstance(total, int) and len(offers) >= total):
                break
        return offers

    async def list_offers(self) -> list[RemoteOffer]:
        offers = await self._paged("api/flexibleOffers/me/search", OfferKind.account)
        offers += await self._paged("api/v1/item-management/me/offers/me/search", OfferKind.item)
        try:
            topups = await self._http.request("GET", "api/predefinedOffers/me")
            offers += [
                parse.parse_offer(r, OfferKind.topup)
                for r in parse.results(topups)
                if parse.first(r, "id", "offerId")
            ]
        except ConnectorError as exc:
            if exc.status not in (403, 404):
                raise
        unique: dict[str, RemoteOffer] = {}
        for offer in offers:
            unique.setdefault(offer.remote_id, offer)
        self._offer_kinds = {o.remote_id: o.kind for o in unique.values()}
        return list(unique.values())

    async def _kind(self, remote_id: str) -> OfferKind:
        if remote_id not in self._offer_kinds:
            await self.list_offers()
        try:
            return self._offer_kinds[remote_id]
        except KeyError:
            raise ConnectorError(
                f"Offer {remote_id} not found", retryable=False, status=404
            ) from None

    async def set_offer_price(self, remote_id: str, price: Money) -> RemoteResult:
        kind = await self._kind(remote_id)
        body = {"amount": float(price.amount), "currency": price.currency}
        if kind is OfferKind.account:
            await self._http.request(
                "PUT", f"api/flexibleOffersUser/me/{remote_id}/changePrice", json_body=body
            )
        elif kind is OfferKind.item:
            await self._http.request(
                "PUT", f"api/v1/item-management/me/offers/{remote_id}/price", json_body=body
            )
        else:
            raise ConnectorError(
                "Changing the price of a top-up offer is not supported.", retryable=False
            )
        return RemoteResult(ok=True)

    async def set_offer_state(self, remote_id: str, state: OfferState) -> RemoteResult:
        if state is OfferState.closed:
            raise ConnectorError("Closing an offer from here is not supported.", retryable=False)
        kind = await self._kind(remote_id)
        action = "pause" if state is OfferState.paused else "resume"
        if kind is OfferKind.account:
            await self._http.request("PUT", f"api/flexibleOffersUser/me/{remote_id}/{action}")
        else:
            # Top-ups are item-management offers on Eldorado (told apart by their category).
            await self._http.request(
                "POST", f"api/v1/item-management/me/offers/{remote_id}/{action}"
            )
        return RemoteResult(ok=True, remote_state=state.value)

    # ---- vault (instant delivery) ----------------------------------------------------

    async def vault_list(self, offer_id: str) -> list[VaultEntry]:
        body = await self._http.request(
            "GET", f"api/flexibleOffersUser/me/offers/{offer_id}/secretDetails"
        )
        entries = parse.vault_entries(body)
        if entries is None:
            # A 200 that is not a list of accounts (an HTML page, another JSON shape) must never
            # read as an empty vault: restock would then upload accounts that are not needed.
            raise ConnectorError(
                "Eldorado returned something that is not the list of vault accounts; "
                "nothing was uploaded.",
                retryable=True,
            )
        return entries

    async def vault_add(self, offer_id: str, entry: VaultEntry) -> RemoteResult:
        try:
            body = await self._http.request(
                "POST",
                f"api/flexibleOffersUser/me/offers/{offer_id}/secretDetails",
                json_body=parse.vault_payload(entry),
            )
        except ConnectorError as exc:
            # 409 (concurrent edit), 429 or a timeout: Eldorado may have stored it anyway.
            if not exc.retryable and exc.status != 409:
                raise
            try:
                stored = await self.vault_list(offer_id)
            except ConnectorError:
                raise exc from None
            login = entry.login.strip().lower()
            for existing in stored:
                if existing.login.strip().lower() == login:
                    return RemoteResult(
                        ok=True, remote_state=existing.remote_id, detail="already in the vault"
                    )
            raise ConnectorError(str(exc), retryable=True, status=exc.status) from exc
        remote_id = parse.first(body, "id") if isinstance(body, dict) else None
        return RemoteResult(ok=True, remote_state=str(remote_id) if remote_id else None)

    async def vault_remove(self, offer_id: str, entry_id: str) -> RemoteResult:
        await self._http.request(
            "DELETE", f"api/flexibleOffersUser/me/offers/{offer_id}/secretDetails/{entry_id}"
        )
        return RemoteResult(ok=True)

    # ---- chat --------------------------------------------------------------------------

    async def _chat_user(self) -> str:
        _, _, user_id = await self._http.chat_session()
        return user_id or await self._seller_user_id()

    async def chat_list_messages(self, conversation_id: str, since: datetime) -> list[ChatMessage]:
        seller_id = await self._chat_user()
        body = await self._http.talkjs(
            "GET", f"conversations/{conversation_id}/messages", params={"limit": 30}
        )
        messages = [parse.parse_message(m, conversation_id, seller_id) for m in parse.results(body)]
        return sorted((m for m in messages if m.sent_at >= since), key=lambda m: m.sent_at)

    async def chat_send(self, conversation_id: str, text: str) -> RemoteResult:
        seller_id = await self._chat_user()
        # Same body the old seller portal sent in production.
        await self._http.talkjs(
            "POST",
            f"conversations/{conversation_id}/messages",
            json_body={"text": text, "sender": seller_id},
        )
        return RemoteResult(ok=True)

    # ---- reviews -----------------------------------------------------------------------

    async def list_reviews(self, since: datetime) -> list[Review]:
        user_id = await self._seller_user_id()
        reviews: list[Review] = []
        cursor: str | None = None
        for _ in range(MAX_PAGES):
            body = await self._http.request(
                "GET",
                f"api/orders/reviews/{user_id}",
                params={"pageSize": PAGE_SIZE, "pageDirection": "Next", "cursor": cursor},
            )
            page = [
                r for r in (parse.parse_review(x) for x in parse.results(body)) if r is not None
            ]
            reviews.extend(r for r in page if r.created_at >= since)
            cursor = parse.next_cursor(body)
            if not cursor or not page or min(r.created_at for r in page) < since:
                break
        return reviews
