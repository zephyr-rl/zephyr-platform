"""Import an old store's item deliveries (item_delivery_config.json, read-only).

The old "Item Delivery" sent accounts from a category for every custom-item order:

- global ``enabled`` → the store's "Automatic deliveries" switch;
- ``defaultGuideText`` → the Deliveries message ({items} first, then the guide, as before);
  each account is written "🟢Twitch Account N / - User / - Password", the old format;
- ``twitchConfirmationFilter`` → the CONFIRM / CANCEL texts (and whether they are asked);
- ``offerMappings`` → each offer's pool, delivery mode (automatic, or held for approval),
  accounts per unit, confirmation and its own guide. "Tip" offers become text-only listings.

Offers the platform has not read yet are counted; ``sync_first`` reads the store's offers
from the marketplace before mapping (only reads, nothing is changed there). Running it again
sets the same values.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.platform import Platform
from app.listings import groups, service
from app.listings.models import DeliveryMode, Listing
from app.stock.models import StockPool
from app.stores import service as stores
from app.stores.models import StoreModule

CONFIG = "item_delivery_config.json"
TIP_IDS = {"__tip__", "tip", "text"}
ITEM_LABEL = "Twitch Account"  # the old message always said this


@dataclass
class ItemsReport:
    synced: str = ""
    automatic: bool = True
    confirmation: bool = True
    offers_set: list[str] = field(default_factory=list)
    text_offers: list[str] = field(default_factory=list)
    not_synced: list[str] = field(default_factory=list)  # old offer ids (first 8)
    no_pool: list[str] = field(default_factory=list)
    in_card: list[str] = field(default_factory=list)
    dry_run: bool = False


def _template(guide: str | None) -> str:
    guide = (guide or "").strip()
    return f"{{items}}\n\n{guide}" if guide else "{items}"


async def migrate_items(
    session: AsyncSession,
    platform: Platform,
    actor: audit.Actor,
    *,
    folder: Path,
    store_id: int,
    dry_run: bool = False,
    sync_first: bool = False,
) -> ItemsReport:
    report = ItemsReport(dry_run=dry_run)
    cfg: dict[str, Any] = json.loads((folder / CONFIG).read_text(encoding="utf-8"))

    if sync_first:
        result = await service.sync_listings(session, platform, actor, store_id)
        await session.flush()
        linked = await groups.link_pending(session, actor, store_id)
        report.synced = (
            f"{result.created} new, {result.updated} updated, {linked} linked to cards"
            + (f"; errors: {'; '.join(result.errors)}" if result.errors else "")
        )

    # 1. The Deliveries module: message, confirmation texts, kill switch.
    flt: dict[str, Any] = cfg.get("twitchConfirmationFilter") or {}
    report.automatic = bool(cfg.get("enabled", True))
    report.confirmation = bool(flt.get("enabled", True))
    row = await session.scalar(
        select(StoreModule).where(
            StoreModule.store_id == store_id, StoreModule.module_key == "deliveries"
        )
    )
    current: dict[str, Any] = dict(row.settings) if row is not None else {}
    settings = {
        **current,
        "auto_deliver": report.automatic,
        "message_template": _template(cfg.get("defaultGuideText")),
        "item_label": ITEM_LABEL,
    }
    for old, new in (
        ("singleAccountTemplate", "confirmation_single"),
        ("multiAccountTemplate", "confirmation_multi"),
        ("clarificationTemplate", "clarification"),
        ("cancellationTemplate", "cancellation"),
    ):
        text = str(flt.get(old) or "").strip()
        if text:
            settings[new] = text
    await stores.set_module(session, platform, actor, store_id, "deliveries", settings=settings)

    # 2. Offers.
    pools = {
        p.legacy_id: p
        for p in await session.scalars(select(StockPool).where(StockPool.store_id == store_id))
        if p.legacy_id
    }
    default_guide = str(cfg.get("defaultGuideText") or "").strip()
    for offer_id, m in (cfg.get("offerMappings") or {}).items():
        if not isinstance(m, dict):
            continue
        listing = await session.scalar(
            select(Listing).where(Listing.store_id == store_id, Listing.remote_offer_id == offer_id)
        )
        if listing is None:
            report.not_synced.append(offer_id[:8])
            continue
        if listing.group_id is not None:
            report.in_card.append(listing.title[:60])
            continue
        category = str(m.get("categoryId") or "")
        kind = str(m.get("deliveryType") or "").strip().lower()
        guide = str(m.get("customGuideText") or "").strip()
        if category in TIP_IDS or kind in {"tip", "text"}:
            changes: dict[str, Any] = {
                "delivery_mode": DeliveryMode.text,
                "pool_id": None,
                "delivery_template": guide or None,
                "require_confirmation": False,
            }
            await service.update_listing(session, actor, listing, changes)
            report.text_offers.append(listing.title[:60])
            continue
        pool = pools.get(category)
        if pool is None:
            report.no_pool.append(listing.title[:60])
            continue
        changes = {
            "pool_id": pool.id,
            "delivery_mode": DeliveryMode.auto
            if m.get("autoDeliver", True)
            else DeliveryMode.assisted,
            "units_per_sale": max(1, min(100, int(m.get("accountsPerUnit") or 1))),
            "require_confirmation": report.confirmation
            and bool(m.get("requireTwitchConfirmation", True)),
            # Same guide as the store: use the store's message, so one edit changes both.
            "delivery_template": None if guide in ("", default_guide) else _template(guide),
        }
        await service.update_listing(session, actor, listing, changes)
        report.offers_set.append(listing.title[:60])

    if dry_run:
        await session.rollback()
    else:
        await session.commit()
    return report


def format_items_report(r: ItemsReport, store: str) -> str:
    lines = [
        f"Legacy item deliveries for {store}{' (DRY RUN, nothing saved)' if r.dry_run else ''}"
    ]
    if r.synced:
        lines.append(f"Offers read from the marketplace: {r.synced}")
    lines.append(
        f"Automatic deliveries: {'on' if r.automatic else 'off'}; "
        f"CONFIRM / CANCEL asked: {'yes' if r.confirmation else 'no'}"
    )
    lines.append(
        f"Offers delivering accounts ({len(r.offers_set)}): {', '.join(r.offers_set) or 'none'}"
    )
    if r.text_offers:
        lines.append(f"Text-only offers ({len(r.text_offers)}): {', '.join(r.text_offers)}")
    if r.no_pool:
        lines.append(
            f"Offers whose old category has no pool here (left as they were): {', '.join(r.no_pool)}"
        )
    if r.in_card:
        lines.append(f"Offers in a cross-platform card (set on the card): {', '.join(r.in_card)}")
    if r.not_synced:
        lines.append(
            f"Offers not read yet ({len(r.not_synced)}; gone from the marketplace or not synced): "
            + ", ".join(r.not_synced)
        )
    return "\n".join(lines)
