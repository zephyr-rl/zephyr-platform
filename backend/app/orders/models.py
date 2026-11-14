"""Orders seen on the marketplaces and how each one is fulfilled.

Invariants enforced by the database (see the orders migration):
- I2: one fulfilment per order (``unique(order_id)``), so an order is processed once however many
  workers, polls or retries see it.
- I1: a stock item can be held by at most one active reservation
  (partial unique index on ``reservations(item_id) WHERE active``), and items only go
  reserved → delivered once (stock item trigger).
"""

from __future__ import annotations

import enum
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, TimestampMixin


class FulfilmentState(enum.StrEnum):
    pending = "pending"  # created, waiting for the worker
    awaiting_buyer = "awaiting_buyer"  # confirmation asked, waiting for the buyer's reply
    awaiting_approval = "awaiting_approval"  # assisted delivery: waiting for the owner
    reserved = "reserved"  # stock set aside, credentials not sent yet
    sent = "sent"  # credentials sent to the buyer; items are delivered
    done = "done"  # order marked delivered on the marketplace
    manual = "manual"  # nothing automatic: the owner delivers it
    awaiting_owner = "awaiting_owner"  # top-up: instructions sent, the owner does the top-up
    needs_attention = "needs_attention"  # stopped; a person must look at it
    cancelled = "cancelled"  # order cancelled (by the buyer, the owner or the marketplace)
    simulated = "simulated"  # shadow mode: decisions recorded, nothing sent, no stock used


ACTIVE_STATES = frozenset(
    {
        FulfilmentState.pending,
        FulfilmentState.awaiting_buyer,
        FulfilmentState.awaiting_approval,
        FulfilmentState.reserved,
        FulfilmentState.sent,
        FulfilmentState.awaiting_owner,
    }
)


def _enum(cls: type[enum.StrEnum], name: str) -> Enum:
    return Enum(cls, name=name, values_callable=lambda e: [m.value for m in e])


class Order(TimestampMixin, Base):
    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("connection_id", "remote_id"),
        Index("ix_orders_store_created", "store_id", "remote_created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    store_id: Mapped[int] = mapped_column(
        ForeignKey("stores.id", ondelete="RESTRICT"), nullable=False
    )
    connection_id: Mapped[int] = mapped_column(
        ForeignKey("marketplace_connections.id", ondelete="RESTRICT"), nullable=False
    )
    listing_id: Mapped[int | None] = mapped_column(ForeignKey("listings.id", ondelete="RESTRICT"))
    remote_id: Mapped[str] = mapped_column(String(120), nullable=False)
    remote_offer_id: Mapped[str | None] = mapped_column(String(120))
    state: Mapped[str] = mapped_column(String(30), nullable=False)
    remote_state: Mapped[str | None] = mapped_column(String(40))
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    total_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    buyer_name: Mapped[str | None] = mapped_column(String(120))
    conversation_id: Mapped[str | None] = mapped_column(String(120))
    offer_title: Mapped[str | None] = mapped_column(String(300))
    category: Mapped[str | None] = mapped_column(String(40))
    remote_created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Fulfilment(TimestampMixin, Base):
    __tablename__ = "fulfilments"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    store_id: Mapped[int] = mapped_column(
        ForeignKey("stores.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    mode: Mapped[str] = mapped_column(String(20), nullable=False)
    require_confirmation: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    shadow: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    state: Mapped[FulfilmentState] = mapped_column(
        _enum(FulfilmentState, "fulfilment_state"), nullable=False, default=FulfilmentState.pending
    )
    units: Mapped[int] = mapped_column(Integer, nullable=False)
    pool_id: Mapped[int | None] = mapped_column(ForeignKey("stock_pools.id", ondelete="RESTRICT"))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    # Buyer confirmation (Twitch): when it was asked and the last chat message already read.
    confirmation_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    clarification_sent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    replies_read_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    buyer_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_by: Mapped[str | None] = mapped_column(String(200))
    state_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Reservation(Base):
    __tablename__ = "reservations"
    __table_args__ = (
        Index(
            "uq_reservations_active_item",
            "item_id",
            unique=True,
            postgresql_where=text("active"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    fulfilment_id: Mapped[int] = mapped_column(
        ForeignKey("fulfilments.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    item_id: Mapped[int] = mapped_column(
        ForeignKey("stock_items.id", ondelete="RESTRICT"), nullable=False
    )
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class FulfilmentEvent(Base):
    """Append-only timeline of a fulfilment, shown on the order page."""

    __tablename__ = "fulfilment_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    fulfilment_id: Mapped[int] = mapped_column(
        ForeignKey("fulfilments.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    actor_label: Mapped[str] = mapped_column(String(200), nullable=False)
    event: Mapped[str] = mapped_column(String(40), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
