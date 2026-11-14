from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import AuthDep, OwnerDep, PlatformDep, SessionDep
from app.core import audit, jobs
from app.core.crypto import CryptoError
from app.notifications import catalog, service
from app.notifications.models import (
    DeliveryState,
    NotificationChannel,
    NotificationDelivery,
    NotificationRule,
)

router = APIRouter(prefix="/notifications", tags=["notifications"])


# ---- schemas ------------------------------------------------------------------------------


class VariableOut(BaseModel):
    name: str
    description: str
    sample: str


class EventInfoOut(BaseModel):
    type: str
    label: str
    description: str
    group: str
    variables: list[VariableOut]


class PresetOut(BaseModel):
    key: str
    name: str
    event_type: str
    template: dict[str, Any]


class CatalogOut(BaseModel):
    events: list[EventInfoOut]
    presets: list[PresetOut]


class ChannelOut(BaseModel):
    id: int
    name: str
    kind: str
    target: str
    sender_name: str | None
    avatar_url: str | None
    enabled: bool
    include_shadow: bool
    last_sent_at: datetime | None
    last_error: str | None
    rules: int


class ChannelIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    url: str = Field(min_length=1, max_length=500)
    sender_name: str | None = Field(None, max_length=80)
    avatar_url: str | None = Field(None, max_length=500)
    include_shadow: bool = False


class ChannelPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=80)
    url: str | None = Field(None, max_length=500)
    sender_name: str | None = Field(None, max_length=80)
    avatar_url: str | None = Field(None, max_length=500)
    enabled: bool | None = None
    include_shadow: bool | None = None


class RuleOut(BaseModel):
    id: int
    name: str
    event_type: str
    channel_id: int
    store_id: int | None
    enabled: bool
    template: dict[str, Any]
    filters: dict[str, Any]
    updated_at: datetime


class RuleIn(BaseModel):
    name: str = Field("", max_length=120)
    event_type: str
    channel_id: int
    store_id: int | None = None
    enabled: bool = True
    template: dict[str, Any]
    filters: dict[str, Any] = Field(default_factory=dict)


class RulePatch(BaseModel):
    name: str | None = Field(None, max_length=120)
    channel_id: int | None = None
    store_id: int | None = None
    all_stores: bool = False
    enabled: bool | None = None
    template: dict[str, Any] | None = None
    filters: dict[str, Any] | None = None


class PreviewIn(BaseModel):
    event_type: str
    template: dict[str, Any]


class DeliveryOut(BaseModel):
    id: int
    created_at: datetime
    sent_at: datetime | None
    event_type: str
    rule_id: int | None
    channel_id: int
    channel: str
    store_id: int | None
    state: str
    summary: str
    attempts: int
    last_error: str | None


class TestOut(BaseModel):
    ok: bool
    error: str | None


def _bad(exc: service.NotificationError) -> HTTPException:
    code = (
        status.HTTP_404_NOT_FOUND
        if isinstance(exc, service.NotFound)
        else status.HTTP_422_UNPROCESSABLE_CONTENT
    )
    return HTTPException(code, str(exc))


# ---- catalog ------------------------------------------------------------------------------


@router.get("/catalog", response_model=CatalogOut)
async def get_catalog(auth: AuthDep) -> CatalogOut:
    return CatalogOut(
        events=[
            EventInfoOut(
                type=e.type,
                label=e.label,
                description=e.description,
                group=e.group,
                variables=[
                    VariableOut(name=k, description=d, sample=s)
                    for k, (d, s) in e.variables.items()
                ],
            )
            for e in catalog.EVENTS.values()
        ],
        presets=[
            PresetOut(key=p.key, name=p.name, event_type=p.event_type, template=p.template)
            for p in catalog.PRESETS
        ],
    )


@router.post("/preview")
async def preview(body: PreviewIn, auth: AuthDep) -> dict[str, Any]:
    try:
        return service.preview(body.event_type, body.template)
    except service.NotificationError as exc:
        raise _bad(exc) from None


# ---- channels -----------------------------------------------------------------------------


