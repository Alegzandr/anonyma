"""PDF writer: true redaction (PyMuPDF), metadata, signatures and active content purged.

* Text-layer spans are redacted line by line. Glyph removal uses a rectangle
  shrunk to the glyph centres (MuPDF removes every glyph whose box merely
  touches the rectangle); a second pass with the full rectangle blanks image
  pixels and removes vector graphics fully covered. The visible box is then
  painted: white with the substitute on the original baseline (tags / realistic
  mode) or black (blackout mode). Boxes are clipped vertically so that they spare
  the neighbouring lines; substitutes keep a readable size (see ``layout_text``).
* Invisible text (render mode 3, transparent, tiny, off-page, white on white,
  hidden layer) is deleted when ``config.remove_invisible_text`` (otherwise only
  its replaced spans are, the substitute being written invisibly).
* OCR-only regions of the rendered page and barcodes: glyphs, image pixels and
  covered vector art removed, vector glyphs touching the region removed as
  well, then an opaque box. A form field whose appearance lies under such a
  region is deleted (its appearance would be drawn over the box).
* Image XObjects with replacements are redrawn in place from their own OCR
  boxes (transparency kept, every placement updated); untouched images keep
  their exact look, JPEG metadata stripped losslessly; images that could not
  be verified are deleted.
* Digital signatures are removed (signature fields and appearances, ``/Perms``,
  ``/DSS``, ``/SigFlags``): an INFO issue says so.
* Purged: document information, XMP and every metadata stream, page piece
  info, XFA forms, bookmarks, thumbnails, JavaScript and actions, every
  annotation except links and form fields, external links (URI, launch,
  remote), embedded files (re-attached anonymized). Form field values/labels
  and every textual string of every object are anonymized.
* Saved as a full rewrite (single revision, garbage collected, new trailer
  ``/ID``), without encryption. Original layer visibility is restored.
"""

from __future__ import annotations

import contextlib
import math
import secrets
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pymupdf
from PIL import Image, ImageDraw, ImageFont

from anonyma.extractors._archive import UniqueNames
from anonyma.extractors._media import BLANKED_CODE, blanked_message
from anonyma.extractors._pdf import TEXT_FLAGS, encode_pdf_string, iter_strings, object_sources
from anonyma.extractors._pdfobjects import (
    LayerState,
    delete_keys,
    image_kind,
    image_pixels,
    open_document,
    restore_layers,
    store_image_pixels,
    strip_jpeg_metadata,
)
from anonyma.models import (
    BBox,
    Document,
    Issue,
    Replacement,
    ReplacementMode,
    ReplacementPlan,
    Segment,
    SegmentContext,
    SegmentKind,
    Severity,
    WriteContext,
    Writer,
    apply_replacements,
)
from anonyma.registry import register_writer
from anonyma.writers.image import page_redaction_boxes

KEPT_ANNOTS = frozenset({pymupdf.PDF_ANNOT_LINK, pymupdf.PDF_ANNOT_WIDGET})
REMOVED_LINKS = frozenset(
    {pymupdf.LINK_URI, pymupdf.LINK_LAUNCH, pymupdf.LINK_GOTOR, pymupdf.LINK_NAMED}
)
#: Fraction of the edge characters / line height trimmed from glyph-removal boxes.
H_INSET = 0.3
V_INSET = 0.3
#: Ink extent around the baseline, in font sizes: descenders of the line above,
#: accented capitals of the line below. A painted box stops there...
INK_DESCENT = 0.25
INK_ASCENT = 0.95
#: ...but always covers this much of its own line.
KEEP_ABOVE = 0.6
KEEP_BELOW = 0.15
#: Substitutes drawn on the page (see ``layout_text``): narrowest horizontal
#: scale, readable font size (fraction of the original, absolute minimum in
#: points), smallest size before running over the following words.
MIN_CONDENSE = 0.6
MIN_SIZE_RATIO = 0.65
MIN_SIZE = 5.0
HARD_SIZE_RATIO = 0.5
HARD_MIN_SIZE = 4.0
#: Space kept between a substitute and the next word, in font sizes.
WORD_GAP = 0.15
#: Keys removed from every object (private application data, metadata streams).
PURGED_KEYS = ("Metadata", "PieceInfo")

SIGNATURE_REMOVED = "pdf.signature_removed"
FIELD_REMOVED = "pdf.field_removed"


def _rect(box: BBox) -> pymupdf.Rect:
    return pymupdf.Rect(box.x0, box.y0, box.x1, box.y1)


def _line_groups(segment: Segment, rep: Replacement) -> list[list[BBox]]:
    """Character boxes of ``rep`` per line (split on ``\\n``), empty lines kept."""
    boxes = segment.char_boxes or ()
    groups: list[list[BBox]] = [[]]
    for index in range(rep.start, min(rep.end, len(boxes))):
        if segment.text[index] == "\n":
            groups.append([])
            continue
        box = boxes[index]
        if box.x1 > box.x0 and box.y1 > box.y0:
            groups[-1].append(box)
    return groups


