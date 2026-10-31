"""OpenDocument extractor: odt, ods, odp (direct XML manipulation, see ``_odf``)."""

from __future__ import annotations

from pathlib import Path

from anonyma.extractors import _odf
from anonyma.extractors._empty import empty_file
from anonyma.models import Document, ExtractionContext, Extractor
from anonyma.registry import register_extractor


@register_extractor
class OdfExtractor(Extractor):
    name = "odf"
    extensions = {
        ".odt": "odt",
        ".ott": "odt",
        ".ods": "ods",
        ".ots": "ods",
        ".odp": "odp",
        ".otp": "odp",
    }

    @classmethod
    def sniff(cls, head: bytes) -> str | None:
        return _odf.sniff_odf(head)

    def extract(self, source: Path, document: Document, ctx: ExtractionContext) -> None:
        if empty_file(source, document):
            return
        _odf.extract(source, document, ctx, document.format)
