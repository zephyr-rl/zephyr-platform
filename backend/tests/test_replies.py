"""Buyer reply classification: never act on a question or a doubt."""

from __future__ import annotations

import pytest

from app.modules.deliveries.replies import Reply, classify


@pytest.mark.parametrize(
    "text",
    [
        "CONFIRM",
        "confirm",
        "Confirmed!",
        "yes",
        "ok",
        "yes please",
        "I confirm",
        "go ahead send it",
        "i understand, send accounts",
        "proceed",
        "Sure",
    ],
)
def test_confirmations(text: str) -> None:
    assert classify(text) is Reply.confirm


@pytest.mark.parametrize(
    "text",
    [
        "CANCEL",
        "cancel please",
        "refund",
        "no",
        "nope",
        "I want to cancel",
        "please refund",
        "cancel my order",
        "no thanks",
        "i dont want it",
    ],
)
def test_cancellations(text: str) -> None:
    assert classify(text) is Reply.cancel


@pytest.mark.parametrize(
    "text",
    [
        "confirm?",  # any question
        "can I link it later",
        "what if my account is restricted",
        "I don't understand",
        "don't cancel",
        "do not send yet",
        "is this legit",
        "how do i check",
        "",
        "   ",
        "yes but I have a question about the second account and the email",  # long
        "hello",
    ],
)
def test_doubts_and_questions_are_never_acted_on(text: str) -> None:
    assert classify(text) is Reply.unknown


def test_spanish_only_when_the_pack_is_on() -> None:
    assert classify("confirmo") is Reply.unknown
    assert classify("confirmo", ["es"]) is Reply.confirm
    assert classify("Sí, listo", ["es"]) is Reply.confirm
    assert classify("quiero cancelar", ["es"]) is Reply.cancel
    assert classify("no cancelen", ["es"]) is Reply.unknown
    assert classify("¿puedo vincularla después?", ["es"]) is Reply.unknown
    assert classify("cancel", ["es"]) is Reply.cancel  # English always applies


def test_unknown_language_codes_are_ignored() -> None:
    assert classify("confirm", ["xx"]) is Reply.confirm