def line_parts(segment: Segment, rep: Replacement) -> list[tuple[list[BBox], str]] | None:
    """The lines of ``rep``, each with its part of the substitute, when the
    substitute is cut by the same line breaks as the value (a number cut at a line
    end): each part is then painted where the original part was. None
    otherwise (the substitute is painted whole on the first line)."""
    groups = _line_groups(segment, rep)
    parts = [part.strip() for part in rep.substitute.split("\n")]
    if len(groups) < 2 or len(parts) != len(groups):
        return None
    pairs = [(g, part) for g, part in zip(groups, parts, strict=True) if g]
    return pairs if len(pairs) > 1 and all(part for _, part in pairs) else None


def full_rect(boxes: Sequence[BBox]) -> pymupdf.Rect:
    rect = _rect(boxes[0])
    for box in boxes[1:]:
        rect |= _rect(box)
    return rect


def inner_rect(boxes: Sequence[BBox]) -> pymupdf.Rect:
    """Rectangle touching every glyph of ``boxes`` but none of their neighbours."""
    rect = full_rect(boxes)
    first, last = boxes[0], boxes[-1]
    dx0 = (first.x1 - first.x0) * H_INSET
    dx1 = (last.x1 - last.x0) * H_INSET
    dy = rect.height * V_INSET
    inner = pymupdf.Rect(rect.x0 + dx0, rect.y0 + dy, rect.x1 - dx1, rect.y1 - dy)
    if inner.is_empty:
        cx, cy = (rect.x0 + rect.x1) / 2, (rect.y0 + rect.y1) / 2
        inner = pymupdf.Rect(cx - 0.1, cy - 0.1, cx + 0.1, cy + 0.1)
    return inner


def _inside(rect: pymupdf.Rect, container: pymupdf.Rect) -> bool:
    overlap = rect & container
    return not overlap.is_empty and abs(overlap) >= 0.5 * abs(rect)


@dataclass(frozen=True, slots=True)
class TextLine:
    """One horizontal span of the original page: glyph box, baseline, font size."""

    rect: pymupdf.Rect
    baseline: float
    size: float


def page_lines(page: pymupdf.Page) -> list[TextLine]:
    """Horizontal text spans of ``page`` (geometry only, no text kept)."""
    lines: list[TextLine] = []
    data = page.get_text("dict", flags=TEXT_FLAGS)
    for block in data.get("blocks", []):
        for line in block.get("lines", []):
            dx, dy = line.get("dir", (1.0, 0.0))
            if dx <= 0 or abs(dy) > 0.01:
                continue
            for span in line.get("spans", []):
                size = float(span.get("size", 0.0))
                if size < 1.0 or not str(span.get("text", "")).strip():
                    continue
                rect = pymupdf.Rect(span["bbox"])
                if not rect.is_empty:
                    lines.append(TextLine(rect, float(span["origin"][1]), size))
    return lines


@dataclass(frozen=True, slots=True)
class Placement:
    """Where a replaced line is painted: its box (clear of the neighbouring lines),
    the original baseline and font size when known."""

    box: pymupdf.Rect
    baseline: float | None
    size: float


def place(rect: pymupdf.Rect, lines: Sequence[TextLine]) -> Placement:
    """Box painted over a replaced line, clipped vertically so that it spares the
    lines above and below.

    Glyph boxes span the font's whole ascender..descender range, taller than the
    line pitch of ordinary paragraphs (10 pt Helvetica: 13.7 pt boxes for ~12 pt
    leading): painted whole, the box cut the descenders of the line above. The
    original glyphs are removed from the content stream beforehand (pass 1), so
    the box only needs to cover the line's own ink zone, which it keeps:
    at least ``KEEP_ABOVE`` / ``KEEP_BELOW`` font sizes around the baseline.
    """
    fallback = Placement(rect, None, rect.height * 0.75)
    own = max(lines, key=lambda line: abs(line.rect & rect), default=None)
    if own is None or abs(own.rect & rect) < 0.3 * abs(rect):
        return fallback
    base, size = own.baseline, own.size
    y0, y1 = rect.y0, rect.y1
    for line in lines:
        if line.rect.x1 <= rect.x0 or line.rect.x0 >= rect.x1:
            continue
        if abs(line.baseline - base) < 0.5 * min(size, line.size):
            continue  # same line (another span, an overlapping text layer)
        if line.baseline < base and line.rect.y1 > y0:
            y0 = max(y0, min(line.baseline + INK_DESCENT * line.size, base - KEEP_ABOVE * size))
        elif line.baseline > base and line.rect.y0 < y1:
            y1 = min(y1, max(line.baseline - INK_ASCENT * line.size, base + KEEP_BELOW * size))
    if y1 - y0 < 1.0:
        return Placement(rect, base, size)
    return Placement(pymupdf.Rect(rect.x0, y0, rect.x1, y1), base, size)


@dataclass(frozen=True, slots=True)
class TextLayout:
    """A substitute laid out from ``origin``: font size, horizontal scale, width."""

    origin: pymupdf.Point
    size: float
    scale: float
    width: float


