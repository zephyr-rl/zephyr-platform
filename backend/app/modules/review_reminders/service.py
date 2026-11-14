"""Review reminders: ask buyers for a review some time after their order.

Rules (from the old reminder tool, which ran them in production):
- only delivered or completed orders, at least ``delay_hours`` and at most ``lookback_days`` old;
- one reminder per order, ever (database), and one per buyer within ``buyer_cooldown_days``;
- the review filter: skip orders with a negative review, with any review, or none;
- at most ``daily_cap`` a day, one at a time with a random pause between two (humanised).

Each send is committed as "sending" before the chat message goes out; if the worker dies there,
the next run reads the chat before sending again, so nobody gets the reminder twice.

Manual mode (default): the owner picks the buyers in the panel and starts the batch. Automatic
mode queues the candidates by itself. Nothing is queued or sent while the store is in shadow
mode: the candidates are only shown.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.base import ConnectorError, OrderState
from app.connectors.runtime import open_connector
from app.core import audit, jobs
from app.core.db import utcnow
from app.core.platform import Platform
from app.modules.catalog import ReviewRemindersSettings
from app.modules.deliveries import messages
from app.modules.review_reminders.models import ReminderState, ReviewReminder
from app.orders.models import Order
from app.stores.models import MarketplaceConnection, Store, StoreModule

SEND_JOB = "review_reminders.send"
AUTO_JOB = "review_reminders.auto"
DONE_STATES = (OrderState.delivered.value, OrderState.completed.value)
ACTOR = audit.Actor.system("review-reminders")
MAX_ATTEMPTS = 3


class ReminderError(Exception):
    pass


@dataclass
class Candidate:
    order_id: int
    remote_id: str
    buyer: str
    title: str | None
    placed_at: datetime
    rating: int | None  # the order's review, if any (1-5)


async def settings_for(
    session: AsyncSession, store_id: int
) -> tuple[ReviewRemindersSettings, bool]:
    row = await session.scalar(
        select(StoreModule).where(
            StoreModule.store_id == store_id, StoreModule.module_key == "review_reminders"
        )
    )
    return ReviewRemindersSettings.model_validate(row.settings if row else {}), bool(
        row and row.enabled
    )


def _skip_for_review(rating: int | None, review_filter: str) -> bool:
    if rating is None or review_filter == "remind_all":
        return False
    if review_filter == "skip_reviewed":
        return True
    return rating <= 2  # skip_negative


async def _reviews(
    session: AsyncSession, platform: Platform, store_id: int, since: datetime
) -> dict[str, int]:
    """Marketplace order id -> rating, for the store's reviews since ``since``."""
    out: dict[str, int] = {}
    for connection in await session.scalars(
        select(MarketplaceConnection).where(MarketplaceConnection.store_id == store_id)
    ):
        connector = await open_connector(session, platform, connection)
        try:
            for review in await connector.list_reviews(since):
                if review.order_id:
                    out[review.order_id] = review.rating
        finally:
            await connector.aclose()
    return out


async def _recently_reminded_buyers(
    session: AsyncSession, store_id: int, cooldown: timedelta, now: datetime
) -> set[str]:
    rows = await session.scalars(
        select(ReviewReminder.buyer_name).where(
            ReviewReminder.store_id == store_id,
            ReviewReminder.buyer_name.is_not(None),
            (
                (ReviewReminder.state.in_((ReminderState.queued, ReminderState.sending)))
                | (ReviewReminder.sent_at >= now - cooldown)
            ),
        )
    )
    return {b.lower() for b in rows if b}


async def candidates(
    session: AsyncSession, platform: Platform, store_id: int, now: datetime | None = None
) -> list[Candidate]:
    now = now or utcnow()
    settings, _ = await settings_for(session, store_id)
    newest = now - timedelta(hours=settings.delay_hours)
    oldest = now - timedelta(days=settings.lookback_days)
    reminded = select(ReviewReminder.remote_order_id).where(ReviewReminder.store_id == store_id)
    orders = list(
        await session.scalars(
            select(Order)
            .where(
                Order.store_id == store_id,
                Order.state.in_(DONE_STATES),
                Order.conversation_id.is_not(None),
                Order.remote_created_at >= oldest,
                Order.remote_created_at <= newest,
                Order.remote_id.not_in(reminded),
            )
            .order_by(Order.remote_created_at)
        )
    )
    if not orders:
        return []
    ratings = await _reviews(session, platform, store_id, oldest - timedelta(days=1))
    busy = await _recently_reminded_buyers(
        session, store_id, timedelta(days=settings.buyer_cooldown_days), now
    )
    out: list[Candidate] = []
    for order in orders:
        buyer = (order.buyer_name or "").strip()
        rating = ratings.get(order.remote_id)
        if _skip_for_review(rating, settings.review_filter):
            continue
        if buyer and buyer.lower() in busy:
            continue
        busy.add(buyer.lower())  # one per buyer in this list too
        out.append(
            Candidate(
                order.id, order.remote_id, buyer, order.offer_title, order.remote_created_at, rating
            )
        )
    return out


