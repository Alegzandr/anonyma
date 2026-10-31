"""Fast accent/case-insensitive multi-pattern search (Aho-Corasick).

Used by propagation, business lists and the verifier. Needles and haystacks are
compared after :func:`anonyma.textutils.normalize`; offsets are mapped back to the
original text.

Word boundaries are separator-aware so that names are found inside file names,
paths and URLs: a match may not start or end in the middle of a run of letters
(``dupont`` is not found in ``jdupont``) or of digits, but ``_``, ``-``, ``.``,
``/``, letter/digit transitions and camelCase humps (``JeanDupont``) all count as
boundaries.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field

import ahocorasick

from anonyma.textutils import normalize, normalize_with_map


def boundary_ok(text: str, start: int, end: int) -> bool:
    """True if ``text[start:end]`` is not glued to adjacent letters/digits."""
    if start > 0:
        before, first = text[start - 1], text[start]
        if before.isalpha() and first.isalpha():
            if not (before.islower() and first.isupper()):
                return False
        elif before.isdigit() and first.isdigit():
            return False
    if end < len(text):
        last, after = text[end - 1], text[end]
        if last.isalpha() and after.isalpha():
            if not (last.islower() and after.isupper()):
                return False
        elif last.isdigit() and after.isdigit():
            return False
    return True


#: File extensions after a name in a file-name-like token (``lettre_riviere.docx``).
_EXTENSIONS = frozenset(
    {
        "pdf", "doc", "docx", "odt", "rtf", "txt", "xls", "xlsx", "ods", "csv", "ppt", "pptx",
        "odp", "jpg", "jpeg", "png", "gif", "tif", "tiff", "heic", "eml", "msg", "zip", "html",
        "htm", "xml", "json", "mp3", "mp4", "wav", "mov",
    }
)  # fmt: skip


def in_identifier(text: str, start: int, end: int) -> bool:
    """``text[start:end]`` is a word of a file name or an identifier written in lower
    case (``lettre_resiliation_riviere.docx``, ``dossier-riviere.pdf``): an ordinary
    word there is no prose, a known name must still be found."""
    before = text[start - 1] if start > 0 else ""
    after = text[end] if end < len(text) else ""
    if before == "_" or after == "_":
        return True
    if after == ".":
        tail = text[end + 1 : end + 6]
        ext = "".join(c for c in tail[:5] if c.isalnum()).lower()
        for size in range(len(ext), 1, -1):
            nxt = text[end + 1 + size] if end + 1 + size < len(text) else ""
            if ext[:size] in _EXTENSIONS and not nxt.isalnum():
                return before in ("", "-", "/", "\\") or before.isspace()
    return False


@dataclass(frozen=True, slots=True)
class Match[T]:
    start: int
    end: int
    key: str
    payloads: tuple[T, ...]


@dataclass(slots=True)
class _Entry[T]:
    payloads: list[T] = field(default_factory=list)
    #: Payloads that only match when the occurrence starts with a capital letter.
    capital_only: list[T] = field(default_factory=list)


class MultiMatcher[T]:
    """Collect needles with payloads, then :meth:`find` them in many texts."""

    def __init__(self) -> None:
        self._entries: dict[str, _Entry[T]] = {}
        self._automaton: ahocorasick.Automaton | None = None

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, needle: object) -> bool:
        return isinstance(needle, str) and normalize(needle) in self._entries

    def add(self, needle: str, payload: T, *, require_capital: bool = False) -> None:
        key = normalize(needle)
        if not key:
            return
        entry = self._entries.setdefault(key, _Entry())
        (entry.capital_only if require_capital else entry.payloads).append(payload)
        self._automaton = None

    def _build(self) -> ahocorasick.Automaton | None:
        if self._automaton is None and self._entries:
            automaton = ahocorasick.Automaton()
            for key in self._entries:
                automaton.add_word(key, key)
            automaton.make_automaton()
            self._automaton = automaton
        return self._automaton

    def find(
        self, text: str, *, prepared: tuple[str, list[int]] | None = None
    ) -> Iterator[Match[T]]:
        """Every (possibly overlapping) occurrence, in end order."""
        automaton = self._build()
        if automaton is None or not text:
            return
        norm, index = prepared if prepared is not None else normalize_with_map(text)
        for end_pos, key in automaton.iter(norm):
            begin = end_pos - len(key) + 1
            start = index[begin]
            end = index[end_pos] + 1
            # Extend over combining marks that normalisation removed.
            while end < len(text) and not text[end].isspace() and _is_combining(text[end]):
                end += 1
            if not boundary_ok(text, start, end):
                continue
            entry = self._entries[key]
            payloads = list(entry.payloads)
            if entry.capital_only and (text[start].isupper() or in_identifier(text, start, end)):
                payloads.extend(entry.capital_only)
            if payloads:
                yield Match(start, end, key, tuple(payloads))


def _is_combining(ch: str) -> bool:
    import unicodedata

    return unicodedata.combining(ch) != 0


def leftmost_longest[T](matches: Iterable[Match[T]]) -> list[Match[T]]:
    """Non-overlapping selection preferring earlier, then longer, matches."""
    ordered = sorted(matches, key=lambda m: (m.start, -(m.end - m.start)))
    kept: list[Match[T]] = []
    cursor = -1
    for m in ordered:
        if m.start >= cursor:
            kept.append(m)
            cursor = m.end
    return kept


__all__ = ["Match", "MultiMatcher", "boundary_ok", "in_identifier", "leftmost_longest"]
