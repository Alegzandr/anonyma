"""Text normalisation and accent/case-insensitive search with offset mapping."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterator

_SPACES = re.compile(r"\s+")


def strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def normalize(text: str) -> str:
    """Canonical comparison key: no accents, casefolded, single spaces, trimmed."""
    return _SPACES.sub(" ", strip_accents(text).casefold()).strip()


def normalize_with_map(text: str) -> tuple[str, list[int]]:
    """Normalise ``text`` char by char and map each output char to its source index.

    Whitespace runs collapse to one space. Unlike :func:`normalize`, leading and
    trailing whitespace is kept so that offsets stay meaningful.
    """
    out: list[str] = []
    index: list[int] = []
    previous_space = False
    for i, ch in enumerate(text):
        if ch.isspace():
            if previous_space:
                continue
            out.append(" ")
            index.append(i)
            previous_space = True
            continue
        previous_space = False
        for n in strip_accents(ch).casefold():
            out.append(n)
            index.append(i)
    return "".join(out), index


def _is_word_char(ch: str) -> bool:
    return ch.isalnum() or ch == "_"


def find_all(haystack: str, needle: str, *, whole_word: bool = True) -> Iterator[tuple[int, int]]:
    """Yield ``(start, end)`` offsets in ``haystack`` of accent/case-insensitive
    occurrences of ``needle``."""
    target = normalize(needle)
    if not target:
        return
    norm, index = normalize_with_map(haystack)
    pos = norm.find(target)
    while pos != -1:
        start = index[pos]
        end = index[pos + len(target) - 1] + 1
        ok = True
        if whole_word:
            before = haystack[start - 1] if start > 0 else ""
            after = haystack[end] if end < len(haystack) else ""
            if (before and _is_word_char(before) and _is_word_char(haystack[start])) or (
                after and _is_word_char(after) and _is_word_char(haystack[end - 1])
            ):
                ok = False
        if ok:
            yield start, end
        pos = norm.find(target, pos + 1)


def safe_filename(name: str) -> str:
    """Make a single path component safe on Windows, macOS and Linux."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    reserved = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
    if cleaned.split(".")[0].upper() in reserved:
        cleaned = f"_{cleaned}"
    return cleaned or "_"