async def sent_last_day(session: AsyncSession, store_id: int, now: datetime) -> int:
    return int(
        await session.scalar(
            select(func.count()).where(
                ReviewReminder.store_id == store_id,
                ReviewReminder.state == ReminderState.sent,
                ReviewReminder.sent_at >= now - timedelta(days=1),
            )
        )
        or 0
    )


async def queued_count(session: AsyncSession, store_id: int) -> int:
    return int(
        await session.scalar(
            select(func.count()).where(
                ReviewReminder.store_id == store_id,
                ReviewReminder.state.in_((ReminderState.queued, ReminderState.sending)),
            )
        )
        or 0
    )


async def _kick(session: AsyncSession, store_id: int, delay: timedelta = timedelta()) -> None:
    await jobs.enqueue(
        session,
        SEND_JOB,
        {},
        store_id=store_id,
        run_at=utcnow() + delay,
        idempotency_key=f"reminders:{store_id}:{int((utcnow() + delay).timestamp())}",
    )


async def queue(
    session: AsyncSession,
    platform: Platform,
    actor: audit.Actor,
    store_id: int,
    order_ids: list[int],
) -> int:
    """Queue reminders for these orders (they must still be candidates), up to what is left of
    today's limit. Returns how many were queued."""
    store = await session.get(Store, store_id)
    if store is None:
        raise ReminderError("Store not found.")
    if store.shadow_mode:
        raise ReminderError("The store is in shadow mode: reminders are not sent.")
    settings, enabled = await settings_for(session, store_id)
    if not enabled:
        raise ReminderError("Turn on Review reminders for this store first.")
    room = settings.daily_cap - await sent_last_day(session, store_id, utcnow())
    room -= await queued_count(session, store_id)
    if room <= 0:
        raise ReminderError("Today's limit is reached (sent and queued). Try again tomorrow.")
    wanted = set(order_ids)
    chosen = [c for c in await candidates(session, platform, store_id) if c.order_id in wanted]
    chosen = chosen[:room]  # a batch never goes past today's limit
    for c in chosen:
        await session.execute(
            insert(ReviewReminder)
            .values(
                store_id=store_id,
                remote_order_id=c.remote_id,
                order_id=c.order_id,
                buyer_name=c.buyer or None,
                queued_by=actor.label,
            )
            .on_conflict_do_nothing(index_elements=["store_id", "remote_order_id"])
        )
    if chosen:
        audit.record(
            session,
            actor,
            "review_reminders.queued",
            store_id=store_id,
            after={"count": len(chosen), "orders": [c.remote_id for c in chosen][:100]},
        )
        await _kick(session, store_id)
    return len(chosen)


async def cancel_queued(session: AsyncSession, actor: audit.Actor, store_id: int) -> int:
    rows = list(
        await session.scalars(
            select(ReviewReminder)
            .where(
                ReviewReminder.store_id == store_id, ReviewReminder.state == ReminderState.queued
            )
            .with_for_update(skip_locked=True)
        )
    )
    for r in rows:
        r.state = ReminderState.skipped
        r.note = f"Removed from the queue by {actor.label}."
    if rows:
        audit.record(
            session,
            actor,
            "review_reminders.cancelled",
            store_id=store_id,
            after={"count": len(rows)},
        )
    return len(rows)


