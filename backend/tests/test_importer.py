"""Every supplier format the old Account Manager accepted, plus the e-mail variants."""

from __future__ import annotations

import pytest

from app.stock.importer import parse


def creds(text: str) -> list[tuple[str, str, str | None]]:
    return [(a.login, a.password, a.token) for a in parse(text).accounts]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("user1:pass1", [("user1", "pass1", None)]),
        ("user1:pass1:tok123", [("user1", "pass1", "tok123")]),
        ("user1|pass1", [("user1", "pass1", None)]),
        ("user1|pass1|tok123", [("user1", "pass1", "tok123")]),
        ("user1 / pass1", [("user1", "pass1", None)]),
        ("mail@x.com pass1", [("mail@x.com", "pass1", None)]),
        ("user1\tpass1\ttok123", [("user1", "pass1", "tok123")]),
        ("ЛОГИН user1 / Пароль pass1", [("user1", "pass1", None)]),
        ("Логин: user1 / пароль: pass1", [("user1", "pass1", None)]),
        ("name: user1 password: pass1", [("user1", "pass1", None)]),
        ("name: user1 password: pass1 token: tok123", [("user1", "pass1", "tok123")]),
        (
            "1. user1:pass1\n2) user2:pass2\n3- user3:pass3",
            [
                ("user1", "pass1", None),
                ("user2", "pass2", None),
                ("user3", "pass3", None),
            ],
        ),
    ],
)
def test_single_line_formats(text: str, expected: list[tuple[str, str, str | None]]) -> None:
    assert creds(text) == expected


def test_email_credentials_are_recognised() -> None:
    [account] = parse("rluser:pw1:mail@inbox.lv:mailpw:https://inbox.lv").accounts
    assert (account.login, account.password) == ("rluser", "pw1")
    assert (account.email, account.email_password, account.email_provider) == (
        "mail@inbox.lv",
        "mailpw",
        "https://inbox.lv",
    )
    assert account.token is None
    assert account.payload()["email"] == "mail@inbox.lv"


def test_structured_blocks_with_tokens() -> None:
    text = """
name: alpha
password: a-pass
token: t-alpha

name: beta
password: b-pass

username: gamma
pass: g-pass
"""
    assert creds(text) == [
        ("alpha", "a-pass", "t-alpha"),
        ("beta", "b-pass", None),
        ("gamma", "g-pass", None),
    ]


def test_token_on_its_own_line_attaches_to_previous_account() -> None:
    assert creds("user1:pass1\ntoken: abc") == [("user1", "pass1", "abc")]


def test_supplier_headers_are_ignored_not_invalid() -> None:
    result = parse("Accounts (part 3/40) — order #1791842685137EBFS-1:\nuser1:pass1\nuser2:pass2")
    assert len(result.accounts) == 2
    assert result.ignored_lines == [1]
    assert result.invalid_lines == []


def test_glued_numbered_accounts_are_split() -> None:
    assert creds("1. user1:pass1 2. user2:pass2") == [
        ("user1", "pass1", None),
        ("user2", "pass2", None),
    ]


def test_trailing_list_number_is_removed() -> None:
    assert creds("user1:pass1 2.\n2. user2:pass2") == [
        ("user1", "pass1", None),
        ("user2", "pass2", None),
    ]


def test_invalid_lines_are_reported_with_their_number() -> None:
    result = parse("user1:pass1\nnot-an-account\n\nuser2:pass2")
    assert [a.line for a in result.accounts] == [1, 4]
    assert result.invalid_lines == [2]


def test_invisible_characters_are_stripped() -> None:
    assert creds("﻿user1:pass1​") == [("user1", "pass1", None)]


def test_detected_format() -> None:
    assert parse("a:b\nc:d").detected_format == "colon"
    assert parse("a:b\nc|d").detected_format == "mixed"
    assert parse("").detected_format == "unknown"


def test_passwords_may_contain_symbols() -> None:
    assert creds("user1:p@ss/w0rd!#") == [("user1", "p@ss/w0rd!#", None)]