def layout_text(
    rect: pymupdf.Rect,
    text: str,
    *,
    size: float | None = None,
    baseline: float | None = None,
    room: float | None = None,
    overflow: bool = False,
) -> TextLayout | None:
    """Lay ``text`` out at the left of ``rect`` within ``room`` points of width.

    Substitutes are often wider than the words they replace (a six-letter name
    → ``[PERSONNE_001]``); fitted by shrinking alone, they became unreadable 3–4 pt
    text. Tags cannot be abbreviated (they are batch-wide identifiers) and the
    following words cannot be moved (that would mean rewriting the content stream
    with another font). Options compared for a substitute that does not fit:
    shrinking only (unreadable), condensing only (illegible below ~60 %),
    drawing over the following words (both texts illegible), or painting the box
    over them (the line then reads as if words were missing: misleading, tried
    and rejected as the usual fallback). Hence, in this order:

    1. the free space after the word is used (``room``, computed by the caller:
       whitespace, right margin, up to the next glyph, box or cell border);
    2. the font shrinks and the glyphs are condensed in equal proportion, down to
       ``MIN_SIZE_RATIO`` of the original size (``MIN_SIZE`` points at least) and
       ``MIN_CONDENSE`` of their width: readable, and enough for a tag about
       2.5 times wider than the room;
    3. then the font shrinks further, down to ``HARD_SIZE_RATIO`` (``HARD_MIN_SIZE``
       points at least): smaller, still readable, the line stays intact;
    4. only then, with ``overflow`` (text lines), the text extends to the right at
       that size and the caller paints its box over the glyphs it runs into (they
       stay in the text layer). Without ``overflow`` (OCR boxes over image
       content, invisible text) it keeps shrinking to stay inside ``rect``.
    """
    if not text.strip() or rect.width <= 0 or rect.height <= 0:
        return None
    nominal = size if size and size > 0 else rect.height * 0.75
    room = max(room or rect.width, rect.width)
    natural = pymupdf.get_text_length(text, fontname="helv", fontsize=nominal)
    font_size, scale = nominal, 1.0
    if natural > room:
        ratio = room / natural
        floor = min(nominal, max(MIN_SIZE_RATIO * nominal, MIN_SIZE))
        font_size = max(floor, nominal * math.sqrt(ratio))
        scale = min(1.0, max(MIN_CONDENSE, ratio * nominal / font_size))
        fitting = room / (natural / nominal * scale)
        if fitting < font_size:
            hard = min(nominal, max(HARD_SIZE_RATIO * nominal, HARD_MIN_SIZE))
            font_size = max(hard if overflow else 2.0, fitting)
    if baseline is None:
        baseline = rect.y0 + (rect.height + font_size * 0.7) / 2
    width = natural * font_size / nominal * scale
    return TextLayout(pymupdf.Point(rect.x0, baseline), font_size, scale, width)


def draw_text(
    page: pymupdf.Page, layout: TextLayout, text: str, *, invisible: bool = False
) -> None:
    """Write ``text`` as laid out (Helvetica, condensed by a horizontal scale)."""
    morph = None
    if layout.scale < 1.0:
        morph = (layout.origin, pymupdf.Matrix(layout.scale, 0, 0, 1, 0, 0))
    page.insert_text(
        layout.origin,
        text,
        fontsize=layout.size,
        fontname="helv",
        color=(0, 0, 0),
        overlay=True,
        render_mode=3 if invisible else 0,
        morph=morph,
    )


def fit_text(page: pymupdf.Page, rect: pymupdf.Rect, text: str, *, invisible: bool = False) -> None:
    """Write ``text`` inside ``rect`` (condensed, then shrunk, so that it fits)."""
    layout = layout_text(rect, text)
    if layout is not None:
        draw_text(page, layout, text, invisible=invisible)


@dataclass(slots=True)
class _Obstacles:
    """What a substitute must not run over on a page: remaining glyphs, painted
    boxes, vector graphics and images (a table cell bounds its content)."""

    glyphs: list[pymupdf.Rect]
    boxes: list[pymupdf.Rect]
    containers: list[pymupdf.Rect]
    right: float

    @classmethod
    def of(cls, page: pymupdf.Page, boxes: list[pymupdf.Rect]) -> _Obstacles:
        glyphs: list[pymupdf.Rect] = []
        data = page.get_text("rawdict", flags=TEXT_FLAGS)
        for block in data.get("blocks", []):
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    for char in span.get("chars", []):
                        if str(char.get("c", "")).strip():
                            glyphs.append(pymupdf.Rect(char["bbox"]))
        art: list[pymupdf.Rect] = []
        with contextlib.suppress(Exception):
            art.extend(pymupdf.Rect(d.get("rect") or (0, 0, 0, 0)) for d in page.get_drawings())
        with contextlib.suppress(Exception):
            art.extend(pymupdf.Rect(info["bbox"]) for info in page.get_image_info())
        art = [r for r in art if r.width > 0 or r.height > 0]
        # Rotated pages: text and page coordinates differ, no extension at all.
        right = page.rect.x1 if page.rotation == 0 else 0.0
        return cls(glyphs, boxes, art, right)

    @staticmethod
    def _bands(placement: Placement) -> tuple[float, float, float, float]:
        """Vertical span of the line's ink (for graphics) and of its glyph centres."""
        box = placement.box
        if placement.baseline is None:
            mid, half = (box.y0 + box.y1) / 2, box.height / 2
            return mid - 0.4 * half, mid + 0.4 * half, mid - half, mid + half
        base, size = placement.baseline, placement.size
        return base - 0.6 * size, base + 0.1 * size, base - 0.9 * size, base + 0.3 * size

    def _line_glyphs(self, placement: Placement) -> list[pymupdf.Rect]:
        """Remaining glyphs on the same line as ``placement``."""
        _, _, low, high = self._bands(placement)
        return [g for g in self.glyphs if low <= (g.y0 + g.y1) / 2 <= high]

    def room(self, placement: Placement, gap: float) -> float:
        """Width available from the box's left edge to the next obstacle."""
        box = placement.box
        top, bottom, _, _ = self._bands(placement)
        right = self.right - gap
        eps = 0.01
        art = [r for r in self.containers if r.y1 > top and r.y0 < bottom]
        boxes = [r for r in self.boxes if r.y1 > top and r.y0 < bottom]
        for rect in (*self._line_glyphs(placement), *boxes, *art):
            if rect.x0 >= box.x1 - eps:
                right = min(right, rect.x0 - gap)
        for rect in art:
            if rect.x0 <= box.x0 + eps and rect.x1 >= box.x1 - eps:
                right = min(right, rect.x1 - gap)  # cell, frame or background edge
        return float(max(box.width, right - box.x0))

    def covered(self, placement: Placement, right: float) -> float:
        """Right edge of a box reaching ``right``, widened to whole glyphs."""
        box = placement.box
        for glyph in self._line_glyphs(placement):
            if glyph.x1 > box.x1 and glyph.x0 < right:
                right = max(right, glyph.x1)
        return right


