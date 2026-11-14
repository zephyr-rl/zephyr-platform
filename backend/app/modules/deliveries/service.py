"""Order fulfilment.

Each fulfilment moves through states, one committed step at a time, so a crash or a timeout
always leaves it in a known place from which the next run continues:

    pending ─┬─ manual (owner delivers)                               ┌─ done
             ├─ text: send message ──────────────────────── mark delivered ┘
             ├─ awaiting_buyer (confirmation) ── confirm ─┐
             │                                └─ cancel ─ cancelled
             └─ reserve stock ── awaiting_approval (assisted) ── approve ─┐
                              └──────────────────────────────────────────┴─ reserved
    reserved ── send credentials ── sent (items delivered, I1) ── mark delivered ── done

- I2: one fulfilment per order (database), and the row is locked while a step runs, so two
  workers never process the same order at once.
- I4: before sending, the order is read again from the marketplace; when a previous attempt may
  have sent the credentials already, the chat is read before sending again.
- I9: in shadow mode the decisions are recorded and nothing is sent; no stock is touched.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import func, literal_column, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.connectors.base import (
    ConnectorError,
    DeliveryPayload,
    MarketplaceConnector,
    OrderState,
    RemoteOrder,
)
from app.connectors.runtime import open_connector
from app.core import audit, events, jobs
from app.core.db import utcnow
from app.core.platform import Platform
from app.listings.models import DeliveryMode, Listing
from app.modules.catalog import DeliveriesSettings, TopUpsSettings
from app.modules.deliveries import messages
from app.modules.deliveries.replies import Reply, classify
from app.orders.models import (
    ACTIVE_STATES,
    Fulfilment,
    FulfilmentEvent,
    FulfilmentState,
    Order,
    Reservation,
)
from app.stock import service as stock
from app.stock.models import ItemState, StockItem, StockItemEvent, StockPool
from app.stores.models import MarketplaceConnection, Store, StoreModule

log = logging.getLogger(__name__)
ACTOR = audit.Actor.system("deliveries")
MAX_ATTEMPTS = 6
PROCESS_JOB = "deliveries.process"


class NotEnoughStock(Exception):
    pass


# ---- helpers -------------------------------------------------------------------------------


async def settings_for(session: AsyncSession, store_id: int) -> DeliveriesSettings:
    row = await session.scalar(
        select(StoreModule).where(
            StoreModule.store_id == store_id, StoreModule.module_key == "deliveries"
        )
    )
    return DeliveriesSettings.model_validate(row.settings if row else {})


def note(
    session: AsyncSession,
    fulfilment: Fulfilment,
    event: str,
    message: str,
    *,
    actor: audit.Actor = ACTOR,
    **details: Any,
) -> None:
    session.add(
        FulfilmentEvent(
            fulfilment_id=fulfilment.id,
            actor_label=actor.label,
            event=event,
            message=message,
            details=details or None,
        )
    )


def set_state(fulfilment: Fulfilment, state: FulfilmentState) -> None:
    if fulfilment.state is not state:
        fulfilment.state = state
        fulfilment.state_changed_at = utcnow()


async def _connection(session: AsyncSession, order: Order) -> MarketplaceConnection:
    connection = await session.get(MarketplaceConnection, order.connection_id)
    assert connection is not None
    return connection


# ---- polling -------------------------------------------------------------------------------


@dataclass
class PollResult:
    seen: int = 0
    new_paid: int = 0
    updated: int = 0


TOPUP = "topup"  # fulfilment mode: instructions sent, then the owner does the top-up


def _mode_for(
    listing: Listing | None, settings: DeliveriesSettings, topups: TopUpsSettings | None = None
) -> tuple[str, bool, int, int | None]:
    """(mode, require_confirmation, units per sale, pool) for a new order."""
    if listing is None:
        return DeliveryMode.manual.value, False, 1, None
    if listing.kind == "topup":
        # A top-up is never marked delivered by the platform: at most the instructions are sent
        # (when the Top-Ups module is on and the offer is not switched off there).
        send = topups is not None and listing.remote_offer_id not in topups.skip_offers
        return (TOPUP if send else DeliveryMode.manual.value), False, 1, None
    mode = listing.delivery_mode
    if mode is DeliveryMode.auto and not settings.auto_deliver:
        mode = DeliveryMode.assisted  # kill switch: hold automatic deliveries for approval
    return mode.value, listing.require_confirmation, listing.units_per_sale, listing.pool_id


async def poll_store(platform: Platform, store_id: int) -> PollResult:
    """Read recent orders, record them, and start a fulfilment for each new paid order."""
    result = PollResult()
    sm = platform.db.sessionmaker
    async with sm() as session:
        store = await session.get(Store, store_id)
        if store is None:
            return result
        settings = await settings_for(session, store_id)
        topups_row = await session.scalar(
            select(StoreModule).where(
                StoreModule.store_id == store_id, StoreModule.module_key == "topups"
            )
        )
        topups = (
            TopUpsSettings.model_validate(topups_row.settings)
            if topups_row is not None and topups_row.enabled
            else None
        )
        connections = list(
            await session.scalars(
                select(MarketplaceConnection).where(MarketplaceConnection.store_id == store_id)
            )
        )
        since = utcnow() - timedelta(hours=settings.lookback_hours)
        for connection in connections:
            connector = await open_connector(session, platform, connection)
            try:
                remote_orders = await connector.list_orders(since, set())
            finally:
                await connector.aclose()
            for remote in remote_orders:
                result.seen += 1
                created = await _record_order(session, store, connection, remote, settings, topups)
                result.new_paid += created
        await session.commit()
    return result


async def _record_order(
    session: AsyncSession,
    store: Store,
    connection: MarketplaceConnection,
    remote: RemoteOrder,
    settings: DeliveriesSettings,
    topups: TopUpsSettings | None = None,
) -> int:
    listing = None
    if remote.offer_id:
        listing = await session.scalar(
            select(Listing).where(
                Listing.connection_id == connection.id, Listing.remote_offer_id == remote.offer_id
            )
        )
    values = {
        "state": remote.state.value,
        "remote_state": remote.remote_state,
        "listing_id": listing.id if listing else None,
        "updated_at": utcnow(),
    }
    row: Any = (
        await session.execute(
            insert(Order)
            .values(
                store_id=store.id,
                connection_id=connection.id,
                remote_id=remote.remote_id,
                remote_offer_id=remote.offer_id,
                quantity=remote.quantity,
                total_amount=remote.total.amount,
                currency=remote.total.currency[:3],
                buyer_name=remote.buyer_name,
                conversation_id=remote.conversation_id,
                offer_title=remote.offer_title,
                category=remote.category,
                remote_created_at=remote.created_at,
                **values,
            )
            .on_conflict_do_update(index_elements=["connection_id", "remote_id"], set_=values)
            .returning(Order.id, literal_column("(xmax = 0)").label("inserted"))
        )
    ).one()
    order_id, inserted = row.id, bool(row.inserted)
    fulfilment = await session.scalar(select(Fulfilment).where(Fulfilment.order_id == order_id))
    if fulfilment is not None:
        await _follow_remote_state(session, fulfilment, remote)
        return 0
    if inserted and _instant_sale(listing, remote):
        # Delivered by the marketplace from the vault: nothing to do here but announce the sale.
        events.publish(
            session,
            "order.delivered",
            {
                "order": remote.remote_id,
                "delivery": "instant",
                "listing_id": listing.id if listing else None,
            },
            store_id=store.id,
        )
    if remote.state is not OrderState.paid:
        return 0  # orders delivered or cancelled before the platform saw them are only recorded
    if listing is not None and listing.restock_enabled:
        return 0  # instant delivery: the marketplace delivers from the vault

    mode, confirmation, units, pool_id = _mode_for(listing, settings, topups)
    fulfilment_id = await session.scalar(
        insert(Fulfilment)
        .values(
            order_id=order_id,
            store_id=store.id,
            mode=mode,
            require_confirmation=confirmation,
            shadow=store.shadow_mode,
            state=FulfilmentState.pending,
            units=max(1, remote.quantity) * units,
            pool_id=pool_id,
        )
        .on_conflict_do_nothing(index_elements=["order_id"])  # I2: created once
        .returning(Fulfilment.id)
    )
    if fulfilment_id is None:
        return 0
    fulfilment = await session.get(Fulfilment, fulfilment_id)
    assert fulfilment is not None
    reason = "" if listing else " No listing matches this offer: read the offers in Listings."
    note(session, fulfilment, "created", f"Paid order seen ({mode} delivery).{reason}")
    events.publish(
        session,
        "order.paid",
        {
            "order": remote.remote_id,
            "title": remote.offer_title,
            "quantity": remote.quantity,
            "total": str(remote.total.amount),
            "currency": remote.total.currency,
            "mode": mode,
        },
        store_id=store.id,
    )
    await jobs.enqueue(
        session,
        PROCESS_JOB,
        {"fulfilment_id": fulfilment_id},
        store_id=store.id,
        idempotency_key=f"fulfil:{fulfilment_id}:start",
    )
    return 1


INSTANT_FRESH = timedelta(hours=2)


def _instant_sale(listing: Listing | None, remote: RemoteOrder) -> bool:
    """An account sale the marketplace delivered from the vault, seen for the first time and
    recent (orders from before the platform started are not announced)."""
    if listing is None or listing.kind != "account":
        return False
    if utcnow() - remote.created_at > INSTANT_FRESH:
        return False
    if remote.state in (OrderState.delivered, OrderState.completed):
        return True
    return remote.state is OrderState.paid and listing.restock_enabled


async def _follow_remote_state(
    session: AsyncSession, fulfilment: Fulfilment, remote: RemoteOrder
) -> None:
    """The marketplace changed the order while it was waiting here."""
    if fulfilment.state not in ACTIVE_STATES or remote.state in (
        OrderState.paid,
        OrderState.unknown,
    ):
        return
    locked = await session.scalar(
        select(Fulfilment).where(Fulfilment.id == fulfilment.id).with_for_update(skip_locked=True)
    )
    if locked is None:  # a worker is on it; it re-reads the order itself
        return
    if remote.state in (OrderState.cancelled, OrderState.refunded):
        if locked.state is FulfilmentState.sent:
            set_state(locked, FulfilmentState.needs_attention)
            note(
                session,
                locked,
                "cancelled_after_send",
                f"The order was {remote.remote_state} on the marketplace "
                "after the credentials were sent.",
            )
        else:
            await release(session, locked, "order cancelled on the marketplace")
            set_state(locked, FulfilmentState.cancelled)
            note(session, locked, "cancelled", f"Order {remote.remote_state} on the marketplace.")
            events.publish(
                session, "order.cancelled", {"order": remote.remote_id}, store_id=locked.store_id
            )
    elif remote.state in (OrderState.delivered, OrderState.completed):
        if locked.state is FulfilmentState.awaiting_owner:
            set_state(locked, FulfilmentState.done)
            note(session, locked, "done", "Top-up marked delivered on the marketplace.")
        elif locked.state is FulfilmentState.sent:
            set_state(locked, FulfilmentState.done)
            note(session, locked, "done", "The marketplace shows the order as delivered.")
        else:
            await release(session, locked, "delivered outside the platform")
            set_state(locked, FulfilmentState.manual)
            note(
                session,
                locked,
                "delivered_elsewhere",
                "The order was delivered outside the platform; nothing was sent from here.",
            )


# ---- stock ---------------------------------------------------------------------------------


async def reserve(session: AsyncSession, fulfilment: Fulfilment, order: Order) -> list[StockItem]:
    if fulfilment.pool_id is None:
        raise NotEnoughStock("No stock pool is set for this listing.")
    pool = await stock.get_pool(session, fulfilment.store_id, fulfilment.pool_id, lock=True)
    items = list(
        (
            await session.scalars(
                select(StockItem)
                .where(StockItem.pool_id == pool.id, StockItem.state == ItemState.available)
                .order_by(StockItem.position, StockItem.id)
                .limit(fulfilment.units)
                .with_for_update(skip_locked=True)
            )
        ).all()
    )
    if len(items) < fulfilment.units:
        raise NotEnoughStock(
            f"{pool.name} has {len(items)} available; this order needs {fulfilment.units}."
        )
    before = await _available(session, pool.id)
    for item in items:
        session.add(Reservation(fulfilment_id=fulfilment.id, item_id=item.id))
        item.state = ItemState.reserved
        session.add(_item_event(item, "reserved", "available", "reserved", fulfilment, order))
    await session.flush()
    stock._announce_level(session, pool, before, before - len(items))
    return items


async def _available(session: AsyncSession, pool_id: int) -> int:
    return int(
        await session.scalar(
            select(func.count()).where(
                StockItem.pool_id == pool_id, StockItem.state == ItemState.available
            )
        )
        or 0
    )


def _item_event(
    item: StockItem, event: str, before: str, after: str, fulfilment: Fulfilment, order: Order
) -> StockItemEvent:
    return StockItemEvent(
        item_id=item.id,
        actor_label=ACTOR.label,
        event=event,
        from_state=before,
        to_state=after,
        batch=f"fulfilment:{fulfilment.id}",
        details={"order": order.remote_id},
    )


async def reserved_items(session: AsyncSession, fulfilment: Fulfilment) -> list[StockItem]:
    return list(
        (
            await session.scalars(
                select(StockItem)
                .join(Reservation, Reservation.item_id == StockItem.id)
                .where(Reservation.fulfilment_id == fulfilment.id, Reservation.active.is_(True))
                .order_by(StockItem.position, StockItem.id)
            )
        ).all()
    )


async def release(session: AsyncSession, fulfilment: Fulfilment, reason: str) -> int:
    """Return reserved (never sent) items to stock."""
    order = await session.get(Order, fulfilment.order_id)
    assert order is not None
    released = 0
    for item in await reserved_items(session, fulfilment):
        if item.state is not ItemState.reserved:
            continue  # already delivered: never comes back
        item.state = ItemState.available
        session.add(_item_event(item, "released", "reserved", "available", fulfilment, order))
        released += 1
    for reservation in await session.scalars(
        select(Reservation).where(
            Reservation.fulfilment_id == fulfilment.id, Reservation.active.is_(True)
        )
    ):
        held = await session.get(StockItem, reservation.item_id)
        if held is not None and held.state is ItemState.available:
            reservation.active = False
            reservation.released_at = utcnow()
    if released:
        note(session, fulfilment, "released", f"{released} account(s) returned to stock: {reason}.")
    return released


# ---- processing ----------------------------------------------------------------------------


@dataclass
class Context:
    session: AsyncSession
    platform: Platform
    fulfilment: Fulfilment
    order: Order
    settings: DeliveriesSettings
    connector: MarketplaceConnector | None = None

    async def remote(self) -> MarketplaceConnector:
        if self.connector is None:
            self.connector = await open_connector(
                self.session, self.platform, await _connection(self.session, self.order)
            )
        return self.connector


async def process(platform: Platform, fulfilment_id: int) -> FulfilmentState | None:
    """Run every step that can run now. Returns the state reached (None if locked elsewhere)."""
    sm = platform.db.sessionmaker
    while True:
        async with sm() as session:
            fulfilment = await session.scalar(
                select(Fulfilment)
                .where(Fulfilment.id == fulfilment_id)
                .with_for_update(skip_locked=True)
            )
            if fulfilment is None:
                return None  # another worker holds it (I2)
            order = await session.get(Order, fulfilment.order_id)
            assert order is not None
            ctx = Context(
                session,
                platform,
                fulfilment,
                order,
                await settings_for(session, fulfilment.store_id),
            )
            try:
                moved = await _step(ctx)
            except ConnectorError as exc:
                await session.rollback()
                await _record_failure(sm, fulfilment_id, exc)
                raise
            finally:
                if ctx.connector is not None:
                    await ctx.connector.aclose()
            state = fulfilment.state
            await session.commit()
        if not moved:
            return state


async def _record_failure(
    sm: async_sessionmaker[AsyncSession], fulfilment_id: int, exc: ConnectorError
) -> None:
    async with sm() as session:
        fulfilment = await session.scalar(
            select(Fulfilment).where(Fulfilment.id == fulfilment_id).with_for_update()
        )
        assert fulfilment is not None
        fulfilment.attempts += 1
        fulfilment.last_error = str(exc)[:2000]
        if not exc.retryable or fulfilment.attempts >= MAX_ATTEMPTS:
            set_state(fulfilment, FulfilmentState.needs_attention)
            note(session, fulfilment, "failed", f"Stopped: {exc}")
            order = await session.get(Order, fulfilment.order_id)
            events.publish(
                session,
                "order.delivery_failed",
                {"order": order.remote_id if order else None, "error": str(exc)[:500]},
                store_id=fulfilment.store_id,
            )
        else:
            note(session, fulfilment, "retrying", f"Marketplace error, will retry: {exc}")
        await session.commit()


async def _step(ctx: Context) -> bool:
    """Advance one step. Returns True when another step may follow at once."""
    f = ctx.fulfilment
    if f.shadow:
        return await _simulate(ctx) if f.state is FulfilmentState.pending else False
    handlers = {
        FulfilmentState.pending: _start,
        FulfilmentState.awaiting_approval: _approved,
        FulfilmentState.reserved: _send_credentials,
        FulfilmentState.sent: _mark_delivered,
    }
    handler = handlers.get(f.state)
    return await handler(ctx) if handler else False


async def _simulate(ctx: Context) -> bool:
    """I9: record what would happen; send nothing, touch no stock."""
    f, order, s = ctx.fulfilment, ctx.order, ctx.session
    lines = [f"Shadow mode: nothing was sent. Mode: {f.mode}."]
    if f.mode in (DeliveryMode.auto.value, DeliveryMode.assisted.value):
        available = await _available(s, f.pool_id) if f.pool_id else 0
        if f.require_confirmation:
            lines.append("Would ask the buyer to CONFIRM before delivering.")
        if f.mode == DeliveryMode.assisted.value:
            lines.append("Would wait for your approval.")
        lines.append(
            f"Would deliver {f.units} account(s) from the pool ({available} available)."
            if available >= f.units
            else f"Would stop: the pool has {available} available, {f.units} needed."
        )
    elif f.mode == DeliveryMode.text.value:
        lines.append("Would send the listing's message and mark the order delivered.")
    elif f.mode == TOPUP:
        lines.append("Top-up: would send the instructions; you would do the top-up.")
    else:
        lines.append("Manual listing: you deliver it.")
    note(s, f, "simulated", " ".join(lines))
    audit.record(
        s,
        audit.Actor.system("shadow"),
        "shadow.fulfil",
        entity_type="order",
        entity_id=order.remote_id,
        store_id=f.store_id,
        after={"mode": f.mode, "units": f.units},
    )
    set_state(f, FulfilmentState.simulated)
    return False


async def _start(ctx: Context) -> bool:
    f, s = ctx.fulfilment, ctx.session
    if f.mode == DeliveryMode.manual.value:
        set_state(f, FulfilmentState.manual)
        note(s, f, "manual", "Manual listing: deliver it yourself.")
        return False

    current = await (await ctx.remote()).get_order(ctx.order.remote_id)  # I4: still paid?
    if current.state is not OrderState.paid:
        await _follow_remote_state(s, f, current)
        return False

    if f.mode == TOPUP:
        return await _send_topup_instructions(ctx)

    if f.mode == DeliveryMode.text.value:
        listing = await s.get(Listing, ctx.order.listing_id) if ctx.order.listing_id else None
        text = (listing.delivery_template if listing else "") or ""
        await _send(ctx, text, kind="message")
        note(s, f, "message_sent", "Listing message sent to the buyer.")
        set_state(f, FulfilmentState.sent)
        return True

    if f.require_confirmation and f.buyer_confirmed_at is None:
        template = (
            ctx.settings.confirmation_single if f.units == 1 else ctx.settings.confirmation_multi
        )
        await _send(ctx, messages.fill(template, quantity=f.units), kind="confirmation")
        f.confirmation_sent_at = f.replies_read_until = utcnow()
        set_state(f, FulfilmentState.awaiting_buyer)
        note(s, f, "confirmation_asked", "Asked the buyer to reply CONFIRM or CANCEL.")
        events.publish(
            s, "order.awaiting_confirmation", {"order": ctx.order.remote_id}, store_id=f.store_id
        )
        return False

    try:
        items = await reserve(s, f, ctx.order)
    except NotEnoughStock as exc:
        set_state(f, FulfilmentState.needs_attention)
        f.last_error = str(exc)
        note(s, f, "no_stock", f"Not delivered: {exc}")
        events.publish(
            s,
            "order.delivery_failed",
            {"order": ctx.order.remote_id, "error": str(exc)},
            store_id=f.store_id,
        )
        return False
    note(s, f, "reserved", f"{len(items)} account(s) set aside.")
    if f.mode == DeliveryMode.assisted.value and f.approved_by is None:
        set_state(f, FulfilmentState.awaiting_approval)
        note(s, f, "awaiting_approval", "Waiting for your approval to deliver.")
        return False
    set_state(f, FulfilmentState.reserved)
    return True


async def topup_settings(session: AsyncSession, store_id: int) -> TopUpsSettings:
    row = await session.scalar(
        select(StoreModule).where(
            StoreModule.store_id == store_id, StoreModule.module_key == "topups"
        )
    )
    return TopUpsSettings.model_validate(row.settings if row else {})


async def _already_in_chat(ctx: Context, text: str) -> bool:
    """After a failed attempt the message may have been sent anyway (I4): look before resending."""
    if ctx.fulfilment.attempts == 0 or not ctx.order.conversation_id:
        return False
    since = ctx.fulfilment.state_changed_at - timedelta(minutes=1)
    chat = await (await ctx.remote()).chat_list_messages(ctx.order.conversation_id, since)
    head = text.strip()[:60]
    return any(m.from_seller and m.text.strip().startswith(head) for m in chat)


async def _send_topup_instructions(ctx: Context) -> bool:
    f, s, order = ctx.fulfilment, ctx.session, ctx.order
    listing = await s.get(Listing, order.listing_id) if order.listing_id else None
    cfg = await topup_settings(s, f.store_id)
    template = (
        (listing.delivery_template if listing else None)
        or cfg.offer_messages.get(order.remote_offer_id or "")
        or cfg.instructions
    )
    text = messages.fill(
        template,
        buyer=order.buyer_name or "there",
        product=order.offer_title or "your top-up",
        quantity=order.quantity,
        order=order.remote_id[:8],
    )
    if not await _already_in_chat(ctx, text):
        await _send(ctx, text, kind="instructions")
    set_state(f, FulfilmentState.awaiting_owner)
    f.last_error = None
    note(s, f, "instructions_sent", "Top-up instructions sent. Waiting for you to do the top-up.")
    return False


async def _approved(ctx: Context) -> bool:
    if ctx.fulfilment.approved_by is None:
        return False
    set_state(ctx.fulfilment, FulfilmentState.reserved)
    return True


async def _send(ctx: Context, text: str, *, kind: str) -> None:
    if not ctx.order.conversation_id:
        raise ConnectorError("The order has no chat conversation to send to.", retryable=False)
    if not text.strip():
        raise ConnectorError(f"The {kind} message is empty.", retryable=False)
    await (await ctx.remote()).chat_send(ctx.order.conversation_id, text)


async def _credentials_already_sent(ctx: Context, logins: list[str]) -> bool:
    """I4: a previous attempt (or a crash right after sending) may have sent the message already.
    The chat is read before every send, so the credentials are never sent twice."""
    if not ctx.order.conversation_id:
        return False
    since = ctx.fulfilment.state_changed_at - timedelta(minutes=1)
    messages_ = await (await ctx.remote()).chat_list_messages(ctx.order.conversation_id, since)
    return any(m.from_seller and logins[0] in m.text for m in messages_)


async def _send_credentials(ctx: Context) -> bool:
    f, s, order = ctx.fulfilment, ctx.session, ctx.order
    current = await (await ctx.remote()).get_order(order.remote_id)  # I4: still paid?
    if current.state is not OrderState.paid:
        await _follow_remote_state(s, f, current)
        return False
    items = await reserved_items(s, f)
    revealed = await stock.reveal(s, ctx.platform, items)
    if not await _credentials_already_sent(ctx, [r.login for r in revealed]):
        listing = await s.get(Listing, order.listing_id) if order.listing_id else None
        template = (listing.delivery_template if listing else None) or ctx.settings.message_template
        text = messages.fill(
            template,
            items=messages.items_block(revealed, ctx.settings.item_label),
            quantity=len(revealed),
            order=order.remote_id,
        )
        await _send(ctx, text, kind="delivery")
    # The credentials left: the items are delivered for good (I1), whatever happens next.
    for item in items:
        item.state = ItemState.delivered
        s.add(_item_event(item, "delivered", "reserved", "delivered", f, order))
    set_state(f, FulfilmentState.sent)
    note(s, f, "sent", f"{len(items)} account(s) sent to the buyer in the chat.")
    return True


async def _mark_delivered(ctx: Context) -> bool:
    f, s = ctx.fulfilment, ctx.session
    result = await (await ctx.remote()).mark_delivered(
        ctx.order.remote_id, DeliveryPayload(message="")
    )
    set_state(f, FulfilmentState.done)
    f.last_error = None
    note(
        s,
        f,
        "done",
        "Order marked delivered on the marketplace."
        + (f" ({result.detail})" if result.detail else ""),
    )
    events.publish(
        s, "order.delivered", {"order": ctx.order.remote_id, "units": f.units}, store_id=f.store_id
    )
    return False


# ---- buyer confirmations --------------------------------------------------------------------


async def check_replies(platform: Platform, store_id: int) -> int:
    """Read the chats of orders waiting for the buyer's CONFIRM / CANCEL. Returns decisions made."""
    sm = platform.db.sessionmaker
    async with sm() as session:
        ids = list(
            await session.scalars(
                select(Fulfilment.id).where(
                    Fulfilment.store_id == store_id,
                    Fulfilment.state == FulfilmentState.awaiting_buyer,
                    Fulfilment.shadow.is_(False),
                )
            )
        )
    decided = 0
    for fulfilment_id in ids:
        decided += await _check_one(platform, fulfilment_id)
    return decided


