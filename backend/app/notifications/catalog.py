"""What each event offers to a notification template: variables (with sample values for the
live preview) and ready-made presets in the style of the old Discord notifications."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class EventInfo:
    type: str
    label: str
    description: str
    variables: dict[str, tuple[str, str]]  # name -> (description, sample value)
    group: str = "Orders"


_STORE = {
    "store": ("Store name", "ZephyrStore"),
    "time": ("When it happened", "Nov 14, 2026 23:41"),
}

_ORDER: dict[str, tuple[str, str]] = {
    **_STORE,
    "order": ("Order id", "8f3c2a1e-77b4-4c4e-9a51-3d2f0c6b1a90"),
    "short_order": ("First 8 characters of the order id", "8f3c2a1e"),
    "order_url": ("Link to the order on the marketplace", "https://www.eldorado.gg/order/8f3c2a1e"),
    "buyer": ("Buyer's username", "NightOwl92"),
    "title": ("Offer title", "Fortnite account, 50+ skins"),
    "listing": ("Listing name in the panel", "Fortnite account, 50+ skins"),
    "quantity": ("Units bought", "2"),
    "total": ("Order total with currency", "$19.98 USD"),
    "amount": ("Order total, number only", "19.98"),
    "currency": ("Currency", "USD"),
}

_DELIVERY: dict[str, tuple[str, str]] = {
    "accounts": ("Accounts delivered", "2"),
    "delivery": ("How it was delivered", "Automatic (chat)"),
    "pool": ("Stock pool used", "Fortnite accounts"),
    "stock_left": ("Accounts left in the pool", "41"),
    "platform": ("Platform of a cross-platform card's offer", "PlayStation"),
    "bundle": ("Bundle detail when a unit has several accounts", "(1 unit x 2 accounts)"),
}

EVENTS: dict[str, EventInfo] = {
    e.type: e
    for e in (
        EventInfo(
            "order.delivered",
            "Sale delivered",
            "An order was delivered: by the platform, from the vault or by hand.",
            {**_ORDER, **_DELIVERY},
        ),
        EventInfo(
            "order.paid",
            "New paid order",
            "A paid order was seen. Sent before anything is delivered.",
            {**_ORDER, "delivery": ("How it will be delivered", "Assisted (needs approval)")},
        ),
        EventInfo(
            "order.awaiting_confirmation",
            "Waiting for the buyer",
            "The buyer was asked to confirm before the delivery.",
            _ORDER,
        ),
        EventInfo(
            "order.delivery_failed",
            "Delivery needs you",
            "A delivery stopped and needs a person.",
            {**_ORDER, "error": ("What went wrong", "Marketplace error 503 after 6 tries")},
        ),
        EventInfo(
            "order.cancelled",
            "Order cancelled",
            "An order was cancelled (by the buyer or on the marketplace).",
            {**_ORDER, "by": ("Who cancelled", "buyer")},
        ),
        EventInfo(
            "chat.message_received",
            "Buyer message",
            "A buyer wrote in an order chat and nobody has answered yet.",
            {
                **_ORDER,
                "message": ("The buyer's message", "Hi, the password doesn't work"),
                "urgent": ("“Urgent” when it contains an alert word", "Urgent"),
                "attachment": ("“Yes” when the buyer sent a file", ""),
            },
            group="Chat",
        ),
        EventInfo(
            "stock.low",
            "Stock running low",
            "A pool went below its low-stock threshold.",
            {**_STORE, "pool": ("Pool", "Fortnite accounts"), "available": ("Available", "4")},
            group="Stock",
        ),
        EventInfo(
            "stock.depleted",
            "Stock empty",
            "A pool has no available accounts left.",
            {**_STORE, "pool": ("Pool", "Fortnite accounts"), "available": ("Available", "0")},
            group="Stock",
        ),
        EventInfo(
            "stock.imported",
            "Stock imported",
            "Accounts were added to a pool.",
            {**_STORE, "pool": ("Pool", "Fortnite accounts"), "added": ("Accounts added", "120")},
            group="Stock",
        ),
        EventInfo(
            "stock.quarantined",
            "Accounts quarantined",
            "Accounts were taken out of sale for review.",
            {
                **_STORE,
                "count": ("How many", "1"),
                "reason": ("Why", "already in the vault"),
                "listing": ("Listing, when it came from a restock", "Fortnite account, 50+ skins"),
            },
            group="Stock",
        ),
        EventInfo(
            "offer.restocked",
            "Vault restocked",
            "Accounts were uploaded to the vault of an instant-delivery listing.",
            {
                **_STORE,
                "listing": ("Listing", "Fortnite account, 50+ skins"),
                "uploaded": ("Accounts uploaded", "2"),
                "vault": ("Accounts now in the vault", "3"),
                "resumed": ("“Offer resumed” when it was paused", "Offer resumed"),
            },
            group="Stock",
        ),
        EventInfo(
            "report.daily_ready",
            "Daily report",
            "The day's sales summary, sent once per store at the report time.",
            {
                **_STORE,
                "date": ("Day of the report", "Nov 14, 2026"),
                "timezone": ("Store time zone", "America/Argentina/Buenos_Aires"),
                "revenue": ("Revenue of valid orders", "$412.50 USD"),
                "orders": ("Valid orders", "37"),
                "accounts": ("Accounts delivered", "52"),
                "level": ("Day level by accounts delivered", "Standard"),
                "by_type": (
                    "Breakdown by product type",
                    "Accounts (instant): 30 accounts in 25 orders, $280.00 USD\n"
                    "Items: 22 accounts in 12 orders, $132.50 USD",
                ),
                "by_listing": (
                    "Breakdown by listing",
                    "Fortnite account, 50+ skins: 30 accounts in 25 orders, $280.00 USD\n"
                    "Twitch drops bundle: 22 accounts in 12 orders, $132.50 USD",
                ),
                "stock": ("Stock per pool at closing", "Fortnite accounts: 41\nTwitch drops: 120"),
                "cancelled": ("Cancelled or refunded orders", "2 ($14.98 USD)"),
            },
            group="Reports",
        ),
        EventInfo(
            "system.error",
            "System error",
            "Something in the platform failed and needs a look.",
            {**_STORE, "message": ("Error", "The marketplace rejected the credentials")},
            group="System",
        ),
        EventInfo(
            "auth.login_failed",
            "Failed sign-in",
            "Someone entered a wrong password for the panel.",
            {"email": ("E-mail used", "owner@example.com"), "ip": ("IP address", "203.0.113.7")},
            group="System",
        ),
    )
}


def sample(event_type: str) -> dict[str, str]:
    return {k: v[1] for k, v in EVENTS[event_type].variables.items()}


@dataclass(frozen=True)
class Preset:
    key: str
    name: str
    event_type: str
    template: dict[str, Any] = field(default_factory=dict)


def _f(name: str, value: str, inline: bool = True) -> dict[str, Any]:
    return {"name": name, "value": value, "inline": inline}


# Same information and order as the old notifications, written in English.
PRESETS: tuple[Preset, ...] = (
    Preset(
        "sale",
        "Sale delivered",
        "order.delivered",
        {
            "title": "🛒 New sale delivered!",
            "description": "Order **#{short_order}** was delivered on {store}.",
            "color": "#2ECC71",
            "fields": [
                _f("🆔 Order", "`#{short_order}`"),
                _f("👤 Buyer", "**{buyer}**"),
                _f("⚡ Delivery", "{delivery}"),
                _f("💰 Price", "**{total}**"),
                _f("🔗 Order", "[Open #{short_order}]({order_url})"),
                _f("🎮 Offer", "{title}", False),
                _f("🕹️ Platform", "{platform}"),
                _f("📁 Pool", "`{pool}`"),
                _f("🔢 Accounts", "**{accounts}** {bundle}"),
                _f("📊 Stock left", "**{stock_left}** accounts"),
            ],
            "footer": "Zephyr Platform • Sales",
        },
    ),
    Preset(
        "chat",
        "Buyer message",
        "chat.message_received",
        {
            "title": "💬 New message from {buyer}",
            "description": "> {message}",
            "color": "#3498DB",
            "fields": [
                _f("🚨 Priority", "**{urgent}**"),
                _f("👤 Buyer", "**{buyer}**"),
                _f("🆔 Order", "`#{short_order}`"),
                _f("🎮 Product", "{title}", False),
                _f("📎 Attachment", "{attachment}"),
                _f("🔗 Direct link", "[Open the chat]({order_url})", False),
            ],
            "footer": "Zephyr Platform • Chat",
        },
    ),
    Preset(
        "daily",
        "Daily report",
        "report.daily_ready",
        {
            "title": "📊 Daily sales summary",
            "description": "Sales of **{date}** in {store}.\nTime zone: **{timezone}**.",
            "color": "#F1C40F",
            "fields": [
                _f("💰 Revenue", "**{revenue}**"),
                _f("📦 Orders", "**{orders}**"),
                _f("🏆 Accounts delivered", "**{accounts}** ({level})"),
                _f("📊 By product type", "{by_type}", False),
                _f("🏷️ By listing", "{by_listing}", False),
                _f("💾 Stock at closing", "{stock}", False),
                _f("⚠️ Cancelled / refunded", "{cancelled}", False),
            ],
            "footer": "Zephyr Platform • Daily report",
        },
    ),
    Preset(
        "failed",
        "Delivery needs you",
        "order.delivery_failed",
        {
            "title": "⚠️ Delivery stopped: order #{short_order}",
            "description": "{error}",
            "color": "#E74C3C",
            "fields": [
                _f("👤 Buyer", "**{buyer}**"),
                _f("🎮 Offer", "{title}", False),
                _f("🔗 Order", "[Open #{short_order}]({order_url})"),
            ],
            "footer": "Zephyr Platform",
        },
    ),
    Preset(
        "low_stock",
        "Stock running low",
        "stock.low",
        {
            "title": "📉 {pool} is running low",
            "description": "Only **{available}** accounts left in {store}.",
            "color": "#E67E22",
            "fields": [],
            "footer": "Zephyr Platform • Stock",
        },
    ),
    Preset(
        "empty_stock",
        "Stock empty",
        "stock.depleted",
        {
            "title": "⛔ {pool} is empty",
            "description": "No accounts left in {store}. Listings using it cannot deliver.",
            "color": "#E74C3C",
            "fields": [],
            "footer": "Zephyr Platform • Stock",
        },
    ),
    Preset(
        "restocked",
        "Vault restocked",
        "offer.restocked",
        {
            "title": "📦 Vault restocked",
            "description": "**{uploaded}** account(s) uploaded to {listing}. {resumed}",
            "color": "#1ABC9C",
            "fields": [_f("In the vault", "**{vault}**")],
            "footer": "Zephyr Platform • Restock",
        },
    ),
)

PRESETS_BY_KEY = {p.key: p for p in PRESETS}
