"""Messages sent to buyers. Defaults come from the old portal, where buyers already saw them."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

DEFAULT_DELIVERY_MESSAGE = "Thank you for your purchase!\n\n{items}"

DEFAULT_CONFIRMATION_SINGLE = (
    "⚠️ IMPORTANT NOTICE: EPIC GAMES LINKING RESTRICTION ⚠️\n\n"
    "Hello! Thank you for your purchase.\n\n"
    "📦 Your Order: 1 Twitch Account\n\n"
    "Before fulfilling your order, please read this important update from Epic Games:\n\n"
    '"Epic Games now restricts switching to a different Twitch account on the same Epic account, '
    "and the restriction can only be lifted once every 365 days. Buyers can no longer link several "
    'Twitch accounts one after another."\n\n'
    "🔗 Please verify right now if your Epic Games account is eligible to link a new Twitch account:\n"
    "https://www.epicgames.com/help/wizards/w4\n\n"
    "To protect your purchase and ensure you don't encounter issues:\n"
    '👉 Reply "CONFIRM" — If you have checked your account status, can link this Twitch account, '
    "and wish to receive the credentials.\n"
    '👉 Reply "CANCEL" — If your Epic Games account is currently restricted or you would prefer an '
    "immediate full refund.\n\n"
    "(Note: Once account credentials are delivered, orders cannot be refunded)."
)

DEFAULT_CONFIRMATION_MULTI = (
    "⚠️ IMPORTANT NOTICE: EPIC GAMES LINKING RESTRICTION ⚠️\n\n"
    "Hello! Thank you for your purchase.\n\n"
    "📦 Your Order: {quantity} Twitch Accounts\n\n"
    "Before fulfilling your order, please read this critical update from Epic Games:\n\n"
    '"Epic Games now restricts switching to a different Twitch account on the same Epic account, '
    "and the restriction can only be lifted once every 365 days. Buyers can no longer link several "
    'Twitch accounts one after another."\n\n'
    "⚠️ Please note: You will NOT be able to link all {quantity} Twitch accounts to the same Epic "
    "Games account. You must have {quantity} separate Epic Games accounts to claim them.\n\n"
    "🔗 You can check your account linking status directly on Epic Games here:\n"
    "https://www.epicgames.com/help/wizards/w4\n\n"
    "Please choose how you would like to proceed:\n"
    '👉 Reply "CONFIRM" — If you understand this policy, have separate Epic Games accounts for each '
    "Twitch account, and wish to receive all {quantity} accounts.\n"
    '👉 Reply "CANCEL" — If you did not know about this restriction and prefer an immediate full '
    "refund for the entire order.\n\n"
    "(Note: Once account credentials are delivered, orders cannot be refunded)."
)

DEFAULT_CLARIFICATION = (
    "ℹ️ Clarification regarding your order:\n\n"
    "As a reminder, Epic Games strictly restricts linking multiple Twitch accounts to a single Epic "
    "account (it can only be reset once every 365 days):\n"
    "https://www.epicgames.com/help/wizards/w4\n\n"
    "To protect your purchase:\n"
    '• Reply "CONFIRM" to proceed with the delivery.\n'
    '• Reply "CANCEL" to receive an immediate full refund.\n\n'
    "If you have specific questions, please wait a moment and you will be assisted directly."
)

DEFAULT_CANCELLATION = (
    "✅ Order Canceled & Refunded\n\n"
    "Your order has been canceled and fully refunded as requested due to the Epic Games linking "
    "restrictions. No accounts were used or charged.\n\n"
    "Thank you for your understanding, and feel free to check your Epic Games status at "
    "https://www.epicgames.com/help/wizards/w4 for future purchases! Have a great day!"
)

# Messages the old portal sent automatically. While both systems run side by side (shadow mode),
# seeing one of these in a chat is not "the seller took over the conversation".
OLD_SYSTEM_MARKERS = (
    "Activation instructions",
    "Activation Guide",
    "Twitch Account",
    "Follow this step-by-step guide",
    "IMPORTANT NOTICE: EPIC GAMES LINKING RESTRICTION",
    "Clarification regarding your order",
    "Order Canceled & Refunded",
    "Thank you for your purchase",
)


class Credentials(Protocol):
    @property
    def login(self) -> str: ...
    @property
    def password(self) -> str: ...


def fill(template: str, **values: object) -> str:
    """Replace {name} placeholders without breaking on other braces in the text."""
    for key, value in values.items():
        template = template.replace("{" + key + "}", str(value))
    return template


def items_block(items: Sequence[Credentials], label: str) -> str:
    blocks = []
    for n, item in enumerate(items, start=1):
        blocks.append(f"🟢{label} {n}\n- User: {item.login}\n- Password: {item.password}")
    return "\n\n".join(blocks)


def is_automated(text: str, sent_by_us: Sequence[str]) -> bool:
    text = text.strip()
    if not text:
        return True
    if any(marker in text for marker in OLD_SYSTEM_MARKERS):
        return True
    return any(text[:60] == sent.strip()[:60] for sent in sent_by_us if sent.strip())
