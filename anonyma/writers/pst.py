"""PST writer: a zip archive of the anonymized eml messages (folder tree kept)."""

from __future__ import annotations

from pathlib import Path

from anonyma.extractors import pst as _pst_extractor  # noqa: F401  (pypff required)
from anonyma.models import Document, Issue, ReplacementPlan, WriteContext, Writer
from anonyma.registry import register_writer
from anonyma.writers.zip import write_zip


@register_writer
class PstWriter(Writer):
    name = "pst"
    formats = frozenset({"pst"})

    def output_extension(self, document: Document) -> str:
        return ".zip"

    def output_format(self, document: Document) -> str:
        return "zip"

    def write(
        self,
        document: Document,
        plan: ReplacementPlan,
        destination: Path,
        ctx: WriteContext,
    ) -> list[Issue]:
        entries: list[tuple[str, Path]] = []
        for child in document.children:
            output = ctx.child_outputs.get(child.id)
            if output is not None and output.path is not None:
                entries.append((output.name, output.path))
        write_zip(entries, [], destination)
        return []
