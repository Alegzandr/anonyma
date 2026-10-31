"""CSV / TSV extractor: one segment per non-empty cell.

Cells below a recognised header (``Nom``, ``Téléphone``...) carry its field hint in
their label (:mod:`anonyma.fieldhints`).
"""

from __future__ import annotations

from pathlib import Path

from anonyma.codec import decode_bytes
from anonyma.extractors._tabular import column_letters, sniff_delimiter, tokenize_csv
from anonyma.fieldhints import header_hints, with_column_hint
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
        fields = [f for f in tokenize_csv(text, delimiter) if f.value.strip()]
        rows: dict[int, dict[int, str]] = {}
        for f in fields:
            rows.setdefault(f.row, {})[f.col] = f.value
        header_row = min(rows) if rows else -1
        hints = header_hints(rows)
        for f in fields:
            label = f"{column_letters(f.col)}{f.row + 1}"
            if f.row != header_row:
                label = with_column_hint(label, hints.get(f.col))
            document.segments.append(
                Segment(
                    id=f"r{f.row}c{f.col}",
                    text=f.value,
                    locator=(f.row, f.col),
                    context=SegmentContext(SegmentKind.CELL, label=label),
                )
            )
