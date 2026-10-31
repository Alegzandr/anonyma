"""Legacy Office formats: doc, xls, ppt (see ``_legacy``)."""

from __future__ import annotations

from pathlib import Path

from anonyma import libreoffice
from anonyma.extractors import _ooxml
from anonyma.extractors._empty import empty_file
from anonyma.extractors._legacy import (
    FLAVORS,
    LIBREOFFICE_REQUIRED,
    MODE_LIBREOFFICE,
    MODE_XLRD,
    TO_MODERN,
    ole_metadata,
    open_xls,
    xls_items,
)
from anonyma.models import (
    Document,
    ExtractionContext,
    Extractor,
    Segment,
    SegmentContext,
    SegmentKind,
    Severity,
)
from anonyma.registry import register_extractor

ITEM_KINDS = {
    "sheet": SegmentKind.ATTRIBUTE,
    "cell": SegmentKind.CELL,
    "number": SegmentKind.CELL,
    "note": SegmentKind.COMMENT,
    "author": SegmentKind.METADATA,
    "link": SegmentKind.ATTRIBUTE,
    "name": SegmentKind.HIDDEN,
}


@register_extractor
class LegacyOfficeExtractor(Extractor):
    name = "legacy_office"
    extensions = {
        ".doc": "doc",
        ".dot": "doc",
        ".xls": "xls",
        ".xlt": "xls",
        ".ppt": "ppt",
        ".pps": "ppt",
        ".pot": "ppt",
    }

    def extract(self, source: Path, document: Document, ctx: ExtractionContext) -> None:
        if empty_file(source, document):
            return
        data = source.read_bytes()
        for field, value in ole_metadata(data):
            document.segments.append(
                Segment(
                    id=f"ole_{field}",
                    text=value,
                    locator=("ole", field),
                    context=SegmentContext(SegmentKind.METADATA, label=field),
                )
            )
        soffice = libreoffice.find_soffice(ctx.config)
        if soffice is not None:
            _modern, target = TO_MODERN[document.format]
            work = ctx.temp_path("-lo")
            try:
                converted = libreoffice.convert(
                    source, target, work, soffice, timeout=ctx.config.libreoffice_timeout
                )
            except libreoffice.ConversionError:
                document.add_issue(
                    Severity.ERROR,
                    "libreoffice.conversion",
                    "La conversion par LibreOffice a échoué : fichier non traité.",
                )
                return
            document.properties["legacy_mode"] = MODE_LIBREOFFICE
            document.properties["converted_path"] = str(converted)
            _ooxml.extract(converted, document, ctx, FLAVORS[document.format]())
            return
        if document.format == "xls":
            document.properties["legacy_mode"] = MODE_XLRD
            book = open_xls(data)
            for item in xls_items(book):
                document.segments.append(
                    Segment(
                        id="x_" + "_".join(map(str, item.key)),
                        text=item.text,
                        locator=item.key,
                        context=SegmentContext(ITEM_KINDS[item.kind], label=item.label),
                    )
                )
            document.add_issue(
                Severity.INFO,
                "legacy.xlrd",
                "LibreOffice absent : le classeur .xls sera produit au format .xlsx "
                "(valeurs uniquement, sans formules ni mise en forme).",
            )
            return
        document.add_issue(Severity.ERROR, "libreoffice.required", LIBREOFFICE_REQUIRED)
