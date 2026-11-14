"""The modules shipped in v1.

Stage 2 declares each module with its settings schema and requirements so stores can switch them
on and off. Their behaviour (job handlers, subscriptions) is added in later stages.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from pydantic import BaseModel, ConfigDict, Field

from app.connectors.base import Capability
from app.core.modules import ModuleContext, ModuleRegistry, PeriodicTask
from app.modules.deliveries.messages import (
    DEFAULT_CANCELLATION,
    DEFAULT_CLARIFICATION,
    DEFAULT_CONFIRMATION_MULTI,
    DEFAULT_CONFIRMATION_SINGLE,
    DEFAULT_DELIVERY_MESSAGE,
)


class _Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DeliveriesSettings(_Settings):
    auto_deliver: bool = Field(
        True,
        description="Turn off to hold every automatic listing for your approval (kill switch).",
    )
    message_template: str = Field(
        DEFAULT_DELIVERY_MESSAGE,
        min_length=1,
        max_length=4000,
        description="Message with the credentials. {items} is replaced by the accounts.",
    )
    item_label: str = Field(
        "Account",
        min_length=1,
        max_length=40,
        description='Name of each item, e.g. "Twitch Account".',
    )
    confirmation_single: str = Field(
        DEFAULT_CONFIRMATION_SINGLE,
        min_length=1,
        max_length=4000,
        description="Asked before delivering one account when the listing needs confirmation.",
    )
    confirmation_multi: str = Field(
        DEFAULT_CONFIRMATION_MULTI,
        min_length=1,
        max_length=4000,
        description="Same, for several accounts. {quantity} is replaced by the number.",
    )
    clarification: str = Field(
        DEFAULT_CLARIFICATION,
        min_length=1,
        max_length=4000,
        description="Sent once when the buyer's reply is not a clear CONFIRM or CANCEL.",
    )
    cancellation: str = Field(
        DEFAULT_CANCELLATION,
        min_length=1,
        max_length=4000,
        description="Sent after the order is cancelled at the buyer's request.",
    )
    reply_languages: list[str] = Field(
        default_factory=lambda: ["en"],
        description='Languages recognised in buyer replies: "en" (always) and "es".',
    )
    lookback_hours: int = Field(72, ge=1, le=720, description="How far back orders are read.")


class RestockSettings(_Settings):
    interval_minutes: int = Field(10, ge=1, le=1440, description="How often listings are checked.")
    target_quantity: int = Field(5, ge=1, le=500, description="Units kept visible per listing.")


class TopUpsSettings(_Settings):
    notify_new_orders: bool = Field(True, description="Alert when a top-up order arrives.")


class ReviewRemindersSettings(_Settings):
    delay_hours: int = Field(24, ge=1, le=336, description="Wait after delivery before reminding.")
    message_template: str = Field(
        "Hi! If everything went well, a review would help us a lot. Thank you!",
        min_length=1,
        max_length=2000,
    )


class ChatMonitorSettings(_Settings):
    alert_keywords: list[str] = Field(
        default_factory=lambda: ["not working", "wrong password", "refund", "banned"],
        max_length=200,
        description="Buyer messages containing any of these words raise an alert.",
    )


class ReportsSettings(_Settings):
    daily_report_time: str = Field(
        "23:59", pattern=r"^([01]\d|2[0-3]):[0-5]\d$", description="Store local time (HH:MM)."
    )


class AccountCheckSettings(_Settings):
    pass


@dataclass
class DeclaredModule:
    key: str
    name: str
    description: str
    settings_schema: type[BaseModel]
    requires: frozenset[Capability] = field(default_factory=frozenset)
    default_enabled: bool = False

    def register(self, ctx: ModuleContext) -> None:
        return None


class DeliveriesModule(DeclaredModule):
    def register(self, ctx: ModuleContext) -> None:
        # Imported here: the service imports this catalog for its settings.
        from app.modules.deliveries import jobs as delivery_jobs

        delivery_jobs.register(ctx)
        ctx.periodic.append(
            PeriodicTask("orders-poll", "orders.poll", timedelta(seconds=60), module_key=self.key)
        )
        ctx.periodic.append(
            PeriodicTask(
                "confirmation-replies",
                "deliveries.check_replies",
                timedelta(seconds=60),
                module_key=self.key,
            )
        )


V1_MODULES: tuple[DeclaredModule, ...] = (
    DeliveriesModule(
        "deliveries",
        "Deliveries",
        "Delivers stock to buyers when orders are paid.",
        DeliveriesSettings,
        frozenset({Capability.ORDERS, Capability.DELIVER}),
        default_enabled=True,
    ),
    DeclaredModule(
        "restock",
        "Restock listings",
        "Keeps listings stocked from the matching stock pools.",
        RestockSettings,
        frozenset({Capability.OFFERS, Capability.VAULT}),
    ),
    DeclaredModule(
        "topups",
        "Top-Ups",
        "Handles top-up orders that are fulfilled by hand.",
        TopUpsSettings,
        frozenset({Capability.ORDERS}),
    ),
    DeclaredModule(
        "review_reminders",
        "Review reminders",
        "Asks buyers for a review some time after delivery.",
        ReviewRemindersSettings,
        frozenset({Capability.ORDERS, Capability.CHAT}),
    ),
    DeclaredModule(
        "chat_monitor",
        "Chat monitor",
        "Flags buyer messages that need attention.",
        ChatMonitorSettings,
        frozenset({Capability.CHAT}),
    ),
    DeclaredModule(
        "reports",
        "Reports",
        "Daily sales report and statistics.",
        ReportsSettings,
        frozenset({Capability.ORDERS}),
        default_enabled=True,
    ),
    DeclaredModule(
        "account_check",
        "Account check",
        "Checks the inventory of stocked accounts.",
        AccountCheckSettings,
    ),
)


def build_module_registry() -> ModuleRegistry:
    registry = ModuleRegistry()
    for module in V1_MODULES:
        registry.add(module)
    return registry
