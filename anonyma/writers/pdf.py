"""PDF writer: true redaction (PyMuPDF), metadata and active content purged.

* Each replaced span is redacted line by line. Glyph removal uses a rectangle
  shrunk to the glyph centres (MuPDF removes every glyph whose box merely
  touches the rectangle); a second pass with the full rectangle blanks image
  pixels and removes vector graphics fully covered. The visible box is then
  painted: white with the substitute fitted inside (tags / realistic mode) or
  black (blackout mode).
* Images are replaced by their anonymized child output, or deleted.
* Purged: document information, XMP, bookmarks, thumbnails, JavaScript and
  actions, every annotation except links and form fields, external links
  (URI, launch, remote), embedded files (re-attached anonymized). Form field
  values/labels and every textual string of every object are anonymized.
* Saved with garbage collection, cleaning and compression, without encryption.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pymupdf

from anonyma.extractors._media import BLANKED_CODE, blanked_message
from anonyma.extractors._pdf import encode_pdf_string, iter_strings, object_sources
from anonyma.extractors.pdf import open_pdf
from anonyma.models import (
    BBox,
    Document,
    Issue,
    Replacement,
    ReplacementMode,
    ReplacementPlan,
    Segment,
    Severity,
    WriteContext,
    Writer,
    apply_replacements,
)
from anonyma.registry import register_writer

KEPT_ANNOTS = frozenset({pymupdf.PDF_ANNOT_LINK, pymupdf.PDF_ANNOT_WIDGET})
REMOVED_LINKS = frozenset(
    {pymupdf.LINK_URI, pymupdf.LINK_LAUNCH, pymupdf.LINK_GOTOR, pymupdf.LINK_NAMED}
)
#: Fraction of the edge characters / line height trimmed from glyph-removal boxes.
H_INSET = 0.3
V_INSET = 0.3


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


def fit_text(page: pymupdf.Page, rect: pymupdf.Rect, text: str) -> None:
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
        (rect.x0, baseline), text, fontsize=size, fontname="helv", color=(0, 0, 0), overlay=True
    )


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
        doc = open_pdf(document.source)
        try:
            issues = self._write(doc, document, plan, ctx)
            doc.save(
                destination,
                garbage=4,
                clean=True,
                deflate=True,
                deflate_images=True,
                deflate_fonts=True,
                encryption=pymupdf.PDF_ENCRYPT_NONE,
            )
        finally:
            doc.close()
        return issues

    def _write(
        self, doc: pymupdf.Document, document: Document, plan: ReplacementPlan, ctx: WriteContext
    ) -> list[Issue]:
        issues: list[Issue] = []
        by_kind: dict[str, list[tuple[Segment, list[Replacement]]]] = {}
        for seg in document.segments:
            reps = plan.for_segment(seg.id)
            if reps and seg.locator:
                by_kind.setdefault(str(seg.locator[0]), []).append((seg, reps))
        blanked = self._images(doc, document, ctx)
        self._redact(doc, by_kind.get("text", []) + by_kind.get("ocr", []), ctx.mode)
        self._widgets(doc, by_kind.get("widget", []))
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
        # Last: earlier steps (form field updates...) rewrite object dictionaries.
        self._object_strings(doc, by_kind.get("obj", []))
        if blanked:
            issues.append(Issue(Severity.WARNING, BLANKED_CODE, blanked_message(blanked)))
        return issues

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

    @staticmethod
    def _images(doc: pymupdf.Document, document: Document, ctx: WriteContext) -> int:
        blanked = 0
        images: dict[str, tuple[int, int]] = document.properties.get("pdf_images", {})
        for child_id, (xref, page_number) in images.items():
            page = doc[page_number]
            output = ctx.child_outputs.get(child_id)
            if output is not None and output.path is not None:
                page.replace_image(xref, filename=str(output.path))
            else:
                page.delete_image(xref)
                blanked += 1
        for xref, page_number in document.properties.get("pdf_unextracted", []):
            doc[page_number].delete_image(xref)
            blanked += 1
        return blanked

    @staticmethod
    def _redact(
        doc: pymupdf.Document,
        items: Sequence[tuple[Segment, list[Replacement]]],
        mode: ReplacementMode,
    ) -> None:
        per_page: dict[int, list[tuple[bool, list[list[BBox]], str]]] = {}
        for seg, reps in items:
            is_ocr = seg.locator[0] == "ocr"
            for rep in reps:
                lines = line_boxes(seg, rep)
                if lines:
                    per_page.setdefault(int(seg.locator[1]), []).append(
                        (is_ocr, lines, rep.substitute)
                    )
        fill = (0.0, 0.0, 0.0) if mode is ReplacementMode.BLACKOUT else (1.0, 1.0, 1.0)
        for page_number, entries in per_page.items():
            page = doc[page_number]
            # Text extraction includes form field appearances: their anonymized
            # value is redrawn by the field itself, so nothing is painted there.
            fields = [pymupdf.Rect(w.rect) for w in page.widgets() or []]
            # Pass 1: remove glyphs (boxes shrunk so that neighbours survive).
            for is_ocr, lines, _sub in entries:
                for boxes in lines:
                    rect = full_rect(boxes) if is_ocr else inner_rect(boxes)
                    page.add_redact_annot(rect, fill=False)
            page.apply_redactions(
                images=pymupdf.PDF_REDACT_IMAGE_NONE,
                graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                text=pymupdf.PDF_REDACT_TEXT_REMOVE,
            )
            # Pass 2: blank image pixels and covered vector graphics, keep text.
            for _is_ocr, lines, _sub in entries:
                for boxes in lines:
                    page.add_redact_annot(full_rect(boxes), fill=False)
            page.apply_redactions(
                images=pymupdf.PDF_REDACT_IMAGE_PIXELS,
                graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
                text=pymupdf.PDF_REDACT_TEXT_NONE,
            )
            for _is_ocr, lines, substitute in entries:
                rects = [full_rect(boxes) for boxes in lines]
                if any(_inside(rects[0], field) for field in fields):
                    continue
                for rect in rects:
                    page.draw_rect(rect, color=None, fill=fill, overlay=True, width=0)
                if mode is not ReplacementMode.BLACKOUT:
                    fit_text(page, rects[0], substitute)

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
