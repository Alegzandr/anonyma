"""Plain text family: txt, log, md."""

from __future__ import annotations

from pathlib import Path

from anonyma.codec import chunk_text, decode_bytes
from anonyma.models import (
    Document,
    ExtractionContext,
    Extractor,
    Segment,
    SegmentContext,
    SegmentKind,
)
from anonyma.registry import register_extractor


@register_extractor
class TextExtractor(Extractor):
    name = "text"
    extensions = {".txt": "txt", ".log": "log", ".md": "md", ".markdown": "md", ".text": "txt"}

    def extract(self, source: Path, document: Document, ctx: ExtractionContext) -> None:
        text, encoding = decode_bytes(source.read_bytes())
        document.properties["encoding"] = encoding
        for i, (offset, chunk) in enumerate(chunk_text(text)):
            document.segments.append(
                Segment(
                    id=f"c{i}",
                    text=chunk,
                    locator=(offset,),
                    context=SegmentContext(SegmentKind.BODY, label=f"bloc {i + 1}"),
                )
            )
