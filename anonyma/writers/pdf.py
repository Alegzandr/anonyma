"""PDF writer: true redaction (PyMuPDF), metadata, signatures and active content purged.

* Text-layer spans are redacted line by line. Glyph removal uses a rectangle
  shrunk to the glyph centres (MuPDF removes every glyph whose box merely
  touches the rectangle); a second pass with the full rectangle blanks image
  pixels and removes vector graphics fully covered. The visible box is then
  painted: white with the substitute fitted inside (tags / realistic mode) or
  black (blackout mode).
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

import secrets
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pymupdf
from PIL import Image, ImageDraw, ImageFont

from anonyma.extractors._media import BLANKED_CODE, blanked_message
from anonyma.extractors._pdf import encode_pdf_string, iter_strings, object_sources
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
from anonyma.writers.image import redaction_boxes

KEPT_ANNOTS = frozenset({pymupdf.PDF_ANNOT_LINK, pymupdf.PDF_ANNOT_WIDGET})
REMOVED_LINKS = frozenset(
    {pymupdf.LINK_URI, pymupdf.LINK_LAUNCH, pymupdf.LINK_GOTOR, pymupdf.LINK_NAMED}
)
#: Fraction of the edge characters / line height trimmed from glyph-removal boxes.
H_INSET = 0.3
V_INSET = 0.3
#: Keys removed from every object (private application data, metadata streams).
PURGED_KEYS = ("Metadata", "PieceInfo")

SIGNATURE_REMOVED = "pdf.signature_removed"
FIELD_REMOVED = "pdf.field_removed"


def _rect(box: BBox) -> pymupdf.Rect:
    return pymupdf.Rect(box.x0, box.y0, box.x1, box.y1)


def line_boxes(segment: Segment, rep: Replacement) -> list[list[BBox]]:
    """Character boxes of ``rep`` grouped by line (split on ``\\n``)."""
    boxes = segment.char_boxes or ()
    groups: list[list[BBox]] = [[]]
    for index in range(rep.start, min(rep.end, len(boxes))):
        if segment.text[index] == "\n":
            groups.append([])
            continue
        box = boxes[index]
        if box.x1 > box.x0 and box.y1 > box.y0:
            groups[-1].append(box)
    return [g for g in groups if g]


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


def fit_text(page: pymupdf.Page, rect: pymupdf.Rect, text: str, *, invisible: bool = False) -> None:
    """Write ``text`` inside ``rect``, shrinking the font so that it fits."""
    if not text.strip() or rect.width <= 0 or rect.height <= 0:
        return
    size = rect.height * 0.75
    length = pymupdf.get_text_length(text, fontname="helv", fontsize=size)
    if length > rect.width:
        size *= rect.width / length
    size = max(size, 2.0)
    baseline = rect.y0 + (rect.height + size * 0.7) / 2
    page.insert_text(
        (rect.x0, baseline),
        text,
        fontsize=size,
        fontname="helv",
        color=(0, 0, 0),
        overlay=True,
        render_mode=3 if invisible else 0,
    )


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
        per_image: dict[int, list[tuple[Segment, ReplacementPlan]]] = {}
        for seg in document.segments:
            entry = boxes.get(seg.id)
            reps = plan.for_segment(seg.id)
            if entry is None or not reps:
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
            per_image.setdefault(xref, []).append(
                (pixel_seg, ReplacementPlan(plan.document_id, {seg.id: reps}))
            )
        for xref, entries in per_image.items():
            storage = image_kind(doc, xref)
            image = image_pixels(doc, xref)
            canvas = image
            regions: list[tuple[BBox, str]] = []
            for pixel_seg, sub_plan in entries:
                if pixel_seg.context.kind is SegmentKind.BARCODE and pixel_seg.context.bbox:
                    regions.append((pixel_seg.context.bbox, ""))
                else:
                    regions.extend(redaction_boxes(pixel_seg, sub_plan))
            paint_boxes(canvas, regions, ctx.mode)
            store_image_pixels(doc, xref, canvas, storage)
        blanked = 0
        for xref in document.properties.get("pdf_unextracted", []):
            if PdfWriter._delete_image(doc, int(xref)):
                blanked += 1
        PdfWriter._strip_jpeg(doc, set(per_image))
        return blanked

    @staticmethod
    def _delete_image(doc: pymupdf.Document, xref: int) -> bool:
        """Replace image ``xref`` by a transparent pixel (every placement)."""
        for page in doc:
            if any(item[0] == xref for item in page.get_images(full=True)):
                page.delete_image(xref)
                return True
        return False

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
                        lines = [[seg.context.bbox]]
                    else:
                        lines = line_boxes(seg, rep)
                    if lines:
                        page_work.regions.append(_Region(region_kind, lines, rep.substitute))
        if ctx.config.remove_invisible_text:
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
            # Pass 2: blank image pixels and covered vector graphics, keep text.
            for region in visible:
                for boxes in region.lines:
                    page.add_redact_annot(full_rect(boxes), fill=False)
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
            for region in visible:
                rects = [full_rect(boxes) for boxes in region.lines]
                if region.kind == "text" and any(_inside(rects[0], f) for f in skipped):
                    continue  # a form field redraws its own (anonymized) value
                for rect in rects:
                    page.draw_rect(rect, color=None, fill=fill, overlay=True, width=0)
                if mode is not ReplacementMode.BLACKOUT:
                    fit_text(page, rects[0], region.substitute)
            self._paint_hidden(page, regions, mode)
        return removed_fields

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
        used: set[str] = set()
        for child_id in children:
            output = ctx.child_outputs.get(child_id)
            if output is None or output.path is None:
                blanked += 1
                continue
            name = Path(output.name).name or "piece_jointe"
            base, i = name, 2
            while name.casefold() in used:
                name = f"{Path(base).stem} ({i}){Path(base).suffix}"
                i += 1
            used.add(name.casefold())
            doc.embfile_add(name, output.path.read_bytes(), filename=name, ufilename=name, desc="")
        return blanked
