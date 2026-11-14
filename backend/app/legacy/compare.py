"""Shadow-mode comparison: what the old system did against what the platform would have done.

Runs during Stage 10, while a store is in shadow mode next to the old system. Reads the old
store folder read-only (``item_delivery_logs.json``, ``topup_logs.json``, ``restock_logs.json``)
and the platform's simulated deliveries and ``shadow.*`` decisions, for the same period:

- deliveries: orders delivered by the old system / that the platform would deliver, and the
  number of accounts of each;
- top-ups: orders that got the instructions;
- restock: accounts uploaded to each offer's vault.

Never prints credentials: the old logs hold the delivered messages, which are not read.
The old logs keep only their latest entries (about 200); the report says when they do not cover
the whole period.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import AuditLog
from app.orders.models import Fulfilment, FulfilmentState, Order

TOPUP = "topup"


@dataclass
class Section:
    both: list[str] = field(default_factory=list)
    only_old: list[str] = field(default_factory=list)
    only_new: list[str] = field(default_factory=list)
    different: list[str] = field(default_factory=list)  # "<id>: old X, new Y"

    @property
    def ok(self) -> bool:
        return not (self.only_old or self.only_new or self.different)


@dataclass
class Comparison:
    store_id: int
    since: datetime
    until: datetime
    deliveries: Section = field(default_factory=Section)
    topups: Section = field(default_factory=Section)
    restock: Section = field(default_factory=Section)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.deliveries.ok and self.topups.ok and self.restock.ok


def _entries(folder: Path, name: str) -> list[dict[str, Any]]:
    path = folder / name
    if not path.is_file():
        return []
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    return [e for e in data if isinstance(e, dict)] if isinstance(data, list) else []


def _at(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _coverage(
    entries: list[dict[str, Any]], since: datetime, name: str, notes: list[str]
) -> list[dict[str, Any]]:
    stamps = [t for t in (_at(e.get("timestamp")) for e in entries) if t]
    if stamps and min(stamps) > since and len(entries) >= 150:
        notes.append(
            f"{name} only goes back to {min(stamps):%Y-%m-%d %H:%M} UTC: "
            "earlier differences cannot be checked."
        )
    return entries


def old_side(
    folder: Path, since: datetime, until: datetime, notes: list[str]
) -> tuple[dict[str, int], set[str], Counter[str]]:
    deliveries: dict[str, int] = {}
    logs = _coverage(
        _entries(folder, "item_delivery_logs.json"), since, "item_delivery_logs.json", notes
    )
    for e in logs:
        at = _at(e.get("timestamp"))
        raw = e.get("details")
        details: dict[str, Any] = raw if isinstance(raw, dict) else {}
        order = str(details.get("orderId") or e.get("orderId") or "")
        if e.get("type") == "SUCCESS" and order and at and since <= at < until:
            deliveries[order] = int(details.get("totalAccounts") or details.get("quantity") or 1)

    topups: set[str] = set()
    logs = _coverage(_entries(folder, "topup_logs.json"), since, "topup_logs.json", notes)
    for e in logs:
        at = _at(e.get("timestamp"))
        if e.get("level") == "SUCCESS" and e.get("orderId") and at and since <= at < until:
            topups.add(str(e["orderId"]))

    restock: Counter[str] = Counter()
    logs = _coverage(_entries(folder, "restock_logs.json"), since, "restock_logs.json", notes)
    for e in logs:
        at = _at(e.get("timestamp"))
        if e.get("type") == "SUCCESS" and e.get("offerId") and at and since <= at < until:
            restock[str(e["offerId"])] += 1
    return deliveries, topups, restock


async def new_side(
    session: AsyncSession, store_id: int, since: datetime, until: datetime
) -> tuple[dict[str, int], set[str], Counter[str]]:
    rows = await session.execute(
        select(Order.remote_id, Fulfilment.mode, Fulfilment.units)
        .join(Fulfilment, Fulfilment.order_id == Order.id)
        .where(
            Fulfilment.store_id == store_id,
            Fulfilment.state == FulfilmentState.simulated,
            Fulfilment.created_at >= since,
            Fulfilment.created_at < until,
        )
    )
    deliveries: dict[str, int] = {}
    topups: set[str] = set()
    for remote_id, mode, units in rows:
        if mode == TOPUP:
            topups.add(remote_id)
        elif mode != "manual":
            deliveries[remote_id] = units
    restock: Counter[str] = Counter(
        str(t)
        for t in await session.scalars(
            select(AuditLog.entity_id).where(
                AuditLog.store_id == store_id,
                AuditLog.action == "shadow.vault_add",
                AuditLog.at >= since,
                AuditLog.at < until,
            )
        )
    )
    return deliveries, topups, restock


def _counts(old: dict[str, int], new: dict[str, int], section: Section, unit: str) -> None:
    for key in sorted(old.keys() | new.keys()):
        if key in old and key in new:
            if old[key] == new[key]:
                section.both.append(key)
            else:
                section.different.append(f"{key}: old {old[key]} {unit}, new {new[key]} {unit}")
        elif key in old:
            section.only_old.append(key)
        else:
            section.only_new.append(key)


async def compare(
    session: AsyncSession, store_id: int, folder: Path, since: datetime, until: datetime
) -> Comparison:
    result = Comparison(store_id, since, until)
    old_d, old_t, old_r = old_side(folder, since, until, result.notes)
    new_d, new_t, new_r = await new_side(session, store_id, since, until)
    _counts(old_d, new_d, result.deliveries, "accounts")
    _counts({k: 1 for k in old_t}, {k: 1 for k in new_t}, result.topups, "")
    _counts(dict(old_r), dict(new_r), result.restock, "uploads")
    return result


def format_comparison(c: Comparison, store_name: str) -> str:
    def block(title: str, s: Section, what: str) -> list[str]:
        lines = [f"{title}: {len(s.both)} the same"]
        if s.only_old:
            lines.append(
                f"  Only the old system ({len(s.only_old)}): "
                + ", ".join(x[:8] for x in s.only_old)
            )
        if s.only_new:
            lines.append(
                f"  Only the platform ({len(s.only_new)}): " + ", ".join(x[:8] for x in s.only_new)
            )
        for d in s.different:
            lines.append(f"  Different {what}: {d}")
        return lines

    lines = [
        f"Shadow comparison for {store_name}, {c.since:%Y-%m-%d %H:%M} to {c.until:%Y-%m-%d %H:%M} UTC",
        "Result: " + ("the same" if c.ok else "DIFFERENCES to check"),
        *block("Deliveries (orders)", c.deliveries, "accounts"),
        *block("Top-up instructions (orders)", c.topups, ""),
        *block("Vault uploads (offers)", c.restock, "uploads"),
    ]
    lines += [f"Note: {n}" for n in c.notes]
    lines.append(
        "Orders the old system handled before the platform saw them, or the reverse, can appear on "
        "one side only around the start and end of the period."
    )
    return "\n".join(lines)
