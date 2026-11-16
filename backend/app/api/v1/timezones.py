"""The list of time zones for the store form (from the tz database the backend itself uses)."""

from __future__ import annotations

from datetime import UTC, datetime
from functools import lru_cache
from zoneinfo import ZoneInfo, available_timezones

from fastapi import APIRouter
from pydantic import BaseModel

from app.api.deps import AuthDep

router = APIRouter(tags=["time zones"])

REGIONS = (
    "Africa",
    "America",
    "Antarctica",
    "Arctic",
    "Asia",
    "Atlantic",
    "Australia",
    "Europe",
    "Indian",
    "Pacific",
)


@lru_cache(maxsize=1)
def zone_names() -> tuple[str, ...]:
    """Region/City names plus UTC, without old duplicates of the same city.

    The tz database keeps old names as aliases (America/Buenos_Aires for
    America/Argentina/Buenos_Aires): when a shorter name ends in the same city as a longer one
    in the same region, only the longer, current one is listed. Aliases are still accepted.
    """
    names = {n for n in available_timezones() if n.split("/")[0] in REGIONS and "/" in n}
    longer: dict[tuple[str, str], int] = {}
    for n in names:
        parts = n.split("/")
        key = (parts[0], parts[-1])
        longer[key] = max(longer.get(key, 0), len(parts))
    kept = [n for n in names if len(n.split("/")) == longer[(n.split("/")[0], n.split("/")[-1])]]
    return ("UTC", *sorted(kept))


def offset_label(minutes: int) -> str:
    sign = "+" if minutes >= 0 else "-"
    h, m = divmod(abs(minutes), 60)
    return f"UTC{sign}{h:02d}:{m:02d}"


class TimeZoneOut(BaseModel):
    id: str
    region: str
    city: str
    offset_minutes: int
    offset: str


@router.get("/timezones", response_model=list[TimeZoneOut])
async def list_timezones(auth: AuthDep) -> list[TimeZoneOut]:
    now = datetime.now(UTC)
    out = []
    for name in zone_names():
        delta = now.astimezone(ZoneInfo(name)).utcoffset()
        minutes = int(delta.total_seconds() // 60) if delta else 0
        region, _, rest = name.partition("/")
        out.append(
            TimeZoneOut(
                id=name,
                region=region if rest else "UTC",
                city=(rest or name).replace("_", " ").replace("/", " / "),
                offset_minutes=minutes,
                offset=offset_label(minutes),
            )
        )
    return out
