"""Plain text family writer: txt, log, md."""

from __future__ import annotations

from pathlib import Path

from anonyma.codec import TextEncoding
from anonyma.models import (
    FILENAME_SEGMENT_ID,
    Document,
    Issue,
    ReplacementPlan,
    WriteContext,
    Writer,
)
from anonyma.registry import register_writer


@register_writer
class TextWriter(Writer):
    name = "text"
    formats = frozenset({"txt", "log", "md"})

    def write(
        self,
        document: Document,
        plan: ReplacementPlan,
        destination: Path,
        ctx: WriteContext,
    ) -> list[Issue]:
        encoding: TextEncoding = document.properties.get("encoding", TextEncoding("utf-8"))
        body = sorted(
            (s for s in document.segments if s.id != FILENAME_SEGMENT_ID),
            key=lambda s: int(s.locator[0]),
        )
        text = "".join(plan.apply(segment) for segment in body)
        destination.write_bytes(encoding.encode(text))
        return []
