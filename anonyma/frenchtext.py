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


def grouped(count: int) -> str:
    """« 7 217 »: thousands separated by a narrow no-break space."""
    return f"{count:,}".replace(",", NNBSP)


def item_progress(done: int, total: int, step: str) -> str:
    """Progress inside the file being analysed: « fichier en cours : 1 240/7 217
    segments · modèle de langue ». Counts and a detector label only."""
    step = step[:1].lower() + step[1:]
    noun = "segment" if total < 2 else "segments"
    text = f"fichier en cours : {grouped(done)}/{grouped(total)} {noun}"
    return f"{text} · {step}" if step else text


def typo(text: str) -> str:
    """Replace the ordinary spaces French typography requires to be unbreakable (before
    « : ; ? ! » and inside « guillemets »), so Qt and terminals never wrap there."""
    text = _BEFORE_COLON.sub(NBSP + ":", text)
    text = _BEFORE_PUNCT.sub(NNBSP + r"\1", text)
    text = _OPEN_QUOTE.sub("«" + NBSP, text)
    return _CLOSE_QUOTE.sub(NBSP + "»", text)
