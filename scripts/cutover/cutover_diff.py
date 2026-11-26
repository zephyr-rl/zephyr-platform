"""Cut-over check: what would change if the old Zephyr settings were imported again now.

Snapshots the platform, runs the three settings importers inside one transaction with commits
turned into flushes, snapshots again, prints the differences and rolls everything back.
Nothing is saved, nothing is sent (Discord transport refuses every request).
"""
import asyncio, json, sys
from pathlib import Path

import httpx
from sqlalchemy import select

from app.core.config import get_settings
from app.core.audit import Actor
from app.core.platform import build_platform
from app.listings.models import Listing, ListingGroup
from app.notifications import discord
from app.notifications.models import NotificationRule
from app.stores.models import Store, StoreModule
from app.legacy.items import migrate_items
from app.legacy.restock import migrate_restock
from app.legacy.settings import migrate_settings

FOLDER = Path(r"C:\EldoradoBots\Eldorado API - ZephyrStore")
SKIP = {"updated_at", "created_at", "last_restock_at", "last_restock_note", "vault_count",
        "last_synced_at", "synced_at", "remote_state", "price_amount", "stock_count"}


def _refuse(request: httpx.Request) -> httpx.Response:
    raise RuntimeError("network refused during the cut-over check")


discord.transport = httpx.MockTransport(_refuse)


def row(obj) -> dict:
    out = {}
    for c in obj.__table__.columns:
        if c.key in SKIP:
            continue
        v = getattr(obj, c.key)
        out[c.key] = v if isinstance(v, (str, int, float, bool, type(None), dict, list)) else str(v)
    return out


async def snap(session, store_id):
    data = {}
    for m in await session.scalars(select(StoreModule).where(StoreModule.store_id == store_id)):
        data[f"module {m.module_key}"] = {"enabled": m.enabled, **(m.settings or {})}
    for l in await session.scalars(select(Listing).where(Listing.store_id == store_id)):
        data[f"listing {l.id} {l.title[:45]}"] = row(l)
    for g in await session.scalars(select(ListingGroup).where(ListingGroup.store_id == store_id)):
        data[f"card {g.id} {g.title[:45]}"] = row(g)
    for r in await session.scalars(select(NotificationRule).where(NotificationRule.store_id == store_id)):
        d = row(r)
        d.pop("channel_id", None)
        data[f"rule {r.id} {r.event_type}"] = d
    st = await session.get(Store, store_id)
    data["store"] = {"timezone": getattr(st, "timezone", None)}
    return json.loads(json.dumps(data, default=str))


def short(v):
    s = json.dumps(v, ensure_ascii=False)
    return s if len(s) <= 300 else s[:300] + f"... ({len(s)} chars)"


async def main():
    platform = build_platform(get_settings())
    actor = Actor.system("cutover-check")
    try:
        async with platform.db.sessionmaker() as session:
            store_id = await session.scalar(select(Store.id).where(Store.slug == "zephyr"))
            before = await snap(session, store_id)
            session.commit = session.flush  # type: ignore[method-assign]
            await migrate_items(session, platform, actor, folder=FOLDER, store_id=store_id)
            await migrate_restock(session, platform, actor, folder=FOLDER, store_id=store_id)
            await migrate_settings(session, platform, actor, folder=FOLDER, store_id=store_id)
            await session.flush()
            after = await snap(session, store_id)
            await session.rollback()
    finally:
        await platform.db.dispose()
    n = 0
    for key in sorted(before.keys() | after.keys()):
        b, a = before.get(key), after.get(key)
        if b == a:
            continue
        if b is None or a is None:
            print(f"* {key}: {'would be created' if b is None else 'would be removed'}")
            n += 1
            continue
        for f in sorted(set(b) | set(a)):
            if b.get(f) != a.get(f):
                n += 1
                print(f"* {key} / {f}\n    platform now: {short(b.get(f))}\n    old system:   {short(a.get(f))}")
    print(f"\n{n} difference(s)")


asyncio.run(main())
