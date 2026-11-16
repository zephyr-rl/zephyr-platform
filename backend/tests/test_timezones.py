"""The time zone list for the store form."""

from __future__ import annotations

from app.api.v1.timezones import offset_label, zone_names


def test_zone_names_current_names_only() -> None:
    names = zone_names()
    assert names[0] == "UTC"
    assert "America/Argentina/Buenos_Aires" in names and "America/Buenos_Aires" not in names
    assert "America/Indiana/Indianapolis" in names and "America/Indianapolis" not in names
    assert "Europe/Madrid" in names and "Europe/Oslo" in names  # a link, but another city
    assert not any(n in names for n in ("GB", "Japan", "EST5EDT", "localtime", "Etc/GMT+3"))


def test_offset_label() -> None:
    assert offset_label(-180) == "UTC-03:00"
    assert offset_label(330) == "UTC+05:30"
    assert offset_label(0) == "UTC+00:00"


async def test_endpoint_and_alias_still_accepted(auth_client) -> None:  # type: ignore[no-untyped-def]
    rows = (await auth_client.get("/api/v1/timezones")).json()
    ba = next(r for r in rows if r["id"] == "America/Argentina/Buenos_Aires")
    assert (
        ba["region"] == "America"
        and ba["city"] == "Argentina / Buenos Aires"
        and ba["offset"] == "UTC-03:00"
    )
    # What a browser may report is still accepted by the store form.
    created = await auth_client.post(
        "/api/v1/stores", json={"name": "A", "slug": "a", "timezone": "America/Buenos_Aires"}
    )
    assert created.status_code == 201
    wrong = await auth_client.post(
        "/api/v1/stores", json={"name": "B", "slug": "b", "timezone": "Buenos Aires/Argentina"}
    )
    assert wrong.status_code == 422
