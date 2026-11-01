"""Outlook thread indexes: the ``Thread-Index`` mail header, the msg property
``PR_CONVERSATION_INDEX`` and the Dataverse ``conversationindex`` column.

An index is a 22-byte header (a reserved byte ``0x01``, the high 40 bits of the
FILETIME at which the thread started, a 16-byte GUID) followed by one 5-byte block
per reply (time delta since the header, random bits, a sequence count). A reply's
index is its parent's followed by one more block: that is how clients rebuild
threads. Base64 in mail headers, hexadecimal in CRM exports.

Left as is, an index gives away the real start date of its thread (and with it the
secret date offset of a realistic batch, so every real date of the batch). Every
value that decodes as an index is therefore detected wherever it appears
(:func:`find`, entity type ``INDEX_CONVERSATION``) and rebuilt by the replacer
(:func:`rebuild`), in every mode:

* the start time is shifted by the batch date offset (realistic mode), or becomes the
  neutral date of 1 March 1970 (tags, blackout), whose reply deltas are zeroed;
* the GUID is a keyed hash of the original one (batch key, kept in the mapping
  table in pseudonymization);
* the header depends only on the original header, so every message of a thread gets
  the same new header and a reply still extends its parent's index, in every file
  and format of the batch (eml header, JSON, CSV...).

The rebuilt value keeps the original's encoding (base64, hexadecimal and its case).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
from datetime import UTC, datetime, timedelta

HEADER = 22
BLOCK = 5
#: The start time keeps the high 40 bits of a FILETIME (units of 2**24 ticks).
UNIT = 2**24
_TICKS_PER_DAY = 864_000_000_000
_EPOCH = datetime(1601, 1, 1, tzinfo=UTC)
NEUTRAL = datetime(1970, 3, 1, tzinfo=UTC)
#: Start times outside these years are not read as an index (random hexadecimal).
MIN_YEAR, MAX_YEAR = 1995, 2100
#: Candidates: hexadecimal (``01`` + 21 bytes + 5-byte blocks) and base64 (``AQ``...).
_HEX = re.compile(r"(?<![0-9A-Za-z])01[0-9A-Fa-f]{42}(?:[0-9A-Fa-f]{10}){0,40}(?![0-9A-Za-z])")
_B64 = re.compile(r"(?<![A-Za-z0-9+/=])AQ[A-Za-z0-9+/]{26,300}={0,2}(?![A-Za-z0-9+/=])")


def _valid(raw: bytes) -> bool:
    if len(raw) < HEADER or (len(raw) - HEADER) % BLOCK or raw[0] != 1:
        return False
    start = _moment(int.from_bytes(raw[1:6], "big"))
    return start is not None and (start.year == NEUTRAL.year or MIN_YEAR <= start.year <= MAX_YEAR)


def _moment(units: int) -> datetime | None:
    try:
        return _EPOCH + timedelta(microseconds=units * UNIT // 10)
    except OverflowError:
        return None


def decode(text: str) -> tuple[bytes, str] | None:
    """``(index bytes, encoding)`` of ``text`` (``hex``, ``HEX`` or ``base64``), or
    None when it is not a thread index."""
    value = "".join(text.split())
    raw: bytes
    if value and len(value) % 2 == 0 and all(c in "0123456789abcdefABCDEF" for c in value):
        raw = bytes.fromhex(value)
        encoding = "hex" if any(c in "abcdef" for c in value) else "HEX"
    else:
        try:
            raw = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError):
            return None
        encoding = "base64"
    return (raw, encoding) if _valid(raw) else None


def encode(raw: bytes, encoding: str) -> str:
    if encoding == "base64":
        return base64.b64encode(raw).decode("ascii")
    return raw.hex() if encoding == "hex" else raw.hex().upper()


def start_time(text: str) -> datetime | None:
    """Start time of the thread an index encodes (to 1.7 s), or None."""
    decoded = decode(text)
    return _moment(int.from_bytes(decoded[0][1:6], "big")) if decoded else None


def find(text: str) -> list[tuple[int, int]]:
    """Spans of the thread indexes written in ``text``."""
    spans = [(m.start(), m.end()) for m in _HEX.finditer(text)]
    spans += [(m.start(), m.end()) for m in _B64.finditer(text)]
    return sorted(s for s in spans if decode(text[s[0] : s[1]]) is not None)


def rebuild(raw: bytes, key: bytes, shift_days: int | None) -> bytes:
    """New index for ``raw``: start time shifted by ``shift_days`` (None: the neutral
    date, reply deltas zeroed), GUID hashed with ``key``, reply blocks kept."""
    units = int.from_bytes(raw[1:6], "big")
    if shift_days is None:
        ticks = (NEUTRAL - _EPOCH) // timedelta(microseconds=1) * 10
        units = -(-ticks // UNIT)  # rounded up: read back, the neutral date itself
    else:
        units = max(0, (units * UNIT + shift_days * _TICKS_PER_DAY) // UNIT)
    guid = hmac.new(key, b"thread-index\x1f" + raw[6:HEADER], hashlib.sha256).digest()[:16]
    blocks = bytearray(raw[HEADER:])
    if shift_days is None:  # no interval survives a neutral date
        for start in range(0, len(blocks), BLOCK):
            blocks[start : start + 4] = bytes(4)
    return b"\x01" + (units % 2**40).to_bytes(5, "big") + guid + bytes(blocks)


def rebuild_text(text: str, key: bytes, shift_days: int | None) -> str | None:
    """:func:`rebuild` on an index written as text, in the same encoding."""
    decoded = decode(text)
    if decoded is None:
        return None
    raw, encoding = decoded
    lead = text[: len(text) - len(text.lstrip())]
    trail = text[len(text.rstrip()) :]
    return lead + encode(rebuild(raw, key, shift_days), encoding) + trail


__all__ = ["decode", "encode", "find", "rebuild", "rebuild_text", "start_time"]
