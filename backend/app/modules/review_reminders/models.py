"""Review reminders: at most one per order, ever."""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class ReminderState(enum.StrEnum):
    queued = "queued"  # waiting for its turn (paced, within the daily cap)
    sending = "sending"  # committed right before the chat message is sent
    sent = "sent"
    skipped = "skipped"  # not sent: reviewed in the meantime, cancelled order, owner removed it
    failed = "failed"


class ReviewReminder(Base):
    __tablename__ = "review_reminders"
    __table_args__ = (UniqueConstraint("store_id", "remote_order_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    store_id: Mapped[int] = mapped_column(
        ForeignKey("stores.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # One reminder per order, ever, whatever happens to it. Keyed by the marketplace order id so
    # orders the old system already reminded can be imported before the platform has seen them.
    remote_order_id: Mapped[str] = mapped_column(String(120), nullable=False)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id", ondelete="SET NULL"))
    buyer_name: Mapped[str | None] = mapped_column(String(120))
    state: Mapped[ReminderState] = mapped_column(
        Enum(ReminderState, name="reminder_state", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=ReminderState.queued,
        index=True,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    queued_by: Mapped[str] = mapped_column(String(120), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
