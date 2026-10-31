"""PowerPoint (pptx) extractor (direct OOXML manipulation, see ``_ooxml``)."""

from __future__ import annotations

from pathlib import Path

from anonyma.extractors import _ooxml
from anonyma.extractors._empty import empty_file
from anonyma.extractors._ooxml_flavors import PptxFlavor
from anonyma.extractors._zip import is_zip
from anonyma.models import Document, ExtractionContext, Extractor
from anonyma.registry import register_extractor


@register_extractor
class PptxExtractor(Extractor):
    name = "pptx"
    extensions = {
        ".pptx": "pptx",
        ".pptm": "pptx",
        ".potx": "pptx",
        ".potm": "pptx",
        ".ppsx": "pptx",
        ".ppsm": "pptx",
    }

    @classmethod
    def sniff(cls, head: bytes) -> str | None:
        if is_zip(head) and b"[Content_Types].xml" in head and b"ppt/" in head:
            return "pptx"
        return None

    def extract(self, source: Path, document: Document, ctx: ExtractionContext) -> None:
        if empty_file(source, document):
            return
        _ooxml.extract(source, document, ctx, PptxFlavor())
