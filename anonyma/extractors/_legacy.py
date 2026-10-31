"""Legacy binary Office formats (doc, xls, ppt).

With LibreOffice, a legacy file is converted to its OOXML counterpart in the
batch temporary folder, extracted and written with the OOXML engine, then
converted back. Without LibreOffice, ``xls`` is read with ``xlrd`` (pure Python,
BSD) and written as a fresh ``xlsx`` (values only); ``doc``/``ppt`` are refused
with an explicit error.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from anonyma.extractors._ooxml import Flavor
from anonyma.extractors._ooxml_flavors import DocxFlavor, PptxFlavor, XlsxFlavor

#: legacy format -> (modern extension, LibreOffice import filter target)
TO_MODERN = {
    "doc": ("docx", "docx:MS Word 2007 XML"),
    "xls": ("xlsx", "xlsx:Calc MS Excel 2007 XML"),
    "ppt": ("pptx", "pptx:Impress MS PowerPoint 2007 XML"),
}
#: legacy format -> LibreOffice export filter target
TO_LEGACY = {
    "doc": "doc:MS Word 97",
    "xls": "xls:MS Excel 97",
    "ppt": "ppt:MS PowerPoint 97",
}
FLAVORS: dict[str, type[Flavor]] = {"doc": DocxFlavor, "xls": XlsxFlavor, "ppt": PptxFlavor}

MODE_LIBREOFFICE = "libreoffice"
MODE_XLRD = "xlrd"

LIBREOFFICE_REQUIRED = (
    "LibreOffice requis pour traiter ce format ancien (.doc, .ppt) : installez-le ou "
    "indiquez son emplacement dans les réglages."
)

OLE_TEXT_FIELDS = (
    "title",
    "subject",
    "author",
    "keywords",
    "comments",
    "template",
    "last_saved_by",
    "category",
    "manager",
    "company",
    "content_status",
)


def ole_metadata(data: bytes) -> Iterator[tuple[str, str]]:
    """Text properties of the OLE SummaryInformation streams (``(field, value)``)."""
    import olefile

    if not olefile.isOleFile(io.BytesIO(data)):
        return
    try:
        ole = olefile.OleFileIO(io.BytesIO(data))
    except OSError:
        return
    try:
        meta = ole.get_metadata()
    finally:
        ole.close()
    codepage = getattr(meta, "codepage", None) or 1252
    for name in OLE_TEXT_FIELDS:
        value = getattr(meta, name, None)
        if isinstance(value, bytes):
            try:
                text = value.decode(f"cp{codepage}", errors="replace")
            except LookupError:
                text = value.decode("cp1252", errors="replace")
        elif isinstance(value, str):
            text = value
        else:
            continue
        text = text.rstrip("\x00")
        if text.strip():
            yield name, text


# ---------------------------------------------------------------------------
# xls through xlrd
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class XlsItem:
    """One text-bearing item of an xls workbook, in a deterministic order."""

    key: tuple[str | int, ...]
    text: str
    kind: str  # "sheet" | "cell" | "number" | "note" | "author" | "link" | "name"
    label: str


def open_xls(data: bytes) -> Any:
    import xlrd

    try:
        return xlrd.open_workbook(file_contents=data, formatting_info=True, on_demand=False)
    except Exception:
        return xlrd.open_workbook(file_contents=data, formatting_info=False, on_demand=False)


def _column(index: int) -> str:
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def number_text(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else repr(value)


def xls_items(book: Any) -> Iterator[XlsItem]:
    import xlrd

    for s_index in range(book.nsheets):
        sheet = book.sheet_by_index(s_index)
        number = s_index + 1
        yield XlsItem(("sheet", s_index), sheet.name, "sheet", "nom de feuille")
        for r in range(sheet.nrows):
            for c in range(sheet.ncols):
                cell = sheet.cell(r, c)
                ref = f"feuille {number}!{_column(c)}{r + 1}"
                if cell.ctype == xlrd.XL_CELL_TEXT and str(cell.value).strip():
                    yield XlsItem(("cell", s_index, r, c), str(cell.value), "cell", ref)
                elif cell.ctype == xlrd.XL_CELL_NUMBER:
                    text = number_text(cell.value)
                    if sum(ch.isdigit() for ch in text) >= 6:
                        yield XlsItem(("number", s_index, r, c), text, "number", ref)
        for (r, c), note in sorted(getattr(sheet, "cell_note_map", {}).items()):
            ref = f"feuille {number}!{_column(c)}{r + 1}"
            if getattr(note, "text", ""):
                yield XlsItem(("note", s_index, r, c), note.text, "note", f"commentaire {ref}")
            if getattr(note, "author", ""):
                yield XlsItem(("author", s_index, r, c), note.author, "author", "auteur")
        for l_index, link in enumerate(getattr(sheet, "hyperlink_list", [])):
            for attr in ("url_or_path", "desc", "textmark", "quicktip"):
                value = getattr(link, attr, None)
                if isinstance(value, str) and value.strip():
                    yield XlsItem(
                        ("link", s_index, l_index, attr), value, "link", f"lien feuille {number}"
                    )
    for n_index, name in enumerate(getattr(book, "name_obj_list", [])):
        value = getattr(name, "name", "")
        if isinstance(value, str) and value.strip() and not value.startswith("_xlnm"):
            yield XlsItem(("name", n_index), value, "name", "nom défini")
