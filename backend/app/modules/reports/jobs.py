"""Job handlers of the reports module."""

from __future__ import annotations

from typing import Any

from app.core.jobs import JobContext
from app.core.modules import ModuleContext
from app.core.platform import Platform
from app.modules.reports import service


async def daily(ctx: JobContext, payload: dict[str, Any]) -> dict[str, Any]:
    assert ctx.store_id is not None
    platform: Platform = ctx.services["platform"]
    day = await service.run_daily(platform, ctx.store_id)
    return {"reported": day.isoformat() if day else None}


def register(ctx: ModuleContext) -> None:
    ctx.jobs.register(service.DAILY_JOB, daily)
