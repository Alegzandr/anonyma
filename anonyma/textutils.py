"""Text normalisation and accent/case-insensitive search with offset mapping."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterator
from pathlib import PurePosixPath

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


#: A masked value in a file or folder name (masquage mode): fixed length, so that
#: the name does not reveal the length of what it hides.
NAME_MASK = "█████"
_MASK_RUN = re.compile("█+")


def safe_filename(name: str) -> str:
    """Make a single path component safe on Windows, macOS and Linux: no reserved
    character or device name, at most :data:`MAX_NAME_BYTES` (extension kept). Runs
    of block characters (masked values) become :data:`NAME_MASK`."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    cleaned = _MASK_RUN.sub(NAME_MASK, cleaned)
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
