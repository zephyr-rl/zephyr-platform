"""Turn Eldorado payloads into the platform's core types.

Field names come from the old seller portal, which ran against the live API. Several fields
exist under more than one name (``createdDate``/``createdAt``, ``reviewRating``/
``feedbackRating``), so every reader tries each known spelling.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.connectors.base import (
    ChatMessage,
    Money,
    OfferKind,
    OfferState,
    OrderState,
    RemoteOffer,
    RemoteOrder,
    Review,
    VaultEntry,
)

ORDER_STATES: dict[str, OrderState] = {
    "pendingpayment": OrderState.pending_payment,
    "pending": OrderState.pending_payment,
    "paid": OrderState.paid,
    "delivered": OrderState.delivered,
    "received": OrderState.completed,  # the buyer confirmed receipt
    "completed": OrderState.completed,
    "canceled": OrderState.cancelled,
    "cancelled": OrderState.cancelled,
    "refunded": OrderState.refunded,
    "disputed": OrderState.disputed,
}
REMOTE_STATE_FILTER: dict[OrderState, list[str]] = {
    OrderState.paid: ["Paid"],
    OrderState.delivered: ["Delivered"],
    OrderState.completed: ["Received", "Completed"],
    OrderState.cancelled: ["Canceled"],
    OrderState.refunded: ["Refunded"],
    OrderState.disputed: ["Disputed"],
}
OFFER_STATES = {
    "active": OfferState.active,
    "paused": OfferState.paused,
    "closed": OfferState.closed,
}
CANCEL_REASONS = frozenset(
    {"Buyer_Does_Not_Meet_Criteria_For_Order", "Out_Of_Stock", "Buyer_Does_Not_Need_It_Anymore"}
)


def first(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return value
    return None


def parse_time(value: Any) -> datetime:
    if isinstance(value, (int, float)):  # TalkJS uses milliseconds
        return datetime.fromtimestamp(value / 1000 if value > 1e11 else value, UTC)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return datetime.now(UTC)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return datetime.now(UTC)


def money(value: Any, default_currency: str = "USD") -> Money:
    if isinstance(value, dict):
        amount = value.get("amount", value.get("value", 0))
        currency = str(value.get("currency") or default_currency)
    else:
        amount, currency = value, default_currency
    try:
        return Money(amount=Decimal(str(amount if amount is not None else 0)), currency=currency)
    except InvalidOperation:
        return Money(amount=Decimal(0), currency=currency)


def order_state(raw: Any) -> tuple[OrderState, str]:
    text = str(raw.get("state") if isinstance(raw, dict) else raw or "")
    return ORDER_STATES.get(text.lower().replace("_", ""), OrderState.unknown), text


def parse_order(data: dict[str, Any]) -> RemoteOrder:
    details = data.get("orderOfferDetails") or {}
    state, remote_state = order_state(data.get("state"))
    return RemoteOrder(
        remote_id=str(first(data, "id", "orderId")),
        offer_id=(str(data["offerId"]) if data.get("offerId") else None),
        state=state,
        remote_state=remote_state or None,
        quantity=int(first(data, "purchaseQuantity", "quantity") or 1),
        total=money(data.get("totalPrice")),
        buyer_name=first(data, "buyerUsername"),
        buyer_id=(
            str(first(data, "buyerId", "buyerUserId"))
            if first(data, "buyerId", "buyerUserId")
            else None
        ),
        conversation_id=first(data, "talkJsConversationId"),
        created_at=parse_time(first(data, "createdDate", "createdAt")),
        offer_title=first(details, "offerTitle", "gameCategoryTitle"),
        category=first(details, "category"),
        raw=data,
    )


def is_top_up(data: dict[str, Any]) -> bool:
    category = str(data.get("category", "")).lower()
    seo = str(data.get("gameSeoAlias", "")).lower()
    title = str(data.get("gameCategoryTitle", "")).lower()
    return (
        "topup" in category
        or any(k in seo for k in ("credits", "vbucks", "v-bucks", "rocket-pass", "topup"))
        or any(k in title for k in ("credits", "v-bucks", "vbucks", "rocket pass", "crew", "topup"))
    )


def platform_of(data: dict[str, Any]) -> str | None:
    values = data.get("tradeEnvironmentValues") or []
    names = [str(v.get("value")).strip() for v in values if isinstance(v, dict) and v.get("value")]
    return ", ".join(dict.fromkeys(names))[:80] or None


def parse_offer(data: dict[str, Any], kind: OfferKind) -> RemoteOffer:
    state_text = str(first(data, "offerState", "state", "status") or "").lower()
    quantity = first(data, "quantity", "availableQuantity", "stock")
    if kind is OfferKind.item and is_top_up(data):
        kind = OfferKind.topup
    return RemoteOffer(
        remote_id=str(first(data, "id", "offerId")),
        title=str(
            first(data, "offerTitle", "title", "name", "gameCategoryTitle") or "Untitled offer"
        ),
        # An unknown state is treated as paused: never as something that sells.
        state=OFFER_STATES.get(state_text, OfferState.paused) if state_text else OfferState.active,
        price=money(first(data, "pricePerUnit", "price", "unitPrice")),
        quantity=int(quantity) if quantity is not None else None,
        kind=kind,
        platform=platform_of(data),
        raw=data,
    )


def results(body: Any) -> list[dict[str, Any]]:
    if isinstance(body, list):
        return [x for x in body if isinstance(x, dict)]
    if isinstance(body, dict):
        for key in ("results", "data", "items", "offers"):
            value = body.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
        if isinstance(body.get("reviews"), dict):  # {"userOrderInfo": …, "reviews": {"results": …}}
            return results(body["reviews"])
    return []


def next_cursor(body: Any) -> str | None:
    """Cursor of the next page (orders and reviews are paged with cursors)."""
    if not isinstance(body, dict):
        return None
    if isinstance(body.get("reviews"), dict):
        body = body["reviews"]
    value = first(body, "nextPageCursor", "nextCursor")
    return str(value) if value else None


def parse_vault_entry(data: dict[str, Any]) -> VaultEntry:
    structured = data.get("structuredDetails") or {}
    account = structured.get("accountDetails") or {}
    email = structured.get("emailDetails") or {}
    return VaultEntry(
        remote_id=str(data["id"]) if data.get("id") else None,
        login=str(account.get("accountLogin") or ""),
        password=str(account.get("accountPassword") or ""),
        email=email.get("emailLogin"),
        email_password=email.get("emailPassword"),
        email_provider_url=email.get("emailProviderUrl"),
        additional_info=structured.get("additionalInfo") or None,
    )


def vault_payload(entry: VaultEntry) -> dict[str, Any]:
    email: dict[str, str] | None = None
    if entry.email and entry.email_password:
        provider = entry.email_provider_url
        if not provider and "@" in entry.email:
            provider = "https://" + entry.email.rsplit("@", 1)[-1].strip().lower()
        email = {
            "emailLogin": entry.email.strip(),
            "emailPassword": entry.email_password.strip(),
            "emailProviderUrl": provider or "",
        }
    return {
        "structuredDetails": {
            "accountDetails": {
                "accountLogin": entry.login.strip(),
                "accountPassword": entry.password.strip(),
            },
            "additionalInfo": entry.additional_info or "",
            "emailDetails": email,
        }
    }


def _flatten(content: Any) -> str:
    """TalkJS rich content: [{"type": "text", "children": ["Hi", {"type": "bold", ...}]}]."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(_flatten(part) for part in content)
    if isinstance(content, dict):
        if isinstance(content.get("text"), str):
            return str(content["text"])
        return _flatten(content.get("children", []))
    return ""


