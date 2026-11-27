"""The modules shipped in v1.

Stage 2 declares each module with its settings schema and requirements so stores can switch them
on and off. Their behaviour (job handlers, subscriptions) is added in later stages.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.connectors.base import Capability
from app.core.modules import ModuleContext, ModuleRegistry, PeriodicTask
from app.modules.deliveries.messages import (
    DEFAULT_CANCELLATION,
    DEFAULT_CLARIFICATION,
    DEFAULT_CONFIRMATION_MULTI,
    DEFAULT_CONFIRMATION_SINGLE,
    DEFAULT_DELIVERY_MESSAGE,
    DEFAULT_LANGUAGE_CLARIFICATION,
    DEFAULT_TOPUP_INSTRUCTIONS,
)


class _Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DeliveriesSettings(_Settings):
    auto_deliver: bool = Field(
        True,
        description="Turn off to hold every automatic listing for your approval (kill switch).",
        json_schema_extra={"ui": "hidden"},  # the switch is in Listings
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
    language_clarification: str = Field(
        DEFAULT_LANGUAGE_CLARIFICATION,
        min_length=1,
        max_length=4000,
        description=(
            "Sent once when the buyer answers CONFIRM or CANCEL in French or Italian. "
            "Nothing is delivered or cancelled because of that reply."
        ),
    )
    cancellation: str = Field(
        DEFAULT_CANCELLATION,
        min_length=1,
        max_length=4000,
        description="Sent after the order is cancelled at the buyer's request.",
    )
    reply_languages: list[str] = Field(
        default_factory=lambda: ["en", "es"],
        description='Languages recognised in buyer replies: "en" (always) and "es".',
    )
    lookback_hours: int = Field(72, ge=1, le=720, description="How far back orders are read.")


class RestockSettings(_Settings):
    interval_minutes: int = Field(
        1, ge=1, le=1440, description="How often the vault of each listing is checked."
    )


class TopUpsSettings(_Settings):
    instructions: str = Field(
        DEFAULT_TOPUP_INSTRUCTIONS,
        min_length=1,
        max_length=4000,
        description="Sent when a top-up is paid, for listings set to send instructions. "
        "{buyer}, {product}, {quantity} and {order} are replaced. "
        "A listing can have its own message.",
    )
    skip_offers: list[str] = Field(
        default_factory=list,
        max_length=500,
        description="Marketplace offer ids of top-up listings that get no instructions.",
        json_schema_extra={"ui": "listings"},  # edited per listing in Listings
    )
    offer_messages: dict[str, str] = Field(
        default_factory=dict,
        description="Instructions per marketplace offer id (imported from the old system). "
        "A message set on the listing comes first.",
        json_schema_extra={"ui": "listings"},
    )


class ReviewRemindersSettings(_Settings):
    automatic: bool = Field(
        False,
        description="Send reminders by itself. Off: you choose the buyers and start each batch.",
    )
    message_template: str = Field(
        "Hi {buyer}! Thank you for your purchase. If everything went well, a review would help "
        "my store a lot. Thank you!",
        min_length=1,
        max_length=4000,
        description="{buyer}, {product} and {order} are replaced.",
    )
    delay_hours: int = Field(
        24, ge=1, le=336, description="An order is reminded at least this long after it was placed."
    )
    lookback_days: int = Field(14, ge=1, le=60, description="Older orders are never reminded.")
    review_filter: Literal["skip_negative", "skip_reviewed", "remind_all"] = Field(
        "skip_negative",
        description="Skip negative: not when that order got a negative review. "
        "Skip reviewed: not when it got any review. Remind all: review or not.",
    )
    buyer_cooldown_days: int = Field(
        14, ge=0, le=90, description="At most one reminder per buyer within this many days."
    )
    daily_cap: int = Field(35, ge=1, le=200, description="At most this many reminders per day.")
    min_delay_seconds: int = Field(
        20, ge=5, le=600, description="Shortest pause between two reminders."
    )
    max_delay_seconds: int = Field(
        50, ge=5, le=1800, description="Longest pause between two reminders."
    )

    @model_validator(mode="after")
    def _delays(self) -> ReviewRemindersSettings:
        if self.max_delay_seconds < self.min_delay_seconds:
            raise ValueError("The longest pause must be at least the shortest pause.")
        return self


class ChatMonitorSettings(_Settings):
    conversations: int = Field(
        30,
        ge=5,
        le=100,
        description="How many of the most recent open orders have their chat read.",
    )
    max_age_minutes: int = Field(
        5, ge=1, le=120, description="Messages older than this never raise an alert."
    )
    cooldown_minutes: int = Field(
        20, ge=1, le=1440, description="At most one alert per conversation within this time."
    )
    seller_active_minutes: int = Field(
        3, ge=0, le=60, description="No alert while you wrote in that chat within this time."
    )
    alert_keywords: list[str] = Field(
        default_factory=lambda: ["not working", "wrong password", "refund", "banned", "scam"],
        max_length=200,
        description="Messages with any of these words are marked urgent.",
    )
    courtesy_replies: list[str] = Field(
        default_factory=lambda: [
            "thanks",
            "thank you",
            "ty",
            "thx",
            "ok",
            "okay",
            "received",
            "done",
            "perfect",
            "gracias",
            "listo",
            "perfecto",
        ],
        max_length=200,
        description="Short replies like these on delivered orders are not alerted.",
    )


class ReportsSettings(_Settings):
    daily_report_time: str = Field(
        "23:59", pattern=r"^([01]\d|2[0-3]):[0-5]\d$", description="Store local time (HH:MM)."
    )


@dataclass
class DeclaredModule:
    key: str
    name: str
    description: str
    settings_schema: type[BaseModel]
    requires: frozenset[Capability] = field(default_factory=frozenset)
    default_enabled: bool = False
    # Cannot be turned off (other modules depend on it).
    always_on: bool = False
    # Managed on another page of the panel (not listed in Automation).
    managed_in: str | None = None

    def register(self, ctx: ModuleContext) -> None:
        return None


class DeliveriesModule(DeclaredModule):
    def register(self, ctx: ModuleContext) -> None:
        # Imported here: the service imports this catalog for its settings.
        from app.modules.deliveries import jobs as delivery_jobs

        delivery_jobs.register(ctx)
        # Offer schedules (Listings page) belong to no module: registered here because this
        # module is always on; module_key None runs it for every active store.
        from app.listings import schedule_jobs

        schedule_jobs.register(ctx)
        ctx.periodic.append(
            PeriodicTask("offer-schedule", "listings.schedule", timedelta(seconds=60))
        )
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


class RestockModule(DeclaredModule):
    def register(self, ctx: ModuleContext) -> None:
        from app.modules.restock import jobs as restock_jobs

        restock_jobs.register(ctx)
        # Runs every minute; each listing is checked when its interval has passed.
        ctx.periodic.append(
            PeriodicTask("restock", "restock.run", timedelta(seconds=60), module_key=self.key)
        )


class ReviewRemindersModule(DeclaredModule):
    def register(self, ctx: ModuleContext) -> None:
        from app.modules.review_reminders import jobs as reminder_jobs

        reminder_jobs.register(ctx)
        # Automatic mode only: queues today's share of candidates (manual mode queues nothing).
        ctx.periodic.append(
            PeriodicTask(
                "review-reminders",
                "review_reminders.auto",
                timedelta(minutes=10),
                module_key=self.key,
            )
        )


class ChatMonitorModule(DeclaredModule):
    def register(self, ctx: ModuleContext) -> None:
        from app.modules.chat_monitor import jobs as chat_jobs

        chat_jobs.register(ctx)
        ctx.periodic.append(
            PeriodicTask(
                "chat-monitor", "chat_monitor.scan", timedelta(seconds=30), module_key=self.key
            )
        )


class ReportsModule(DeclaredModule):
    def register(self, ctx: ModuleContext) -> None:
        from app.modules.reports import jobs as report_jobs

        report_jobs.register(ctx)
        # Checks every minute whether the store's report time has come; sends each day once.
        ctx.periodic.append(
            PeriodicTask(
                "daily-report", "reports.daily", timedelta(seconds=60), module_key=self.key
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
        always_on=True,
    ),
    RestockModule(
        "restock",
        "Restock listings",
        "Keeps the vault of instant-delivery listings stocked from their pools.",
        RestockSettings,
        frozenset({Capability.OFFERS, Capability.VAULT}),
        managed_in="listings",
    ),
    DeclaredModule(
        "topups",
        "Top-Ups",
        "Sends your instructions when a top-up is paid and lists the top-ups waiting for you.",
        TopUpsSettings,
        frozenset({Capability.ORDERS, Capability.CHAT}),
        managed_in="listings",
    ),
    ReviewRemindersModule(
        "review_reminders",
        "Review reminders",
        "Asks buyers for a review after their order, paced and capped per day.",
        ReviewRemindersSettings,
        frozenset({Capability.ORDERS, Capability.CHAT, Capability.REVIEWS}),
    ),
    ChatMonitorModule(
        "chat_monitor",
        "Chat monitor",
        "Alerts you when a buyer writes in an order chat and nobody has answered. "
        "Reads the orders the Deliveries module records.",
        ChatMonitorSettings,
        frozenset({Capability.CHAT}),
        default_enabled=True,
    ),
    ReportsModule(
        "reports",
        "Reports",
        "Daily sales report at the store's report time, and the numbers in Reports.",
        ReportsSettings,
        frozenset({Capability.ORDERS}),
        default_enabled=True,
    ),
)


def build_module_registry() -> ModuleRegistry:
    registry = ModuleRegistry()
    for module in V1_MODULES:
        registry.add(module)
    return registry
