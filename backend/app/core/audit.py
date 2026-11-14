"""Append-only audit log (invariant I5).

The table is protected by a database trigger (see the initial migration): UPDATE and DELETE
are rejected, so history cannot be rewritten from the application.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.core.crypto import REDACTED
from app.core.db import Base

_SECRET_KEY_PATTERN = re.compile(
    r"pass(word)?|secret|token|api[_-]?key|webhook|credential|cookie|authorization|private",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Actor:
    """Who performed an action: a user, or a system process such as the worker."""

    label: str
    user_id: int | None = None
    ip: str | None = None

    @classmethod
    def system(cls, name: str) -> Actor:
        return cls(label=f"system:{name}")


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    actor_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    actor_label: Mapped[str] = mapped_column(String(200), nullable=False)
    actor_ip: Mapped[str | None] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    entity_type: Mapped[str | None] = mapped_column(String(60))
    entity_id: Mapped[str | None] = mapped_column(String(100))
    store_id: Mapped[int | None] = mapped_column(
        ForeignKey("stores.id", ondelete="SET NULL"), index=True
    )
    before: Mapped[Any | None] = mapped_column(JSONB)
    after: Mapped[Any | None] = mapped_column(JSONB)
    reason: Mapped[str | None] = mapped_column(Text)


def redact(value: Any) -> Any:
    """Return a copy of ``value`` with every secret-looking field replaced."""
    if isinstance(value, dict):
        return {
            k: (
                REDACTED
                if _SECRET_KEY_PATTERN.search(str(k)) and v not in (None, "")
                else redact(v)
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def record(
    session: AsyncSession,
    actor: Actor,
    action: str,
    *,
    entity_type: str | None = None,
    entity_id: object | None = None,
    store_id: int | None = None,
    before: Any = None,
    after: Any = None,
    reason: str | None = None,
) -> AuditLog:
    """Add an audit entry to the current transaction (committed with the change it describes)."""
    entry = AuditLog(
        actor_user_id=actor.user_id,
        actor_label=actor.label,
        actor_ip=actor.ip,
        action=action,
        entity_type=entity_type,
        entity_id=None if entity_id is None else str(entity_id),
        store_id=store_id,
        before=redact(before),
        after=redact(after),
        reason=reason,
    )
    session.add(entry)
    return entry
