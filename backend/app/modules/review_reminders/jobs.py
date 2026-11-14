"""Job handlers of the review reminders module."""

from __future__ import annotations

from typing import Any

from app.core.jobs import JobContext
from app.core.modules import ModuleContext
from app.core.platform import Platform
from app.modules.review_reminders import service


async def send(ctx: JobContext, payload: dict[str, Any]) -> dict[str, Any]:
    assert ctx.store_id is not None
    platform: Platform = ctx.services["platform"]
    return await service.send_next(platform, ctx.store_id)


async def automatic(ctx: JobContext, payload: dict[str, Any]) -> dict[str, Any]:
    assert ctx.store_id is not None
    platform: Platform = ctx.services["platform"]
    return {"queued": await service.run_automatic(platform, ctx.store_id)}


def register(ctx: ModuleContext) -> None:
    ctx.jobs.register(service.SEND_JOB, send)
    ctx.jobs.register(service.AUTO_JOB, automatic)
