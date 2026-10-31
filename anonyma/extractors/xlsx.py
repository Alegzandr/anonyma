"""Excel (xlsx) extractor (direct OOXML manipulation, see ``_ooxml``)."""

from __future__ import annotations

from pathlib import Path

from anonyma.extractors import _ooxml
from anonyma.extractors._ooxml_flavors import XlsxFlavor
from anonyma.extractors._zip import is_zip
from anonyma.models import Document, ExtractionContext, Extractor
from anonyma.registry import register_extractor


@register_extractor
class XlsxExtractor(Extractor):
    name = "xlsx"
    extensions = {".xlsx": "xlsx", ".xlsm": "xlsx", ".xltx": "xlsx", ".xltm": "xlsx"}

    @classmethod
    def sniff(cls, head: bytes) -> str | None:
        if is_zip(head) and b"[Content_Types].xml" in head and b"xl/" in head:
            return "xlsx"
        return None

    def extract(self, source: Path, document: Document, ctx: ExtractionContext) -> None:
        _ooxml.extract(source, document, ctx, XlsxFlavor())
