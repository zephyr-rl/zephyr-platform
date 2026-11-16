"""Import an old store's restock setup (restock_config.json, read-only).

- Global switch, check interval and default target → the Restock module of the store.
- ``crossPlatformGroups`` → cross-platform cards (pool, target, on/off, extra text, e-mail
  details, the PC / PlayStation / Xbox offers). Offers not synced yet are attached later, by
  "Read offers from marketplace".
- ``offerMappings`` with a category → that listing's pool and vault settings. "Manual only"
  mappings (no category) are left alone.
- ``additionalInfoPresets`` are not imported: their text is already in each card.

Running it again updates what it created (cards are matched by the old group id).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.platform import Platform
from app.listings import groups, service
from app.listings.models import DEVICES, DeliveryMode, Listing, ListingGroup
from app.stock.models import StockPool
from app.stores import service as stores

CONFIG = "restock_config.json"


@dataclass
class RestockReport:
    module: str = ""
    cards_created: list[str] = field(default_factory=list)
    cards_updated: list[str] = field(default_factory=list)
    waiting_offers: list[str] = field(default_factory=list)  # card / platform not synced yet
    offers_set: list[str] = field(default_factory=list)
    offers_not_synced: int = 0
    manual_only: int = 0
    no_pool: list[str] = field(default_factory=list)
    presets_skipped: int = 0
    dry_run: bool = False


def _ranked_ready(name: str, title: str, explicit: Any) -> bool:
    # The old system's rule for uploading the e-mail details with the account.
    low_n, low_t = name.lower(), title.lower()
    return bool(
        explicit
        or ("ranked" in low_n and "ready" in low_n)
        or ("ranked" in low_t and "ready" in low_t)
    )


async def migrate_restock(
    session: AsyncSession,
    platform: Platform,
    actor: audit.Actor,
    *,
    folder: Path,
    store_id: int,
    dry_run: bool = False,
) -> RestockReport:
    report = RestockReport(dry_run=dry_run)
    cfg = json.loads((folder / CONFIG).read_text(encoding="utf-8"))
    pools = {
        p.legacy_id: p
        for p in await session.scalars(select(StockPool).where(StockPool.store_id == store_id))
        if p.legacy_id
    }

    # 1. The module: on/off, interval (seconds in the old system, minutes here), default target.
    interval = max(1, math.ceil(int(cfg.get("checkInterval") or 60) / 60))
    target = max(1, min(100, int(cfg.get("targetStock") or 1)))
    await stores.set_module(
        session,
        platform,
        actor,
        store_id,
        "restock",
        enabled=bool(cfg.get("enabled")),
        settings={"interval_minutes": interval, "target_quantity": target},
    )
    report.module = (
        f"{'on' if cfg.get('enabled') else 'off'}, every {interval} min, default target {target}"
    )

    # 2. Cross-platform cards.
    for legacy_id, g in (cfg.get("crossPlatformGroups") or {}).items():
        title = str(g.get("title") or g.get("categoryName") or "Cross-platform card").strip()[:300]
        pool = pools.get(str(g.get("categoryId") or ""))
        if pool is None:
            report.no_pool.append(title[:60])
        group = await session.scalar(
            select(ListingGroup).where(
                ListingGroup.store_id == store_id, ListingGroup.legacy_id == str(legacy_id)
            )
        )
        created = group is None
        if group is None:
            group = ListingGroup(store_id=store_id, title=title, legacy_id=str(legacy_id))
            session.add(group)
        group.title = title
        group.pool_id = pool.id if pool else None
        group.restock_enabled = bool(g.get("enabled", True)) and pool is not None
        group.restock_target = max(1, min(100, int(g.get("targetStock") or target)))
        group.auto_resume = True
        group.vault_additional_info = str(g.get("additionalInfo") or "").strip()[:1000] or None
        group.include_email_details = _ranked_ready(
            str(g.get("categoryName") or ""), title, g.get("includeEmailDetails")
        )
        group.devices = {
            d: str((g.get("devices") or {}).get(d, {}).get("offerId") or "").strip()
            for d in DEVICES
            if str((g.get("devices") or {}).get(d, {}).get("offerId") or "").strip()
        }
        await session.flush()
        missing = await groups.apply(session, actor, group)
        audit.record(
            session,
            actor,
            "listing_group.imported",
            entity_type="listing_group",
            entity_id=group.id,
            store_id=store_id,
            after=groups.snapshot(group),
        )
        (report.cards_created if created else report.cards_updated).append(title[:60])
        report.waiting_offers += [f"{title[:40]} / {DEVICES[d]}" for d in missing]

    # 3. Single offers with a category.
    for offer_id, m in (cfg.get("offerMappings") or {}).items():
        pool = pools.get(str(m.get("categoryId") or ""))
        if pool is None:
            report.manual_only += 1
            continue
        listing = await session.scalar(
            select(Listing).where(Listing.store_id == store_id, Listing.remote_offer_id == offer_id)
        )
        if listing is None:
            report.offers_not_synced += 1
            continue
        if listing.group_id is not None:
            continue
        changes: dict[str, Any] = {
            "pool_id": pool.id,
            "delivery_mode": DeliveryMode.manual,
            "restock_enabled": bool(m.get("enabled")),
            "restock_target": max(1, min(100, int(m.get("targetStock") or target))),
            "auto_resume": bool(m.get("autoResume", True)),
            "include_email_details": _ranked_ready(
                pool.name, listing.title, m.get("includeEmailDetails")
            ),
        }
        if m.get("additionalInfo"):
            changes["vault_additional_info"] = str(m["additionalInfo"]).strip()[:1000]
        await service.update_listing(session, actor, listing, changes)
        report.offers_set.append(listing.title[:60])

    report.presets_skipped = len(cfg.get("additionalInfoPresets") or {})
    if dry_run:
        await session.rollback()
    else:
        await session.commit()
    return report


def format_restock_report(r: RestockReport, store: str) -> str:
    lines = [f"Legacy restock setup for {store}{' (DRY RUN, nothing saved)' if r.dry_run else ''}"]
    lines.append(f"Restock module: {r.module}")
    lines.append(f"Cross-platform cards created: {', '.join(r.cards_created) or 'none'}")
    lines.append(f"Cross-platform cards updated: {', '.join(r.cards_updated) or 'none'}")
    if r.no_pool:
        lines.append(f"Cards without a matching pool (restock left off): {', '.join(r.no_pool)}")
    if r.waiting_offers:
        lines.append(
            f"Offers not synced yet ({len(r.waiting_offers)}; attached by 'Read offers from "
            f"marketplace'): {', '.join(r.waiting_offers)}"
        )
    lines.append(
        f"Single offers set up: {len(r.offers_set)}; not synced yet: {r.offers_not_synced}"
    )
    lines.append(f"Single offers left as they are (manual only in the old system): {r.manual_only}")
    if r.presets_skipped:
        lines.append(f"Extra-text presets not imported (already in each card): {r.presets_skipped}")
    return "\n".join(lines)
