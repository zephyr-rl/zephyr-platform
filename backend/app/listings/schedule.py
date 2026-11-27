"""Offer schedule: pause an offer every day at one time and turn it back on at another.

Times are in the store's time zone. The window may cross midnight (off 01:00, on 09:00, or
off 23:30, on 08:00). The scheduler acts only when the wanted state changes, so pausing or
resuming an offer by hand in between is respected until the next scheduled time.

When a schedule is first saved inside the "off" window the offer is paused at once; inside the
"on" window nothing is done until the next off time (an offer you paused for another reason is
not turned on by saving a schedule).

In shadow mode the change is only recorded (the connector is the shadow one).
"""

from __future__ import annotations

import logging
from datetime import datetime, time
from typing import Literal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.base import ConnectorError, OfferState
from app.connectors.runtime import open_connector
from app.core import audit, events
from app.core.db import utcnow
from app.core.platform import Platform
from app.listings.models import Listing
from app.stores.models import MarketplaceConnection, Store

log = logging.getLogger(__name__)

RUN_JOB = "listings.schedule"
ACTOR = audit.Actor.system("offer-schedule")

Wanted = Literal["off", "on"]


class ScheduleError(Exception):
    pass


def wanted(off: time, on: time, now: time) -> Wanted:
    """The state the schedule asks for at local time ``now``."""
    if off == on:
        raise ScheduleError("The pause and the turn-on times must differ.")
    if off < on:
        return "off" if off <= now < on else "on"
    return "off" if now >= off or now < on else "on"


def describe(off: time | None, on: time | None) -> str | None:
    if off is None or on is None:
        return None
    return f"Paused from {off:%H:%M} to {on:%H:%M} every day"


def set_schedule(
    session: AsyncSession,
    actor: audit.Actor,
    listing: Listing,
    off: time | None,
    on: time | None,
) -> None:
    if (off is None) != (on is None):
        raise ScheduleError("Set both times, or neither to remove the schedule.")
    if off is not None and on is not None:
        wanted(off, on, off)  # validates
    if listing.removed_remotely and off is not None:
        raise ScheduleError("This offer no longer exists on the marketplace.")
    before = describe(listing.schedule_off, listing.schedule_on)
    listing.schedule_off, listing.schedule_on = off, on
    listing.schedule_applied = None  # the next run decides from scratch
    listing.schedule_note = None
    audit.record(
        session,
        actor,
        "listing.schedule_set" if off is not None else "listing.schedule_removed",
        entity_type="listing",
        entity_id=listing.id,
        store_id=listing.store_id,
        before={"schedule": before},
        after={"schedule": describe(off, on)},
    )


async def run_store(platform: Platform, store_id: int, now: datetime | None = None) -> int:
    """Apply every due schedule of a store. Returns how many offers changed state."""
    now = now or utcnow()
    changed = 0
    async with platform.db.sessionmaker() as session:
        store = await session.get(Store, store_id)
        if store is None or not store.is_active:
            return 0
        local = now.astimezone(ZoneInfo(store.timezone)).time().replace(second=0, microsecond=0)
        rows = list(
            await session.scalars(
                select(Listing).where(
                    Listing.store_id == store_id,
                    Listing.schedule_off.is_not(None),
                    Listing.schedule_on.is_not(None),
                    Listing.removed_remotely.is_(False),
                )
            )
        )
        for listing in rows:
            assert listing.schedule_off is not None and listing.schedule_on is not None
            want = wanted(listing.schedule_off, listing.schedule_on, local)
            if want == listing.schedule_applied:
                continue
            first = listing.schedule_applied is None
            if first and want == "on":
                listing.schedule_applied = "on"
                listing.schedule_last_at = now
                await session.commit()
                continue
            if await _apply(session, platform, store, listing, want, now):
                changed += 1
            await session.commit()
    return changed


async def _apply(
    session: AsyncSession,
    platform: Platform,
    store: Store,
    listing: Listing,
    want: Wanted,
    now: datetime,
) -> bool:
    target = OfferState.paused if want == "off" else OfferState.active
    verb = "Paused" if want == "off" else "Turned on"
    local = now.astimezone(ZoneInfo(store.timezone))
    if listing.remote_state == OfferState.closed.value:
        listing.schedule_applied = want
        listing.schedule_last_at = now
        listing.schedule_note = f"{local:%d/%m %H:%M}: closed on the marketplace, left as it is."
        return False
    if listing.remote_state == target.value:
        listing.schedule_applied = want
        listing.schedule_last_at = now
        listing.schedule_note = f"{local:%d/%m %H:%M}: already {target.value}."
        return False
    connection = await session.get(MarketplaceConnection, listing.connection_id)
    assert connection is not None
    connector = await open_connector(session, platform, connection)
    try:
        await connector.set_offer_state(listing.remote_offer_id, target)
    except ConnectorError as exc:
        listing.schedule_note = f"{local:%d/%m %H:%M}: could not change it: {exc}"
        if not exc.retryable:
            listing.schedule_applied = want  # do not retry every minute
            listing.schedule_last_at = now
        log.warning("schedule: listing %s -> %s failed: %s", listing.id, want, exc)
        events.publish(
            session,
            "system.error",
            {
                "message": (
                    f"Offer schedule: could not {'pause' if want == 'off' else 'turn on'} "
                    f'"{listing.title[:80]}": {exc}'
                )
            },
            store_id=listing.store_id,
        )
        return False
    finally:
        await connector.aclose()
    shadow = store.shadow_mode
    listing.schedule_applied = want
    listing.schedule_last_at = now
    listing.schedule_note = (
        f"{local:%d/%m %H:%M}: {'would be ' + verb.lower() if shadow else verb.lower()} "
        "by the schedule."
    )
    if not shadow:
        listing.remote_state = target.value
        audit.record(
            session,
            ACTOR,
            "listing.paused" if want == "off" else "listing.resumed",
            entity_type="listing",
            entity_id=listing.id,
            store_id=listing.store_id,
            after={"state": target.value},
            reason=f"Schedule: {describe(listing.schedule_off, listing.schedule_on)}",
        )
    return True
