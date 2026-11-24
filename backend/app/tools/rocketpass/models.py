"""Saved Epic accounts (encrypted sessions) and the history of Rocket Pass checks."""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Enum, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, TimestampMixin


class CheckKind(enum.StrEnum):
    check = "check"  # read the pass
    claim = "claim"  # claim a pending pass (Fortnite Crew) and wait for it
    xbox = "xbox"  # Xbox Live sync first, then claim


class CheckState(enum.StrEnum):
    waiting = "waiting"  # waiting for the owner to approve the Microsoft sign-in
    queued = "queued"
    running = "running"
    done = "done"
    failed = "failed"


class EpicAccount(TimestampMixin, Base):
    __tablename__ = "rp_epic_accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    # Encrypted (SecretBox, context "rp-epic:<account_id>"). Empty when the session expired.
    refresh_token: Mapped[str | None] = mapped_column(Text)
    refresh_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    xbox_refresh_token: Mapped[str | None] = mapped_column(Text)  # encrypted, "rp-xbox:<id>"
    xbox_gamertag: Mapped[str | None] = mapped_column(String(120))
    last_result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PassCheck(TimestampMixin, Base):
    __tablename__ = "rp_checks"

    id: Mapped[int] = mapped_column(primary_key=True)
    epic_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("rp_epic_accounts.id", ondelete="SET NULL"), index=True
    )
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[CheckKind] = mapped_column(Enum(CheckKind, name="rp_check_kind"), nullable=False)
    state: Mapped[CheckState] = mapped_column(
        Enum(CheckState, name="rp_check_state"), nullable=False
    )
    progress: Mapped[str | None] = mapped_column(String(200))
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(String(200), nullable=False)
    # The Microsoft sign-in while it waits for the owner (device code encrypted).
    xbox_user_code: Mapped[str | None] = mapped_column(String(32))
    xbox_verification_uri: Mapped[str | None] = mapped_column(String(300))
    xbox_device_code: Mapped[str | None] = mapped_column(Text)
    xbox_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