async def _check_one(platform: Platform, fulfilment_id: int) -> int:
    sm = platform.db.sessionmaker
    async with sm() as session:
        f = await session.scalar(
            select(Fulfilment)
            .where(Fulfilment.id == fulfilment_id)
            .with_for_update(skip_locked=True)
        )
        if f is None or f.state is not FulfilmentState.awaiting_buyer:
            return 0
        order = await session.get(Order, f.order_id)
        assert order is not None and order.conversation_id
        ctx = Context(session, platform, f, order, await settings_for(session, f.store_id))
        try:
            decision = await _read_replies(ctx)
        finally:
            if ctx.connector is not None:
                await ctx.connector.aclose()
        await session.commit()
    if decision is Reply.confirm:
        async with sm() as session:
            await jobs.enqueue(
                session,
                PROCESS_JOB,
                {"fulfilment_id": fulfilment_id},
                store_id=f.store_id,
                idempotency_key=f"fulfil:{fulfilment_id}:confirmed",
            )
            await session.commit()
    return 1 if decision is not None else 0


async def _read_replies(ctx: Context) -> Reply | None:
    f, s, order, cfg = ctx.fulfilment, ctx.session, ctx.order, ctx.settings
    remote = await ctx.remote()
    current = await remote.get_order(order.remote_id)
    if current.state is not OrderState.paid:
        await _follow_remote_state(s, f, current)
        return None
    since = f.replies_read_until or f.confirmation_sent_at or f.created_at
    chat = await remote.chat_list_messages(order.conversation_id or "", since)
    ours = [cfg.confirmation_single, cfg.confirmation_multi, cfg.clarification, cfg.cancellation]
    new = [m for m in chat if m.sent_at > since]
    if not new:
        return None
    f.replies_read_until = max(m.sent_at for m in new)

    if any(m.from_seller and not messages.is_automated(m.text, ours) for m in new):
        set_state(f, FulfilmentState.needs_attention)
        note(
            s, f, "seller_took_over", "You wrote in the chat, so automation stopped for this order."
        )
        return None

    for message in (m for m in new if not m.from_seller):
        reply = classify(message.text, cfg.reply_languages)
        note(
            s, f, "buyer_replied", f"Buyer replied; read as {reply.value}.", text=message.text[:300]
        )
        if reply is Reply.confirm:
            f.buyer_confirmed_at = utcnow()
            set_state(f, FulfilmentState.pending)
            note(s, f, "confirmed", "The buyer confirmed. Delivering.")
            return Reply.confirm
        if reply is Reply.cancel:
            await remote.cancel_order(order.remote_id, "Buyer_Does_Not_Need_It_Anymore")
            await _send(ctx, cfg.cancellation, kind="cancellation")
            set_state(f, FulfilmentState.cancelled)
            note(s, f, "cancelled", "The buyer asked to cancel. Order cancelled and refunded.")
            events.publish(
                s, "order.cancelled", {"order": order.remote_id, "by": "buyer"}, store_id=f.store_id
            )
            return Reply.cancel
        if not f.clarification_sent:
            await _send(ctx, cfg.clarification, kind="clarification")
            f.clarification_sent = True
            note(
                s, f, "clarification_sent", "The reply was not clear: sent the clarification once."
            )
            return Reply.unknown
        set_state(f, FulfilmentState.needs_attention)
        note(
            s,
            f,
            "unclear_reply",
            "The buyer's reply is still not clear. Please answer in the chat.",
        )
        events.publish(
            s,
            "order.delivery_failed",
            {"order": order.remote_id, "error": "unclear buyer reply"},
            store_id=f.store_id,
        )
        return Reply.unknown
    return None