def parse_message(data: dict[str, Any], conversation_id: str, seller_id: str) -> ChatMessage:
    sender = first(data, "senderId", "sender")
    text = data.get("text") if isinstance(data.get("text"), str) else _flatten(data.get("content"))
    return ChatMessage(
        remote_id=str(data.get("id")),
        conversation_id=conversation_id,
        author_id=str(sender) if sender else None,
        from_seller=bool(sender) and str(sender) == seller_id,
        text=text or "",
        sent_at=parse_time(first(data, "createdAt", "timestamp")),
    )


def review_rating(data: dict[str, Any]) -> int | None:
    numeric = data.get("rating")
    if isinstance(numeric, (int, float)):
        return max(1, min(5, round(numeric)))
    word = str(first(data, "reviewRating", "feedbackRating") or "").lower()
    if word == "positive":
        return 5
    if word == "negative":
        return 1
    if word == "neutral":
        return 3
    return None


def parse_review(data: dict[str, Any]) -> Review | None:
    if isinstance(data.get("orderReview"), dict):
        # Reviews endpoint: {"orderReview": {"id": <order id>, "date", "review": {…}}, "buyer"}
        outer = data["orderReview"]
        inner = outer.get("review") if isinstance(outer.get("review"), dict) else {}
        data = {
            **inner,
            "orderId": outer.get("id"),
            "createdDate": outer.get("date"),
            "reviewText": inner.get("reviewMessage"),
        }
    rating = review_rating(data)
    if rating is None:
        return None
    order_id = first(data, "orderId", "order_id")
    return Review(
        remote_id=str(first(data, "id", "reviewId") or order_id or ""),
        order_id=str(order_id) if order_id else None,
        rating=rating,
        text=first(data, "reviewText", "reviewMessage", "feedbackText", "comment", "text"),
        created_at=parse_time(first(data, "createdDate", "createdAt", "date")),
    )
