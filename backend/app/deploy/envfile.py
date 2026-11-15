"""The install folder's .env file: plain KEY=value lines."""

from __future__ import annotations

import base64
import os
import secrets
from pathlib import Path


def read(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def write(path: Path, values: dict[str, str]) -> None:
    lines = [
        "# Zephyr Platform settings. Keep a copy of this file outside the server:",
        "# without ZP_SECRET_KEYS the stored credentials cannot be read.",
        *(f"{k}={v}" for k, v in values.items()),
        "",
    ]
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines), encoding="utf-8")
    os.replace(tmp, path)


def new_key() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")


def new_password() -> str:
    return secrets.token_urlsafe(24)


def load_into_environment(path: Path) -> dict[str, str]:
    values = read(path)
    os.environ.update(values)
    return values
