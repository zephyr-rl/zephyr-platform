"""Chat monitor: tell the owner when a buyer writes and nobody has answered.

Reads the chats of the most recent open orders and raises ``chat.message_received`` with the rules
the old monitor used in production (so it does not spam):

- only messages newer than the last scan, and at most ``max_age_minutes`` old;
- not when the seller already answered after the buyer, or wrote by hand in the last
  ``seller_active_minutes`` (automatic messages do not count as the seller writing);
- at most one alert per conversation every ``cooldown_minutes``;
- not for a short "thanks" / "ok" on a delivered order;
- not for CONFIRM / CANCEL replies: the delivery flow reads them (ours, or the old system's
  while a store is in shadow mode). Such a message does not count as waiting either.

Completed, cancelled and refunded orders are not read. Only reads: works in shadow mode too.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.base import ChatMessage, ConnectorError, OrderState
from app.connectors.runtime import open_connector
from app.core import events
from app.core.db import utcnow
from app.core.platform import Platform
from app.listings.models import Listing
from app.modules.catalog import ChatMonitorSettings, DeliveriesSettings
from app.modules.chat_monitor.models import ChatThread
from app.modules.deliveries import messages
from app.modules.deliveries.replies import Reply, classify
from app.orders.models import Fulfilment, FulfilmentState, Order
from app.stores.models import MarketplaceConnection, StoreModule

log = logging.getLogger(__name__)
SCAN_JOB = "chat_monitor.scan"
CLOSED = (OrderState.completed.value, OrderState.cancelled.value, OrderState.refunded.value)
_WORDS = re.compile(r"[^\w\s]")


@dataclass
class ScanResult:
    conversations: int = 0
    alerts: int = 0
    errors: list[str] = field(default_factory=list)


async def _settings(
    session: AsyncSession, store_id: int
) -> tuple[ChatMonitorSettings, DeliveriesSettings]:
    rows = {
        r.module_key: r.settings
        for r in await session.scalars(
            select(StoreModule).where(
                StoreModule.store_id == store_id,
                StoreModule.module_key.in_(("chat_monitor", "deliveries")),
            )
        )
    }
    return (
        ChatMonitorSettings.model_validate(rows.get("chat_monitor", {})),
        DeliveriesSettings.model_validate(rows.get("deliveries", {})),
    )


async def _our_messages(session: AsyncSession, store_id: int, cfg: DeliveriesSettings) -> list[str]:
    """Texts this platform sends by itself: seeing them is not the seller writing."""
    ours = [
        cfg.message_template,
        cfg.confirmation_single,
        cfg.confirmation_multi,
        cfg.clarification,
        cfg.cancellation,
    ]
    ours += [
        t
        for t in await session.scalars(
            select(Listing.delivery_template).where(
                Listing.store_id == store_id, Listing.delivery_template.is_not(None)
            )
        )
        if t
    ]
    # Templates start with fixed text before any {placeholder}.
    return [t.split("{", 1)[0] for t in ours if t.split("{", 1)[0].strip()]


def _automated(text: str, ours: list[str]) -> bool:
    text = text.strip()
    if messages.is_automated(text, []):  # empty, or a message of the old system
        return True
    return any(text.startswith(prefix.strip()[:60]) for prefix in ours)


def _normal(text: str) -> str:
    return " ".join(_WORDS.sub(" ", text.lower()).split())


# Short closing messages buyers send after the seller's answer ("thanks bro", "ima check",
# "todo bien"). Matched as whole words in messages of up to 8 words without a question. The
# owner's own list (Chat monitor → courtesy replies) is added to these.
COURTESY = (
    "thanks",
    "thank you",
    "thank u",
    "thanks bro",
    "thx",
    "ty",
    "tysm",
    "tyvm",
    "tks",
    "appreciate",
    "appreciate it",
    "appreciated",
    "np",
    "no problem",
    "no worries",
    "no worrys",
    "ok",
    "okay",
    "okok",
    "kk",
    "k",
    "perfect",
    "great",
    "nice",
    "cool",
    "awesome",
    "legit",
    "love",
    "best",
    "got it",
    "received",
    "worked",
    "works",
    "it works",
    "done",
    "ima check",
    "i will check",
    "ill check",
    "let me check",
    "lemme check",
    "will check",
    "i figure it out",
    "i figured it out",
    "figured it out",
    "all good",
    "good",
    "gracias",
    "muchas gracias",
    "todo bien",
    "esta bien",
    "dale",
    "genial",
    "perfecto",
    "listo",
    "joya",
    "merci",
    "danke",
    "obrigado",
    "valeu",
)
MAX_COURTESY_WORDS = 8
DONE_STATES = (
    OrderState.delivered.value,
    OrderState.completed.value,
    OrderState.cancelled.value,
    OrderState.refunded.value,
)


def is_courtesy(text: str, extra: list[str] | tuple[str, ...] = ()) -> bool:
    if "?" in text:
        return False
    words = _normal(text)
    if not words or len(words.split()) > MAX_COURTESY_WORDS:
        return False
    padded = f" {words} "
    return any((phrase := _normal(p)) and f" {phrase} " in padded for p in (*COURTESY, *extra))


def no_reply_needed(
    text: str, state: str | None, settings: ChatMonitorSettings, deliveries: DeliveriesSettings
) -> bool:
    """A buyer message nobody has to answer by hand."""
    if classify(text, deliveries.reply_languages) is not Reply.unknown:
        return True  # CONFIRM / CANCEL: the delivery flow answers it
    return state in DONE_STATES and is_courtesy(text, settings.courtesy_replies)


def _urgent(text: str, keywords: list[str]) -> bool:
    low = _normal(text)
    return any(_normal(k) and _normal(k) in low for k in keywords)


async def scan_store(platform: Platform, store_id: int, now: datetime | None = None) -> ScanResult:
    result = ScanResult()
    now = now or utcnow()
    async with platform.db.sessionmaker() as session:
        settings, deliveries = await _settings(session, store_id)
        ours = await _our_messages(session, store_id, deliveries)
        orders = list(
            await session.scalars(
                select(Order)
                .where(
                    Order.store_id == store_id,
                    Order.conversation_id.is_not(None),
                    Order.state.not_in(CLOSED),
                )
                .order_by(Order.remote_created_at.desc())
                .limit(settings.conversations)
            )
        )
        by_connection: dict[int, list[Order]] = defaultdict(list)
        for order in orders:
            by_connection[order.connection_id].append(order)
        for connection_id, group in by_connection.items():
            connection = await session.get(MarketplaceConnection, connection_id)
            assert connection is not None
            connector = await open_connector(session, platform, connection)
            try:
                for order in group:
                    try:
                        chat = await connector.chat_list_messages(
                            order.conversation_id or "", now - timedelta(days=2)
                        )
                    except ConnectorError as exc:
                        result.errors.append(f"{order.remote_id[:8]}: {exc}")
                        if not exc.retryable:
                            break  # credentials or access problem: the next scan tries again
                        continue
                    result.conversations += 1
                    if await _process(session, order, chat, settings, deliveries, ours, now):
                        result.alerts += 1
            finally:
                await connector.aclose()
        await _settle_closed(session, store_id, settings, deliveries)
        await session.commit()
    return result


async def _settle_closed(
    session: AsyncSession,
    store_id: int,
    settings: ChatMonitorSettings,
    deliveries: DeliveriesSettings,
) -> None:
    """Threads of orders that are no longer scanned (completed, cancelled, refunded)."""
    rows = await session.execute(
        select(ChatThread, Order.state)
        .join(Order, Order.id == ChatThread.order_id)
        .where(
            ChatThread.store_id == store_id,
            ChatThread.last_buyer_text.is_not(None),
            ChatThread.no_reply_needed.is_(False),
            Order.state.in_(CLOSED),
        )
    )
    for thread, state in rows:
        if thread.last_buyer_text and no_reply_needed(
            thread.last_buyer_text, state, settings, deliveries
        ):
            thread.no_reply_needed = True


async def _thread(session: AsyncSession, order: Order) -> ChatThread:
    thread = await session.scalar(
        select(ChatThread).where(
            ChatThread.connection_id == order.connection_id,
            ChatThread.conversation_id == order.conversation_id,
        )
    )
    if thread is None:
        thread = ChatThread(
            store_id=order.store_id,
            connection_id=order.connection_id,
            conversation_id=order.conversation_id,
            order_id=order.id,
        )
        session.add(thread)
        await session.flush()
    return thread


async def _process(
    session: AsyncSession,
    order: Order,
    chat: list[ChatMessage],
    settings: ChatMonitorSettings,
    deliveries: DeliveriesSettings,
    ours: list[str],
    now: datetime,
) -> bool:
    """Update the thread; publish an alert when the rules allow. Returns True if alerted."""
    thread = await _thread(session, order)
    # System messages have no author: they are neither the buyer nor the seller.
    buyer = [m for m in chat if not m.from_seller and m.author_id]
    by_hand = [m for m in chat if m.from_seller and not _automated(m.text, ours)]
    if by_hand:
        latest_seller = max(m.sent_at for m in by_hand)
        if thread.last_seller_at is None or latest_seller > thread.last_seller_at:
            thread.last_seller_at = latest_seller
    new = [m for m in buyer if thread.seen_until is None or m.sent_at > thread.seen_until]
    if chat:
        newest = max(m.sent_at for m in chat)
        if thread.seen_until is None or newest > thread.seen_until:
            thread.seen_until = newest
    if buyer:
        latest_buyer = max(buyer, key=lambda m: m.sent_at)
        if thread.last_buyer_at is None or latest_buyer.sent_at > thread.last_buyer_at:
            thread.last_buyer_at = latest_buyer.sent_at
            thread.last_buyer_text = latest_buyer.text[:2000]
            thread.urgent = _urgent(latest_buyer.text, settings.alert_keywords)
    if thread.last_buyer_text is not None:
        # Every scan: the order state (delivered or not) can change the answer.
        thread.no_reply_needed = no_reply_needed(
            thread.last_buyer_text, order.state, settings, deliveries
        )
    if not new:
        return False

    message = max(new, key=lambda m: m.sent_at)
    if thread.last_seller_at is not None and thread.last_seller_at >= message.sent_at:
        return False  # already answered
    if now - message.sent_at > timedelta(minutes=settings.max_age_minutes):
        return False  # stale
    if thread.last_seller_at is not None and now - thread.last_seller_at < timedelta(
        minutes=settings.seller_active_minutes
    ):
        return False  # the seller is in the chat right now
    if thread.last_alert_at is not None and now - thread.last_alert_at < timedelta(
        minutes=settings.cooldown_minutes
    ):
        return False
    text = message.text.strip()
    if no_reply_needed(text, order.state, settings, deliveries):
        return False
    fulfilment = await session.scalar(select(Fulfilment).where(Fulfilment.order_id == order.id))
    if fulfilment is not None and fulfilment.state is FulfilmentState.awaiting_buyer:
        return False  # deliveries reads this reply

    thread.last_alert_at = now
    events.publish(
        session,
        "chat.message_received",
        {
            "order": order.remote_id,
            "buyer": order.buyer_name or "",
            "title": order.offer_title or "",
            "message": text[:1500] or "(no text)",
            "urgent": _urgent(text, settings.alert_keywords),
            "conversation_id": order.conversation_id,
            "listing_id": order.listing_id,
        },
        store_id=order.store_id,
    )
    return True


async def mark_all_handled(session: AsyncSession, store_id: int, who: str) -> int:
    """Every conversation now waiting for a reply in the store counts as read."""
    rows = await session.scalars(
        select(ChatThread).where(
            ChatThread.store_id == store_id, ChatThread.last_buyer_at.is_not(None)
        )
    )
    count = 0
    for thread in rows:
        if thread.needs_reply:
            await mark_handled(session, thread, who)
            count += 1
    return count


async def mark_handled(session: AsyncSession, thread: ChatThread, by: str) -> None:
    thread.handled_at = utcnow()
    thread.handled_by = by[:120]
