from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base, TimestampMixin


class Store(TimestampMixin, Base):
    __tablename__ = "stores"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    slug: Mapped[str] = mapped_column(String(60), unique=True, nullable=False)
    timezone: Mapped[str] = mapped_column(String(60), nullable=False, default="UTC")
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Every marketplace write of this store is replaced by a recorded decision (invariant I9).
    shadow_mode: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    connections: Mapped[list[MarketplaceConnection]] = relationship(back_populates="store")
    modules: Mapped[list[StoreModule]] = relationship(back_populates="store")


class MarketplaceConnection(TimestampMixin, Base):
    __tablename__ = "marketplace_connections"
    __table_args__ = (UniqueConstraint("store_id", "connector_key", "label"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = mapped_column(
        ForeignKey("stores.id", ondelete="CASCADE"), nullable=False, index=True
    )
    connector_key: Mapped[str] = mapped_column(String(40), nullable=False)
    label: Mapped[str] = mapped_column(String(80), nullable=False, default="main")
    # AES-GCM ciphertext of the credentials JSON (invariant I7). Never returned by the API.
    credentials_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    # Last characters of each credential, for display ("••••••a1b2").
    credentials_hint: Mapped[dict[str, str]] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="unknown")
    status_detail: Mapped[str | None] = mapped_column(Text)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    store: Mapped[Store] = relationship(back_populates="connections")


class StoreModule(TimestampMixin, Base):
    __tablename__ = "store_modules"
    __table_args__ = (UniqueConstraint("store_id", "module_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = mapped_column(
        ForeignKey("stores.id", ondelete="CASCADE"), nullable=False, index=True
    )
    module_key: Mapped[str] = mapped_column(String(60), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    # Incremented on every change; the audit log keeps every previous version (invariant I8).
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    store: Mapped[Store] = relationship(back_populates="modules")
