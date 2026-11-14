"""Posting to a Discord webhook."""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

import httpx

_WEBHOOK = re.compile(
    r"^https://(?:(?:canary|ptb)\.)?discord(?:app)?\.com/api(?:/v\d+)?/webhooks/(\d{5,25})/([\w-]{20,120})$"
)

# Tests replace this with an httpx.MockTransport; None means the real network.
transport: httpx.AsyncBaseTransport | None = None


class WebhookError(Exception):
    def __init__(
        self, message: str, *, retryable: bool, retry_after: timedelta | None = None
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


def check_url(url: str) -> str:
    url = url.strip()
    if not _WEBHOOK.match(url):
        raise ValueError(
            "Paste the full Discord webhook URL (https://discord.com/api/webhooks/<id>/<token>)."
        )
    return url


def url_hint(url: str) -> str:
    match = _WEBHOOK.match(url)
    if not match:
        return "webhook"
    return f"discord.com/…/webhooks/…{match.group(1)[-4:]}"


TIMEOUT = 15.0


async def post(url: str, body: dict[str, Any]) -> None:
    """Send one message. Raises WebhookError; ``retryable`` says whether trying later can help."""
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, transport=transport) as client:
            response = await client.post(url, json=body, params={"wait": "true"})
    except httpx.HTTPError as exc:
        raise WebhookError(
            f"Discord could not be reached: {type(exc).__name__}", retryable=True
        ) from exc
    if response.status_code in (200, 204):
        return
    detail = _detail(response)
    if response.status_code == 429:
        try:
            wait = float(response.json().get("retry_after", 5))
        except ValueError:
            wait = 5.0
        raise WebhookError(
            "Discord rate limit",
            retryable=True,
            retry_after=timedelta(seconds=min(max(wait, 1), 300)),
        )
    if response.status_code >= 500:
        raise WebhookError(f"Discord error {response.status_code}", retryable=True)
    if response.status_code in (401, 403, 404):
        raise WebhookError(
            f"Discord refused the webhook ({response.status_code}): "
            "it was deleted or the URL is wrong.",
            retryable=False,
        )
    raise WebhookError(
        f"Discord rejected the message ({response.status_code}): {detail}", retryable=False
    )


def _detail(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return response.text[:300]
    return str(data.get("message") or data)[:300] if isinstance(data, dict) else str(data)[:300]
