"""CSV / TSV extractor: one segment per non-empty cell.

Cells below a recognised header (``Nom``, ``Téléphone``...) carry its field hint in
their label (:mod:`anonyma.fieldhints`). A header row made only of structural names
(``activityid``, ``createdon``, ``sender_name``: :mod:`anonyma.structure`) is schema:
its cells are not exposed and never rewritten. A cell holding a file (base64,
``data:`` URI: :mod:`anonyma.extractors._embedded`) has it extracted as a child.
"""

from __future__ import annotations

import bisect
import re
from pathlib import Path

from anonyma.codec import decode_bytes
from anonyma.extractors._embedded import PROPERTY as EMBEDDED
from anonyma.extractors._embedded import (
    EmbeddedRef,
    extract_blobs,
    hint_from,
    masked,
    name_key,
    visible,
)
from anonyma.extractors._tabular import CsvField, column_letters, sniff_delimiter, tokenize_csv
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
from anonyma.structure import PROPERTY as STRUCTURE
from anonyma.structure import is_structural, vocabulary


def delimiter_for(document: Document, text: str) -> str:
    if document.format == "tsv":
        return "\t"
    return sniff_delimiter(text)


def structural_header(rows: dict[int, dict[int, str]]) -> dict[int, str]:
    """Column index -> name of a header row made only of structural names (at least
    two, all distinct), followed by data that is not (a list of identifiers has no
    header); empty otherwise."""
    if not rows:
        return {}
    first = min(rows)
    cells = {c: t.strip() for c, t in rows[first].items() if t.strip()}
    if len(cells) < 2 or len(set(cells.values())) < len(cells):
        return {}
    if not all(is_structural(t) for t in cells.values()):
        return {}
    data = (t for r, row in rows.items() if r != first for t in row.values() if t.strip())
    if not any(not is_structural(t.strip()) for t in data):
        return {}
    return cells


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
        header = structural_header(rows)
        names = {c: name_key(t) for c, t in rows.get(header_row, {}).items()}
        lines = [m.start() for m in re.finditer("\n", text)]
        embedded: dict[str, list[EmbeddedRef]] = {}
        for f in fields:
            if header and f.row == header_row:
                continue  # structural header: schema, never exposed nor rewritten
            label = f"{column_letters(f.col)}{f.row + 1}"
            value = f.value
            if f.row != header_row:
                label = with_column_hint(label, hints.get(f.col))
                shown = self._embedded(f, f.value, rows, names, document, ctx, embedded)
                if shown is None:
                    continue
                value = shown
            column = header.get(f.col)
            where = f"ligne {f.row + 1} du tableau, colonne {column_letters(f.col)}"
            if column:
                where += f" « {column} »"
            document.segments.append(
                Segment(
                    id=f"r{f.row}c{f.col}",
                    text=value,
                    locator=(f.row, f.col),
                    context=SegmentContext(
                        SegmentKind.CELL,
                        label=label,
                        where=where,
                        line=bisect.bisect_left(lines, f.start) + 1,
                    ),
                )
            )
        document.properties[EMBEDDED] = embedded
        document.properties[STRUCTURE] = vocabulary(header.values())

    @staticmethod
    def _embedded(
        f: CsvField,
        value: str,
        rows: dict[int, dict[int, str]],
        names: dict[int, str],
        document: Document,
        ctx: ExtractionContext,
        embedded: dict[str, list[EmbeddedRef]],
    ) -> str | None:
        """The cell text to expose (files replaced by marks), None when nothing is
        left of it."""
        siblings = {names[c]: t for c, t in rows[f.row].items() if c in names and c != f.col}
        refs = extract_blobs(value, hint_from(names.get(f.col, ""), siblings), document, ctx)
        if not refs:
            return value
        embedded[f"r{f.row}c{f.col}"] = refs
        value = masked(value, refs)
        return value if visible(value) else None