def paint_boxes(
    image: Image.Image, boxes: Sequence[tuple[BBox, str]], mode: ReplacementMode
) -> None:
    """Opaque boxes drawn into image pixels, like the boxes painted on pages:
    white with the substitute in black (black boxes in blackout mode)."""
    draw = ImageDraw.Draw(image)
    blackout = mode is ReplacementMode.BLACKOUT
    bands = len(image.getbands())
    box_fill: tuple[int, ...] = (0 if blackout else 255,) * min(bands, 3)
    ink: tuple[int, ...] = (0,) * min(bands, 3)
    if bands in (2, 4):  # alpha: fully opaque
        box_fill += (255,)
        ink += (255,)
    width, height = image.size
    for box, label in boxes:
        x0, y0 = max(0, int(box.x0)), max(0, int(box.y0))
        x1, y1 = min(width, int(box.x1 + 0.999)), min(height, int(box.y1 + 0.999))
        if x1 <= x0 or y1 <= y0:
            continue
        draw.rectangle((x0, y0, x1 - 1, y1 - 1), fill=box_fill)
        if blackout or not label.strip():
            continue
        size = max(6, int((y1 - y0) * 0.6))
        try:
            font: Any = ImageFont.load_default(size=size)
            text_box = draw.textbbox((0, 0), label, font=font)
            if text_box[2] - text_box[0] > x1 - x0:
                size = max(6, int(size * (x1 - x0) / (text_box[2] - text_box[0])))
                font = ImageFont.load_default(size=size)
                text_box = draw.textbbox((0, 0), label, font=font)
        except (OSError, TypeError):
            continue
        text_w, text_h = text_box[2] - text_box[0], text_box[3] - text_box[1]
        if text_w <= x1 - x0 and text_h <= y1 - y0:
            position = (x0 + (x1 - x0 - text_w) / 2, y0 + (y1 - y0 - text_h) / 2 - text_box[1])
            draw.text(position, label, fill=ink, font=font)


def remove_javascript(doc: pymupdf.Document) -> None:
    """Drop JavaScript code, additional actions and the JavaScript name tree."""
    for xref in range(1, doc.xref_length()):
        try:
            source = doc.xref_object(xref, compressed=False)
        except Exception:
            continue
        if not any(token in source for token in ("/JavaScript", "/JS", "/AA")):
            continue
        for key in doc.xref_get_keys(xref):
            kind, value = doc.xref_get_key(xref, key)
            if key in ("JS", "AA", "JavaScript") or (
                kind == "dict" and ("/JavaScript" in value or "/JS" in value)
            ):
                doc.xref_set_key(xref, key, "null")


def _delete(doc: pymupdf.Document, xref: int, *keys: str) -> None:
    delete_keys(doc, xref, keys)


def _acroform(doc: pymupdf.Document) -> int | None:
    """Object number of the AcroForm dictionary.

    An inline dictionary is moved to an object of its own: keys can only be
    deleted (set to ``null``) at the top level of an object.
    """
    catalog = doc.pdf_catalog()
    kind, value = doc.xref_get_key(catalog, "AcroForm")
    if kind == "dict":
        xref = doc.get_new_xref()
        doc.update_object(xref, value)
        doc.xref_set_key(catalog, "AcroForm", f"{xref} 0 R")
        kind, value = "xref", f"{xref} 0 R"
    if kind == "xref":
        return int(value.split()[0])
    return None


