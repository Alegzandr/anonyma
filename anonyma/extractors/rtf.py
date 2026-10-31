"""RTF extractor (custom tokenizer, see ``_rtf``)."""

from __future__ import annotations

from pathlib import Path

from anonyma.extractors._media import is_placeholder
from anonyma.extractors._rtf import parse_rtf
from anonyma.models import (
    Document,
    ExtractionContext,
    Extractor,
    Segment,
    SegmentContext,
)
from anonyma.registry import register_extractor


@register_extractor
class RtfExtractor(Extractor):
    name = "rtf"
    extensions = {".rtf": "rtf"}

    @classmethod
    def sniff(cls, head: bytes) -> str | None:
        return "rtf" if head.lstrip(b"\xef\xbb\xbf \r\n\t").startswith(b"{\\rtf") else None

    def extract(self, source: Path, document: Document, ctx: ExtractionContext) -> None:
        raw = source.read_bytes()
        parsed = parse_rtf(raw)
        counters: dict[str, int] = {}
        for i, unit in enumerate(parsed.units):
            counters[unit.label] = counters.get(unit.label, 0) + 1
            document.segments.append(
                Segment(
                    id=f"u{i}",
                    text=unit.text,
                    locator=(i,),
                    context=SegmentContext(
                        unit.kind, label=f"{unit.label}, bloc {counters[unit.label]}"
                    ),
                )
            )
        pictures: dict[str, int] = {}
        for index, picture in enumerate(parsed.pictures):
            data = picture.data()
            if is_placeholder(data):
                continue
            ctx.budget.consume(len(data), depth=ctx.depth + 1)
            child = ctx.extract_child(data, f"image{index + 1}{picture.ext}", document)
            pictures[child.id] = index
        document.properties["rtf_pictures"] = pictures
