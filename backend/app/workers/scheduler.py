"""Scheduler process: enqueues periodic jobs registered by modules.

Only one scheduler is active at a time (PostgreSQL advisory lock). Each run of a periodic task is
enqueued with an idempotency key built from the time bucket, so a restart or a second scheduler
can never enqueue the same run twice.

``python -m app.workers.scheduler``
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from datetime import datetime

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import jobs
from app.core.config import get_settings
from app.core.db import utcnow
from app.core.logging import configure_logging
from app.core.modules import PeriodicTask
from app.core.platform import Platform, build_platform
from app.stores.models import Store, StoreModule

log = logging.getLogger("scheduler")

LEADER_LOCK_ID = 7_310_001


def bucket(now: datetime, task: PeriodicTask) -> int:
    return int(now.timestamp() // task.interval.total_seconds())


async def _target_stores(session: AsyncSession, task: PeriodicTask) -> list[int]:
    stmt = select(Store.id).where(Store.is_active.is_(True))
    if task.module_key:
        stmt = stmt.join(
            StoreModule,
            (StoreModule.store_id == Store.id)
            & (StoreModule.module_key == task.module_key)
            & StoreModule.enabled.is_(True),
        )
    return list(await session.scalars(stmt))


async def enqueue_due(platform: Platform, now: datetime | None = None) -> int:
    """Enqueue the current run of every periodic task. Safe to call any number of times."""
    now = now or utcnow()
    created = 0
    async with platform.db.sessionmaker() as session, session.begin():
        for task in platform.module_context.periodic:
            b = bucket(now, task)
            store_ids: list[int | None] = (
                list(await _target_stores(session, task)) if task.per_store else [None]
            )
            for store_id in store_ids:
                key = f"periodic:{task.name}:{store_id or '-'}:{b}"
                before = await session.scalar(
                    select(jobs.Job.id).where(jobs.Job.idempotency_key == key)
                )
                await jobs.enqueue(
                    session, task.job_type, {"bucket": b}, store_id=store_id, idempotency_key=key
                )
                created += before is None
    return created


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    platform = build_platform(settings)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    async with platform.db.engine.connect() as conn:
        while not stop.is_set():
            got = await conn.scalar(text(f"select pg_try_advisory_lock({LEADER_LOCK_ID})"))
            await conn.commit()
            if got:
                break
            log.info("another scheduler is active; waiting")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), 15)
        log.info("scheduler is the leader", extra={"tasks": len(platform.module_context.periodic)})
        while not stop.is_set():
            try:
                created = await enqueue_due(platform)
                if created:
                    log.info("enqueued periodic jobs", extra={"count": created})
            except Exception:
                log.exception("scheduler round failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), 5)
    await platform.db.dispose()


if __name__ == "__main__":
    asyncio.run(main())
