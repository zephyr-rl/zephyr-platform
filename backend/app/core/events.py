"""Domain events with a transactional outbox.

``publish`` writes the event in the same transaction as the change that caused it, so an event
exists if and only if the change was committed. The dispatcher (run by the worker) delivers
pending events to in-process subscribers. Subscribers must be quick and idempotent; heavy work
belongs in a job they enqueue.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, Text, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, utcnow

log = logging.getLogger(__name__)

EVENT_TYPES: frozenset[str] = frozenset(
    {
        "order.received",
        "order.paid",
        "order.awaiting_confirmation",
        "order.delivered",
        "order.delivery_failed",
        "order.cancelled",
        "order.refunded",
        "stock.imported",
        "stock.low",
        "stock.depleted",
        "stock.quarantined",
        "offer.restocked",
        "offer.paused",
        "offer.price_changed",
        "chat.message_received",
        "review.received",
        "report.daily_ready",
        "system.error",
        "auth.login_failed",
        "config.changed",
    }
)

MAX_DISPATCH_ATTEMPTS = 10


class OutboxEvent(Base):
    __tablename__ = "outbox_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    store_id: Mapped[int | None] = mapped_column(
        ForeignKey("stores.id", ondelete="SET NULL"), index=True
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)


@dataclass(frozen=True)
class Event:
    id: int
    type: str
    store_id: int | None
    payload: dict[str, Any]
    created_at: datetime


Subscriber = Callable[[Event], Awaitable[None]]


class EventBus:
    def __init__(self) -> None:
        self._subscribers: dict[str, list[Subscriber]] = defaultdict(list)

    def subscribe(self, event_type: str, handler: Subscriber) -> None:
        if event_type != "*" and event_type not in EVENT_TYPES:
            raise ValueError(f"Unknown event type {event_type!r}")
        self._subscribers[event_type].append(handler)

    def handlers_for(self, event_type: str) -> list[Subscriber]:
        return [*self._subscribers.get(event_type, []), *self._subscribers.get("*", [])]

    async def dispatch_pending(
        self, sessionmaker: async_sessionmaker[AsyncSession], *, limit: int = 100
    ) -> int:
        """Deliver up to ``limit`` pending events. Returns how many were dispatched."""
        dispatched = 0
        async with sessionmaker() as session, session.begin():
            rows = (
                await session.scalars(
                    select(OutboxEvent)
                    .where(
                        OutboxEvent.dispatched_at.is_(None),
                        OutboxEvent.attempts < MAX_DISPATCH_ATTEMPTS,
                    )
                    .order_by(OutboxEvent.id)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            ).all()
            for row in rows:
                event = Event(row.id, row.type, row.store_id, row.payload, row.created_at)
                try:
                    for handler in self.handlers_for(row.type):
                        await handler(event)
                except Exception as exc:
                    row.attempts += 1
                    row.last_error = f"{type(exc).__name__}: {exc}"[:2000]
                    log.exception("Event %s (%s) handler failed", row.id, row.type)
                    continue
                row.dispatched_at = utcnow()
                row.attempts += 1
                dispatched += 1
        return dispatched


def publish(
    session: AsyncSession,
    event_type: str,
    payload: dict[str, Any] | None = None,
    *,
    store_id: int | None = None,
) -> OutboxEvent:
    """Queue an event in the current transaction."""
    if event_type not in EVENT_TYPES:
        raise ValueError(f"Unknown event type {event_type!r}")
    event = OutboxEvent(type=event_type, store_id=store_id, payload=payload or {})
    session.add(event)
    return event
