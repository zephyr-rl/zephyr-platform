"""Read-only: vault contents of the Zephyr cards to trim, and how the platform knows each account."""
import asyncio
import collections

from sqlalchemy import select

from app.core.config import get_settings
from app.core.platform import build_platform
from app.connectors.runtime import open_connector
from app.listings.models import Listing, ListingGroup
from app.stock import service as stock
from app.stock.models import StockPool
from app.stores.models import MarketplaceConnection, Store

WANT = ("113 DROPS", "69 DROPS", "Ranked Ready Driver")


async def main() -> None:
    platform = build_platform(get_settings())
    try:
        async with platform.db.sessionmaker() as s:
            store = await s.scalar(select(Store).where(Store.slug == "zephyr"))
            groups = [g for g in await s.scalars(select(ListingGroup).where(ListingGroup.store_id == store.id))
                      if any(w in g.title for w in WANT)]
            for g in groups:
                pool = await s.get(StockPool, g.pool_id) if g.pool_id else None
                print(f"\nCARD {g.id} {g.title[:40]!r} target={g.restock_target} pool={pool.name if pool else None} restock={g.restock_enabled}")
                for lst in await s.scalars(select(Listing).where(Listing.group_id == g.id)):
                    conn = await s.get(MarketplaceConnection, lst.connection_id)
                    c = await open_connector(s, platform, conn)
                    try:
                        vault = await c.vault_list(lst.remote_offer_id)
                    finally:
                        await c.aclose()
                    states = collections.Counter()
                    ids = sum(1 for e in vault if e.remote_id)
                    for e in vault:
                        it = await stock.find_by_login(s, platform, store.id, e.login)
                        if it is None:
                            states["not in platform"] += 1
                        else:
                            p = await s.get(StockPool, it.pool_id)
                            states[f"{it.state.value} in {p.name}"] += 1
                    print(f"  {lst.device:12} listing={lst.id} state={lst.remote_state:7} vault={len(vault)} with_id={ids} -> {dict(states)}")
    finally:
        await platform.db.dispose()


asyncio.run(main())
