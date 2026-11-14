"""Job handlers of the restock module."""

from __future__ import annotations

from typing import Any

from app.core.jobs import JobContext
from app.core.modules import ModuleContext
from app.core.platform import Platform
from app.modules.restock import service


async def run(ctx: JobContext, payload: dict[str, Any]) -> dict[str, Any]:
    assert ctx.store_id is not None
    platform: Platform = ctx.services["platform"]
    if "listing_id" in payload:
        results = [await service.restock_listing(platform, int(payload["listing_id"]))]
    else:
        results = await service.run_store(platform, ctx.store_id)
    return {
        "listings": len(results),
        "uploaded": sum(r.uploaded for r in results),
        "quarantined": sum(r.quarantined for r in results),
    }


def register(ctx: ModuleContext) -> None:
    ctx.jobs.register(service.RUN_JOB, run)
