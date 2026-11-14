"""Job handlers of the chat monitor module."""

from __future__ import annotations

from typing import Any

from app.core.jobs import JobContext
from app.core.modules import ModuleContext
from app.core.platform import Platform
from app.modules.chat_monitor import service


async def scan(ctx: JobContext, payload: dict[str, Any]) -> dict[str, Any]:
    assert ctx.store_id is not None
    platform: Platform = ctx.services["platform"]
    result = await service.scan_store(platform, ctx.store_id)
    return {
        "conversations": result.conversations,
        "alerts": result.alerts,
        "errors": result.errors[:5],
    }


def register(ctx: ModuleContext) -> None:
    ctx.jobs.register(service.SCAN_JOB, scan)
