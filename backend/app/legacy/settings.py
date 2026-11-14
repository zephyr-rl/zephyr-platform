"""Import an old store's notification and report settings (read-only on the old folder).

- ``discord_notifications_config.json`` + ``config.json`` → Discord channels (the webhook URLs,
  encrypted; one channel per distinct URL) and per-store rules made from the English presets,
  keeping which fields were shown, the colours, and the offers switched off.
- ``config.json`` → the chat cooldown, the daily report time and the store time zone.
- ``daily_report_cache.json`` → the last day already reported, so it is not reported again.

The old texts are not copied: the platform's messages are in English. Idempotent: running it
again changes nothing that is already there.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.platform import Platform
from app.modules.reports.models import DailyReport
from app.notifications import catalog, discord
from app.notifications import service as notifications
from app.notifications.models import NotificationChannel, NotificationRule
from app.stores import service as stores
from app.stores.models import Store

# Old "show this field" switches → the preset field they control.
SALE_FIELDS = {
    "showOrderId": "🆔 Order",
    "showBuyer": "👤 Buyer",
    "showModality": "⚡ Delivery",
    "showPrice": "💰 Price",
    "showOfferTitle": "🎮 Offer",
    "showCategory": "📁 Pool",
    "showQuantity": "🔢 Accounts",
    "showRemainingStock": "📊 Stock left",
}
DAILY_FIELDS = {
    "showTotalRevenue": "💰 Revenue",
    "showTotalOrders": "📦 Orders",
    "showTotalAccounts": "🏆 Accounts delivered",
    "showProductBreakdown": "📊 By product type",
    "showOffersBreakdown": "🏷️ By listing",
    "showStockSummary": "💾 Stock at closing",
    "showCancelledOrders": "⚠️ Cancelled / refunded",
}


@dataclass
class SettingsReport:
    store: str
    dry_run: bool
    channels_created: list[str] = field(default_factory=list)
    channels_reused: list[str] = field(default_factory=list)
    rules_created: list[str] = field(default_factory=list)
    rules_present: list[str] = field(default_factory=list)
    module_settings: dict[str, Any] = field(default_factory=dict)
    timezone: str | None = None
    last_reported: str | None = None
    notes: list[str] = field(default_factory=list)


def _read(folder: Path, name: str) -> dict[str, Any]:
    path = folder / name
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    return data if isinstance(data, dict) else {}


def _template(
    preset_key: str, shown: dict[str, Any], mapping: dict[str, str], color: Any
) -> dict[str, Any]:
    template: dict[str, Any] = json.loads(json.dumps(catalog.PRESETS_BY_KEY[preset_key].template))
    hidden = {name for key, name in mapping.items() if shown.get(key) is False}
    template["fields"] = [f for f in template["fields"] if f["name"] not in hidden]
    if isinstance(color, str) and len(color) == 7 and color.startswith("#"):
        try:
            int(color[1:], 16)
            template["color"] = color.upper()
        except ValueError:
            pass
    return template


async def _channel(
    session: AsyncSession,
    platform: Platform,
    actor: audit.Actor,
    report: SettingsReport,
    url: str,
    name: str,
) -> NotificationChannel | None:
    try:
        url = discord.check_url(url)
    except ValueError:
        report.notes.append(f"Skipped an invalid webhook URL for {name}.")
        return None
    for existing in await session.scalars(select(NotificationChannel)):
        if notifications.channel_url(platform, existing) == url:
            report.channels_reused.append(existing.name)
            return existing
    taken = {c.name for c in await session.scalars(select(NotificationChannel))}
    final, n = name, 2
    while final in taken:
        final, n = f"{name} {n}", n + 1
    channel = await notifications.create_channel(session, platform, actor, name=final, url=url)
    report.channels_created.append(channel.name)
    return channel


async def _rule(
    session: AsyncSession,
    actor: audit.Actor,
    report: SettingsReport,
    store: Store,
    channel: NotificationChannel,
    preset_key: str,
    template: dict[str, Any],
    filters: dict[str, Any] | None = None,
) -> None:
    preset = catalog.PRESETS_BY_KEY[preset_key]
    label = f"{preset.name} ({store.name})"
    exists = await session.scalar(
        select(NotificationRule).where(
            NotificationRule.store_id == store.id,
            NotificationRule.event_type == preset.event_type,
            NotificationRule.channel_id == channel.id,
        )
    )
    if exists:
        report.rules_present.append(label)
        return
    await notifications.create_rule(
        session,
        actor,
        name=label,
        event_type=preset.event_type,
        channel_id=channel.id,
        store_id=store.id,
        template=template,
        filters=filters,
    )
    report.rules_created.append(label)


async def migrate_settings(
    session: AsyncSession,
    platform: Platform,
    actor: audit.Actor,
    *,
    folder: Path,
    store_id: int,
    dry_run: bool = False,
) -> SettingsReport:
    if not await asyncio.to_thread(folder.is_dir):
        raise FileNotFoundError(folder)
    store = await session.get(Store, store_id)
    if store is None:
        raise ValueError(f"Store {store_id} not found")
    report = SettingsReport(store=store.name, dry_run=dry_run)
    config = _read(folder, "config.json")
    notif = _read(folder, "discord_notifications_config.json")

    # Store time zone and module settings.
    tz = config.get("dailyReportTimezone")
    if isinstance(tz, str):
        try:
            ZoneInfo(tz)
        except Exception:
            report.notes.append(f"Unknown time zone {tz!r}: kept {store.timezone}.")
        else:
            if store.timezone != tz:
                await stores.update_store(session, actor, store, {"timezone": tz})
            report.timezone = tz
    if "chatNotificationCooldownMinutes" in config:
        minutes = max(1, min(1440, int(config["chatNotificationCooldownMinutes"])))
        await stores.set_module(
            session,
            platform,
            actor,
            store.id,
            "chat_monitor",
            settings={"cooldown_minutes": minutes},
        )
        report.module_settings["chat_monitor.cooldown_minutes"] = minutes
    if "dailyReportHour" in config:
        at = f"{int(config['dailyReportHour']):02d}:{int(config.get('dailyReportMinute', 0)):02d}"
        await stores.set_module(
            session,
            platform,
            actor,
            store.id,
            "reports",
            enabled=bool(config.get("dailyReportEnabled", True)),
            settings={"daily_report_time": at},
        )
        report.module_settings["reports.daily_report_time"] = at

    # Channels and rules.
    if notif.get("enabled", True):
        sales_url = notif.get("salesWebhookUrl") or config.get("discordSalesWebhookUrl") or ""
        chat_url = notif.get("chatWebhookUrl") or config.get("discordChatWebhookUrl") or ""
        excluded = [str(o) for o in notif.get("disabledOfferIds") or []]
        if notif.get("offerFilterMode", "all") != "all":
            report.notes.append(
                "The old sales alerts used an offer allow-list: choose the listings in the rule."
            )
        if sales_url:
            sales = await _channel(
                session, platform, actor, report, sales_url, f"{store.name} sales"
            )
            if sales is not None:
                await _rule(
                    session,
                    actor,
                    report,
                    store,
                    sales,
                    "sale",
                    _template(
                        "sale", notif.get("fields") or {}, SALE_FIELDS, notif.get("embedColor")
                    ),
                    {"exclude_offer_ids": excluded},
                )
                daily = notif.get("dailySummary") or {}
                if daily.get("enabled", True):
                    await _rule(
                        session,
                        actor,
                        report,
                        store,
                        sales,
                        "daily",
                        _template("daily", daily.get("fields") or {}, DAILY_FIELDS, None),
                    )
        if chat_url:
            chat = await _channel(session, platform, actor, report, chat_url, f"{store.name} chat")
            if chat is not None:
                await _rule(
                    session,
                    actor,
                    report,
                    store,
                    chat,
                    "chat",
                    catalog.PRESETS_BY_KEY["chat"].template,
                )
    else:
        report.notes.append("Discord notifications were switched off in the old store.")

    # The last day the old system reported is not reported again.
    cache = _read(folder, "daily_report_cache.json")
    last = cache.get("last_reported_date")
    if isinstance(last, str):
        day = date.fromisoformat(last)
        old = cache.get("last_summary") or {}
        revenue = sum(
            float(old.get(k) or 0)
            for k in ("accounts_revenue", "items_revenue", "rocket_pass_revenue")
        )
        await session.execute(
            insert(DailyReport)
            .values(
                store_id=store.id,
                day=day,
                summary={
                    "imported": True,
                    "revenue": f"{revenue:.2f}",
                    "orders": int(old.get("total_orders") or 0),
                    "accounts": int(old.get("total_accounts") or 0),
                },
            )
            .on_conflict_do_nothing(index_elements=["store_id", "day"])
        )
        report.last_reported = last

    audit.record(
        session,
        actor,
        "legacy.settings_migrated",
        store_id=store.id,
        after={
            "channels_created": report.channels_created,
            "rules_created": report.rules_created,
            "module_settings": report.module_settings,
            "timezone": report.timezone,
            "last_reported": report.last_reported,
            "dry_run": dry_run,
        },
    )
    if dry_run:
        await session.rollback()
    else:
        await session.commit()
    return report


def format_settings_report(r: SettingsReport) -> str:
    def items(xs: list[str]) -> str:
        return ", ".join(xs) if xs else "none"

    lines = [
        f"Legacy settings for {r.store} {'(DRY RUN, nothing saved)' if r.dry_run else ''}".strip(),
        f"Channels created: {items(r.channels_created)}",
        f"Channels already present (same webhook): {items(r.channels_reused)}",
        f"Rules created: {items(r.rules_created)}",
        f"Rules already present: {items(r.rules_present)}",
        f"Time zone: {r.timezone or 'unchanged'}",
        "Module settings: "
        + (", ".join(f"{k} = {v}" for k, v in r.module_settings.items()) or "none"),
        f"Last day already reported by the old system: {r.last_reported or 'none'}",
    ]
    lines += [f"Note: {n}" for n in r.notes]
    return "\n".join(lines)