async def _channels_out(
    session: SessionDep, channels: list[NotificationChannel]
) -> list[ChannelOut]:
    counts: dict[int, int] = {}
    for rule in await session.scalars(select(NotificationRule)):
        counts[rule.channel_id] = counts.get(rule.channel_id, 0) + 1
    return [
        ChannelOut(
            id=c.id,
            name=c.name,
            kind=c.kind.value,
            target=c.target_hint,
            sender_name=c.sender_name,
            avatar_url=c.avatar_url,
            enabled=c.enabled,
            include_shadow=c.include_shadow,
            last_sent_at=c.last_sent_at,
            last_error=c.last_error,
            rules=counts.get(c.id, 0),
        )
        for c in channels
    ]


@router.get("/channels", response_model=list[ChannelOut])
async def list_channels(auth: AuthDep, session: SessionDep) -> list[ChannelOut]:
    rows = list(
        await session.scalars(select(NotificationChannel).order_by(NotificationChannel.name))
    )
    return await _channels_out(session, rows)


@router.post("/channels", response_model=ChannelOut, status_code=status.HTTP_201_CREATED)
async def create_channel(
    body: ChannelIn, auth: OwnerDep, platform: PlatformDep, session: SessionDep
) -> ChannelOut:
    try:
        channel = await service.create_channel(session, platform, auth.actor, **body.model_dump())
    except service.NotificationError as exc:
        raise _bad(exc) from None
    except CryptoError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from None
    await session.commit()
    return (await _channels_out(session, [channel]))[0]


@router.patch("/channels/{channel_id}", response_model=ChannelOut)
async def update_channel(
    channel_id: int, body: ChannelPatch, auth: OwnerDep, platform: PlatformDep, session: SessionDep
) -> ChannelOut:
    try:
        channel = await service.get_channel(session, channel_id)
        await service.update_channel(
            session, platform, auth.actor, channel, body.model_dump(exclude_unset=True)
        )
    except service.NotificationError as exc:
        raise _bad(exc) from None
    await session.commit()
    return (await _channels_out(session, [channel]))[0]


