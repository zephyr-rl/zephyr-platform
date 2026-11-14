"""Parser for account lists in every format suppliers send.

Pure functions, no database: the API shows a preview of the result before anything is saved.

Supported, one account per line unless stated:
- ``login:password`` and ``login:password:token`` (Twitch token)
- ``login:password:email:email_password[:email_provider_url]`` (the third field contains ``@``)
- ``login|password[|token]``, ``login / password``, ``login password [token]`` (spaces or tabs)
- Russian supplier format: ``ЛОГИН login / Пароль password``
- ``name: login password: password [token: token]`` on one line, or as a block of
  ``name:`` / ``password:`` / ``token:`` lines (blank line between accounts)
- numbered lists (``1. login:pass``, ``1) …``, ``1- …``), including two accounts glued on
  one line (``user:pass2. user2:pass2``)
- supplier headers such as ``Accounts (part 3/40) — order #123…`` are ignored
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

_INVISIBLE = re.compile(r"[​-‏⁠﻿]")
_LEADING_NUMBER = re.compile(r"^\s*\d+\s*[.)-]\s+|^\s*\d+\s*[.)]\s*")
_TRAILING_LIST_NUMBER = re.compile(r"\s*(\d+)\s*[.)-]\s*$")
_LEADING_LIST_NUMBER = re.compile(r"^\s*(\d+)\s*[.)-]")
# "user1:pass1 2. user2:pass2": a list pasted without line breaks.
_GLUED_SPLIT = re.compile(r"\s+\d+\s*[.)-]\s*(?=[^:\s]+:\S)")
_RUSSIAN = re.compile(r"ЛОГИН\s*:?\s*(\S+)\s*[/\\]\s*Пароль\s*:?\s*(\S+)", re.IGNORECASE)
_STRUCTURED_LINE = re.compile(
    r"^\s*(?:name|username|user|login|email)\s*:\s*(\S+)\s+(?:password|pass|pwd)\s*:\s*(\S+)"
    r"(?:\s+token\s*:\s*(\S+))?\s*$",
    re.IGNORECASE,
)
_BLOCK_NAME = re.compile(r"^\s*(?:name|username|user|login|email)\s*:\s*(\S+)\s*$", re.IGNORECASE)
_BLOCK_PASS = re.compile(r"^\s*(?:password|pass|pwd)\s*:\s*(\S+)\s*$", re.IGNORECASE)
_BLOCK_TOKEN = re.compile(r"^\s*token\s*:\s*(\S+)\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class ParsedAccount:
    line: int
    login: str
    password: str
    format: str
    token: str | None = None
    email: str | None = None
    email_password: str | None = None
    email_provider: str | None = None

    def payload(self) -> dict[str, str]:
        data = {"login": self.login, "password": self.password}
        if self.email:
            data["email"] = self.email
        if self.email_password:
            data["email_password"] = self.email_password
        if self.email_provider:
            data["email_provider"] = self.email_provider
        return data


@dataclass
class ParseResult:
    accounts: list[ParsedAccount] = field(default_factory=list)
    invalid_lines: list[int] = field(default_factory=list)
    ignored_lines: list[int] = field(default_factory=list)

    @property
    def detected_format(self) -> str:
        counts = Counter(a.format for a in self.accounts)
        if not counts:
            return "unknown"
        if len(counts) == 1:
            return next(iter(counts))
        return "mixed"


def normalise_login(login: str) -> str:
    """Identity used for duplicate detection: case and surrounding spaces do not matter."""
    return login.strip().lower()


def _is_header(line: str) -> bool:
    lower = line.lower()
    return lower.startswith("accounts (part") or "order #" in lower


def _clean(line: str) -> str:
    return _INVISIBLE.sub("", line).strip()


def _from_fields(fields: list[str], line_no: int, fmt: str) -> ParsedAccount | None:
    fields = [f.strip() for f in fields]
    if len(fields) < 2 or not fields[0] or not fields[1]:
        return None
    login = fields[0]
    if len(fields) >= 4 and "@" in fields[2]:
        return ParsedAccount(
            line_no,
            login,
            fields[1],
            f"{fmt}+email",
            email=fields[2],
            email_password=fields[3] or None,
            email_provider=":".join(fields[4:]).strip() or None,
        )
    if len(fields) == 2:
        return ParsedAccount(line_no, login, fields[1], fmt)
    password = (":" if fmt == "colon" else " ").join(fields[1:-1]).strip()
    token = fields[-1] or None
    if not password:
        return None
    return ParsedAccount(line_no, login, password, f"{fmt}+token" if token else fmt, token=token)


def _parse_line(line: str, line_no: int) -> ParsedAccount | None:
    line = _LEADING_NUMBER.sub("", line, count=1).strip()
    if not line:
        return None
    if m := _RUSSIAN.search(line):
        return ParsedAccount(line_no, m.group(1), m.group(2), "russian")
    if m := _STRUCTURED_LINE.match(line):
        return ParsedAccount(
            line_no,
            m.group(1),
            m.group(2),
            "labelled" + ("+token" if m.group(3) else ""),
            token=m.group(3),
        )
    if ":" in line and not re.search(r"\s", line.split(":", 1)[0]):
        return _from_fields(line.split(":"), line_no, "colon")
    if "|" in line:
        return _from_fields(line.split("|"), line_no, "pipe")
    if " / " in line:
        parts = line.split(" / ")
        return _from_fields(parts, line_no, "slash") if len(parts) == 2 else None
    parts = line.split()
    if len(parts) >= 2:
        return _from_fields(parts, line_no, "spaces")
    return None


def parse(text: str) -> ParseResult:
    result = ParseResult()
    raw_lines = [(_clean(line), i) for i, line in enumerate(text.splitlines(), start=1)]

    # Remove the list number that some exports put at the END of the previous line
    # ("user:pass 2." followed by a line starting with "2.").
    lines: list[tuple[str, int]] = []
    meaningful = [(text_, n) for text_, n in raw_lines if text_]
    for idx, (line, n) in enumerate(meaningful):
        if idx + 1 < len(meaningful):
            trailing = _TRAILING_LIST_NUMBER.search(line)
            following = _LEADING_LIST_NUMBER.match(meaningful[idx + 1][0])
            if (
                trailing
                and following
                and int(following.group(1)) - int(trailing.group(1)) in (0, 1)
            ):
                line = line[: trailing.start()].rstrip()
        lines.append((line, n))
    by_number = dict((n, t) for t, n in lines)

    pending_login: tuple[str, int] | None = None
    pending_password: str | None = None

    def flush() -> None:
        nonlocal pending_login, pending_password
        if pending_login and pending_password:
            result.accounts.append(
                ParsedAccount(pending_login[1], pending_login[0], pending_password, "labelled")
            )
        elif pending_login:
            result.invalid_lines.append(pending_login[1])
        pending_login, pending_password = None, None

    for line, n in raw_lines:
        if not line:
            flush()
            continue
        line = by_number.get(n, line)
        if _is_header(line):
            result.ignored_lines.append(n)
            continue
        if m := _BLOCK_NAME.match(line):
            flush()
            pending_login = (m.group(1), n)
            continue
        if m := _BLOCK_PASS.match(line):
            if pending_login and not pending_password:
                pending_password = m.group(1)
            else:
                result.invalid_lines.append(n)
            continue
        if m := _BLOCK_TOKEN.match(line):
            token = m.group(1)
            if pending_login and pending_password:
                result.accounts.append(
                    ParsedAccount(
                        pending_login[1],
                        pending_login[0],
                        pending_password,
                        "labelled+token",
                        token=token,
                    )
                )
                pending_login, pending_password = None, None
            elif result.accounts and result.accounts[-1].token is None:
                last = result.accounts.pop()
                result.accounts.append(
                    ParsedAccount(
                        last.line,
                        last.login,
                        last.password,
                        last.format + "+token",
                        token=token,
                        email=last.email,
                        email_password=last.email_password,
                        email_provider=last.email_provider,
                    )
                )
            else:
                result.invalid_lines.append(n)
            continue
        flush()
        for piece in _GLUED_SPLIT.split(line):
            parsed = _parse_line(piece, n)
            if parsed is None:
                result.invalid_lines.append(n)
            else:
                result.accounts.append(parsed)
    flush()
    return result
