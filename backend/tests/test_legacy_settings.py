"""Importing the old Discord notification and report settings."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import Actor
from app.core.platform import Platform
from app.legacy.settings import format_settings_report, migrate_settings
from app.modules.reports.models import DailyReport
from app.notifications import service as notifications
from app.notifications.models import NotificationChannel, NotificationRule
from app.stores import service as stores
from app.stores.models import Store, StoreModule

ACTOR = Actor.system("tests")
SALES = "https://discord.com/api/webhooks/111111111111111111/" + "s" * 68
CHAT = "https://discord.com/api/webhooks/222222222222222222/" + "c" * 68


def old_store(tmp: Path, *, same_url: bool = False) -> Path:
    folder = tmp / "Eldorado API - Kawai smurf"
    folder.mkdir()
    (folder / "config.json").write_text(
        json.dumps(
            {
                "clientId": "never-read",
                "chatNotificationCooldownMinutes": 15,
                "dailyReportEnabled": True,
                "dailyReportHour": 23,
                "dailyReportMinute": 59,
                "dailyReportTimezone": "Europe/Madrid",
            }
        ),
        encoding="utf-8",
    )
    (folder / "discord_notifications_config.json").write_text(
        json.dumps(
            {
                "enabled": True,
                "salesWebhookUrl": SALES,
                "chatWebhookUrl": SALES if same_url else CHAT,
                "embedColor": "#2ecc71",
                "fields": {"showBuyer": False, "showRemainingStock": True},
                "dailySummary": {"enabled": True, "fields": {"showTotalOrders": False}},
                "offerFilterMode": "all",
                "disabledOfferIds": ["offer-off"],
            }
        ),
        encoding="utf-8",
    )
    (folder / "daily_report_cache.json").write_text(
        json.dumps(
            {
                "last_reported_date": "2026-11-12",
                "last_summary": {
                    "total_orders": 13,
                    "total_accounts": 11,
                    "accounts_revenue": 35.7,
                },
            }
        ),
        encoding="utf-8",
    )
    return folder


async def test_imports_channels_rules_and_settings(
    session: AsyncSession, platform: Platform, tmp_path: Path
) -> None:
    store = await stores.create_store(session, platform, ACTOR, name="Kawai smurf", slug="kawai")
    await session.commit()
    store_id = store.id
    folder = old_store(tmp_path)
    report = await migrate_settings(session, platform, ACTOR, folder=folder, store_id=store_id)
    assert report.channels_created == ["Kawai smurf sales", "Kawai smurf chat"]
    assert len(report.rules_created) == 3

    session.expire_all()
    channels = {c.name: c for c in await session.scalars(select(NotificationChannel))}
    assert notifications.channel_url(platform, channels["Kawai smurf chat"]) == CHAT
    assert SALES[-20:] not in channels["Kawai smurf sales"].target_encrypted

    rules = {r.event_type: r for r in await session.scalars(select(NotificationRule))}
    sale = rules["order.delivered"]
    names = [f["name"] for f in sale.template["fields"]]
    assert "👤 Buyer" not in names and "📊 Stock left" in names
    assert sale.filters == {"exclude_offer_ids": ["offer-off"]}
    assert "📦 Orders" not in [f["name"] for f in rules["report.daily_ready"].template["fields"]]
    assert rules["chat.message_received"].channel_id == channels["Kawai smurf chat"].id
    assert all("Kawai smurf" in r.name for r in rules.values())

    refreshed = await session.get(Store, store_id)
    assert refreshed is not None and refreshed.timezone == "Europe/Madrid"
    modules = {
        m.module_key: m.settings
        for m in await session.scalars(select(StoreModule).where(StoreModule.store_id == store_id))
    }
    assert modules["chat_monitor"]["cooldown_minutes"] == 15
    assert modules["reports"]["daily_report_time"] == "23:59"
    imported = await session.scalar(select(DailyReport))
    assert imported is not None and imported.day == date(2026, 11, 12)
    assert imported.summary["imported"] is True

    # Running it again changes nothing.
    again = await migrate_settings(session, platform, ACTOR, folder=folder, store_id=store_id)
    assert again.channels_created == [] and again.rules_created == []
    assert len(again.rules_present) == 3
    assert "Rules already present" in format_settings_report(again)


async def test_one_channel_when_sales_and_chat_share_a_webhook(
    session: AsyncSession, platform: Platform, tmp_path: Path
) -> None:
    store = await stores.create_store(session, platform, ACTOR, name="Jewelry", slug="jewelry")
    await session.commit()
    report = await migrate_settings(
        session, platform, ACTOR, folder=old_store(tmp_path, same_url=True), store_id=store.id
    )
    assert report.channels_created == ["Jewelry sales"]
    assert report.channels_reused == ["Jewelry sales"]
    assert len(report.rules_created) == 3


async def test_dry_run_saves_nothing(
    session: AsyncSession, platform: Platform, tmp_path: Path
) -> None:
    store = await stores.create_store(session, platform, ACTOR, name="Jewelry", slug="jewelry")
    await session.commit()
    report = await migrate_settings(
        session, platform, ACTOR, folder=old_store(tmp_path), store_id=store.id, dry_run=True
    )
    assert report.dry_run and len(report.rules_created) == 3
    assert await session.scalar(select(NotificationChannel)) is None