async def run_automatic(platform: Platform, store_id: int) -> int:
    """Automatic mode: queue today's share of candidates."""
    async with platform.db.sessionmaker() as session:
        settings, enabled = await settings_for(session, store_id)
        store = await session.get(Store, store_id)
        if not (enabled and settings.automatic and store and not store.shadow_mode):
            return 0
        now = utcnow()
        room = settings.daily_cap - await sent_last_day(session, store_id, now)
        room -= await queued_count(session, store_id)
        if room <= 0:
            return 0
        picked = (await candidates(session, platform, store_id, now))[:room]
        count = await queue(session, platform, ACTOR, store_id, [c.order_id for c in picked])
        await session.commit()
        return count


async def send_next(platform: Platform, store_id: int) -> dict[str, Any]:
    """Send one queued reminder, then schedule the next one after a random pause."""
    sm = platform.db.sessionmaker
    async with sm() as session:
        settings, enabled = await settings_for(session, store_id)
        store = await session.get(Store, store_id)
        if not enabled or store is None or store.shadow_mode:
            return {"sent": 0, "reason": "module off or shadow mode"}
        now = utcnow()
        if await sent_last_day(session, store_id, now) >= settings.daily_cap:
            await _kick(session, store_id, timedelta(hours=1))
            await session.commit()
            return {"sent": 0, "reason": "daily cap reached"}
        reminder = await session.scalar(
            select(ReviewReminder)
            .where(
                ReviewReminder.store_id == store_id,
                ReviewReminder.state.in_((ReminderState.sending, ReminderState.queued)),
            )
            .order_by(ReviewReminder.state.desc(), ReviewReminder.id)  # 'sending' first
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if reminder is None:
            return {"sent": 0, "reason": "queue empty"}
        result = await _send_one(session, platform, settings, reminder)
        more = await session.scalar(
            select(ReviewReminder.id)
            .where(
                ReviewReminder.store_id == store_id, ReviewReminder.state == ReminderState.queued
            )
            .limit(1)
        )
        if more:
            pause = random.uniform(settings.min_delay_seconds, settings.max_delay_seconds)  # noqa: S311
            await _kick(session, store_id, timedelta(seconds=pause))
        await session.commit()
        return {"sent": 1 if result is ReminderState.sent else 0, "state": result.value}


async def _send_one(
    session: AsyncSession,
    platform: Platform,
    settings: ReviewRemindersSettings,
    reminder: ReviewReminder,
) -> ReminderState:
    order = await session.get(Order, reminder.order_id) if reminder.order_id else None
    if order is None or order.state not in DONE_STATES or not order.conversation_id:
        reminder.state, reminder.note = ReminderState.skipped, "The order is no longer delivered."
        return reminder.state
    text = messages.fill(
        settings.message_template,
        buyer=order.buyer_name or "there",
        product=order.offer_title or "your order",
        order=order.remote_id[:8],
    )
    connection = await session.get(MarketplaceConnection, order.connection_id)
    assert connection is not None
    connector = await open_connector(session, platform, connection)
    try:
        if reminder.state is ReminderState.sending:
            # A previous run died around the send: look before sending again.
            chat = await connector.chat_list_messages(
                order.conversation_id, reminder.created_at - timedelta(minutes=1)
            )
            if any(m.from_seller and m.text.strip().startswith(text.strip()[:60]) for m in chat):
                reminder.state, reminder.sent_at = ReminderState.sent, utcnow()
                reminder.note = "Found in the chat after an interrupted send."
                return reminder.state
        ratings = await _reviews(session, platform, order.store_id, order.remote_created_at)
        if _skip_for_review(ratings.get(order.remote_id), settings.review_filter):
            reminder.state, reminder.note = ReminderState.skipped, "The buyer left a review."
            return reminder.state
        reminder.state = ReminderState.sending
        reminder.attempts += 1
        await session.commit()  # committed before the message leaves
        try:
            await connector.chat_send(order.conversation_id, text)
        except ConnectorError as exc:
            retry = exc.retryable and reminder.attempts < MAX_ATTEMPTS
            reminder.state = ReminderState.queued if retry else ReminderState.failed
            reminder.note = str(exc)[:500]
            return reminder.state
        reminder.state, reminder.sent_at, reminder.note = ReminderState.sent, utcnow(), None
        return reminder.state
    finally:
        await connector.aclose()


async def history(session: AsyncSession, store_id: int, limit: int = 100) -> list[ReviewReminder]:
    return list(
        await session.scalars(
            select(ReviewReminder)
            .where(ReviewReminder.store_id == store_id)
            .order_by(ReviewReminder.id.desc())
            .limit(limit)
        )
    )
