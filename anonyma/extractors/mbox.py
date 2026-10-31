"""Unix mailboxes (mbox): every message is an eml child; ``From `` lines are segments."""

from __future__ import annotations

import re
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
    messages: list[tuple[bytes | None, bytes]] = []
    separator: bytes | None = None
    body: list[bytes] = []
    previous_blank = True
    started = False
    for line in _LINE.findall(data):
        if line.startswith(b"From ") and previous_blank:
            if started or any(b.strip() for b in body):
                messages.append((separator, _strip_trailing_blank(body)))
            separator = line[5:].rstrip(b"\r\n")
            body = []
            started = True
            previous_blank = False
            continue
        body.append(line)
        previous_blank = not line.strip()
    if started or any(b.strip() for b in body):
        messages.append((separator, _strip_trailing_blank(body)))
    return messages


def _strip_trailing_blank(lines: list[bytes]) -> bytes:
    """Drop the single blank line that separates two messages."""
    if lines and not lines[-1].strip():
        lines = lines[:-1]
    return b"".join(lines)


@register_extractor
class MboxExtractor(Extractor):
    name = "mbox"
    extensions = {".mbox": "mbox", ".mbx": "mbox"}

    def extract(self, source: Path, document: Document, ctx: ExtractionContext) -> None:
        data = source.read_bytes()
        document.properties["newline"] = detect_newline(data)
        children: list[tuple[str | None, str]] = []  # (separator segment id, child id)
        for i, (separator, message) in enumerate(split_mbox(data)):
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
