"""Classify a buyer's reply to the confirmation request: confirm, cancel, or unknown.

Rules (from the old portal, which ran them in production):
- Anything that looks like a question, a doubt or a condition is ``unknown``: it is never acted on.
- A negated command ("don't cancel", "do not send") is ``unknown``.
- Only short, explicit replies count as ``confirm`` or ``cancel``.

English is built in. Other languages are optional packs the owner switches on per store
(buyers write in their own language; the pack only holds the words to recognise).
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass, field


class Reply(enum.StrEnum):
    confirm = "confirm"
    cancel = "cancel"
    unknown = "unknown"


@dataclass(frozen=True)
class LanguagePack:
    uncertainty: tuple[str, ...] = ()
    question_starters: frozenset[str] = field(default_factory=frozenset)
    negated_cancel: tuple[str, ...] = ()
    negated_confirm: tuple[str, ...] = ()
    cancel_phrases: tuple[str, ...] = ()
    cancel_exact: frozenset[str] = field(default_factory=frozenset)
    cancel_words: frozenset[str] = field(default_factory=frozenset)
    confirm_phrases: tuple[str, ...] = ()
    confirm_exact: frozenset[str] = field(default_factory=frozenset)
    affirmations: frozenset[str] = field(default_factory=frozenset)
    confirm_words: frozenset[str] = field(default_factory=frozenset)


ENGLISH = LanguagePack(
    uncertainty=(
        "dont understand",
        "do not understand",
        "not sure",
        "idk",
        "dont know",
        "do not know",
        "what if",
        "how do i",
        "how to",
        "can i",
        "could i",
        "should i",
    ),
    question_starters=frozenset(
        {
            "what",
            "why",
            "how",
            "when",
            "where",
            "who",
            "which",
            "can",
            "could",
            "would",
            "should",
            "shall",
            "is",
            "are",
            "am",
            "do",
            "does",
            "did",
            "if",
        }
    ),
    negated_cancel=("dont cancel", "do not cancel", "no cancel"),
    negated_confirm=(
        "dont confirm",
        "do not confirm",
        "no confirm",
        "dont send",
        "do not send",
        "dont deliver",
        "do not deliver",
    ),
    cancel_phrases=(
        "cancel please",
        "please cancel",
        "pls cancel",
        "cancel pls",
        "cancel order",
        "cancel the order",
        "cancel this order",
        "cancel my order",
        "i want to cancel",
        "i want cancel",
        "want to cancel",
        "refund please",
        "please refund",
        "pls refund",
        "dont want",
        "cancel it",
    ),
    cancel_exact=frozenset({"cancel", "refund", "no", "no thanks", "no ty", "nope", "nah"}),
    cancel_words=frozenset({"cancel", "refund"}),
    confirm_phrases=(
        "i confirm",
        "i understand",
        "i know",
        "send it",
        "deliver please",
        "please deliver",
        "i agree",
        "go ahead",
        "send accounts",
        "send account",
        "yes please",
        "yes pls",
    ),
    confirm_exact=frozenset({"confirm", "confirmed", "proceed", "deliver"}),
    affirmations=frozenset({"yes", "yep", "yeah", "sure", "ok", "okay", "ready"}),
    confirm_words=frozenset({"confirm", "confirmed"}),
)

# Optional pack for buyers who write in Spanish.
SPANISH = LanguagePack(
    uncertainty=(
        "no entiendo",
        "no estoy seguro",
        "no se",
        "que pasa si",
        "como hago",
        "como puedo",
        "deberia",
        "puedo",
        "podria",
    ),
    question_starters=frozenset({"que", "como", "cuando", "donde", "quien", "por"}),
    negated_cancel=("no cancelar", "no cancelen"),
    negated_confirm=("no confirmar", "no enviar", "no mandes", "no entregar"),
    cancel_phrases=("quiero cancelar", "quiero reembolso", "no quiero", "cancelalo"),
    cancel_exact=frozenset({"cancelar", "cancelo", "reembolso", "no gracias"}),
    cancel_words=frozenset({"cancelar", "cancelo", "reembolso"}),
    confirm_phrases=("confirmo", "entendido", "adelante", "enviar cuentas", "si por favor"),
    confirm_exact=frozenset({"confirmar", "confirmo"}),
    affirmations=frozenset({"si", "listo"}),
    confirm_words=frozenset({"confirmar", "confirmo"}),
)

PACKS: dict[str, LanguagePack] = {"en": ENGLISH, "es": SPANISH}


def _merge(codes: list[str]) -> LanguagePack:
    packs = [ENGLISH] + [PACKS[c] for c in codes if c in PACKS and c != "en"]
    return LanguagePack(
        uncertainty=tuple(p for pack in packs for p in pack.uncertainty),
        question_starters=frozenset().union(*(pack.question_starters for pack in packs)),
        negated_cancel=tuple(p for pack in packs for p in pack.negated_cancel),
        negated_confirm=tuple(p for pack in packs for p in pack.negated_confirm),
        cancel_phrases=tuple(p for pack in packs for p in pack.cancel_phrases),
        cancel_exact=frozenset().union(*(pack.cancel_exact for pack in packs)),
        cancel_words=frozenset().union(*(pack.cancel_words for pack in packs)),
        confirm_phrases=tuple(p for pack in packs for p in pack.confirm_phrases),
        confirm_exact=frozenset().union(*(pack.confirm_exact for pack in packs)),
        affirmations=frozenset().union(*(pack.affirmations for pack in packs)),
        confirm_words=frozenset().union(*(pack.confirm_words for pack in packs)),
    )


def classify(text: str, languages: list[str] | None = None) -> Reply:
    if not text or not text.strip():
        return Reply.unknown
    pack = _merge(languages or ["en"])
    raw = text.strip()
    norm = raw.lower().replace("'", "").replace("\u2019", "")
    # Accents do not change meaning here ("sí" = "si").
    norm = norm.translate(str.maketrans("áéíóúü", "aeiouu"))
    clean = re.sub(r"[^\w\s]", " ", norm)
    words = clean.split()
    clean = " ".join(words)
    if not words:
        return Reply.unknown
    word_set = set(words)

    if "?" in raw or "¿" in raw:
        return Reply.unknown
    if any(p in clean for p in pack.uncertainty):
        return Reply.unknown
    if words[0] in pack.question_starters:
        return Reply.unknown
    if any(p in clean for p in pack.negated_cancel) or any(
        p in clean for p in pack.negated_confirm
    ):
        return Reply.unknown

    if any(p in clean for p in pack.cancel_phrases) or clean in pack.cancel_exact:
        return Reply.cancel
    if len(words) <= 4 and word_set & pack.cancel_words:
        return Reply.cancel

    if any(p in clean for p in pack.confirm_phrases) or clean in pack.confirm_exact:
        return Reply.confirm
    if len(words) <= 3 and word_set & pack.affirmations:
        return Reply.confirm
    if len(words) <= 4 and word_set & pack.confirm_words:
        return Reply.confirm
    return Reply.unknown