def remove_signatures(doc: pymupdf.Document) -> tuple[int, dict[int, list[pymupdf.Rect]]]:
    """Delete signature fields, permissions, DSS and signature flags.

    Returns the number of signature fields removed and their rectangles per page.
    """
    removed = 0
    rects: dict[int, list[pymupdf.Rect]] = {}
    for page in doc:
        for widget in list(page.widgets() or []):
            if widget.field_type == pymupdf.PDF_WIDGET_TYPE_SIGNATURE:
                rects.setdefault(page.number, []).append(pymupdf.Rect(widget.rect))
                page.delete_widget(widget)
                removed += 1
    catalog = doc.pdf_catalog()
    _delete(doc, catalog, "Perms", "DSS")
    form = _acroform(doc)
    if form is not None:
        xref = form
        _delete(doc, xref, "SigFlags")
        kind, value = doc.xref_get_key(xref, "Fields")
        if kind == "array":
            kept = []
            tokens = value.strip("[] ").split()
            for i in range(0, len(tokens) - 2, 3):
                ref = int(tokens[i]) if tokens[i].isdigit() else 0
                if ref and doc.xref_get_key(ref, "FT")[1] == "/Sig":
                    removed += 1
                    continue
                kept.append(" ".join(tokens[i : i + 3]))
            doc.xref_set_key(xref, "Fields", "[" + " ".join(kept) + "]")
    return removed, rects


@dataclass(slots=True)
class _Region:
    """One replaced line of one segment on one page."""

    kind: str  # "text", "hidden", "ocr" (OCR and barcodes)
    lines: list[list[BBox]]
    substitute: str


@dataclass(slots=True)
class _PageWork:
    regions: list[_Region] = field(default_factory=list)
    invisible: list[pymupdf.Rect] = field(default_factory=list)


