"""E-mail messages (RFC 5322 / MIME): headers, bodies, attachments, attached mails."""

from __future__ import annotations

from pathlib import Path

from anonyma.extractors._mime import extract_mime
from anonyma.models import Document, ExtractionContext, Extractor
from anonyma.registry import register_extractor


@register_extractor
class EmlExtractor(Extractor):
    name = "eml"
    extensions = {".eml": "eml"}

    def extract(self, source: Path, document: Document, ctx: ExtractionContext) -> None:
        extract_mime(source.read_bytes(), document, ctx)
