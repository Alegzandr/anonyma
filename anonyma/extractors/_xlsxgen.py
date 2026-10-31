"""Minimal xlsx generator (values only), used when an xls is read without LibreOffice."""

from __future__ import annotations

import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG = "http://schemas.openxmlformats.org/package/2006/relationships"
_DECL = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
_ILLEGAL = {c: None for c in range(32) if c not in (9, 10, 13)}


def _text(value: str) -> str:
    return escape(value.translate(_ILLEGAL))


def _column(index: int) -> str:
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


@dataclass(slots=True)
class Sheet:
    name: str
    cells: dict[tuple[int, int], str] = field(default_factory=dict)

    def set_text(self, row: int, col: int, value: str) -> None:
        ref = f"{_column(col)}{row + 1}"
        self.cells[row, col] = (
            f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{_text(value)}</t></is></c>'
        )

    def set_number(self, row: int, col: int, value: float) -> None:
        self.cells[row, col] = f'<c r="{_column(col)}{row + 1}"><v>{value!r}</v></c>'

    def set_date(self, row: int, col: int, serial: float) -> None:
        self.cells[row, col] = f'<c r="{_column(col)}{row + 1}" s="1"><v>{serial!r}</v></c>'

    def set_bool(self, row: int, col: int, value: bool) -> None:
        self.cells[row, col] = f'<c r="{_column(col)}{row + 1}" t="b"><v>{int(value)}</v></c>'

    def xml(self) -> str:
        rows: dict[int, list[tuple[int, str]]] = {}
        for (row, col), cell in self.cells.items():
            rows.setdefault(row, []).append((col, cell))
        body = "".join(
            f'<row r="{row + 1}">' + "".join(c for _, c in sorted(cells)) + "</row>"
            for row, cells in sorted(rows.items())
        )
        return _DECL + f'<worksheet xmlns="{_NS}"><sheetData>{body}</sheetData></worksheet>'


@dataclass(slots=True)
class Workbook:
    sheets: list[Sheet] = field(default_factory=list)

    def add_sheet(self, name: str) -> Sheet:
        sheet = Sheet(name)
        self.sheets.append(sheet)
        return sheet

    def save(self, path: Path) -> None:
        if not self.sheets:
            self.add_sheet("Feuille1")
        sheets = "".join(
            f'<sheet name={quoteattr(s.name.translate(_ILLEGAL))} sheetId="{i + 1}" '
            f'r:id="rId{i + 1}"/>'
            for i, s in enumerate(self.sheets)
        )
        rels = "".join(
            f'<Relationship Id="rId{i + 1}" Type="{_R}/worksheet" '
            f'Target="worksheets/sheet{i + 1}.xml"/>'
            for i in range(len(self.sheets))
        )
        n = len(self.sheets)
        overrides = "".join(
            f'<Override PartName="/xl/worksheets/sheet{i + 1}.xml" ContentType="application/'
            'vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            for i in range(n)
        )
        entries = {
            "[Content_Types].xml": _DECL
            + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.'
            'relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-'
            'officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-'
            f'officedocument.spreadsheetml.styles+xml"/>{overrides}</Types>',
            "_rels/.rels": _DECL + f'<Relationships xmlns="{_PKG}"><Relationship Id="rId1" '
            f'Type="{_R}/officeDocument" Target="xl/workbook.xml"/></Relationships>',
            "xl/workbook.xml": _DECL
            + f'<workbook xmlns="{_NS}" xmlns:r="{_R}"><sheets>{sheets}</sheets></workbook>',
            "xl/_rels/workbook.xml.rels": _DECL
            + f'<Relationships xmlns="{_PKG}">{rels}<Relationship Id="rId{n + 1}" '
            f'Type="{_R}/styles" Target="styles.xml"/></Relationships>',
            "xl/styles.xml": _DECL
            + f'<styleSheet xmlns="{_NS}"><fonts count="1"><font><sz val="11"/>'
            '<name val="Calibri"/></font></fonts><fills count="2"><fill><patternFill '
            'patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>'
            '</fills><borders count="1"><border/></borders><cellStyleXfs count="1"><xf '
            'numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
            '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
            '<xf numFmtId="14" fontId="0" fillId="0" borderId="0" xfId="0" '
            'applyNumberFormat="1"/></cellXfs></styleSheet>',
        }
        for i, sheet in enumerate(self.sheets):
            entries[f"xl/worksheets/sheet{i + 1}.xml"] = sheet.xml()
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, content in entries.items():
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                zf.writestr(info, content.encode("utf-8"))