@register_writer
class PdfWriter(Writer):
    name = "pdf"
    formats = frozenset({"pdf"})

    def write(
        self,
        document: Document,
        plan: ReplacementPlan,
        destination: Path,
        ctx: WriteContext,
    ) -> list[Issue]:
        doc, layers = open_document(document.source)
        try:
            issues = self._write(doc, layers, document, plan, ctx)
            # A fresh identifier: the original one links the output to its source.
            doc.xref_set_key(
                -1, "ID", f"[<{secrets.token_hex(16).upper()}><{secrets.token_hex(16).upper()}>]"
            )
            doc.save(
                destination,
                garbage=4,
                clean=True,
                deflate=True,
                deflate_images=True,
                deflate_fonts=True,
                incremental=False,
                encryption=pymupdf.PDF_ENCRYPT_NONE,
            )
        finally:
            doc.close()
        return issues

    def _write(
        self,
        doc: pymupdf.Document,
        layers: LayerState | None,
        document: Document,
        plan: ReplacementPlan,
        ctx: WriteContext,
    ) -> list[Issue]:
        issues: list[Issue] = []
        by_kind: dict[str, list[tuple[Segment, list[Replacement]]]] = {}
        for seg in document.segments:
            reps = plan.for_segment(seg.id)
            if reps and seg.locator:
                by_kind.setdefault(str(seg.locator[0]), []).append((seg, reps))
        # Form fields first: their indices were computed with every widget present.
        self._widgets(doc, by_kind.get("widget", []))
        signed, signature_rects = remove_signatures(doc)
        if signed or document.properties.get("pdf_signatures"):
            issues.append(
                Issue(
                    Severity.INFO,
                    SIGNATURE_REMOVED,
                    "Signature électronique supprimée : le document anonymisé n'est plus "
                    "signé (la signature d'origine contenait des données personnelles et "
                    "n'aurait plus été valide).",
                )
            )
        blanked = self._images(doc, document, plan, ctx)
        removed_fields = self._redact(doc, document, by_kind, ctx, signature_rects)
        if removed_fields:
            issues.append(
                Issue(
                    Severity.WARNING,
                    FIELD_REMOVED,
                    f"{removed_fields} champ(s) de formulaire supprimé(s) : leur apparence "
                    "contenait des données masquées.",
                )
            )
        for page in doc:
            for annot in list(page.annots() or []):
                if annot.type[0] not in KEPT_ANNOTS:
                    page.delete_annot(annot)
            for link in page.get_links():
                if link.get("kind") in REMOVED_LINKS:
                    page.delete_link(link)
        blanked += self._attachments(doc, document, ctx)
        doc.scrub(
            attached_files=False,
            clean_pages=False,
            embedded_files=False,
            hidden_text=False,
            javascript=True,
            metadata=True,
            redactions=False,
            remove_links=False,
            reset_fields=False,
            reset_responses=True,
            thumbnails=True,
            xml_metadata=True,
        )
        remove_javascript(doc)
        doc.set_metadata({})
        doc.del_xml_metadata()
        doc.set_toc([])
        self._purge_objects(doc)
        # Last: earlier steps (form field updates...) rewrite object dictionaries.
        self._object_strings(doc, by_kind.get("obj", []))
        restore_layers(doc, layers)
        if blanked:
            issues.append(Issue(Severity.WARNING, BLANKED_CODE, blanked_message(blanked)))
        return issues

    @staticmethod
    def _purge_objects(doc: pymupdf.Document) -> None:
        """Metadata streams, private data and XFA forms of every object."""
        catalog = doc.pdf_catalog()
        _delete(doc, catalog, "NeedsRendering")
        form = _acroform(doc)
        if form is not None:
            _delete(doc, form, "XFA", "NeedsRendering")
        for xref in range(1, doc.xref_length()):
            try:
                keys = doc.xref_get_keys(xref)
            except Exception:
                continue
            if any(key in keys for key in PURGED_KEYS):
                _delete(doc, xref, *PURGED_KEYS)

    @staticmethod
    def _object_strings(
        doc: pymupdf.Document, items: Sequence[tuple[Segment, list[Replacement]]]
    ) -> None:
        """Rewrite every planned object string, matched by its original value.

        Matching by value (per object, then document-wide) keeps working after
        PyMuPDF regenerated or reordered a dictionary, and also catches copies.
        """
        per_xref: dict[int, dict[str, str]] = {}
        everywhere: dict[str, str] = {}
        for seg, reps in items:
            new_value = apply_replacements(seg.text, reps)
            per_xref.setdefault(int(seg.locator[1]), {})[seg.text] = new_value
            everywhere.setdefault(seg.text, new_value)
        if not everywhere:
            return
        for xref, source in list(object_sources(doc)):
            mapping = per_xref.get(xref, {})
            out: list[str] = []
            cursor = 0
            for string in iter_strings(source):
                replacement = mapping.get(string.value, everywhere.get(string.value))
                if replacement is None:
                    continue
                out.append(source[cursor : string.start])
                out.append(encode_pdf_string(replacement))
                cursor = string.end
            if out:
                out.append(source[cursor:])
                doc.update_object(xref, "".join(out))

    # -- images -----------------------------------------------------------------

    @staticmethod
    def _images(
        doc: pymupdf.Document, document: Document, plan: ReplacementPlan, ctx: WriteContext
    ) -> int:
        """Redraw images holding replacements, delete unverifiable ones, strip JPEG
        metadata of the others. Returns the number of deleted images."""
        boxes: dict[str, tuple[int, BBox, tuple[BBox, ...]]] = document.properties.get(
            "pdf_image_boxes", {}
        )
        per_image: dict[int, list[Segment]] = {}
        replaced: set[int] = set()
        for seg in document.segments:
            entry = boxes.get(seg.id)
            reps = plan.for_segment(seg.id)
            if entry is None:
                continue
            xref, line_box, char_boxes = entry
            kind = seg.context.kind
            if kind is SegmentKind.BARCODE:
                char_boxes = (line_box,) * len(seg.text)
            pixel_seg = Segment(
                id=seg.id,
                text=seg.text,
                locator=seg.locator,
                context=SegmentContext(kind, bbox=line_box),
                char_boxes=char_boxes,
            )
            per_image.setdefault(xref, []).append(pixel_seg)
            if reps:
                replaced.add(xref)
        for xref, pixel_segs in per_image.items():
            if xref not in replaced:
                continue
            storage = image_kind(doc, xref)
            image = image_pixels(doc, xref)
            canvas = image
            # Lines without replacement are passed too: a replaced word split by
            # the OCR engine over two boxes is masked in both.
            regions = page_redaction_boxes(pixel_segs, plan)
            paint_boxes(canvas, regions, ctx.mode)
            store_image_pixels(doc, xref, canvas, storage)
        blanked = 0
        unextracted = document.properties.get("pdf_unextracted", [])
        # First page showing each image, read once (one page scan per image was
        # quadratic on a long scan without OCR).
        first_page: dict[int, int] = {}
        if unextracted:
            for number, page in enumerate(doc):
                for item in page.get_images(full=True):
                    first_page.setdefault(int(item[0]), number)
        for xref in unextracted:
            if PdfWriter._delete_image(doc, int(xref), first_page):
                blanked += 1
        PdfWriter._strip_jpeg(doc, set(per_image))
        return blanked

    @staticmethod
    def _delete_image(doc: pymupdf.Document, xref: int, first_page: dict[int, int]) -> bool:
        """Replace image ``xref`` by a transparent pixel (every placement: the object
        itself is rewritten, so ``first_page`` stays valid)."""
        number = first_page.get(xref)
        if number is None:
            return False
        doc[number].delete_image(xref)
        return True

    @staticmethod
    def _strip_jpeg(doc: pymupdf.Document, skip: set[int]) -> None:
        for xref in range(1, doc.xref_length()):
            if xref in skip:
                continue
            try:
                if doc.xref_get_key(xref, "Subtype")[1] != "/Image":
                    continue
                filters = doc.xref_get_key(xref, "Filter")[1]
                if filters != "/DCTDecode":
                    continue
                raw = doc.xref_stream_raw(xref)
                stripped = strip_jpeg_metadata(raw)
                if stripped == raw:
                    continue
                params = doc.xref_get_key(xref, "DecodeParms")
                doc.update_stream(xref, stripped, compress=False)
                doc.xref_set_key(xref, "Filter", filters)
                if params[0] != "null":
                    doc.xref_set_key(xref, "DecodeParms", params[1])
            except Exception:
                continue

    # -- page content -----------------------------------------------------------

    @staticmethod
    def _work(
        document: Document,
        by_kind: dict[str, list[tuple[Segment, list[Replacement]]]],
        ctx: WriteContext,
    ) -> dict[int, _PageWork]:
        work: dict[int, _PageWork] = {}
        kinds = (("text", "text"), ("hidden", "hidden"), ("ocr", "ocr"), ("code", "ocr"))
        for locator_kind, region_kind in kinds:
            for seg, reps in by_kind.get(locator_kind, []):
                page_work = work.setdefault(int(seg.locator[1]), _PageWork())
                for rep in reps:
                    if seg.context.kind is SegmentKind.BARCODE and seg.context.bbox is not None:
                        page_work.regions.append(
                            _Region(region_kind, [[seg.context.bbox]], rep.substitute)
                        )
                        continue
                    parts = line_parts(seg, rep)
                    if parts is not None:
                        # One region per line, each with its part of the substitute.
                        page_work.regions.extend(
                            _Region(region_kind, [boxes], text) for boxes, text in parts
                        )
                        continue
                    lines = [g for g in _line_groups(seg, rep) if g]
                    if lines:
                        page_work.regions.append(_Region(region_kind, lines, rep.substitute))
        invisible: dict[int, list[tuple[float, float, float, float]]] = document.properties.get(
            "pdf_invisible", {}
        )
        for page_number, rects in invisible.items():
            if rects:
                work.setdefault(int(page_number), _PageWork()).invisible.extend(
                    pymupdf.Rect(r) for r in rects
                )
        return work

    def _redact(
        self,
        doc: pymupdf.Document,
        document: Document,
        by_kind: dict[str, list[tuple[Segment, list[Replacement]]]],
        ctx: WriteContext,
        signature_rects: dict[int, list[pymupdf.Rect]],
    ) -> int:
        """Apply text, invisible text, OCR and barcode redactions. Returns the
        number of form fields deleted because a masked region covered them."""
        mode = ctx.mode
        fill = (0.0, 0.0, 0.0) if mode is ReplacementMode.BLACKOUT else (1.0, 1.0, 1.0)
        removed_fields = 0
        for page_number, work in self._work(document, by_kind, ctx).items():
            if page_number >= doc.page_count:
                continue
            page = doc[page_number]
            regions = work.regions
            # Painted boxes, measured on the original text layer (neighbouring lines).
            lines = page_lines(page) if any(r.kind == "text" for r in regions) else []
            placements = {
                id(region): [
                    place(full_rect(boxes), lines if region.kind == "text" else [])
                    for boxes in region.lines
                ]
                for region in regions
            }
            # Pass 1: remove glyphs (boxes shrunk so that neighbours survive; OCR
            # regions whole: their glyphs, if any, have no usable text).
            for region in regions:
                for boxes in region.lines:
                    rect = full_rect(boxes) if region.kind == "ocr" else inner_rect(boxes)
                    page.add_redact_annot(rect, fill=False)
            for rect in work.invisible:
                page.add_redact_annot(rect, fill=False)
            page.apply_redactions(
                images=pymupdf.PDF_REDACT_IMAGE_NONE,
                graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                text=pymupdf.PDF_REDACT_TEXT_REMOVE,
            )
            visible = [r for r in regions if r.kind != "hidden"]
            if not visible:
                self._paint_hidden(page, regions, mode)
                continue
            # Pass 2: blank image pixels and covered vector graphics, keep text
            # (within the painted boxes: what lies under the neighbouring lines stays).
            for region in visible:
                for placement in placements[id(region)]:
                    page.add_redact_annot(placement.box, fill=False)
            page.apply_redactions(
                images=pymupdf.PDF_REDACT_IMAGE_PIXELS,
                graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
                text=pymupdf.PDF_REDACT_TEXT_NONE,
            )
            ocr_rects = [full_rect(b) for r in visible if r.kind == "ocr" for b in r.lines]
            if ocr_rects:
                self._remove_vector_glyphs(page, ocr_rects)
                removed_fields += self._remove_covered_fields(page, ocr_rects)
            fields = [pymupdf.Rect(w.rect) for w in page.widgets() or []]
            skipped = fields + signature_rects.get(page_number, [])
            drawn = [
                r
                for r in visible
                if not (
                    r.kind == "text" and any(_inside(full_rect(r.lines[0]), f) for f in skipped)
                )  # a form field redraws its own (anonymized) value
            ]
            layouts: dict[int, TextLayout] = {}
            obstacles: _Obstacles | None = None
            if mode is not ReplacementMode.BLACKOUT:
                layouts, obstacles = self._layouts(page, drawn, placements)
            for region in drawn:
                rects = [p.box for p in placements[id(region)]]
                layout = layouts.get(id(region))
                if layout is not None and layout.width > rects[0].width:
                    # The substitute runs past the word: its box follows it (over
                    # whole glyphs if it had to run into the next word).
                    first = rects[0]
                    right = first.x0 + layout.width
                    if obstacles is not None:
                        right = obstacles.covered(placements[id(region)][0], right)
                    rects[0] = pymupdf.Rect(first.x0, first.y0, right, first.y1)
                for rect in rects:
                    page.draw_rect(rect, color=None, fill=fill, overlay=True, width=0)
                if layout is not None:
                    draw_text(page, layout, region.substitute)
            self._paint_hidden(page, regions, mode)
        return removed_fields

    @staticmethod
    def _layouts(
        page: pymupdf.Page, regions: list[_Region], placements: dict[int, list[Placement]]
    ) -> tuple[dict[int, TextLayout], _Obstacles | None]:
        """Substitute layouts: on the original baseline, at the original size when
        possible; text lines may use the free space after the word (see
        ``layout_text``), OCR boxes stay within themselves. Also returns the
        page's obstacles when they were needed."""
        layouts: dict[int, TextLayout] = {}
        obstacles: _Obstacles | None = None
        for region in regions:
            first = placements[id(region)][0]
            room: float | None = None
            if region.kind == "text":
                natural = pymupdf.get_text_length(
                    region.substitute, fontname="helv", fontsize=first.size
                )
                if natural > first.box.width:
                    if obstacles is None:
                        painted = [p.box for r in regions for p in placements[id(r)]]
                        obstacles = _Obstacles.of(page, painted)
                    room = obstacles.room(first, WORD_GAP * first.size)
            layout = layout_text(
                first.box,
                region.substitute,
                size=first.size,
                baseline=first.baseline,
                room=room,
                overflow=region.kind == "text",
            )
            if layout is not None:
                layouts[id(region)] = layout
        return layouts, obstacles

    @staticmethod
    def _paint_hidden(page: pymupdf.Page, regions: list[_Region], mode: ReplacementMode) -> None:
        """Substitutes of replaced invisible text, written invisibly (searchable)."""
        if mode is ReplacementMode.BLACKOUT:
            return
        for region in regions:
            if region.kind == "hidden":
                rect = full_rect(region.lines[0])
                if rect.height >= 1.0 and page.rect.intersects(rect):
                    fit_text(page, rect, region.substitute, invisible=True)

    @staticmethod
    def _remove_vector_glyphs(page: pymupdf.Page, rects: list[pymupdf.Rect]) -> None:
        """Remove vector paths drawing glyphs under OCR regions (text converted to
        outlines): a box over them would only hide them."""
        try:
            drawings = page.get_drawings()
        except Exception:
            return
        grown = False
        for rect in rects:
            h = max(rect.height, 1.0)
            zone = pymupdf.Rect(rect.x0 - h / 2, rect.y0 - h / 2, rect.x1 + h / 2, rect.y1 + h / 2)
            union = pymupdf.Rect(rect)
            for drawing in drawings:
                box = pymupdf.Rect(drawing.get("rect") or (0, 0, 0, 0))
                if box.is_empty or not box.intersects(rect):
                    continue
                glyph_sized = zone.contains(box)
                # A whole line of outlined text in one path: tall as the line, many
                # segments. Removed whole (privacy first).
                line_like = box.height <= 3 * h and len(drawing.get("items") or ()) >= 4
                if glyph_sized or line_like:
                    union |= box
            if union != rect:
                page.add_redact_annot(union, fill=False)
                grown = True
        if grown:
            page.apply_redactions(
                images=pymupdf.PDF_REDACT_IMAGE_NONE,
                graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
                text=pymupdf.PDF_REDACT_TEXT_NONE,
            )

    @staticmethod
    def _remove_covered_fields(page: pymupdf.Page, rects: list[pymupdf.Rect]) -> int:
        """Delete form fields whose appearance lies under a masked OCR region."""
        removed = 0
        for widget in list(page.widgets() or []):
            area = pymupdf.Rect(widget.rect)
            if any(abs(area & rect) >= 0.3 * abs(rect) for rect in rects if not rect.is_empty):
                page.delete_widget(widget)
                removed += 1
        return removed

    @staticmethod
    def _widgets(doc: pymupdf.Document, items: Sequence[tuple[Segment, list[Replacement]]]) -> None:
        per_widget: dict[tuple[int, int], list[tuple[Segment, list[Replacement]]]] = {}
        for seg, reps in items:
            per_widget.setdefault((int(seg.locator[1]), int(seg.locator[2])), []).append(
                (seg, reps)
            )
        for (page_number, w_index), entries in per_widget.items():
            page = doc[page_number]  # keep a reference: widgets need a live page
            widgets = list(page.widgets() or [])
            if w_index >= len(widgets):
                continue
            widget = widgets[w_index]
            choices = list(widget.choice_values or [])
            for seg, reps in entries:
                new_value = apply_replacements(seg.text, reps)
                prop = str(seg.locator[3])
                if prop == "choice":
                    c_index = int(seg.locator[4])
                    if c_index < len(choices):
                        choices[c_index] = new_value
                else:
                    setattr(widget, prop, new_value)
            if choices:
                widget.choice_values = choices
            widget.update()

    @staticmethod
    def _attachments(doc: pymupdf.Document, document: Document, ctx: WriteContext) -> int:
        blanked = 0
        for name in list(doc.embfile_names()):
            doc.embfile_del(name)
        children = {
            **document.properties.get("pdf_embedded", {}),
            **document.properties.get("pdf_attachments", {}),
        }
        unique = UniqueNames()
        for child_id in children:
            output = ctx.child_outputs.get(child_id)
            if output is None or output.path is None:
                blanked += 1
                continue
            name = unique(Path(output.name).name or "piece_jointe")
            doc.embfile_add(name, output.path.read_bytes(), filename=name, ufilename=name, desc="")
        return blanked
