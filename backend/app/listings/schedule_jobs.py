"""Job handler of the offer schedule."""

from __future__ import annotations

from typing import Any

from app.core.jobs import JobContext
from app.core.modules import ModuleContext
from app.core.platform import Platform
from app.listings import schedule


async def run(ctx: JobContext, payload: dict[str, Any]) -> dict[str, Any]:
    assert ctx.store_id is not None
    platform: Platform = ctx.services["platform"]
    return {"changed": await schedule.run_store(platform, ctx.store_id)}


def register(ctx: ModuleContext) -> None:
    ctx.jobs.register(schedule.RUN_JOB, run)
