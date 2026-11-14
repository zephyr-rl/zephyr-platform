"""Listings: the store's offers on a marketplace and how each one is fulfilled."""

from __future__ import annotations

import enum
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, TimestampMixin


class DeliveryMode(enum.StrEnum):
    # Nothing is sent automatically. The default for every new listing.
    manual = "manual"
    # Stock is reserved and the order waits for the owner to approve the delivery.
    assisted = "assisted"
    # Stock is delivered by itself as soon as the order is paid.
    auto = "auto"
    # Only a message is sent (a guide, instructions); no stock is used.
    text = "text"


NEEDS_POOL = frozenset({DeliveryMode.auto, DeliveryMode.assisted})


class Listing(TimestampMixin, Base):
    __tablename__ = "listings"
    __table_args__ = (UniqueConstraint("connection_id", "remote_offer_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = mapped_column(
        ForeignKey("stores.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    connection_id: Mapped[int] = mapped_column(
        ForeignKey("marketplace_connections.id", ondelete="RESTRICT"), nullable=False
    )
    remote_offer_id: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    remote_state: Mapped[str] = mapped_column(String(20), nullable=False)
    price_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    remote_quantity: Mapped[int | None] = mapped_column(Integer)
    removed_remotely: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # How the listing is fulfilled (set by the owner).
    pool_id: Mapped[int | None] = mapped_column(ForeignKey("stock_pools.id", ondelete="RESTRICT"))
    delivery_mode: Mapped[DeliveryMode] = mapped_column(
        Enum(DeliveryMode, name="delivery_mode", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=DeliveryMode.manual,
    )
    units_per_sale: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    require_confirmation: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    delivery_template: Mapped[str | None] = mapped_column(Text)
    vault_additional_info: Mapped[str | None] = mapped_column(Text)
    include_email_details: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