@router.delete("/channels/{channel_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_channel(channel_id: int, auth: OwnerDep, session: SessionDep) -> None:
    try:
        channel = await service.get_channel(session, channel_id)
        await service.delete_channel(session, auth.actor, channel)
    except service.NotificationError as exc:
        raise _bad(exc) from None
    await session.commit()


@router.post("/channels/{channel_id}/test", response_model=TestOut)
async def test_channel(
    channel_id: int, auth: OwnerDep, platform: PlatformDep, session: SessionDep
) -> TestOut:
    try:
        channel = await service.get_channel(session, channel_id)
    except service.NotificationError as exc:
        raise _bad(exc) from None
    delivery = await service.send_now(
        session, platform, channel, service.test_embed(channel), event_type="test"
    )
    audit.record(
        session,
        auth.actor,
        "notification_channel.tested",
        entity_type="notification_channel",
        entity_id=channel.id,
        after={"ok": delivery.state is DeliveryState.sent},
    )
    await session.commit()
    return TestOut(ok=delivery.state is DeliveryState.sent, error=delivery.last_error)


# ---- rules --------------------------------------------------------------------------------


def _rule_out(r: NotificationRule) -> RuleOut:
    return RuleOut(
        id=r.id,
        name=r.name,
        event_type=r.event_type,
        channel_id=r.channel_id,
        store_id=r.store_id,
        enabled=r.enabled,
        template=r.template,
        filters=r.filters,
        updated_at=r.updated_at,
    )


async def _rule(session: SessionDep, rule_id: int) -> NotificationRule:
    rule = await session.get(NotificationRule, rule_id)
    if rule is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Rule {rule_id} not found")
    return rule


@router.get("/rules", response_model=list[RuleOut])
async def list_rules(auth: AuthDep, session: SessionDep) -> list[RuleOut]:
    rows = await session.scalars(select(NotificationRule).order_by(NotificationRule.id))
    return [_rule_out(r) for r in rows]


@router.post("/rules", response_model=RuleOut, status_code=status.HTTP_201_CREATED)
async def create_rule(body: RuleIn, auth: OwnerDep, session: SessionDep) -> RuleOut:
    try:
        rule = await service.create_rule(session, auth.actor, **body.model_dump())
    except service.NotificationError as exc:
        raise _bad(exc) from None
    await session.commit()
    await session.refresh(rule)
    return _rule_out(rule)


@router.patch("/rules/{rule_id}", response_model=RuleOut)
async def update_rule(
    rule_id: int, body: RulePatch, auth: OwnerDep, session: SessionDep
) -> RuleOut:
    rule = await _rule(session, rule_id)
    changes = body.model_dump(exclude_unset=True, exclude={"all_stores"})
    if body.all_stores:
        changes["store_id"] = None
    elif "store_id" in changes and changes["store_id"] is None:
        changes.pop("store_id")
    try:
        await service.update_rule(session, auth.actor, rule, changes)
    except service.NotificationError as exc:
        raise _bad(exc) from None
    await session.commit()
    await session.refresh(rule)
    return _rule_out(rule)


@router.delete("/rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_rule(rule_id: int, auth: OwnerDep, session: SessionDep) -> None:
    rule = await _rule(session, rule_id)
    await service.delete_rule(session, auth.actor, rule)
    await session.commit()


@router.post("/rules/{rule_id}/test", response_model=TestOut)
async def test_rule(
    rule_id: int, auth: OwnerDep, platform: PlatformDep, session: SessionDep
) -> TestOut:
    """Send the rule's message with sample values, marked [Test]."""
    rule = await _rule(session, rule_id)
    channel = await service.get_channel(session, rule.channel_id)
    delivery = await service.send_now(
        session,
        platform,
        channel,
        service.rule_test_embed(rule),
        event_type=rule.event_type,
        store_id=rule.store_id,
        rule_id=rule.id,
    )
    await session.commit()
    return TestOut(ok=delivery.state is DeliveryState.sent, error=delivery.last_error)


# ---- log ----------------------------------------------------------------------------------


@router.get("/deliveries", response_model=list[DeliveryOut])
async def list_deliveries(
    auth: AuthDep,
    session: SessionDep,
    limit: int = Query(50, ge=1, le=200),
    state: DeliveryState | None = None,
) -> list[DeliveryOut]:
    stmt = (
        select(NotificationDelivery, NotificationChannel.name)
        .join(NotificationChannel, NotificationChannel.id == NotificationDelivery.channel_id)
        .order_by(NotificationDelivery.id.desc())
        .limit(limit)
    )
    if state is not None:
        stmt = stmt.where(NotificationDelivery.state == state)
    return [
        DeliveryOut(
            id=d.id,
            created_at=d.created_at,
            sent_at=d.sent_at,
            event_type=d.event_type,
            rule_id=d.rule_id,
            channel_id=d.channel_id,
            channel=name,
            store_id=d.store_id,
            state=d.state.value,
            summary=d.summary,
            attempts=d.attempts,
            last_error=d.last_error,
        )
        for d, name in (await session.execute(stmt))
    ]


@router.post("/deliveries/{delivery_id}/retry", status_code=status.HTTP_202_ACCEPTED)
async def retry_delivery(delivery_id: int, auth: OwnerDep, session: SessionDep) -> dict[str, int]:
    delivery = await session.get(NotificationDelivery, delivery_id)
    if delivery is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Message not found")
    if delivery.state is not DeliveryState.failed:
        raise HTTPException(status.HTTP_409_CONFLICT, "Only failed messages can be sent again.")
    delivery.state = DeliveryState.pending
    delivery.attempts = 0
    job_id = await jobs.enqueue(
        session,
        service.SEND_JOB,
        {"delivery_id": delivery.id},
        store_id=delivery.store_id,
        idempotency_key=f"notify:{delivery.id}:retry:{int(datetime.now().timestamp())}",
        max_attempts=service.MAX_SEND_ATTEMPTS + 2,
    )
    audit.record(
        session,
        auth.actor,
        "notification.retried",
        entity_type="notification_delivery",
        entity_id=delivery.id,
    )
    await session.commit()
    return {"job_id": job_id}
