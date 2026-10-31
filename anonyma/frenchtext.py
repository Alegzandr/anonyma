"""French typography helpers for user-facing text (GUI, CLI, reports)."""

from __future__ import annotations

import re

NBSP = " "  # before « : » and inside « guillemets »
NNBSP = " "  # narrow no-break space, before « ; ? ! »

_BEFORE_COLON = re.compile(r" :(?=\s|$)")
_BEFORE_PUNCT = re.compile(r" ([;?!])")
_OPEN_QUOTE = re.compile(r"« ")
_CLOSE_QUOTE = re.compile(r" »")


def plural(count: int, singular: str, plural_form: str | None = None) -> str:
    """``count`` followed by the agreed noun: ``plural(1, "fichier")`` → « 1 fichier »,
    ``plural(3, "fichier")`` → « 3 fichiers ». French uses the singular for 0 and 1."""
    word = singular if abs(count) < 2 else (plural_form or singular + "s")
    return f"{count}{NBSP}{word}"


def typo(text: str) -> str:
    """Replace the ordinary spaces French typography requires to be unbreakable (before
    « : ; ? ! » and inside « guillemets »), so Qt and terminals never wrap there."""
    text = _BEFORE_COLON.sub(NBSP + ":", text)
    text = _BEFORE_PUNCT.sub(NNBSP + r"\1", text)
    text = _OPEN_QUOTE.sub("«" + NBSP, text)
    return _CLOSE_QUOTE.sub(NBSP + "»", text)
