"""CSV / TSV extractor: one segment per non-empty cell."""

from __future__ import annotations

from pathlib import Path

from anonyma.codec import decode_bytes
from anonyma.extractors._tabular import column_letters, sniff_delimiter, tokenize_csv
from anonyma.models import (
    Document,
    ExtractionContext,
    Extractor,
    Segment,
    SegmentContext,
    SegmentKind,
)
from anonyma.registry import register_extractor


def delimiter_for(document: Document, text: str) -> str:
    if document.format == "tsv":
        return "\t"
    return sniff_delimiter(text)


@register_extractor
class CsvExtractor(Extractor):
    name = "csv"
    extensions = {".csv": "csv", ".tsv": "tsv", ".tab": "tsv"}

    def extract(self, source: Path, document: Document, ctx: ExtractionContext) -> None:
        text, encoding = decode_bytes(source.read_bytes())
        delimiter = delimiter_for(document, text)
        document.properties["encoding"] = encoding
        document.properties["delimiter"] = delimiter
        for f in tokenize_csv(text, delimiter):
            if not f.value.strip():
                continue
            document.segments.append(
                Segment(
                    id=f"r{f.row}c{f.col}",
                    text=f.value,
                    locator=(f.row, f.col),
                    context=SegmentContext(
                        SegmentKind.CELL, label=f"{column_letters(f.col)}{f.row + 1}"
                    ),
                )
            )
