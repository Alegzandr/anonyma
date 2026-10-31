"""Outlook .msg writer: the converted message is written as an anonymized .eml."""

from __future__ import annotations

from pathlib import Path

from anonyma.extractors import msg as _msg_extractor  # noqa: F401  (same dependency)
from anonyma.extractors._mime import write_mime
from anonyma.models import Document, Issue, ReplacementPlan, WriteContext, Writer
from anonyma.registry import register_writer


@register_writer
class MsgWriter(Writer):
    name = "msg"
    formats = frozenset({"msg"})

    def output_extension(self, document: Document) -> str:
        return ".eml"

    def output_format(self, document: Document) -> str:
        return "eml"

    def write(
        self,
        document: Document,
        plan: ReplacementPlan,
        destination: Path,
        ctx: WriteContext,
    ) -> list[Issue]:
        converted = Path(document.properties["eml_path"])
        data, issues = write_mime(converted.read_bytes(), document, plan, ctx.child_outputs)
        destination.write_bytes(data)
        return issues
