"""Notification channels (where alerts go), rules (which events, how they look) and the log of
every message sent."""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, TimestampMixin


class ChannelKind(enum.StrEnum):
    discord_webhook = "discord_webhook"


class NotificationChannel(TimestampMixin, Base):
    __tablename__ = "notification_channels"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    kind: Mapped[ChannelKind] = mapped_column(
        Enum(ChannelKind, name="channel_kind", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    # The webhook URL is a secret (anyone with it can post): encrypted, shown only as a hint.
    target_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    target_hint: Mapped[str] = mapped_column(String(80), nullable=False)
    sender_name: Mapped[str | None] = mapped_column(String(80))
    avatar_url: Mapped[str | None] = mapped_column(String(500))
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Stores in shadow mode run next to the old system: their alerts are not sent unless asked.
    include_shadow: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)


class NotificationRule(TimestampMixin, Base):
    __tablename__ = "notification_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    channel_id: Mapped[int] = mapped_column(
        ForeignKey("notification_channels.id", ondelete="RESTRICT"), nullable=False
    )
    # None: every store.
    store_id: Mapped[int | None] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"))
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # {"title", "description", "color", "footer", "fields": [{"name", "value", "inline"}]}
    template: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # Optional narrowing: {"listing_ids": [..]} (empty: all listings).
    filters: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)


class DeliveryState(enum.StrEnum):
    pending = "pending"
    sent = "sent"
    failed = "failed"
    skipped = "skipped"  # e.g. store in shadow mode, channel switched off


class NotificationDelivery(Base):
    """One message for one rule and one event. Unique per (event, rule): never sent twice."""

    __tablename__ = "notification_deliveries"
    __table_args__ = (UniqueConstraint("event_id", "rule_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    event_id: Mapped[int | None] = mapped_column(BigInteger)  # outbox event; None for tests
    rule_id: Mapped[int | None] = mapped_column(
        ForeignKey("notification_rules.id", ondelete="SET NULL")
    )
    channel_id: Mapped[int] = mapped_column(
        ForeignKey("notification_channels.id", ondelete="CASCADE"), nullable=False, index=True
    )
    store_id: Mapped[int | None] = mapped_column(ForeignKey("stores.id", ondelete="SET NULL"))
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    state: Mapped[DeliveryState] = mapped_column(
        Enum(DeliveryState, name="delivery_state", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=DeliveryState.pending,
    )
    message: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)  # what is posted
    summary: Mapped[str] = mapped_column(String(300), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
