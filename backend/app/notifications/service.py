"""Channels, rules, and turning events into messages.

Flow: an event is dispatched from the outbox → every matching rule gets one
``NotificationDelivery`` (unique per event and rule, so a re-dispatched event never sends twice),
rendered right away → a ``notifications.send`` job posts it, retrying while Discord is down or
rate limiting.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit, jobs
from app.core.db import utcnow
from app.core.events import Event
from app.core.platform import Platform
from app.notifications import catalog, context, discord, render
from app.notifications.models import (
    ChannelKind,
    DeliveryState,
    NotificationChannel,
    NotificationDelivery,
    NotificationRule,
)
from app.stores.models import Store

log = logging.getLogger(__name__)
SEND_JOB = "notifications.send"
MAX_SEND_ATTEMPTS = 8
SECRET_CONTEXT = "notification-channel"  # noqa: S105 (encryption context, not a secret)


class NotificationError(Exception):
    pass


class NotFound(NotificationError):
    pass


# ---- channels -------------------------------------------------------------------------------


def channel_url(platform: Platform, channel: NotificationChannel) -> str:
    return platform.secrets.decrypt(channel.target_encrypted, context=SECRET_CONTEXT)


async def get_channel(session: AsyncSession, channel_id: int) -> NotificationChannel:
    channel = await session.get(NotificationChannel, channel_id)
    if channel is None:
        raise NotFound(f"Channel {channel_id} not found")
    return channel


def _channel_view(c: NotificationChannel) -> dict[str, Any]:
    return {
        "name": c.name,
        "target": c.target_hint,
        "sender_name": c.sender_name,
        "enabled": c.enabled,
        "include_shadow": c.include_shadow,
    }


async def create_channel(
    session: AsyncSession,
    platform: Platform,
    actor: audit.Actor,
    *,
    name: str,
    url: str,
    sender_name: str | None = None,
    avatar_url: str | None = None,
    include_shadow: bool = False,
) -> NotificationChannel:
    name = name.strip()
    if not name:
        raise NotificationError("Give the channel a name.")
    if await session.scalar(select(NotificationChannel).where(NotificationChannel.name == name)):
        raise NotificationError(f"A channel called {name} already exists.")
    try:
        url = discord.check_url(url)
    except ValueError as exc:
        raise NotificationError(str(exc)) from None
    channel = NotificationChannel(
        name=name,
        kind=ChannelKind.discord_webhook,
        target_encrypted=platform.secrets.encrypt(url, context=SECRET_CONTEXT),
        target_hint=discord.url_hint(url),
        sender_name=(sender_name or "").strip() or None,
        avatar_url=(avatar_url or "").strip() or None,
        include_shadow=include_shadow,
    )
    session.add(channel)
    await session.flush()
    audit.record(
        session,
        actor,
        "notification_channel.created",
        entity_type="notification_channel",
        entity_id=channel.id,
        after=_channel_view(channel),
    )
    return channel


async def update_channel(
    session: AsyncSession,
    platform: Platform,
    actor: audit.Actor,
    channel: NotificationChannel,
    changes: dict[str, Any],
) -> NotificationChannel:
    before = _channel_view(channel)
    if "name" in changes:
        name = str(changes["name"]).strip()
        if not name:
            raise NotificationError("Give the channel a name.")
        clash = await session.scalar(
            select(NotificationChannel).where(
                NotificationChannel.name == name, NotificationChannel.id != channel.id
            )
        )
        if clash:
            raise NotificationError(f"A channel called {name} already exists.")
        channel.name = name
    if changes.get("url"):
        try:
            url = discord.check_url(str(changes["url"]))
        except ValueError as exc:
            raise NotificationError(str(exc)) from None
        channel.target_encrypted = platform.secrets.encrypt(url, context=SECRET_CONTEXT)
        channel.target_hint = discord.url_hint(url)
        channel.last_error = None
    for key in ("sender_name", "avatar_url"):
        if key in changes:
            setattr(channel, key, (changes[key] or "").strip() or None)
    for key in ("enabled", "include_shadow"):
        if changes.get(key) is not None:
            setattr(channel, key, bool(changes[key]))
    after = _channel_view(channel)
    if after != before or changes.get("url"):
        audit.record(
            session,
            actor,
            "notification_channel.updated",
            entity_type="notification_channel",
            entity_id=channel.id,
            before=before,
            after={**after, "url_changed": bool(changes.get("url"))},
        )
    return channel


async def delete_channel(
    session: AsyncSession, actor: audit.Actor, channel: NotificationChannel
) -> None:
    used = await session.scalar(
        select(NotificationRule.id).where(NotificationRule.channel_id == channel.id).limit(1)
    )
    if used:
        raise NotificationError("Delete or move the rules that use this channel first.")
    audit.record(
        session,
        actor,
        "notification_channel.deleted",
        entity_type="notification_channel",
        entity_id=channel.id,
        before=_channel_view(channel),
    )
    await session.delete(channel)


# ---- rules ----------------------------------------------------------------------------------


def check_template(event_type: str, raw: dict[str, Any]) -> render.Template:
    if event_type not in catalog.EVENTS:
        raise NotificationError(f"Unknown event {event_type}.")
    try:
        template = render.Template.model_validate(raw)
    except ValueError as exc:
        raise NotificationError(f"The template is not valid: {exc}") from None
    if not (template.title or template.description or template.fields):
        raise NotificationError("The message needs a title, a description or a field.")
    unknown = render.unknown_variables(template, set(catalog.EVENTS[event_type].variables))
    if unknown:
        names = ", ".join("{" + u + "}" for u in unknown)
        raise NotificationError(f"Unknown variables for this event: {names}.")
    return template


async def _check_store(session: AsyncSession, store_id: int | None) -> None:
    if store_id is not None and await session.get(Store, store_id) is None:
        raise NotificationError(f"Store {store_id} not found.")


def _rule_view(r: NotificationRule) -> dict[str, Any]:
    return {
        "name": r.name,
        "event_type": r.event_type,
        "channel_id": r.channel_id,
        "store_id": r.store_id,
        "enabled": r.enabled,
        "template": r.template,
        "filters": r.filters,
    }


async def create_rule(
    session: AsyncSession,
    actor: audit.Actor,
    *,
    name: str,
    event_type: str,
    channel_id: int,
    store_id: int | None,
    template: dict[str, Any],
    filters: dict[str, Any] | None = None,
    enabled: bool = True,
) -> NotificationRule:
    checked = check_template(event_type, template)
    await get_channel(session, channel_id)
    await _check_store(session, store_id)
    rule = NotificationRule(
        name=name.strip() or catalog.EVENTS[event_type].label,
        event_type=event_type,
        channel_id=channel_id,
        store_id=store_id,
        enabled=enabled,
        template=checked.model_dump(),
        filters=_filters(filters),
    )
    session.add(rule)
    await session.flush()
    audit.record(
        session,
        actor,
        "notification_rule.created",
        entity_type="notification_rule",
        entity_id=rule.id,
        store_id=store_id,
        after=_rule_view(rule),
    )
    return rule


def _filters(raw: dict[str, Any] | None) -> dict[str, Any]:
    """``listing_ids``: only these listings. ``exclude_offer_ids``: never these marketplace offers
    (imported from the old per-offer switches)."""
    raw = raw or {}
    out: dict[str, Any] = {}
    ids = sorted({int(i) for i in raw.get("listing_ids") or []})
    if ids:
        out["listing_ids"] = ids
    excluded = sorted(
        {str(i).strip() for i in raw.get("exclude_offer_ids") or [] if str(i).strip()}
    )
    if excluded:
        out["exclude_offer_ids"] = excluded
    return out


async def update_rule(
    session: AsyncSession, actor: audit.Actor, rule: NotificationRule, changes: dict[str, Any]
) -> NotificationRule:
    before = _rule_view(rule)
    if "channel_id" in changes:
        await get_channel(session, int(changes["channel_id"]))
        rule.channel_id = int(changes["channel_id"])
    if "store_id" in changes:
        await _check_store(session, changes["store_id"])
        rule.store_id = changes["store_id"]
    if "template" in changes:
        rule.template = check_template(rule.event_type, changes["template"]).model_dump()
    if changes.get("name"):
        rule.name = str(changes["name"]).strip()[:120]
    if changes.get("enabled") is not None:
        rule.enabled = bool(changes["enabled"])
    if "filters" in changes:
        rule.filters = _filters(changes["filters"])
    after = _rule_view(rule)
    if after != before:
        audit.record(
            session,
            actor,
            "notification_rule.updated",
            entity_type="notification_rule",
            entity_id=rule.id,
            store_id=rule.store_id,
            before=before,
            after=after,
        )
    return rule


async def delete_rule(session: AsyncSession, actor: audit.Actor, rule: NotificationRule) -> None:
    audit.record(
        session,
        actor,
        "notification_rule.deleted",
        entity_type="notification_rule",
        entity_id=rule.id,
        store_id=rule.store_id,
        before=_rule_view(rule),
    )
    await session.delete(rule)


def preview(event_type: str, template: dict[str, Any]) -> dict[str, Any]:
    """The message with sample values (the live preview in the panel)."""
    checked = check_template(event_type, template)
    return render.render(checked, catalog.sample(event_type))


# ---- events → deliveries --------------------------------------------------------------------


async def handle_event(platform: Platform, event: Event) -> int:
    """Create one delivery per matching rule. Idempotent: safe if the event is dispatched again."""
    created = 0
    async with platform.db.sessionmaker() as session:
        stmt = select(NotificationRule).where(
            NotificationRule.event_type == event.type, NotificationRule.enabled.is_(True)
        )
        if event.store_id is None:
            stmt = stmt.where(NotificationRule.store_id.is_(None))
        else:
            stmt = stmt.where(
                (NotificationRule.store_id.is_(None))
                | (NotificationRule.store_id == event.store_id)
            )
        rules = list(await session.scalars(stmt.order_by(NotificationRule.id)))
        if not rules:
            return 0
        ctx = await context.build(
            session, event.type, event.store_id, event.payload, event.created_at
        )
        store = await session.get(Store, event.store_id) if event.store_id else None
        for rule in rules:
            wanted = rule.filters.get("listing_ids") or []
            if wanted and ctx.listing_id not in wanted:
                continue
            if ctx.offer_id and ctx.offer_id in (rule.filters.get("exclude_offer_ids") or []):
                continue
            channel = await session.get(NotificationChannel, rule.channel_id)
            assert channel is not None
            embed = render.render(render.Template.model_validate(rule.template), ctx.values)
            state, note = DeliveryState.pending, None
            if not channel.enabled:
                state, note = DeliveryState.skipped, "Channel switched off."
            elif store is not None and store.shadow_mode and not channel.include_shadow:
                state, note = DeliveryState.skipped, "Store in shadow mode."
            delivery_id = await session.scalar(
                insert(NotificationDelivery)
                .values(
                    event_id=event.id,
                    rule_id=rule.id,
                    channel_id=channel.id,
                    store_id=event.store_id,
                    event_type=event.type,
                    state=state,
                    message=render.message(
                        embed, sender_name=channel.sender_name, avatar_url=channel.avatar_url
                    ),
                    summary=render.summary(embed),
                    last_error=note,
                )
                .on_conflict_do_nothing(index_elements=["event_id", "rule_id"])
                .returning(NotificationDelivery.id)
            )
            if delivery_id is None or state is not DeliveryState.pending:
                continue
            await jobs.enqueue(
                session,
                SEND_JOB,
                {"delivery_id": delivery_id},
                store_id=event.store_id,
                idempotency_key=f"notify:{delivery_id}",
                max_attempts=MAX_SEND_ATTEMPTS + 2,
            )
            created += 1
        await session.commit()
    return created


async def send(platform: Platform, delivery_id: int) -> DeliveryState:
    """Post one delivery. Raises ``jobs.RetryLater`` while it is worth trying again."""
    async with platform.db.sessionmaker() as session:
        delivery = await session.scalar(
            select(NotificationDelivery)
            .where(NotificationDelivery.id == delivery_id)
            .with_for_update(skip_locked=True)
        )
        if delivery is None or delivery.state is not DeliveryState.pending:
            return delivery.state if delivery else DeliveryState.skipped
        channel = await session.get(NotificationChannel, delivery.channel_id)
        assert channel is not None
        delivery.attempts += 1
        retry: timedelta | None = None
        try:
            await discord.post(channel_url(platform, channel), delivery.message)
        except discord.WebhookError as exc:
            delivery.last_error = str(exc)[:2000]
            if exc.retryable and delivery.attempts < MAX_SEND_ATTEMPTS:
                retry = exc.retry_after or jobs.backoff(delivery.attempts)
            else:
                delivery.state = DeliveryState.failed
                channel.last_error = str(exc)[:2000]
        else:
            delivery.state = DeliveryState.sent
            delivery.sent_at = channel.last_sent_at = utcnow()
            delivery.last_error = channel.last_error = None
        state = delivery.state
        await session.commit()
    if retry is not None:
        raise jobs.RetryLater(retry, "Discord not available")
    return state


async def send_now(
    session: AsyncSession,
    platform: Platform,
    channel: NotificationChannel,
    embed: dict[str, Any],
    *,
    event_type: str,
    store_id: int | None = None,
    rule_id: int | None = None,
) -> NotificationDelivery:
    """Post immediately (test messages). The result is logged like any other delivery."""
    body = render.message(embed, sender_name=channel.sender_name, avatar_url=channel.avatar_url)
    delivery = NotificationDelivery(
        rule_id=rule_id,
        channel_id=channel.id,
        store_id=store_id,
        event_type=event_type,
        message=body,
        summary=render.summary(embed),
        attempts=1,
    )
    session.add(delivery)
    try:
        await discord.post(channel_url(platform, channel), body)
    except discord.WebhookError as exc:
        delivery.state = DeliveryState.failed
        delivery.last_error = channel.last_error = str(exc)[:2000]
    else:
        delivery.state = DeliveryState.sent
        delivery.sent_at = channel.last_sent_at = utcnow()
        channel.last_error = None
    await session.flush()
    return delivery


def test_embed(channel: NotificationChannel) -> dict[str, Any]:
    return {
        "title": "✅ Test message",
        "description": f"The channel **{channel.name}** is connected to Zephyr Platform.",
        "color": 0x2ECC71,
        "footer": {"text": "Zephyr Platform • Test"},
    }


def rule_test_embed(rule: NotificationRule, store_name: str | None = None) -> dict[str, Any]:
    values = catalog.sample(rule.event_type)
    if store_name and "store" in values:
        values["store"] = store_name
    embed = render.render(render.Template.model_validate(rule.template), values)
    embed["title"] = f"[Test] {embed.get('title', rule.name)}"[: render.LIMITS["title"]]
    embed["footer"] = {"text": "Test message with example values: not a real order."}
    return embed


def register(platform: Platform) -> None:
    """Subscribe to every event and register the send job."""

    async def on_event(event: Event) -> None:
        await handle_event(platform, event)

    async def send_job(ctx: jobs.JobContext, payload: dict[str, Any]) -> dict[str, Any]:
        state = await send(platform, int(payload["delivery_id"]))
        return {"state": state.value}

    platform.events.subscribe("*", on_event)
    platform.jobs.register(SEND_JOB, send_job)
