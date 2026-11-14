"""Read-only recorder: saves real Eldorado responses (redacted) to refresh the test fixtures.

Only GET requests are sent; nothing on the account changes. Passwords, tokens, logins, e-mails
and buyer names are replaced before anything is written to disk.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from app.connectors.eldorado.http import EldoradoHttp

_SECRET = re.compile(r"password|token|secret|login|email|buyerusername|username|phone|iban", re.I)

READS: dict[str, tuple[str, dict[str, Any]]] = {
    "users_me": ("api/users/me", {}),
    "seller_orders": ("api/orders/me/seller/orders", {"pageSize": 50}),
    "orders_state_count": ("api/orders/me/statesCount", {}),
    "account_offers": ("api/flexibleOffers/me/search", {"pageSize": 50, "pageIndex": 1}),
    "item_offers": ("api/v1/item-management/me/offers/me/search", {"pageSize": 50, "pageIndex": 1}),
    "predefined_offers": ("api/predefinedOffers/me", {}),
}


def redact(value: Any, *, key: str = "", seen: dict[str, str] | None = None) -> Any:
    seen = {} if seen is None else seen
    if isinstance(value, dict):
        return {k: redact(v, key=k, seen=seen) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, key=key, seen=seen) for v in value]
    if isinstance(value, str) and value and _SECRET.search(key):
        # Same input -> same placeholder, so relations between records survive.
        return seen.setdefault(f"{key}:{value}", f"<{key}-{len(seen) + 1}>")
    return value


async def record(client_id: str, client_secret: str) -> dict[str, Any]:
    """Run the read-only requests and return the redacted bodies by fixture name."""
    http = EldoradoHttp(client_id, client_secret, requests_per_second=2.0)
    found: dict[str, Any] = {}
    seen: dict[str, str] = {}
    try:
        for name, (path, params) in READS.items():
            found[name] = await http.request("GET", path, params=params)
        profile = found.get("users_me")
        user_id = profile.get("id") if isinstance(profile, dict) else None
        if user_id:
            found["reviews"] = await http.request(
                "GET", f"api/orders/reviews/{user_id}", params={"pageSize": 50}
            )
        offers = found.get("account_offers")
        rows = offers.get("results") if isinstance(offers, dict) else None
        first_id = rows[0].get("id") if rows else None
        if first_id:
            found["secret_details"] = await http.request(
                "GET", f"api/flexibleOffersUser/me/offers/{first_id}/secretDetails"
            )
    finally:
        await http.aclose()
    return {name: redact(body, seen=seen) for name, body in found.items()}


def write_fixtures(found: dict[str, Any], out: Path) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, data in found.items():
        path = out / f"{name}.json"
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        paths.append(path)
    return paths
