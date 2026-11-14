"""Stock: pools, items, extra secrets, item history, imports and extractions.

Invariants enforced by the database (see the stock migration):
- I1: an item can be ``delivered`` only once; ``delivered`` and ``void`` are final states.
- I3: items, their history and extraction records are never deleted.
- An account appears at most once per store (``unique(store_id, fingerprint)``), in any state, so
  an account that was already sold or removed can never be imported and sold again.
"""

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
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, TimestampMixin


class ItemType(enum.StrEnum):
    account = "account"
    code = "code"
    text = "text"


class ItemState(enum.StrEnum):
    available = "available"
    reserved = "reserved"  # held by an order being fulfilled (stage 5)
    delivered = "delivered"  # sent to a buyer through an order: final
    extracted = "extracted"  # taken out by hand (manual sale, moved elsewhere); can be undone
    quarantined = "quarantined"  # kept out of sale until reviewed
    void = "void"  # removed for good (invalid, banned, already sold elsewhere): final


def _enum(cls: type[enum.StrEnum], name: str) -> Enum:
    return Enum(cls, name=name, values_callable=lambda e: [m.value for m in e])


class StockPool(TimestampMixin, Base):
    __tablename__ = "stock_pools"
    __table_args__ = (
        UniqueConstraint("store_id", "name"),
        UniqueConstraint("store_id", "legacy_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = mapped_column(
        ForeignKey("stores.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    game: Mapped[str] = mapped_column(String(80), nullable=False, default="Rocket League")
    item_type: Mapped[ItemType] = mapped_column(
        _enum(ItemType, "stock_item_type"), nullable=False, default=ItemType.account
    )
    low_stock_threshold: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    legacy_id: Mapped[str | None] = mapped_column(String(80))


class StockItem(Base):
    __tablename__ = "stock_items"
    __table_args__ = (
        UniqueConstraint("store_id", "fingerprint"),
        Index("ix_stock_items_pool_state_position", "pool_id", "state", "position", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    store_id: Mapped[int] = mapped_column(
        ForeignKey("stores.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    pool_id: Mapped[int] = mapped_column(
        ForeignKey("stock_pools.id", ondelete="RESTRICT"), nullable=False
    )
    # HMAC-SHA256 of the normalised login: finds duplicates without storing logins in clear.
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    # AES-GCM ciphertext of {"login", "password", "email", "email_password", ...}.
    payload_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    login_hint: Mapped[str] = mapped_column(String(80), nullable=False)
    state: Mapped[ItemState] = mapped_column(
        _enum(ItemState, "stock_item_state"), nullable=False, default=ItemState.available
    )
    position: Mapped[int] = mapped_column(BigInteger, nullable=False)
    has_token: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    note: Mapped[str | None] = mapped_column(Text)
    source_import_id: Mapped[int | None] = mapped_column(ForeignKey("stock_imports.id"))
    legacy_ref: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    state_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class StockItemSecret(Base):
    """Extra secrets attached to an item, e.g. a Twitch token for drop accounts."""

    __tablename__ = "stock_item_secrets"
    __table_args__ = (UniqueConstraint("item_id", "kind"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    item_id: Mapped[int] = mapped_column(
        ForeignKey("stock_items.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    value_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class StockItemEvent(Base):
    """Append-only history of every item."""

    __tablename__ = "stock_item_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    item_id: Mapped[int] = mapped_column(
        ForeignKey("stock_items.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    actor_label: Mapped[str] = mapped_column(String(200), nullable=False)
    event: Mapped[str] = mapped_column(String(40), nullable=False)
    from_state: Mapped[str | None] = mapped_column(String(20))
    to_state: Mapped[str | None] = mapped_column(String(20))
    batch: Mapped[str | None] = mapped_column(String(60), index=True)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # Makes the legacy migration re-runnable: one event per old history entry.
    legacy_key: Mapped[str | None] = mapped_column(String(120), unique=True)


class StockImport(Base):
    __tablename__ = "stock_imports"

    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"), nullable=False, index=True)
    pool_id: Mapped[int] = mapped_column(ForeignKey("stock_pools.id"), nullable=False)
    actor_label: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    text_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    detected_format: Mapped[str] = mapped_column(String(40), nullable=False)
    counts: Mapped[dict[str, int]] = mapped_column(JSONB, nullable=False)


class StockExtraction(Base):
    __tablename__ = "stock_extractions"

    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"), nullable=False, index=True)
    pool_id: Mapped[int] = mapped_column(ForeignKey("stock_pools.id"), nullable=False)
    actor_label: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    count: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    undone_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
