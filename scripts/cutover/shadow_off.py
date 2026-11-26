"""Cut-over: take Zephyr out of shadow mode (audited) and list paid orders caught in shadow."""

import asyncio

from sqlalchemy import select

from app.core.audit import Actor
from app.core.config import get_settings
from app.core.platform import build_platform
from app.orders.models import Fulfilment, FulfilmentState, Order
from app.stores import service as stores
from app.stores.models import Store


async def main() -> None:
    platform = build_platform(get_settings())
    try:
        async with platform.db.sessionmaker() as session:
            store = await session.scalar(select(Store).where(Store.slug == "zephyr"))
            assert store is not None
            print("shadow before:", store.shadow_mode)
            await stores.update_store(
                session, Actor.system("claude:cutover-owner-ok"), store, {"shadow_mode": False}
            )
            await session.commit()
            print("shadow after:", store.shadow_mode)
            rows = await session.execute(
                select(Order.remote_id, Order.state, Fulfilment.state)
                .join(Fulfilment, Fulfilment.order_id == Order.id)
                .where(Order.store_id == store.id, Fulfilment.state == FulfilmentState.simulated)
                .where(Order.state.not_in(["delivered", "completed", "cancelled", "refunded"]))
            )
            caught = list(rows)
            print("open orders with a shadow fulfilment:", [(r[0][:8], str(r[1])) for r in caught])
    finally:
        await platform.db.dispose()


asyncio.run(main())
