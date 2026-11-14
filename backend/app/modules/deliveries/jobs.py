"""Job handlers of the deliveries module."""

from __future__ import annotations

from typing import Any

from app.connectors.base import ConnectorError
from app.core.jobs import JobContext, RetryLater, backoff
from app.core.modules import ModuleContext
from app.core.platform import Platform
from app.modules.deliveries import service
from app.orders.models import Fulfilment, FulfilmentState


def _platform(ctx: JobContext) -> Platform:
    platform: Platform = ctx.services["platform"]
    return platform


async def poll(ctx: JobContext, payload: dict[str, Any]) -> dict[str, Any]:
    assert ctx.store_id is not None
    result = await service.poll_store(_platform(ctx), ctx.store_id)
    return {"seen": result.seen, "new_paid": result.new_paid}


async def process(ctx: JobContext, payload: dict[str, Any]) -> dict[str, Any]:
    fulfilment_id = int(payload["fulfilment_id"])
    try:
        state = await service.process(_platform(ctx), fulfilment_id)
    except ConnectorError:
        async with ctx.sessionmaker() as session:
            fulfilment = await session.get(Fulfilment, fulfilment_id)
            assert fulfilment is not None
            if fulfilment.state is FulfilmentState.needs_attention:
                return {"state": fulfilment.state.value}
            raise RetryLater(backoff(fulfilment.attempts), fulfilment.last_error or "") from None
    return {"state": state.value if state else "locked"}


async def check_replies(ctx: JobContext, payload: dict[str, Any]) -> dict[str, Any]:
    assert ctx.store_id is not None
    return {"decisions": await service.check_replies(_platform(ctx), ctx.store_id)}


def register(ctx: ModuleContext) -> None:
    ctx.jobs.register("orders.poll", poll)
    ctx.jobs.register(service.PROCESS_JOB, process)
    ctx.jobs.register("deliveries.check_replies", check_replies)
