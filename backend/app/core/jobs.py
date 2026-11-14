"""Database-backed job queue.

Workers claim jobs with ``SELECT … FOR UPDATE SKIP LOCKED`` so several workers can run at once
without taking the same job. Failed jobs are retried with exponential backoff and end in the
``dead`` state (the dead-letter view) after ``max_attempts``. A job may carry an idempotency key:
enqueueing the same key twice returns the existing job instead of creating a second one.
"""

from __future__ import annotations

import enum
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, utcnow

log = logging.getLogger(__name__)


class JobState(enum.StrEnum):
    queued = "queued"
    running = "running"
    done = "done"
    dead = "dead"


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        Index(
            "ix_jobs_ready",
            "run_at",
            postgresql_where=text("state = 'queued'"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    store_id: Mapped[int | None] = mapped_column(
        ForeignKey("stores.id", ondelete="CASCADE"), index=True
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    state: Mapped[JobState] = mapped_column(
        Enum(JobState, name="job_state", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=JobState.queued,
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(200), unique=True)
    run_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    locked_by: Mapped[str | None] = mapped_column(String(100))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


@dataclass
class JobContext:
    """What a job handler receives besides the payload."""

    job_id: int
    job_type: str
    store_id: int | None
    attempt: int
    sessionmaker: async_sessionmaker[AsyncSession]
    services: dict[str, Any] = field(default_factory=dict)


Handler = Callable[[JobContext, dict[str, Any]], Awaitable[dict[str, Any] | None]]


class RetryLater(Exception):
    """Raised by a handler to be retried after ``delay`` without counting as a failure."""

    def __init__(self, delay: timedelta, reason: str = "") -> None:
        super().__init__(reason)
        self.delay = delay


class JobRegistry:
    def __init__(self) -> None:
        self._handlers: dict[str, Handler] = {}

    def register(self, job_type: str, handler: Handler) -> None:
        if job_type in self._handlers:
            raise ValueError(f"Job type {job_type!r} already registered")
        self._handlers[job_type] = handler

    def handler(self, job_type: str) -> Callable[[Handler], Handler]:
        def decorator(fn: Handler) -> Handler:
            self.register(job_type, fn)
            return fn

        return decorator

    def get(self, job_type: str) -> Handler | None:
        return self._handlers.get(job_type)

    @property
    def types(self) -> list[str]:
        return sorted(self._handlers)


async def enqueue(
    session: AsyncSession,
    job_type: str,
    payload: dict[str, Any] | None = None,
    *,
    store_id: int | None = None,
    run_at: datetime | None = None,
    idempotency_key: str | None = None,
    max_attempts: int = 5,
) -> int:
    """Add a job in the current transaction. Returns the job id (existing one on key clash)."""
    values: dict[str, Any] = {
        "type": job_type,
        "payload": payload or {},
        "store_id": store_id,
        "run_at": run_at or utcnow(),
        "idempotency_key": idempotency_key,
        "max_attempts": max_attempts,
        "state": JobState.queued,
        "attempts": 0,
    }
    stmt = insert(Job).values(**values)
    if idempotency_key is not None:
        stmt = stmt.on_conflict_do_nothing(index_elements=[Job.idempotency_key])
    job_id = await session.scalar(stmt.returning(Job.id))
    if job_id is None:
        job_id = await session.scalar(select(Job.id).where(Job.idempotency_key == idempotency_key))
    assert job_id is not None
    return int(job_id)


async def claim(
    session: AsyncSession, worker_id: str, *, types: list[str] | None = None, limit: int = 1
) -> list[Job]:
    """Lock and mark as running up to ``limit`` ready jobs. Call inside a transaction."""
    stmt = (
        select(Job)
        .where(Job.state == JobState.queued, Job.run_at <= func.now())
        .order_by(Job.run_at, Job.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    if types is not None:
        stmt = stmt.where(Job.type.in_(types))
    jobs = list((await session.scalars(stmt)).all())
    now = utcnow()
    for job in jobs:
        job.state = JobState.running
        job.locked_by = worker_id
        job.locked_at = now
        job.attempts += 1
    return jobs


def backoff(attempt: int) -> timedelta:
    """5 s, 20 s, 80 s, … capped at one hour."""
    return timedelta(seconds=min(5 * 4 ** max(attempt - 1, 0), 3600))


async def recover_stale(session: AsyncSession, lock_timeout: timedelta) -> int:
    """Requeue jobs whose worker died while running them."""
    result = await session.execute(
        update(Job)
        .where(Job.state == JobState.running, Job.locked_at < utcnow() - lock_timeout)
        .values(state=JobState.queued, locked_by=None, locked_at=None, run_at=func.now())
    )
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


async def run_one(
    sessionmaker: async_sessionmaker[AsyncSession],
    registry: JobRegistry,
    worker_id: str,
    *,
    services: dict[str, Any] | None = None,
) -> bool:
    """Claim and execute a single job. Returns False when nothing was ready."""
    async with sessionmaker() as session, session.begin():
        claimed = await claim(session, worker_id, types=registry.types)
        if not claimed:
            return False
        job = claimed[0]
        job_id, job_type, store_id = job.id, job.type, job.store_id
        payload, attempt, max_attempts = dict(job.payload), job.attempts, job.max_attempts

    handler = registry.get(job_type)
    assert handler is not None
    ctx = JobContext(job_id, job_type, store_id, attempt, sessionmaker, services or {})
    values: dict[str, Any]
    try:
        result = await handler(ctx, payload)
    except RetryLater as wait:
        values = {
            "state": JobState.queued,
            "run_at": utcnow() + wait.delay,
            "attempts": attempt - 1,
            "last_error": str(wait) or None,
        }
    except Exception as exc:
        log.exception("Job %s (%s) failed on attempt %s", job_id, job_type, attempt)
        error = f"{type(exc).__name__}: {exc}"[:4000]
        if attempt >= max_attempts:
            values = {"state": JobState.dead, "last_error": error, "finished_at": utcnow()}
        else:
            values = {
                "state": JobState.queued,
                "run_at": utcnow() + backoff(attempt),
                "last_error": error,
            }
    else:
        values = {"state": JobState.done, "result": result, "finished_at": utcnow()}

    async with sessionmaker() as session, session.begin():
        await session.execute(
            update(Job).where(Job.id == job_id).values(locked_by=None, locked_at=None, **values)
        )
    return True
