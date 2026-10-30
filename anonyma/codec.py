"""Byte <-> text decoding that round-trips: encoding, BOM and newlines preserved."""

from __future__ import annotations

import codecs
from dataclasses import dataclass

_BOMS = (
    (codecs.BOM_UTF32_LE, "utf-32-le"),
    (codecs.BOM_UTF32_BE, "utf-32-be"),
    (codecs.BOM_UTF8, "utf-8"),
    (codecs.BOM_UTF16_LE, "utf-16-le"),
    (codecs.BOM_UTF16_BE, "utf-16-be"),
)


@dataclass(frozen=True, slots=True)
class TextEncoding:
    encoding: str
    bom: bytes = b""

    def encode(self, text: str) -> bytes:
        # Substitutes may contain characters the legacy encoding lacks.
        return self.bom + text.encode(self.encoding, errors="replace")


def decode_bytes(data: bytes) -> tuple[str, TextEncoding]:
    """Decode with BOM detection, then UTF-8, then cp1252, then latin-1 (never fails)."""
    for bom, encoding in _BOMS:
        if data.startswith(bom):
            return data[len(bom) :].decode(encoding, errors="replace"), TextEncoding(encoding, bom)
    for encoding in ("utf-8", "cp1252"):
        try:
            return data.decode(encoding), TextEncoding(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1"), TextEncoding("latin-1")


def chunk_text(text: str, soft_limit: int = 1500, hard_limit: int = 4000) -> list[tuple[int, str]]:
    """Split ``text`` into contiguous ``(offset, chunk)`` pieces covering it entirely.

    Cuts happen at line boundaries, preferably on blank lines, so that entities
    rarely straddle two chunks.
    """
    chunks: list[tuple[int, str]] = []
    start = 0
    size = 0
    lines = text.splitlines(keepends=True)
    offset = 0
    for line in lines:
        offset += len(line)
        size += len(line)
        blank = not line.strip()
        if (size >= soft_limit and blank) or size >= hard_limit:
            chunks.append((start, text[start:offset]))
            start = offset
            size = 0
    if start < len(text) or not chunks:
        chunks.append((start, text[start:]))
    return chunks
