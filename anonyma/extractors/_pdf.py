"""PDF helpers shared by the PDF extractor and writer (PyMuPDF).

Coordinates: PyMuPDF text extraction, image boxes and redaction annotations all
use the unrotated page space; rendered pixmaps are rotated, so OCR boxes are
mapped back with ``page.derotation_matrix``.
"""

from __future__ import annotations

import codecs
from collections.abc import Iterator
from dataclasses import dataclass

import pymupdf

from anonyma.models import BBox

#: Text extraction flags: keep whitespace, expand ligatures, do not clip text
#: that lies outside the visible page area (it is still in the file).
TEXT_FLAGS = pymupdf.TEXT_PRESERVE_WHITESPACE

#: A page whose text layer has fewer visible characters than this and whose
#: images cover at least ``SCAN_COVERAGE`` of the page is treated as scanned.
MIN_TEXT_CHARS = 50
SCAN_COVERAGE = 0.3
#: Images smaller than this (pixels, either side) are not extracted as children.
MIN_IMAGE_SIDE = 16
OCR_DPI = 300


def page_coverage(page: pymupdf.Page) -> float:
    """Fraction of the page area covered by images (overlaps counted once per image)."""
    area = float(abs(page.rect))
    if area <= 0:
        return 0.0
    covered = 0.0
    for info in page.get_image_info():
        rect = pymupdf.Rect(info["bbox"]) & page.cropbox
        covered += float(abs(rect))
    return min(covered / area, 1.0)


def has_drawings(page: pymupdf.Page) -> bool:
    try:
        return bool(page.get_drawings())
    except Exception:  # broken content streams: assume yes (recall first)
        return True


def to_bbox(rect: pymupdf.Rect | tuple[float, float, float, float]) -> BBox:
    r = pymupdf.Rect(rect)
    return BBox(r.x0, r.y0, r.x1, r.y1)


def pixel_rect_to_page(page: pymupdf.Page, box: BBox, zoom: float) -> pymupdf.Rect:
    """Map a rendered-pixmap rectangle back to unrotated page coordinates."""
    matrix = page.derotation_matrix
    p0 = pymupdf.Point(box.x0 / zoom, box.y0 / zoom) * matrix
    p1 = pymupdf.Point(box.x1 / zoom, box.y1 / zoom) * matrix
    return pymupdf.Rect(p0, p1).normalize()


def split_box(box: BBox, count: int) -> list[BBox]:
    """Split a line box into ``count`` equal character boxes."""
    if count <= 0:
        return []
    width = (box.x1 - box.x0) / count
    return [
        BBox(box.x0 + i * width, box.y0, box.x0 + (i + 1) * width, box.y1) for i in range(count)
    ]


# ---------------------------------------------------------------------------
# Strings inside PDF objects (Info dict, annotations, structure tree, forms...)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PdfString:
    start: int  # span inside the object source
    end: int
    value: str
    #: Dictionary key the string is the value of ("" inside arrays).
    key: str = ""


def _decode_literal(raw: str) -> bytes:
    out = bytearray()
    i = 0
    n = len(raw)
    escapes = {"n": 10, "r": 13, "t": 9, "b": 8, "f": 12, "(": 40, ")": 41, "\\": 92}
    while i < n:
        ch = raw[i]
        if ch != "\\":
            out.extend(ch.encode("latin-1", errors="replace"))
            i += 1
            continue
        i += 1
        if i >= n:
            break
        nxt = raw[i]
        if nxt in escapes:
            out.append(escapes[nxt])
            i += 1
        elif nxt in "01234567":
            digits = nxt
            i += 1
            while i < n and len(digits) < 3 and raw[i] in "01234567":
                digits += raw[i]
                i += 1
            out.append(int(digits, 8) & 0xFF)
        elif nxt in "\r\n":
            i += 1
            if nxt == "\r" and i < n and raw[i] == "\n":
                i += 1
        else:
            out.extend(nxt.encode("latin-1", errors="replace"))
            i += 1
    return bytes(out)


def decode_pdf_bytes(data: bytes) -> str:
    if data.startswith(codecs.BOM_UTF16_BE):
        return data[2:].decode("utf-16-be", errors="replace")
    if data.startswith(codecs.BOM_UTF8):
        return data[3:].decode("utf-8", errors="replace")
    # PDFDocEncoding is close enough to latin-1 for detection purposes.
    return data.decode("latin-1")


_DELIMITERS = frozenset("()<>[]{}/% \t\r\n\f\x00")


def iter_strings(source: str) -> Iterator[PdfString]:
    """String literals ``(...)`` and hex strings ``<...>`` of an object source,
    with the dictionary key they belong to."""
    i = 0
    n = len(source)
    key = ""
    while i < n:
        ch = source[i]
        if ch == "(":
            start = i
            depth = 1
            i += 1
            while i < n and depth:
                c = source[i]
                if c == "\\":
                    i += 2
                    continue
                if c == "(":
                    depth += 1
                elif c == ")":
                    depth -= 1
                i += 1
            raw = source[start + 1 : i - 1]
            yield PdfString(start, i, decode_pdf_bytes(_decode_literal(raw)), key)
            key = ""
            continue
        if ch == "<":
            if i + 1 < n and source[i + 1] == "<":
                i += 2
                key = ""
                continue
            end = source.find(">", i)
            if end == -1:
                return
            hexdata = "".join(c for c in source[i + 1 : end] if c in "0123456789abcdefABCDEF")
            if len(hexdata) % 2:
                hexdata += "0"
            yield PdfString(i, end + 1, decode_pdf_bytes(bytes.fromhex(hexdata)), key)
            key = ""
            i = end + 1
            continue
        if ch == "/":
            j = i + 1
            while j < n and source[j] not in _DELIMITERS:
                j += 1
            key = source[i + 1 : j]
            i = j
            continue
        if ch == "%":  # comment until end of line
            while i < n and source[i] not in "\r\n":
                i += 1
            continue
        if not ch.isspace():
            key = ""
        i += 1


#: Keys whose strings are PDF operator syntax, never human text (form field
#: default appearance and default style).
OPERATOR_KEYS = frozenset({"DA", "DS"})


def exposed_strings(source: str) -> Iterator[tuple[int, PdfString]]:
    """``(index, string)`` of the strings of an object worth analysing: textual,
    and not operator syntax."""
    for index, string in enumerate(iter_strings(source)):
        if string.key not in OPERATOR_KEYS and is_textual(string.value):
            yield index, string


def encode_pdf_string(value: str) -> str:
    """Hex string, UTF-16BE with BOM (always valid, any character)."""
    return "<" + (codecs.BOM_UTF16_BE + value.encode("utf-16-be")).hex().upper() + ">"


def is_textual(value: str) -> bool:
    return sum(c.isalpha() for c in value) >= 2


def object_sources(doc: pymupdf.Document) -> Iterator[tuple[int, str]]:
    """``(xref, source)`` of every object (stream dictionaries without data)."""
    for xref in range(1, doc.xref_length()):
        try:
            source = doc.xref_object(xref, compressed=False)
        except Exception:
            continue
        if source and source != "null":
            yield xref, source
