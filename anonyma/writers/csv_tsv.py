"""CSV / TSV writer: cells edited in place, dialect and encoding preserved."""

from __future__ import annotations

from pathlib import Path

from anonyma.codec import TextEncoding, decode_bytes
from anonyma.extractors._embedded import PROPERTY as EMBEDDED
from anonyma.extractors._embedded import EmbeddedRef, masked, rebuild
from anonyma.extractors._tabular import quote_csv, tokenize_csv
from anonyma.models import (
    Document,
    Issue,
    ReplacementPlan,
    WriteContext,
    Writer,
    apply_replacements,
)
from anonyma.registry import register_writer


@register_writer
class CsvWriter(Writer):
    name = "csv"
    formats = frozenset({"csv", "tsv"})

    def write(
        self,
        document: Document,
        plan: ReplacementPlan,
        destination: Path,
        ctx: WriteContext,
    ) -> list[Issue]:
        text, detected = decode_bytes(document.source.read_bytes())
        encoding: TextEncoding = document.properties.get("encoding", detected)
        delimiter: str = document.properties.get("delimiter", ",")
        embedded: dict[str, list[EmbeddedRef]] = document.properties.get(EMBEDDED, {})
        issues: list[Issue] = []
        out: list[str] = []
        cursor = 0
        for f in tokenize_csv(text, delimiter):
            segment_id = f"r{f.row}c{f.col}"
            reps = plan.for_segment(segment_id)
            refs = embedded.get(segment_id)
            if not reps and not refs:
                continue
            if refs:
                value, lost = rebuild(
                    apply_replacements(masked(f.value, refs), reps), refs, ctx.child_outputs
                )
                issues.extend(lost)
            else:
                value = apply_replacements(f.value, reps)
            out.append(text[cursor : f.start])
            out.append(quote_csv(value, delimiter, f.quoted))
            cursor = f.end
        out.append(text[cursor:])
        destination.write_bytes(encoding.encode("".join(out)))
        return issues
