"""OpenDocument writer: odt, ods, odp."""

from __future__ import annotations

from pathlib import Path

from anonyma.extractors import _odf
from anonyma.models import Document, Issue, ReplacementPlan, WriteContext, Writer
from anonyma.registry import register_writer


@register_writer
class OdfWriter(Writer):
    name = "odf"
    formats = frozenset({"odt", "ods", "odp"})

    def write(
        self,
        document: Document,
        plan: ReplacementPlan,
        destination: Path,
        ctx: WriteContext,
    ) -> list[Issue]:
        return _odf.write(document, plan, destination, ctx, document.format)
