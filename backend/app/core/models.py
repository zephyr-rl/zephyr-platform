"""Imports every model so ``Base.metadata`` is complete (Alembic, tests)."""

from app.core.audit import AuditLog
from app.core.events import OutboxEvent
from app.core.jobs import Job
from app.identity.models import LoginAttempt, User, UserSession
from app.stock.models import (
    StockExtraction,
    StockImport,
    StockItem,
    StockItemEvent,
    StockItemSecret,
    StockPool,
)
from app.stores.models import MarketplaceConnection, Store, StoreModule

__all__ = [
    "AuditLog",
    "Job",
    "LoginAttempt",
    "MarketplaceConnection",
    "OutboxEvent",
    "StockExtraction",
    "StockImport",
    "StockItem",
    "StockItemEvent",
    "StockItemSecret",
    "StockPool",
    "Store",
    "StoreModule",
    "User",
    "UserSession",
]
