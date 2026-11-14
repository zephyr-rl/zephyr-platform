"""Opening a store's connector at run time, with shadow mode applied (invariant I9)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.base import MarketplaceConnector
from app.connectors.shadow import ShadowConnector, ShadowDecision
from app.core import audit
from app.core.platform import Platform
from app.stores.models import MarketplaceConnection, Store
from app.stores.service import connection_credentials


async def open_connector(
    session: AsyncSession, platform: Platform, connection: MarketplaceConnection
) -> MarketplaceConnector:
    """The connector for a connection. When the store is in shadow mode every write is replaced
    by an audit entry ``shadow.<action>`` and nothing is sent to the marketplace."""
    store = await session.get(Store, connection.store_id)
    assert store is not None
    inner = platform.connectors.create(
        connection.connector_key, connection_credentials(platform, connection)
    )
    if not store.shadow_mode:
        return inner
    store_id = store.id

    async def record(decision: ShadowDecision) -> None:
        # Own session: the decision is kept even if the caller's transaction rolls back.
        async with platform.db.sessionmaker() as own:
            audit.record(
                own,
                audit.Actor.system("shadow"),
                f"shadow.{decision.action}",
                entity_type="remote",
                entity_id=decision.target,
                store_id=store_id,
                after={"connector": decision.connector, **decision.details},
            )
            await own.commit()

    return ShadowConnector(inner, record)
