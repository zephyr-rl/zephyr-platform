"""Turn Zephyr's Restock module on (owner's request), keeping its saved settings."""
import asyncio

from sqlalchemy import select

from app.core.audit import Actor
from app.core.config import get_settings
from app.core.platform import build_platform
from app.stores import service as stores
from app.stores.models import Store, StoreModule


async def main() -> None:
    platform = build_platform(get_settings())
    try:
        async with platform.db.sessionmaker() as s:
            store = await s.scalar(select(Store).where(Store.slug == "zephyr"))
            row = await s.scalar(
                select(StoreModule).where(
                    StoreModule.store_id == store.id, StoreModule.module_key == "restock"
                )
            )
            print("before:", row.enabled if row else None, row.settings if row else None)
            await stores.set_module(
                s, platform, Actor.system("claude:owner-asked-restock-on"), store.id, "restock",
                enabled=True, settings=dict(row.settings) if row else None,
            )
            await s.commit()
            await s.refresh(row)
            print("after:", row.enabled, row.settings, "shadow:", store.shadow_mode)
    finally:
        await platform.db.dispose()


asyncio.run(main())
