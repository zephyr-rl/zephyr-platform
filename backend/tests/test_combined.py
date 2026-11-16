"""All stores together: summaries added up per store's own days, and the combined Overview."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import Actor
from app.core.db import utcnow
from app.core.platform import Platform
from app.orders.models import Order
from app.stores import service as stores

ACTOR = Actor.system("tests")


async def _store(session: AsyncSession, platform: Platform, name: str, tz: str) -> tuple[int, int]:
    store = await stores.create_store(session, platform, ACTOR, name=name, slug=name.lower())
    store.timezone = tz
    connection = await stores.add_connection(
        session, platform, ACTOR, store, connector_key="fake", credentials={"api_key": "test-key"}
    )
    await session.commit()
    return store.id, connection.id


def _order(
    store_id: int, connection_id: int, rid: str, amount: str, at: datetime, state: str = "completed"
) -> Order:
    return Order(
        store_id=store_id,
        connection_id=connection_id,
        remote_id=rid,
        state=state,
        quantity=1,
        total_amount=Decimal(amount),
        offer_title="Offer " + rid,
        remote_created_at=at,
    )


async def test_combined_summary_and_overview(
    auth_client, session: AsyncSession, platform: Platform
) -> None:  # type: ignore[no-untyped-def]
    z_id, z_conn = await _store(session, platform, "Zephyr", "America/Argentina/Buenos_Aires")
    k_id, k_conn = await _store(session, platform, "Kawai", "Europe/Madrid")
    now = utcnow()
    ba_today = now.astimezone(ZoneInfo("America/Argentina/Buenos_Aires")).date()
    session.add_all(
        [
            _order(z_id, z_conn, "Z1", "10.00", now - timedelta(minutes=5)),
            _order(z_id, z_conn, "Z2", "4.00", now - timedelta(minutes=5), state="refunded"),
            _order(k_id, k_conn, "K1", "2.50", now - timedelta(minutes=5)),
        ]
    )
    await session.commit()

    today = (await auth_client.get("/api/v1/all/summary")).json()
    assert today["revenue"] == "12.50" and today["orders"] == 2 and today["cancelled_orders"] == 1
    assert {s["store"]: s["revenue"] for s in today["by_store"]} == {
        "Zephyr": "10.00",
        "Kawai": "2.50",
    }
    assert {r["store"] for r in today["by_listing"]} == {"Zephyr", "Kawai"}

    week = (
        await auth_client.get(
            "/api/v1/all/summary",
            params={
                "start": (ba_today - timedelta(days=6)).isoformat(),
                # Madrid's today can already be the day after Buenos Aires' today.
                "end": (ba_today + timedelta(days=1)).isoformat(),
            },
        )
    ).json()
    assert len(week["series"]) >= 8 and week["revenue"] == "12.50"

    overview = (await auth_client.get("/api/v1/all/overview")).json()
    figures = {s["store"]: s for s in overview["stores"]}
    assert figures["Zephyr"]["orders_today"] == 1 and figures["Kawai"]["revenue_today"] == "2.50"
    assert all(s["shadow_mode"] for s in overview["stores"])
    assert {a["store"] for a in overview["activity"]} <= {"Zephyr", "Kawai"}
