"""Legacy Office writer: doc, xls, ppt.

LibreOffice mode: the OOXML conversion made at extraction time is anonymized by
the OOXML engine (metadata purged, revisions accepted...), then converted back
to the legacy format with a fresh LibreOffice profile.

xlrd mode (xls without LibreOffice): a new xlsx is generated from the values
(text, numbers, dates, booleans), sheet names included; formulas, formatting,
cell notes and hyperlinks are not carried over (they are never copied, so they
cannot leak).
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from anonyma import libreoffice
from anonyma.extractors import _ooxml
from anonyma.extractors._legacy import (
    FLAVORS,
    MODE_LIBREOFFICE,
    MODE_XLRD,
    TO_LEGACY,
    TO_MODERN,
    number_text,
    open_xls,
)
from anonyma.extractors._ooxml_flavors import sanitize_sheet_name
from anonyma.extractors._xlsxgen import Workbook
from anonyma.models import (
    Document,
    Issue,
    ReplacementPlan,
    Severity,
    WriteContext,
    Writer,
    apply_replacements,
)
from anonyma.registry import register_writer


@register_writer
class LegacyOfficeWriter(Writer):
    name = "legacy_office"
    formats = frozenset({"doc", "xls", "ppt"})

    def output_extension(self, document: Document) -> str:
        if document.properties.get("legacy_mode") == MODE_XLRD:
            return ".xlsx"
        return f".{document.format}"

    def output_format(self, document: Document) -> str:
        if document.properties.get("legacy_mode") == MODE_XLRD:
            return "xlsx"
        return document.format

    def write(
        self,
        document: Document,
        plan: ReplacementPlan,
        destination: Path,
        ctx: WriteContext,
    ) -> list[Issue]:
        mode = document.properties.get("legacy_mode")
        if mode == MODE_LIBREOFFICE:
            return self._write_libreoffice(document, plan, destination, ctx)
        if mode == MODE_XLRD:
            return self._write_xlrd(document, plan, destination)
        raise RuntimeError("legacy document was not extracted")

    @staticmethod
    def _write_libreoffice(
        document: Document, plan: ReplacementPlan, destination: Path, ctx: WriteContext
    ) -> list[Issue]:
        soffice = libreoffice.find_soffice(ctx.config)
        if soffice is None:
            raise RuntimeError("LibreOffice unavailable")
        modern, _target = TO_MODERN[document.format]
        intermediate = ctx.temp_path(f".{modern}")
        issues = _ooxml.write(document, plan, intermediate, ctx, FLAVORS[document.format]())
        converted = libreoffice.convert(
            intermediate, TO_LEGACY[document.format], ctx.temp_path("-lo"), soffice
        )
        shutil.move(str(converted), destination)
        return issues

    @staticmethod
    def _write_xlrd(document: Document, plan: ReplacementPlan, destination: Path) -> list[Issue]:
        import xlrd

        book: Any = open_xls(document.source.read_bytes())

        def replaced(key: tuple[str | int, ...], text: str) -> str | None:
            reps = plan.for_segment("x_" + "_".join(map(str, key)))
            return apply_replacements(text, reps) if reps else None

        workbook = Workbook()
        used: set[str] = set()
        for s_index in range(book.nsheets):
            sheet = book.sheet_by_index(s_index)
            name = replaced(("sheet", s_index), sheet.name) or sheet.name
            out = workbook.add_sheet(sanitize_sheet_name(name, used))
            for r in range(sheet.nrows):
                for c in range(sheet.ncols):
                    cell = sheet.cell(r, c)
                    if cell.ctype == xlrd.XL_CELL_TEXT:
                        text = str(cell.value)
                        out.set_text(r, c, replaced(("cell", s_index, r, c), text) or text)
                    elif cell.ctype == xlrd.XL_CELL_NUMBER:
                        new = replaced(("number", s_index, r, c), number_text(cell.value))
                        if new is None:
                            out.set_number(r, c, float(cell.value))
                        else:
                            out.set_text(r, c, new)
                    elif cell.ctype == xlrd.XL_CELL_DATE:
                        out.set_date(r, c, float(cell.value))
                    elif cell.ctype == xlrd.XL_CELL_BOOLEAN:
                        out.set_bool(r, c, bool(cell.value))
        workbook.save(destination)
        dropped = any(
            getattr(book.sheet_by_index(i), "cell_note_map", None)
            or getattr(book.sheet_by_index(i), "hyperlink_list", None)
            for i in range(book.nsheets)
        )
        if dropped:
            return [
                Issue(
                    Severity.INFO,
                    "legacy.dropped",
                    "Commentaires de cellule et liens hypertexte non conservés dans le .xlsx "
                    "produit.",
                )
            ]
        return []
