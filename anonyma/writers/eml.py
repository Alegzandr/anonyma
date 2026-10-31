"""E-mail writer: MIME tree rebuilt with anonymized headers, bodies and children."""

from __future__ import annotations

from pathlib import Path

from anonyma.extractors._mime import write_mime
from anonyma.models import Document, Issue, ReplacementPlan, WriteContext, Writer
from anonyma.registry import register_writer


@register_writer
class EmlWriter(Writer):
    name = "eml"
    formats = frozenset({"eml"})

    def write(
        self,
        document: Document,
        plan: ReplacementPlan,
        destination: Path,
        ctx: WriteContext,
    ) -> list[Issue]:
        data, issues = write_mime(document.source.read_bytes(), document, plan, ctx.child_outputs)
        destination.write_bytes(data)
        return issues
