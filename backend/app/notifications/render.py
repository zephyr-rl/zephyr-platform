"""Templates → Discord messages.

A template is ``{"title", "description", "color", "footer", "fields": [{"name", "value",
"inline"}]}`` where ``{variable}`` is replaced by the event's values. A field whose value is empty
after filling is left out (like the optional fields of the old notifications). Discord's size
limits are applied so a long message is shortened instead of rejected.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

_VAR = re.compile(r"\{([a-z_]+)\}")
_EMPTY_MARKUP = re.compile(r"^[\s`*_>()\[\]#:.,-]*$")

LIMITS = {"title": 256, "description": 4096, "field_name": 256, "field_value": 1024, "footer": 2048}
MAX_FIELDS = 25
MAX_TOTAL = 6000


class TemplateField(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(max_length=256)
    value: str = Field(max_length=1024)
    inline: bool = True


class Template(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field("", max_length=256)
    description: str = Field("", max_length=4096)
    color: str = Field("#2ECC71", pattern=r"^#[0-9A-Fa-f]{6}$")
    footer: str = Field("", max_length=2048)
    fields: list[TemplateField] = Field(default_factory=list, max_length=MAX_FIELDS)

    @field_validator("title", "description", "footer")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()


def fill(text: str, values: dict[str, str]) -> str:
    """Replace ``{name}`` with its value. Unknown names are left as they are, so a typo shows."""
    return _VAR.sub(lambda m: values.get(m.group(1), m.group(0)), text)


def unknown_variables(template: Template, known: set[str]) -> list[str]:
    texts = [template.title, template.description, template.footer]
    for f in template.fields:
        texts += [f.name, f.value]
    found = {m.group(1) for t in texts for m in _VAR.finditer(t)}
    return sorted(found - known)


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _blank(text: str) -> bool:
    return bool(_EMPTY_MARKUP.match(text))


def render(template: Template, values: dict[str, str]) -> dict[str, Any]:
    """The Discord embed for these values."""
    embed: dict[str, Any] = {"color": int(template.color[1:], 16)}
    title = _cut(fill(template.title, values).strip(), LIMITS["title"])
    description = _cut(fill(template.description, values).strip(), LIMITS["description"])
    if title:
        embed["title"] = title
    if description and not _blank(description):
        embed["description"] = description
    fields = []
    for f in template.fields:
        value = fill(f.value, values).strip()
        # Left out when empty, or when every variable in it was empty (only decoration left).
        if not value or _all_variables_empty(f.value, values):
            continue
        fields.append(
            {
                "name": _cut(fill(f.name, values).strip() or "​", LIMITS["field_name"]),
                "value": _cut(value, LIMITS["field_value"]),
                "inline": f.inline,
            }
        )
    if fields:
        embed["fields"] = fields[:MAX_FIELDS]
    footer = _cut(fill(template.footer, values).strip(), LIMITS["footer"])
    if footer:
        embed["footer"] = {"text": footer}
    return _fit(embed)


def _all_variables_empty(raw: str, values: dict[str, str]) -> bool:
    names = [m.group(1) for m in _VAR.finditer(raw)]
    if not names:
        return False
    return all(not values.get(n, "x").strip() for n in names)


def _fit(embed: dict[str, Any]) -> dict[str, Any]:
    """Discord rejects embeds over 6000 characters in total: drop fields from the end."""

    def total() -> int:
        n = len(embed.get("title", "")) + len(embed.get("description", ""))
        n += len(embed.get("footer", {}).get("text", ""))
        return n + sum(len(f["name"]) + len(f["value"]) for f in embed.get("fields", []))

    while total() > MAX_TOTAL and embed.get("fields"):
        embed["fields"].pop()
    if total() > MAX_TOTAL:
        embed["description"] = _cut(embed.get("description", ""), 2000)
    return embed


def message(
    embed: dict[str, Any], *, sender_name: str | None, avatar_url: str | None
) -> dict[str, Any]:
    body: dict[str, Any] = {"embeds": [embed], "allowed_mentions": {"parse": []}}
    if sender_name:
        body["username"] = sender_name[:80]
    if avatar_url:
        body["avatar_url"] = avatar_url
    return body


def summary(embed: dict[str, Any]) -> str:
    text = embed.get("title") or embed.get("description") or "(message)"
    return _cut(" ".join(str(text).split()), 300)
