"""PST writer: a folder tree of anonymized eml messages (mailbox folders kept).

The output is a directory named after the anonymized stem of the .pst file; the
pipeline re-reads it for verification and packs it into a zip when the .pst was
itself embedded in a container.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from anonyma.extractors import pst as _pst_extractor  # noqa: F401  (pypff required)
from anonyma.extractors._archive import UniqueNames
from anonyma.models import FOLDER_FORMAT, Document, Issue, ReplacementPlan, WriteContext, Writer
from anonyma.registry import register_writer
from anonyma.textutils import safe_filename


@register_writer
class PstWriter(Writer):
    name = "pst"
    formats = frozenset({"pst"})
    directory_output = True

    def output_extension(self, document: Document) -> str:
        return ""

    def output_format(self, document: Document) -> str:
        return FOLDER_FORMAT

    def write(
        self,
        document: Document,
        plan: ReplacementPlan,
        destination: Path,
        ctx: WriteContext,
    ) -> list[Issue]:
        destination.mkdir(parents=True)
        unique = UniqueNames()
        for child in document.children:
            output = ctx.child_outputs.get(child.id)
            if output is None or output.path is None:
                continue  # omitted: reported by the pipeline
            parts = [
                safe_filename(p) for p in output.name.replace("\\", "/").split("/") if p
            ] or ["message.eml"]
            relative = unique("/".join(parts))
            target = destination.joinpath(*relative.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(output.path, target)
        return []