# ---- owner actions -----------------------------------------------------------------------------


class ActionError(Exception):
    pass


async def approve(session: AsyncSession, actor: audit.Actor, fulfilment: Fulfilment) -> None:
    if fulfilment.state is not FulfilmentState.awaiting_approval:
        raise ActionError("Only orders waiting for approval can be approved.")
    if fulfilment.approved_by is not None:
        raise ActionError("This delivery is already approved and on its way.")
    fulfilment.approved_by = actor.label
    note(session, fulfilment, "approved", "Delivery approved.", actor=actor)
    audit.record(
        session,
        actor,
        "order.delivery_approved",
        entity_type="fulfilment",
        entity_id=fulfilment.id,
        store_id=fulfilment.store_id,
    )
    await jobs.enqueue(
        session,
        PROCESS_JOB,
        {"fulfilment_id": fulfilment.id},
        store_id=fulfilment.store_id,
        idempotency_key=f"fulfil:{fulfilment.id}:approved",
    )


async def retry(session: AsyncSession, actor: audit.Actor, fulfilment: Fulfilment) -> None:
    if fulfilment.state is not FulfilmentState.needs_attention:
        raise ActionError("Only orders that need attention can be retried.")
    has_sent = await session.scalar(
        select(func.count())
        .select_from(Reservation)
        .join(StockItem, StockItem.id == Reservation.item_id)
        .where(Reservation.fulfilment_id == fulfilment.id, StockItem.state == ItemState.delivered)
    )
    has_reserved = await session.scalar(
        select(func.count())
        .select_from(Reservation)
        .join(StockItem, StockItem.id == Reservation.item_id)
        .where(Reservation.fulfilment_id == fulfilment.id, StockItem.state == ItemState.reserved)
    )
    target = (
        FulfilmentState.sent
        if has_sent
        else FulfilmentState.reserved
        if has_reserved
        else FulfilmentState.pending
    )
    set_state(fulfilment, target)
    fulfilment.attempts = 0
    note(session, fulfilment, "retried", "Retried by hand.", actor=actor)
    audit.record(
        session,
        actor,
        "order.delivery_retried",
        entity_type="fulfilment",
        entity_id=fulfilment.id,
        store_id=fulfilment.store_id,
        after={"from": target.value},
    )
    count = int(
        await session.scalar(
            select(func.count())
            .select_from(FulfilmentEvent)
            .where(FulfilmentEvent.fulfilment_id == fulfilment.id)
        )
        or 0
    )
    await jobs.enqueue(
        session,
        PROCESS_JOB,
        {"fulfilment_id": fulfilment.id},
        store_id=fulfilment.store_id,
        idempotency_key=f"fulfil:{fulfilment.id}:retry:{count}",
    )


async def resolve(
    session: AsyncSession, actor: audit.Actor, fulfilment: Fulfilment, reason: str
) -> int:
    """The owner handled the order outside the platform. Unsent items go back to stock."""
    if fulfilment.state in (
        FulfilmentState.done,
        FulfilmentState.cancelled,
        FulfilmentState.simulated,
    ):
        raise ActionError("This order is already closed.")
    released = await release(session, fulfilment, "handled by hand")
    set_state(fulfilment, FulfilmentState.manual)
    note(
        session, fulfilment, "resolved", f"Marked as handled by hand. {reason}".strip(), actor=actor
    )
    audit.record(
        session,
        actor,
        "order.resolved_by_hand",
        entity_type="fulfilment",
        entity_id=fulfilment.id,
        store_id=fulfilment.store_id,
        reason=reason or None,
        after={"released": released},
    )
    return released


async def pools_by_id(session: AsyncSession, store_id: int) -> dict[int, str]:
    return {
        p.id: p.name
        for p in await session.scalars(select(StockPool).where(StockPool.store_id == store_id))
    }
