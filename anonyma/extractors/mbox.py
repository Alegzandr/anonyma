"""Unix mailboxes (mbox): every message is an eml child; ``From `` lines are segments."""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

from anonyma.extractors._mime import detect_newline
from anonyma.models import (
    Document,
    ExtractionContext,
    Extractor,
    Segment,
    SegmentContext,
    SegmentKind,
)
from anonyma.registry import register_extractor

_LINE = re.compile(rb"[^\n]*\n|[^\n]+")


def split_mbox(data: bytes) -> list[tuple[bytes | None, bytes]]:
    """Split into ``(separator line without "From " and newline, message bytes)``.

    A separator is a line starting with ``From `` at the start of the file or after
    a blank line. Content before the first separator is a message without one.
    """
    return list(iter_mbox(data))


def iter_mbox(data: bytes) -> Iterator[tuple[bytes | None, bytes]]:
    """:func:`split_mbox`, one message at a time, each sliced from ``data`` once (a
    2 GB box no longer holds every line and every message in memory together)."""
    separator: bytes | None = None
    body_start = 0  # offset of the current message's first line
    last_start = -1  # offset of its last line so far, -1 when it has none
    last_blank = False
    has_text = False  # a non-blank line in the current message
    previous_blank = True
    started = False

    def body(end: int) -> bytes:
        # The single blank line separating two messages is not part of either.
        return data[body_start : last_start if last_blank else end]

    for match in _LINE.finditer(data):
        line_start = match.start()
        if previous_blank and data.startswith(b"From ", line_start):
            if started or has_text:
                yield separator, body(line_start)
            separator = match.group()[5:].rstrip(b"\r\n")
            body_start, last_start, last_blank, has_text = match.end(), -1, False, False
            started = True
            previous_blank = False
            continue
        blank = not data[line_start : match.end()].strip()
        last_start, last_blank = line_start, blank
        has_text = has_text or not blank
        previous_blank = blank
    if started or has_text:
        yield separator, body(len(data))


@register_extractor
class MboxExtractor(Extractor):
    name = "mbox"
    extensions = {".mbox": "mbox", ".mbx": "mbox"}

    def extract(self, source: Path, document: Document, ctx: ExtractionContext) -> None:
        data = source.read_bytes()
        document.properties["newline"] = detect_newline(data)
        children: list[tuple[str | None, str]] = []  # (separator segment id, child id)
        for i, (separator, message) in enumerate(iter_mbox(data)):
            ctx.check_cancelled()
            segment_id: str | None = None
            if separator is not None:
                segment_id = f"sep{i}"
                document.segments.append(
                    Segment(
                        id=segment_id,
                        text=separator.decode("utf-8", "replace"),
                        locator=("separator", i),
                        context=SegmentContext(SegmentKind.HEADER, label="From_"),
                    )
                )
            ctx.budget.consume(len(message), depth=ctx.depth + 1)
            child = ctx.extract_child(message, f"message-{i + 1:04d}.eml", document)
            children.append((segment_id, child.id))
        document.properties["messages"] = children
