"""Excel (xlsx) writer: replacements re-injected run by run, metadata purged."""

from __future__ import annotations

from pathlib import Path

from anonyma.extractors import _ooxml
from anonyma.extractors._ooxml_flavors import XlsxFlavor
from anonyma.models import Document, Issue, ReplacementPlan, WriteContext, Writer
from anonyma.registry import register_writer


@register_writer
class XlsxWriter(Writer):
    name = "xlsx"
    formats = frozenset({"xlsx"})

    def write(
        self,
        document: Document,
        plan: ReplacementPlan,
        destination: Path,
        ctx: WriteContext,
    ) -> list[Issue]:
        return _ooxml.write(document, plan, destination, ctx, XlsxFlavor())
