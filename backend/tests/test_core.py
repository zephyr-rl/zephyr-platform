"""Crypto (I7), audit log (I5), outbox events and the job queue."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit, events, jobs
from app.core.crypto import CryptoError, SecretBox, generate_key, mask
from app.core.db import utcnow
from app.core.events import Event, EventBus, OutboxEvent
from app.core.jobs import Job, JobContext, JobRegistry, JobState, RetryLater
from app.core.platform import Platform
from tests.conftest import BACKEND

# ---- crypto ------------------------------------------------------------------------------


def test_secret_round_trip_and_context_binding() -> None:
    box = SecretBox(generate_key())
    token = box.encrypt("hunter2-password", context="connection:1:eldorado")
    assert "hunter2" not in token
    assert box.decrypt(token, context="connection:1:eldorado") == "hunter2-password"
    with pytest.raises(CryptoError):
        box.decrypt(token, context="connection:2:eldorado")


def test_same_plaintext_encrypts_differently() -> None:
    box = SecretBox(generate_key())
    assert box.encrypt("x") != box.encrypt("x")


def test_key_rotation_keeps_old_secrets_readable() -> None:
    old_key, new_key = generate_key(), generate_key()
    old_box = SecretBox(f"k1:{old_key}")
    token = old_box.encrypt_json({"api_key": "abc"})
    rotated = SecretBox(f"k2:{new_key},k1:{old_key}")
    assert rotated.decrypt_json(token) == {"api_key": "abc"}
    assert rotated.encrypt("y").startswith("k2:")
    with pytest.raises(CryptoError):
        SecretBox(f"k2:{new_key}").decrypt(token)


def test_tampered_ciphertext_is_rejected() -> None:
    box = SecretBox(generate_key())
    token = box.encrypt("value")
    key_id, payload = token.split(":", 1)
    tampered = f"{key_id}:{payload[:-4]}AAAA"
    with pytest.raises(CryptoError):
        box.decrypt(tampered)


def test_missing_or_bad_keys() -> None:
    with pytest.raises(CryptoError):
        SecretBox("").encrypt("x")
    with pytest.raises(CryptoError):
        SecretBox("k1:dG9vLXNob3J0")


def test_mask_shows_only_the_end() -> None:
    assert mask("abcdefghijkl1234") == "••••••1234"
    assert mask("short") == "••••••"


# ---- audit -------------------------------------------------------------------------------


def test_redact_hides_secret_fields_recursively() -> None:
    data = {
        "name": "x",
        "api_key": "abc",
        "nested": {"password": "p", "webhook_url": "https://…", "ok": 1},
        "items": [{"token": "t"}],
    }
    redacted = audit.redact(data)
    assert redacted["name"] == "x"
    assert redacted["api_key"] == "••••••"
    assert redacted["nested"] == {"password": "••••••", "webhook_url": "••••••", "ok": 1}
    assert redacted["items"] == [{"token": "••••••"}]


async def test_audit_log_is_append_only(session: AsyncSession, platform: Platform) -> None:
    audit.record(session, audit.Actor.system("tests"), "test.action", after={"a": 1})
    await session.commit()
    with pytest.raises(DBAPIError, match="append-only"):
        await session.execute(update(audit.AuditLog).values(action="rewritten"))
    await session.rollback()
    with pytest.raises(DBAPIError, match="append-only"):
        await session.execute(text("DELETE FROM audit_log"))
    await session.rollback()
    assert (await session.scalar(select(audit.AuditLog.action))) == "test.action"


# ---- events ------------------------------------------------------------------------------


async def test_events_exist_only_if_the_transaction_commits(session: AsyncSession) -> None:
    events.publish(session, "stock.low", {"pool": 1})
    await session.rollback()
    events.publish(session, "stock.depleted", {"pool": 1})
    await session.commit()
    assert (await session.scalars(select(OutboxEvent.type))).all() == ["stock.depleted"]


def test_unknown_event_types_are_rejected() -> None:
    bus = EventBus()
    with pytest.raises(ValueError):
        bus.subscribe("order.teleported", lambda e: asyncio.sleep(0))  # type: ignore[arg-type,return-value]


async def test_dispatch_delivers_and_marks_events(
    session: AsyncSession, platform: Platform
) -> None:
    received: list[Event] = []

    async def on_low(event: Event) -> None:
        received.append(event)

    bus = EventBus()
    bus.subscribe("stock.low", on_low)
    events.publish(session, "stock.low", {"pool": 7})
    events.publish(session, "stock.depleted", {"pool": 7})
    await session.commit()

    assert await bus.dispatch_pending(platform.db.sessionmaker) == 2
    assert [e.payload for e in received] == [{"pool": 7}]
    assert await bus.dispatch_pending(platform.db.sessionmaker) == 0  # nothing dispatched twice


async def test_failing_subscriber_keeps_event_pending(
    session: AsyncSession, platform: Platform
) -> None:
    async def boom(event: Event) -> None:
        raise RuntimeError("subscriber down")

    bus = EventBus()
    bus.subscribe("stock.low", boom)
    events.publish(session, "stock.low", {})
    await session.commit()
    assert await bus.dispatch_pending(platform.db.sessionmaker) == 0
    row = await session.scalar(select(OutboxEvent))
    await session.refresh(row)
    assert row is not None and row.dispatched_at is None and row.attempts == 1
    assert "subscriber down" in (row.last_error or "")


# ---- jobs --------------------------------------------------------------------------------


async def test_enqueue_with_idempotency_key_creates_one_job(session: AsyncSession) -> None:
    first = await jobs.enqueue(session, "deliver", {"order": "A1"}, idempotency_key="deliver:A1")
    second = await jobs.enqueue(session, "deliver", {"order": "A1"}, idempotency_key="deliver:A1")
    await session.commit()
    assert first == second
    assert len((await session.scalars(select(Job))).all()) == 1


async def test_job_runs_and_stores_result(session: AsyncSession, platform: Platform) -> None:
    registry = JobRegistry()
    seen: list[dict[str, Any]] = []

    @registry.handler("echo")
    async def echo(ctx: JobContext, payload: dict[str, Any]) -> dict[str, Any]:
        seen.append(payload)
        return {"echo": payload["x"], "attempt": ctx.attempt}

    await jobs.enqueue(session, "echo", {"x": 1})
    await session.commit()
    assert await jobs.run_one(platform.db.sessionmaker, registry, "w1") is True
    assert await jobs.run_one(platform.db.sessionmaker, registry, "w1") is False
    job = await session.scalar(select(Job))
    assert job is not None
    await session.refresh(job)
    assert job.state is JobState.done and job.result == {"echo": 1, "attempt": 1}
    assert seen == [{"x": 1}]


async def test_failed_job_retries_with_backoff_then_goes_dead(
    session: AsyncSession, platform: Platform
) -> None:
    registry = JobRegistry()

    @registry.handler("flaky")
    async def flaky(ctx: JobContext, payload: dict[str, Any]) -> None:
        raise ConnectionError("marketplace timeout")

    job_id = await jobs.enqueue(session, "flaky", max_attempts=3)
    await session.commit()
    for attempt in range(1, 4):
        assert await jobs.run_one(platform.db.sessionmaker, registry, "w1")
        job = await session.get(Job, job_id)
        assert job is not None
        await session.refresh(job)
        assert job.attempts == attempt
        if attempt < 3:
            assert job.state is JobState.queued
            assert job.run_at > utcnow() + jobs.backoff(attempt) - timedelta(seconds=5)
            await session.execute(update(Job).values(run_at=utcnow()))  # skip the wait
            await session.commit()
    assert job.state is JobState.dead
    assert "marketplace timeout" in (job.last_error or "")


def test_backoff_grows_and_is_capped() -> None:
    assert [jobs.backoff(n).total_seconds() for n in (1, 2, 3)] == [5, 20, 80]
    assert jobs.backoff(20) == timedelta(hours=1)


async def test_retry_later_does_not_count_as_attempt(
    session: AsyncSession, platform: Platform
) -> None:
    registry = JobRegistry()

    @registry.handler("wait")
    async def wait(ctx: JobContext, payload: dict[str, Any]) -> None:
        raise RetryLater(timedelta(minutes=5), "rate limited")

    job_id = await jobs.enqueue(session, "wait", max_attempts=1)
    await session.commit()
    await jobs.run_one(platform.db.sessionmaker, registry, "w1")
    job = await session.get(Job, job_id)
    assert job is not None
    await session.refresh(job)
    assert job.state is JobState.queued and job.attempts == 0
    assert job.run_at > utcnow() + timedelta(minutes=4)


async def test_concurrent_workers_never_run_a_job_twice(
    session: AsyncSession, platform: Platform
) -> None:
    registry = JobRegistry()
    runs: list[int] = []

    @registry.handler("count")
    async def count(ctx: JobContext, payload: dict[str, Any]) -> None:
        runs.append(payload["n"])
        await asyncio.sleep(0.01)

    for n in range(40):
        await jobs.enqueue(session, "count", {"n": n})
    await session.commit()

    async def worker(name: str) -> None:
        while await jobs.run_one(platform.db.sessionmaker, registry, name):
            pass

    await asyncio.gather(*(worker(f"w{i}") for i in range(8)))
    assert sorted(runs) == list(range(40))


async def test_stale_running_jobs_are_requeued(session: AsyncSession) -> None:
    job_id = await jobs.enqueue(session, "x")
    await session.commit()
    claimed = await jobs.claim(session, "dead-worker")
    await session.commit()
    assert [j.id for j in claimed] == [job_id]
    await session.execute(update(Job).values(locked_at=utcnow() - timedelta(minutes=30)))
    assert await jobs.recover_stale(session, timedelta(minutes=5)) == 1
    await session.commit()
    job = await session.get(Job, job_id)
    assert job is not None
    await session.refresh(job)
    assert job.state is JobState.queued and job.locked_by is None


def test_every_process_maps_every_table() -> None:
    """The worker and scheduler import only the platform; all foreign keys must still resolve."""
    import subprocess
    import sys

    code = (
        "from app.core.platform import build_platform\n"
        "from app.core.db import Base\n"
        "Base.registry.configure()\n"
        "assert {'stores', 'users', 'audit_log', 'jobs'} <= set(Base.metadata.tables)\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True, cwd=str(BACKEND))


def test_http_client_logs_never_show_urls() -> None:
    """Discord webhook URLs are secrets; httpx logs every request URL at INFO."""
    import logging

    from app.core.logging import configure_logging

    root = logging.getLogger()
    saved = root.handlers[:]
    root.handlers[:] = []
    try:
        configure_logging("INFO")
        assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    finally:
        root.handlers[:] = saved
