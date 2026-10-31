"""Text normalisation and accent/case-insensitive search with offset mapping."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterator
from functools import lru_cache
from pathlib import PurePosixPath

_SPACES = re.compile(r"\s+")


def strip_accents(text: str) -> str:
    if text.isascii():
        return text
    decomposed = unicodedata.normalize("NFD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


#: Strings up to this length go through the :func:`normalize` cache (words, values).
_CACHED_LENGTH = 64


def normalize(text: str) -> str:
    """Canonical comparison key: no accents, casefolded, single spaces, trimmed."""
    if len(text) <= _CACHED_LENGTH:
        return _normalize_short(text)
    return _normalize(text)


def _normalize(text: str) -> str:
    return _SPACES.sub(" ", strip_accents(text).casefold()).strip()


@lru_cache(maxsize=131072)
def _normalize_short(text: str) -> str:
    return _normalize(text)


@lru_cache(maxsize=8192)
def _normalize_char(ch: str) -> str:
    return strip_accents(ch).casefold()


#: An ASCII run, or one other character (normalized on its own).
_RUNS = re.compile(r"[\x00-\x7f]+|[^\x00-\x7f]")


def normalize_with_map(text: str) -> tuple[str, list[int]]:
    """Normalise ``text`` char by char and map each output char to its source index.

    Whitespace runs collapse to one space. Unlike :func:`normalize`, leading and
    trailing whitespace is kept so that offsets stay meaningful.
    """
    out: list[str] = []
    index: list[int] = []
    previous_space = False
    for run in _RUNS.finditer(text):
        start = run.start()
        chunk = run.group()
        if len(chunk) == 1 and not chunk.isascii():
            if chunk.isspace():
                if not previous_space:
                    out.append(" ")
                    index.append(start)
                    previous_space = True
                continue
            previous_space = False
            normalized = _normalize_char(chunk)
            out.append(normalized)
            index.extend([start] * len(normalized))
            continue
        # ASCII: lower() is the case folding, one character for one.
        lowered = chunk.lower()
        pos = 0
        for space in _SPACES.finditer(lowered):
            if space.start() > pos:
                out.append(lowered[pos : space.start()])
                index.extend(range(start + pos, start + space.start()))
                previous_space = False
            if not previous_space:
                out.append(" ")
                index.append(start + space.start())
                previous_space = True
            pos = space.end()
        if pos < len(lowered):
            out.append(lowered[pos:])
            index.extend(range(start + pos, start + len(lowered)))
            previous_space = False
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


#: Device names Windows reserves whatever the extension (``NUL.txt``, ``COM¹.pdf``),
#: trailing spaces of the base ignored.
_RESERVED_NAMES = frozenset(
    {
        "CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
        *(f"{device}{n}" for device in ("COM", "LPT") for n in [*"123456789", "¹", "²", "³"]),
    }
)  # fmt: skip
#: Longest name produced, in UTF-8 bytes (Linux limits names to 255 bytes, Windows
#: paths to 260 characters).
MAX_NAME_BYTES = 200
_SUFFIX = re.compile(r"\.[A-Za-z0-9]{1,10}")


def safe_filename(name: str) -> str:
    """Make a single path component safe on Windows, macOS and Linux: no reserved
    character or device name, at most :data:`MAX_NAME_BYTES` (extension kept)."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    if cleaned.split(".")[0].rstrip(" ").upper() in _RESERVED_NAMES:
        cleaned = f"_{cleaned}"
    if len(cleaned.encode("utf-8")) > MAX_NAME_BYTES:
        stem, dot, ext = cleaned.rpartition(".")
        suffix = f".{ext}" if dot and stem and _SUFFIX.fullmatch(f".{ext}") else ""
        base = cleaned[: len(cleaned) - len(suffix)] if suffix else cleaned
        room = MAX_NAME_BYTES - len(suffix.encode("utf-8"))
        base = base.encode("utf-8")[:room].decode("utf-8", "ignore").rstrip(" .")
        cleaned = base + suffix
    return cleaned or "_"


def safe_suffix(name: str) -> str:
    """Extension of ``name`` usable for a temporary file (``.pdf``), lower case, or
    ``""``: never a separator, stream (``:``) or device trick from an archive entry."""
    suffix = PurePosixPath(name.replace("\\", "/")).suffix.lower()
    return suffix if _SUFFIX.fullmatch(suffix) else ""
