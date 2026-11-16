"""Listings: the store's offers on a marketplace and how each one is fulfilled."""

from __future__ import annotations

import enum
from datetime import datetime
from decimal import Decimal
from typing import Any

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
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
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

# The platforms a cross-platform card joins (one marketplace offer each).
DEVICES: dict[str, str] = {"pc": "PC", "playstation": "PlayStation", "xbox": "Xbox"}


class ListingGroup(TimestampMixin, Base):
    """A cross-platform card: the same product sold as one offer per platform (PC, PlayStation,
    Xbox), all filled from one stock pool with the same vault settings.

    The card's settings are copied to its listings, so restock works per listing as usual.
    ``devices`` keeps the offer id for each platform, also before the listing is synced."""

    __tablename__ = "listing_groups"
    __table_args__ = (UniqueConstraint("store_id", "legacy_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = mapped_column(
        ForeignKey("stores.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    pool_id: Mapped[int | None] = mapped_column(ForeignKey("stock_pools.id", ondelete="RESTRICT"))
    restock_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    restock_target: Mapped[int | None] = mapped_column(Integer)
    auto_resume: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    vault_additional_info: Mapped[str | None] = mapped_column(Text)
    include_email_details: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    devices: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    legacy_id: Mapped[str | None] = mapped_column(String(120))


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
    platform: Mapped[str | None] = mapped_column(String(80))
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

    # Restock (account listings with instant delivery from the marketplace vault).
    restock_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    restock_target: Mapped[int | None] = mapped_column(Integer)  # None: the store default
    auto_resume: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    vault_count: Mapped[int | None] = mapped_column(Integer)
    last_restock_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_restock_note: Mapped[str | None] = mapped_column(Text)
    # Part of a cross-platform card: its vault settings come from the card.
    group_id: Mapped[int | None] = mapped_column(
        ForeignKey("listing_groups.id", ondelete="SET NULL"), index=True
    )
    device: Mapped[str | None] = mapped_column(String(20))


class VaultUploadState(enum.StrEnum):
    pending = "pending"  # item reserved, upload sent or about to be; outcome not known yet
    uploaded = "uploaded"  # confirmed in the vault: the item is delivered (I1)
    failed = "failed"  # confirmed not in the vault: the item went back to stock


class VaultUpload(Base):
    """One stock item sent to a listing's vault. Replaces the old "upload ledger"."""

    __tablename__ = "vault_uploads"

    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = mapped_column(
        ForeignKey("stores.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    listing_id: Mapped[int] = mapped_column(
        ForeignKey("listings.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    # I1: an item is uploaded at most once, to one listing, ever.
    item_id: Mapped[int] = mapped_column(
        ForeignKey("stock_items.id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    state: Mapped[VaultUploadState] = mapped_column(
        Enum(
            VaultUploadState,
            name="vault_upload_state",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        default=VaultUploadState.pending,
    )
    remote_entry_id: Mapped[str | None] = mapped_column(String(120))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
