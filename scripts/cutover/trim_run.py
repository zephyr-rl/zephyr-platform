"""Pull back extras for the 3 Zephyr cards. Usage: trim_run.py [--apply]"""
import asyncio
import sys

from sqlalchemy import select

from app.core.audit import Actor
from app.core.config import get_settings
from app.core.platform import build_platform
from app.listings.models import ListingGroup
from app.modules.restock import trim
from app.stores.models import Store

PLAN = {
    "Ranked Ready Driver": {"pc": 5, "playstation": 1, "xbox": 5},
    "69 DROPS": {"pc": 2, "playstation": 2, "xbox": 2},
    "113 DROPS": {"pc": 2, "playstation": 2, "xbox": 2},
}


async def main(apply: bool) -> None:
    platform = build_platform(get_settings())
    actor = Actor.system("claude:owner-asked-trim")
    try:
        async with platform.db.sessionmaker() as s:
            store = await s.scalar(select(Store).where(Store.slug == "zephyr"))
            groups = list(await s.scalars(select(ListingGroup).where(ListingGroup.store_id == store.id)))
            for key, keep in PLAN.items():
                g = next(x for x in groups if key in x.title)
                r = await trim.trim(s, platform, actor, g, keep, apply=apply)
                print(f"\n{key} -> pool {r.pool}: {'DONE' if r.done else 'plan'}, {r.to_remove} to pull back")
                for p in r.platforms:
                    print(f"  {p.device:12} {p.offer_state:7} vault {p.in_vault:3} keep {p.keep:3} "
                          f"pull {len(p.remove):3} (known {p.known}) removed {p.removed} returned {p.returned} "
                          f"quarantined {p.quarantined} errors {len(p.errors)}")
                    for e in p.errors[:5]:
                        print("     !", e)
    finally:
        await platform.db.dispose()


asyncio.run(main("--apply" in sys.argv))
