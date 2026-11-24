"""What the chat monitor knows about each order conversation."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, TimestampMixin, utcnow

WAITING_HOURS = 24  # a conversation counts as waiting for this long without an answer


class ChatThread(TimestampMixin, Base):
    __tablename__ = "chat_threads"
    __table_args__ = (UniqueConstraint("connection_id", "conversation_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    store_id: Mapped[int] = mapped_column(
        ForeignKey("stores.id", ondelete="CASCADE"), nullable=False, index=True
    )
    connection_id: Mapped[int] = mapped_column(
        ForeignKey("marketplace_connections.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[str] = mapped_column(String(120), nullable=False)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id", ondelete="SET NULL"))
    # Newest message already looked at: only later ones are new.
    seen_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_buyer_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_buyer_text: Mapped[str | None] = mapped_column(Text)
    # Last message the seller wrote by hand (automatic messages do not count).
    last_seller_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_alert_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    urgent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    handled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    handled_by: Mapped[str | None] = mapped_column(String(120))
    # The buyer's last message needs nobody: a CONFIRM / CANCEL reply (the delivery flow, ours or
    # the old system's, reads it) or a "thanks" on a delivered order.
    no_reply_needed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    @property
    def needs_reply(self) -> bool:
        if self.last_buyer_at is None or self.no_reply_needed:
            return False
        # Eldorado does not tell which chats the seller read: after a day without an answer the
        # conversation stops counting (it is still listed under "All").
        if utcnow() - self.last_buyer_at > timedelta(hours=WAITING_HOURS):
            return False
        if self.last_seller_at is not None and self.last_seller_at >= self.last_buyer_at:
            return False
        return self.handled_at is None or self.handled_at < self.last_buyer_at
