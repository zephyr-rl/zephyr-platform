"""Worker process: runs queued jobs and dispatches outbox events.

``python -m app.workers.worker`` — several replicas can run at the same time.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import socket
from datetime import timedelta

from app.core import jobs, stopfile
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.platform import Platform, build_platform

log = logging.getLogger("worker")


class Worker:
    def __init__(self, platform: Platform, worker_id: str | None = None) -> None:
        self.platform = platform
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def tick(self) -> bool:
        """Run one round. Returns True if anything was done (so the loop does not sleep)."""
        sm = self.platform.db.sessionmaker
        did_job = await jobs.run_one(
            sm, self.platform.jobs, self.worker_id, services={"platform": self.platform}
        )
        dispatched = await self.platform.events.dispatch_pending(sm)
        return did_job or dispatched > 0

    async def run(self) -> None:
        settings = self.platform.settings
        log.info("worker started", extra={"worker_id": self.worker_id})
        last_recovery = 0.0
        loop = asyncio.get_running_loop()
        while not self._stop.is_set():
            if loop.time() - last_recovery > 60:
                async with self.platform.db.sessionmaker() as session, session.begin():
                    recovered = await jobs.recover_stale(
                        session, timedelta(seconds=settings.job_lock_timeout_seconds)
                    )
                if recovered:
                    log.warning("requeued stale jobs", extra={"count": recovered})
                last_recovery = loop.time()
            try:
                busy = await self.tick()
            except Exception:
                log.exception("worker tick failed")
                busy = False
            if not busy:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), settings.worker_poll_seconds)
        log.info("worker stopped")


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    platform = build_platform(settings)
    worker = Worker(platform)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # Windows
            loop.add_signal_handler(sig, worker.stop)
    watcher = asyncio.create_task(stopfile.watch(settings.stop_file, worker.stop))
    try:
        await worker.run()
    finally:
        watcher.cancel()
        await platform.db.dispose()


if __name__ == "__main__":
    asyncio.run(main())
