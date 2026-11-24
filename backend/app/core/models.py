"""Imports every model so ``Base.metadata`` is complete (Alembic, tests)."""

from app.core.audit import AuditLog
from app.core.events import OutboxEvent
from app.core.jobs import Job
from app.identity.models import LoginAttempt, User, UserSession
from app.listings.models import Listing, VaultUpload
from app.modules.chat_monitor.models import ChatThread
from app.modules.reports.models import DailyReport
from app.modules.review_reminders.models import ReviewReminder
from app.notifications.models import NotificationChannel, NotificationDelivery, NotificationRule
from app.orders.models import Fulfilment, FulfilmentEvent, Order, Reservation
from app.stock.models import (
    StockExtraction,
    StockImport,
    StockItem,
    StockItemEvent,
    StockItemSecret,
    StockPool,
)
from app.stores.models import MarketplaceConnection, Store, StoreModule
from app.tools.rocketpass.models import EpicAccount, PassCheck

__all__ = [
    "AuditLog",
    "ChatThread",
    "DailyReport",
    "EpicAccount",
    "Fulfilment",
    "FulfilmentEvent",
    "Job",
    "Listing",
    "LoginAttempt",
    "MarketplaceConnection",
    "NotificationChannel",
    "NotificationDelivery",
    "NotificationRule",
    "Order",
    "OutboxEvent",
    "PassCheck",
    "Reservation",
    "ReviewReminder",
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
    "VaultUpload",
]
