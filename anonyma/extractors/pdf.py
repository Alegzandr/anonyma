"""PDF extractor (PyMuPDF).

Every layer is made visible first (hidden optional content is analysed too).
Segments:

* text layer, one segment per block (lines joined by ``\\n``) with per-character
  boxes. Characters nobody can see (render mode 3, transparent, zero/tiny size,
  outside the page, white on white, hidden layer) go to separate HIDDEN
  segments: they are analysed (they often hold identifiers that propagate) and
  the writer deletes them when ``config.remove_invisible_text``;
* every page is rendered once (``config.ocr_full_pages``, scanned pages, vector
  only pages) and OCR'd; OCR lines whose text is already known (text layer,
  OCR of the image they lie on) are dropped, so OCR segments only hold text
  that exists purely visually (images, vector glyphs, stamps, annotation and
  form appearances, fonts without Unicode mapping). QR codes / barcodes of the
  rendered page become BARCODE segments. Without an OCR engine a scanned page
  is an ERROR;
* every image XObject (pages, forms, any layer) is decoded with its soft mask
  and OCR'd at its own resolution (hidden or clipped parts included), its codes
  decoded; segments carry page boxes (first placement) and the writer redraws
  the image itself from pixel boxes (``properties["pdf_image_boxes"]``). JPEG
  metadata (EXIF, XMP...) is exposed;
* document information, XMP and every metadata stream, bookmarks, annotations,
  link targets, form fields (XFA included), embedded file descriptions and every
  textual string of every PDF object; digital signatures (entries and
  certificate fields) as SIGNATURE segments;
* embedded files and file attachment annotations become children.
"""

from __future__ import annotations

import io
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pymupdf
from PIL import Image

from anonyma.codec import chunk_text
from anonyma.extractors._pdf import (
    MIN_IMAGE_SIDE,
    MIN_TEXT_CHARS,
    OCR_DPI,
    SCAN_COVERAGE,
    TEXT_FLAGS,
    exposed_strings,
    has_drawings,
    object_sources,
    page_coverage,
    pixel_rect_to_page,
    split_box,
    to_bbox,
)
from anonyma.extractors._pdfobjects import (
    LayerState,
    dss_certificates,
    image_box_to_page,
    image_pixels,
    is_signature_dict,
    open_document,
    overlaps,
    page_rect_to_pixels,
    signature_texts,
    xfa_streams,
)
from anonyma.extractors._raster import (
    BARCODE_LABEL,
    TextLine,
    code_boxes,
    covered,
    decode_codes,
    normalized,
    recognize,
)
from anonyma.extractors.image import metadata_items, ocr_view
from anonyma.models import (
    BBox,
    Document,
    ExtractionContext,
    Extractor,
    OcrEngine,
    Segment,
    SegmentContext,
    SegmentKind,
    Severity,
)
from anonyma.registry import register_extractor

#: Rendering is capped to this many pixels per page (memory bound).
MAX_RENDER_PIXELS = 60_000_000
#: Longest side of a rendered page: RapidOCR downscales anything larger, so a
#: bigger render only costs time (A4 -> about 170 dpi).
RENDER_MAX_SIDE = 2000
#: Glyphs smaller than this (points) cannot be read.
MIN_VISIBLE_SIZE = 1.0
#: A white glyph on a region whose darkest rendered pixel is at least this light
#: is white on white.
WHITE_LEVEL = 245
FILLED_OR_STROKED = pymupdf.mupdf.FZ_STEXT_FILLED | pymupdf.mupdf.FZ_STEXT_STROKED


def open_pdf(source: Path) -> pymupdf.Document:
    """Open ``source`` with every layer visible (see ``open_document``)."""
    return open_document(source)[0]


@dataclass(slots=True)
class PageText:
    """Text layer of one page."""

    segments: list[Segment] = field(default_factory=list)
    visible: int = 0  # visible non-space characters
    #: Every text-layer character (visible or not), for OCR deduplication.
    chars: list[tuple[str, BBox]] = field(default_factory=list)
    #: Rectangles of invisible text the writer may delete (no visible glyph inside).
    invisible: list[tuple[float, float, float, float]] = field(default_factory=list)


@dataclass(slots=True)
class _Run:
    """Characters of one block, visible or not."""

    chars: list[str] = field(default_factory=list)
    boxes: list[BBox] = field(default_factory=list)

    def add_line(self, chars: list[str], boxes: list[BBox]) -> None:
        if not chars:
            return
        if self.chars:
            last = self.boxes[-1]
            self.chars.append("\n")
            self.boxes.append(BBox(last.x1, last.y0, last.x1, last.y1))
        self.chars.extend(chars)
        self.boxes.extend(boxes)


def _is_white(color: int) -> bool:
    return all(((color >> shift) & 0xFF) >= 0xF0 for shift in (16, 8, 0))


def _dot(box: BBox) -> BBox:
    """A degenerate glyph box made into a tiny rectangle (zero-size fonts)."""
    if box.x1 - box.x0 > 0.01 and box.y1 - box.y0 > 0.01:
        return box
    cx, cy = (box.x0 + box.x1) / 2, (box.y0 + box.y1) / 2
    return BBox(cx - 0.25, cy - 0.25, cx + 0.25, cy + 0.25)


class _Visibility:
    """Decides whether a text-layer character can be seen on the rendered page."""

    def __init__(
        self,
        page: pymupdf.Page,
        render: Image.Image | None,
        zoom: float,
        hidden_layers: frozenset[str],
    ) -> None:
        self.page = page
        # Text coordinates are relative to the crop box (unrotated).
        self.area = pymupdf.Rect(0, 0, page.cropbox.width, page.cropbox.height)
        self.render = render.convert("L") if render is not None else None
        self.zoom = zoom
        self.layer_boxes: list[pymupdf.Rect] = []
        if hidden_layers:
            for span in page.get_texttrace():
                if span.get("layer") in hidden_layers:
                    self.layer_boxes.append(pymupdf.Rect(span["bbox"]))

    def span_hidden(self, span: dict[str, Any]) -> bool:
        if span.get("alpha", 255) == 0:
            return True
        flags = span.get("char_flags")
        if isinstance(flags, int) and not flags & FILLED_OR_STROKED:
            return True
        if _is_white(int(span.get("color", 0))) and self.render is not None:
            return self._on_white(pymupdf.Rect(span["bbox"]))
        return False

    def _on_white(self, rect: pymupdf.Rect) -> bool:
        assert self.render is not None
        pixels = page_rect_to_pixels(self.page, rect, self.zoom) & pymupdf.Rect(
            0, 0, self.render.width, self.render.height
        )
        if pixels.is_empty:
            return True
        region = self.render.crop(
            (int(pixels.x0), int(pixels.y0), int(pixels.x1) + 1, int(pixels.y1) + 1)
        )
        darkest = region.getextrema()[0]
        return isinstance(darkest, (int, float)) and darkest >= WHITE_LEVEL

    def char_hidden(self, box: BBox) -> bool:
        rect = pymupdf.Rect(box.x0, box.y0, box.x1, box.y1)
        centre = pymupdf.Point((box.x0 + box.x1) / 2, (box.y0 + box.y1) / 2)
        if not self.area.contains(centre) and not rect.intersects(self.area):
            return True  # outside the visible page area
        return any(r.contains(centre) for r in self.layer_boxes)


def _segment_from_run(
    run: _Run,
    seg_id: str,
    locator: tuple[str | int, ...],
    kind: SegmentKind,
    label: str,
    page: int,
) -> Segment | None:
    text = "".join(run.chars)
    if not text.strip():
        return None
    bbox = run.boxes[0]
    for box in run.boxes[1:]:
        bbox = bbox.union(box)
    return Segment(
        id=seg_id,
        text=text,
        locator=locator,
        context=SegmentContext(kind, label=label, page=page, bbox=bbox),
        char_boxes=tuple(run.boxes),
    )


def text_layer(
    page: pymupdf.Page,
    render: Image.Image | None = None,
    zoom: float = 1.0,
    hidden_layers: frozenset[str] = frozenset(),
) -> PageText:
    """Visible block segments, invisible text segments and removal rectangles."""
    result = PageText()
    visibility = _Visibility(page, render, zoom, hidden_layers)
    data = page.get_text("rawdict", flags=TEXT_FLAGS, clip=pymupdf.INFINITE_RECT())
    visible_boxes: list[BBox] = []
    hidden_lines: list[list[BBox]] = []
    label = f"page {page.number + 1}"
    for b_index, block in enumerate(data.get("blocks", [])):
        if block.get("type") != 0:
            continue
        shown, hidden = _Run(), _Run()
        for line in block.get("lines", []):
            line_runs: dict[bool, tuple[list[str], list[BBox]]] = {True: ([], []), False: ([], [])}
            for span in line.get("spans", []):
                if float(span.get("size", 0.0)) < MIN_VISIBLE_SIZE:
                    continue  # read from the text trace (rawdict drops zero-size glyphs)
                span_hidden = visibility.span_hidden(span)
                for char in span.get("chars", []):
                    c = char.get("c", "")
                    if not c:
                        continue
                    box = to_bbox(char["bbox"])
                    is_hidden = span_hidden or visibility.char_hidden(box)
                    chars, boxes = line_runs[is_hidden]
                    chars.extend(c)
                    boxes.extend(split_box(box, len(c)) if len(c) > 1 else [box])
            for is_hidden, (chars, boxes) in line_runs.items():
                if not any(ch.isalnum() for ch in chars):
                    continue  # punctuation-only lines cannot carry personal data
                (hidden if is_hidden else shown).add_line(chars, boxes)
                result.chars.extend(zip(chars, boxes, strict=True))
                if is_hidden:
                    hidden_lines.append(boxes)
                else:
                    visible_boxes.extend(boxes)
        seg = _segment_from_run(
            shown,
            f"t{page.number}_{b_index}",
            ("text", page.number, b_index),
            SegmentKind.BODY,
            label,
            page.number,
        )
        if seg is not None:
            result.segments.append(seg)
            result.visible += sum(1 for c in seg.text if not c.isspace())
        seg = _segment_from_run(
            hidden,
            f"h{page.number}_{b_index}",
            ("hidden", page.number, b_index),
            SegmentKind.HIDDEN,
            f"texte invisible {label}",
            page.number,
        )
        if seg is not None:
            result.segments.append(seg)
    # Zero-size and tiny glyphs are missing from rawdict: take them from the trace.
    for t_index, span in enumerate(page.get_texttrace()):
        if float(span.get("size", 0.0)) >= MIN_VISIBLE_SIZE:
            continue
        run = _Run()
        chars = [chr(ch[0]) for ch in span.get("chars", ()) if ch[0] > 0]
        boxes = [_dot(to_bbox(ch[3])) for ch in span.get("chars", ()) if ch[0] > 0]
        if not any(ch.isalnum() for ch in chars):
            continue
        run.add_line(chars, boxes)
        seg = _segment_from_run(
            run,
            f"z{page.number}_{t_index}",
            ("hidden", page.number, f"z{t_index}"),
            SegmentKind.HIDDEN,
            f"texte invisible {label}",
            page.number,
        )
        if seg is not None:
            result.segments.append(seg)
            result.chars.extend(zip(chars, boxes, strict=True))
            hidden_lines.append(boxes)
    result.invisible = _removal_rects(hidden_lines, visible_boxes)
    return result


def _removal_rects(
    lines: Iterable[list[BBox]], visible: list[BBox]
) -> list[tuple[float, float, float, float]]:
    """Rectangles deleting invisible glyphs without touching a visible one.

    Each rectangle is shrunk towards the glyph centres (redaction removes every
    glyph its rectangle touches); glyphs overlapping visible text are left to the
    ordinary anonymization (removing them would remove the visible text too).
    """
    rects: list[tuple[float, float, float, float]] = []
    for boxes in lines:
        for raw in boxes:
            box = _dot(raw)
            dx, dy = (box.x1 - box.x0) * 0.3, (box.y1 - box.y0) * 0.3
            inner = BBox(box.x0 + dx, box.y0 + dy, box.x1 - dx, box.y1 - dy)
            if any(overlaps(inner, v) for v in visible):
                continue
            rects.append((inner.x0, inner.y0, inner.x1, inner.y1))
    return rects


def render_page(page: pymupdf.Page) -> tuple[Image.Image, float]:
    """The page (annotations and form fields included) as an RGB image, and its zoom."""
    longest = max(page.rect.width, page.rect.height, 1.0)
    zoom = min(OCR_DPI / 72, RENDER_MAX_SIDE / longest)
    pixels = abs(page.rect) * zoom * zoom
    if pixels > MAX_RENDER_PIXELS:
        zoom *= (MAX_RENDER_PIXELS / pixels) ** 0.5
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False, annots=True)
    if pix.n != 3:
        pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples), zoom


def _page_box(page: pymupdf.Page, box: BBox, zoom: float) -> BBox:
    return to_bbox(pixel_rect_to_page(page, box, zoom))


def _reference(region: BBox, known: Iterable[tuple[str, BBox]]) -> str:
    """Normalized text of the known characters/lines lying in ``region``."""
    pad = 0.2 * (region.y1 - region.y0)
    area = BBox(region.x0 - pad, region.y0 - pad, region.x1 + pad, region.y1 + pad)
    parts = []
    for text, box in known:
        cx, cy = (box.x0 + box.x1) / 2, (box.y0 + box.y1) / 2
        if area.x0 <= cx <= area.x1 and area.y0 <= cy <= area.y1:
            parts.append(text)
    return normalized("".join(parts))


def page_ocr_segments(
    page: pymupdf.Page,
    lines: list[TextLine],
    zoom: float,
    known_chars: list[tuple[str, BBox]],
    known_lines: list[tuple[str, BBox]],
) -> list[Segment]:
    """OCR segments of a rendered page, minus lines already known."""
    segments: list[Segment] = []
    for i, line in enumerate(lines):
        bbox = _page_box(page, line.bbox, zoom)
        text = normalized(line.text)
        if not text:
            continue
        reference = _reference(bbox, known_chars)
        lines_ref = "".join(normalized(t) for t, box in known_lines if overlaps(box, bbox))
        if covered(text, reference + lines_ref):
            continue
        segments.append(
            Segment(
                id=f"o{page.number}_{i}",
                text=line.text,
                locator=("ocr", page.number, i),
                context=SegmentContext(
                    SegmentKind.OCR,
                    label=f"page {page.number + 1} (OCR)",
                    page=page.number,
                    bbox=bbox,
                ),
                char_boxes=tuple(_page_box(page, box, zoom) for box in line.char_boxes),
            )
        )
    return segments


def _segment(
    seg_id: str, text: str, locator: tuple[str | int, ...], kind: SegmentKind, label: str
) -> Segment:
    return Segment(id=seg_id, text=text, locator=locator, context=SegmentContext(kind, label))


@dataclass(slots=True)
class _ImageText:
    """What the native OCR of one image XObject found (pixel coordinates)."""

    size: tuple[int, int]
    lines: list[tuple[str, BBox]] = field(default_factory=list)
    codes: list[tuple[str, BBox]] = field(default_factory=list)


@register_extractor
class PdfExtractor(Extractor):
    name = "pdf"
    extensions = {".pdf": "pdf"}

    @classmethod
    def sniff(cls, head: bytes) -> str | None:
        return "pdf" if b"%PDF-" in head[:1024] else None

    def extract(self, source: Path, document: Document, ctx: ExtractionContext) -> None:
        try:
            doc, layers = open_document(source)
        except PermissionError:
            document.add_issue(
                Severity.ERROR,
                "extract.encrypted",
                "PDF protégé par mot de passe : impossible à lire.",
            )
            return
        try:
            self._extract(doc, layers, document, ctx)
        finally:
            doc.close()

    def _extract(
        self,
        doc: pymupdf.Document,
        layers: LayerState | None,
        document: Document,
        ctx: ExtractionContext,
    ) -> None:
        config = ctx.config
        segments = document.segments
        hidden_layers = layers.hidden if layers else frozenset()
        images: dict[int, _ImageText] = {}
        image_boxes: dict[str, tuple[int, BBox, tuple[BBox, ...]]] = {}
        unverified: list[int] = []
        invisible: dict[int, list[tuple[float, float, float, float]]] = {}
        missing_ocr: list[int] = []
        engine: OcrEngine | None = None
        engine_loaded = False
        for page in doc:
            ctx.check_cancelled()
            if config.ocr_full_pages and not engine_loaded:
                engine, engine_loaded = ctx.ocr, True
            render: Image.Image | None = None
            zoom = 1.0
            if config.decode_barcodes or (config.ocr_full_pages and engine is not None):
                render, zoom = render_page(page)
            page_text = text_layer(page, render, zoom, hidden_layers)
            scanned = page_text.visible < MIN_TEXT_CHARS and page_coverage(page) >= SCAN_COVERAGE
            vector_only = page_text.visible == 0 and has_drawings(page)
            wants_ocr = config.ocr_full_pages or scanned or vector_only
            if wants_ocr and not engine_loaded:
                engine, engine_loaded = ctx.ocr, True
            if wants_ocr and engine is not None and render is None:
                render, zoom = render_page(page)
            segments.extend(page_text.segments)
            invisible[page.number] = page_text.invisible

            # Image XObjects of the page (forms included), at their own resolution:
            # hidden or clipped parts are read too.
            known_lines: list[tuple[str, BBox]] = []
            known_codes: list[tuple[str, BBox]] = []
            for item in page.get_images(full=True):
                xref, width, height = item[0], item[2], item[3]
                if xref <= 0 or width < MIN_IMAGE_SIDE or height < MIN_IMAGE_SIDE:
                    continue
                if xref not in images and xref not in unverified:
                    if not engine_loaded:
                        engine, engine_loaded = ctx.ocr, True
                    ctx.budget.consume(int(width) * int(height) * 4, depth=ctx.depth + 1)
                    found = self._image(doc, page, xref, engine, document, ctx, image_boxes)
                    if found is None:
                        unverified.append(xref)
                        continue
                    images[xref] = found
                info = images.get(xref)
                if info is None:
                    continue
                for _rect, matrix in self._placements(page, xref):
                    known_lines.extend(
                        (t, image_box_to_page(b, info.size, matrix)) for t, b in info.lines
                    )
                    known_codes.extend(
                        (t, image_box_to_page(b, info.size, matrix)) for t, b in info.codes
                    )

            if wants_ocr and engine is not None and render is not None:
                if not self._only_known_images(page, images):
                    lines = recognize(engine, render, document)
                    if lines is not None:
                        segments.extend(
                            page_ocr_segments(page, lines, zoom, page_text.chars, known_lines)
                        )
            elif wants_ocr and scanned:
                missing_ocr.append(page.number + 1)
            if render is not None:
                for c_index, code in enumerate(decode_codes(render, config, document)):
                    bbox = _page_box(page, code.bbox, zoom)
                    if any(t == code.text and overlaps(b, bbox) for t, b in known_codes):
                        continue
                    segments.append(
                        Segment(
                            id=f"c{page.number}_{c_index}",
                            text=code.text,
                            locator=("code", page.number, c_index),
                            context=SegmentContext(
                                SegmentKind.BARCODE,
                                label=f"{BARCODE_LABEL} page {page.number + 1}",
                                page=page.number,
                                bbox=bbox,
                            ),
                            char_boxes=(bbox,) * len(code.text),
                        )
                    )
        if engine is None and (unverified or (config.ocr_full_pages and doc.page_count)):
            # Pages not rendered for OCR, images not read: the output cannot be
            # fully verified (any ``ocr.*`` issue means "vérification incomplète").
            document.add_issue(
                Severity.WARNING,
                "ocr.unavailable",
                "OCR désactivé ou indisponible : le texte des images et des pages rendues "
                "n'a pas pu être analysé (les images sont supprimées).",
            )
        if missing_ocr:
            document.add_issue(
                Severity.ERROR,
                "pdf.ocr_unavailable",
                f"OCR indisponible : {len(missing_ocr)} page(s) numérisée(s) ne peuvent pas "
                "être analysées.",
            )
        self._metadata(doc, segments)
        attachments = self._annotations(doc, document, ctx)
        embedded = self._embedded(doc, document, ctx)
        signatures = self._signatures(doc, segments)
        for xref, source in object_sources(doc):
            if xref in signatures:
                continue  # binary /Contents; readable entries exposed as SIGNATURE
            for s_index, string in exposed_strings(source):
                segments.append(
                    _segment(
                        f"s{xref}_{s_index}",
                        string.value,
                        ("obj", xref, s_index),
                        SegmentKind.HIDDEN,
                        "objet PDF",
                    )
                )
        document.properties["pdf_image_boxes"] = image_boxes
        document.properties["pdf_unextracted"] = unverified
        document.properties["pdf_invisible"] = invisible
        document.properties["pdf_attachments"] = attachments
        document.properties["pdf_embedded"] = embedded
        document.properties["pdf_signatures"] = sorted(signatures)

    @staticmethod
    def _only_known_images(page: pymupdf.Page, images: dict[int, _ImageText]) -> bool:
        """True when everything visible on ``page`` is an image already OCR'd at
        its own resolution (typical scan, invisible OCR text included): a page
        OCR would only read the same text again."""
        if page.first_annot is not None or page.first_widget is not None:
            return False
        try:
            log = page.get_bboxlog()
            infos = page.get_image_info(xrefs=True)
        except Exception:
            return False
        if any(kind not in ("fill-image", "ignore-text") for kind, _rect in log):
            return False
        placed = sum(1 for kind, _rect in log if kind == "fill-image")
        return bool(infos) and placed == len(infos) and all(i["xref"] in images for i in infos)

    @staticmethod
    def _placements(page: pymupdf.Page, xref: int) -> list[tuple[pymupdf.Rect, pymupdf.Matrix]]:
        try:
            return list(page.get_image_rects(xref, transform=True))
        except Exception:
            return []

    def _image(
        self,
        doc: pymupdf.Document,
        page: pymupdf.Page,
        xref: int,
        engine: OcrEngine | None,
        document: Document,
        ctx: ExtractionContext,
        image_boxes: dict[str, tuple[int, BBox, tuple[BBox, ...]]],
    ) -> _ImageText | None:
        """OCR and codes of one image XObject; None when it cannot be verified."""
        if engine is None:
            return None
        try:
            pixels = image_pixels(doc, xref)
        except Exception:
            return None
        self._image_metadata(doc, xref, document.segments)
        info = _ImageText(pixels.size)
        placements = self._placements(page, xref)
        matrix = placements[0][1] if placements else None
        number = page.number
        label = f"image page {number + 1}"

        def page_boxes(boxes: tuple[BBox, ...]) -> tuple[BBox, ...] | None:
            if matrix is None:
                return None
            return tuple(image_box_to_page(b, pixels.size, matrix) for b in boxes)

        lines = recognize(engine, ocr_view(pixels), document)
        if lines is None:
            return None
        for i, line in enumerate(lines):
            seg_id = f"i{xref}_{i}"
            mapped = page_boxes(line.char_boxes)
            line_box = page_boxes((line.bbox,))
            document.segments.append(
                Segment(
                    id=seg_id,
                    text=line.text,
                    locator=("image", xref, i),
                    context=SegmentContext(
                        SegmentKind.OCR,
                        label=f"{label} (OCR)",
                        page=number,
                        bbox=line_box[0] if line_box else None,
                    ),
                    char_boxes=mapped,
                )
            )
            image_boxes[seg_id] = (xref, line.bbox, line.char_boxes)
            info.lines.append((line.text, line.bbox))
        for c_index, code in enumerate(decode_codes(pixels, ctx.config, document)):
            seg_id = f"ic{xref}_{c_index}"
            mapped = page_boxes((code.bbox,))
            document.segments.append(
                Segment(
                    id=seg_id,
                    text=code.text,
                    locator=("image_code", xref, c_index),
                    context=SegmentContext(
                        SegmentKind.BARCODE,
                        label=f"{BARCODE_LABEL} {label}",
                        page=number,
                        bbox=mapped[0] if mapped else None,
                    ),
                    char_boxes=(mapped[0],) * len(code.text) if mapped else None,
                )
            )
            image_boxes[seg_id] = (xref, code.bbox, code_boxes(code))
            info.codes.append((code.text, code.bbox))
        return info

    @staticmethod
    def _image_metadata(doc: pymupdf.Document, xref: int, segments: list[Segment]) -> None:
        if "DCTDecode" not in doc.xref_get_key(xref, "Filter")[1]:
            return
        try:
            with Image.open(io.BytesIO(doc.xref_stream_raw(xref))) as image:
                items = metadata_items(image)
        except Exception:
            return
        for i, (label, text) in enumerate(items):
            segments.append(
                _segment(
                    f"im{xref}_{i}", text, ("image_meta", xref, i), SegmentKind.METADATA, label
                )
            )

    @staticmethod
    def _metadata(doc: pymupdf.Document, segments: list[Segment]) -> None:
        for key, value in (doc.metadata or {}).items():
            if key in ("format", "encryption") or not isinstance(value, str) or not value.strip():
                continue
            segments.append(_segment(f"m_{key}", value, ("meta", key), SegmentKind.METADATA, key))
        xmp = doc.get_xml_metadata() or ""
        for i, (_offset, chunk) in enumerate(chunk_text(xmp) if xmp.strip() else []):
            segments.append(_segment(f"xmp{i}", chunk, ("xmp", i), SegmentKind.METADATA, "XMP"))
        # Metadata streams of pages, images, fonts... and the XFA form description.
        catalog_xmp = doc.xref_get_key(doc.pdf_catalog(), "Metadata")
        catalog_ref = int(catalog_xmp[1].split()[0]) if catalog_xmp[0] == "xref" else -1
        try:
            streams = [(x, SegmentKind.HIDDEN, "formulaire XFA") for x in xfa_streams(doc)]
        except Exception:
            streams = []
        for xref in range(1, doc.xref_length()):
            if xref == catalog_ref:
                continue
            try:
                if doc.xref_get_key(xref, "Type")[1] == "/Metadata" and doc.xref_is_stream(xref):
                    streams.append((xref, SegmentKind.METADATA, "XMP"))
            except Exception:
                continue
        for xref, kind, label in streams:
            try:
                data = doc.xref_stream(xref) or b""
            except Exception:
                continue
            text = data.decode("utf-8", "replace")
            for i, (_offset, chunk) in enumerate(chunk_text(text) if text.strip() else []):
                segments.append(_segment(f"ms{xref}_{i}", chunk, ("stream", xref, i), kind, label))
        for i, entry in enumerate(doc.get_toc(simple=True)):
            title = str(entry[1])
            if title.strip():
                segments.append(
                    _segment(f"toc{i}", title, ("toc", i), SegmentKind.HIDDEN, "signet")
                )

    @staticmethod
    def _signatures(doc: pymupdf.Document, segments: list[Segment]) -> set[int]:
        """Signature dictionaries (exposed as SIGNATURE segments) and DSS certificates."""
        found: set[int] = set()
        for xref in range(1, doc.xref_length()):
            if not is_signature_dict(doc, xref):
                continue
            found.add(xref)
            try:
                texts = signature_texts(doc, xref)
            except Exception:
                texts = []
            for i, (label, text) in enumerate(texts):
                segments.append(
                    _segment(
                        f"sig{xref}_{i}",
                        text,
                        ("sig", xref, i),
                        SegmentKind.SIGNATURE,
                        f"signature électronique ({label})",
                    )
                )
        try:
            dss = dss_certificates(doc)
        except Exception:
            dss = []
        for i, text in enumerate(dss):
            segments.append(
                _segment(
                    f"dss{i}",
                    text,
                    ("dss", i),
                    SegmentKind.SIGNATURE,
                    "signature électronique (DSS)",
                )
            )
        return found

    @staticmethod
    def _annotations(
        doc: pymupdf.Document, document: Document, ctx: ExtractionContext
    ) -> dict[str, str]:
        attachments: dict[str, str] = {}
        segments = document.segments
        for page in doc:
            label = f"page {page.number + 1}"
            for a_index, annot in enumerate(page.annots() or []):
                info: dict[str, Any] = annot.info or {}
                for key in ("content", "title", "subject"):
                    value = info.get(key)
                    if isinstance(value, str) and value.strip():
                        segments.append(
                            _segment(
                                f"a{page.number}_{a_index}_{key}",
                                value,
                                ("annot", page.number, a_index, key),
                                SegmentKind.COMMENT,
                                f"annotation {label}",
                            )
                        )
                if annot.type[0] == pymupdf.PDF_ANNOT_FILE_ATTACHMENT:
                    try:
                        data = annot.get_file()
                        name = str(annot.file_info.get("filename") or "piece_jointe")
                    except Exception:
                        continue
                    ctx.budget.consume(len(data), depth=ctx.depth + 1)
                    child = ctx.extract_child(data, name, document)
                    attachments[child.id] = name
            for l_index, link in enumerate(page.get_links()):
                target = link.get("uri") or link.get("file")
                if isinstance(target, str) and target.strip():
                    segments.append(
                        _segment(
                            f"l{page.number}_{l_index}",
                            target,
                            ("link", page.number, l_index),
                            SegmentKind.ATTRIBUTE,
                            f"lien {label}",
                        )
                    )
            for w_index, widget in enumerate(page.widgets() or []):
                for prop in ("field_name", "field_value", "field_label"):
                    value = getattr(widget, prop, None)
                    if isinstance(value, str) and value.strip():
                        segments.append(
                            _segment(
                                f"w{page.number}_{w_index}_{prop}",
                                value,
                                ("widget", page.number, w_index, prop),
                                SegmentKind.HIDDEN,
                                f"champ de formulaire {label}",
                            )
                        )
                for c_index, choice in enumerate(widget.choice_values or []):
                    value = choice if isinstance(choice, str) else " ".join(map(str, choice))
                    if value.strip():
                        segments.append(
                            _segment(
                                f"w{page.number}_{w_index}_c{c_index}",
                                value,
                                ("widget", page.number, w_index, "choice", c_index),
                                SegmentKind.HIDDEN,
                                f"champ de formulaire {label}",
                            )
                        )
        return attachments

    @staticmethod
    def _embedded(
        doc: pymupdf.Document, document: Document, ctx: ExtractionContext
    ) -> dict[str, str]:
        embedded: dict[str, str] = {}
        for e_index, name in enumerate(doc.embfile_names()):
            info = doc.embfile_info(name)
            desc = info.get("desc") or info.get("description") or ""
            if isinstance(desc, str) and desc.strip():
                document.segments.append(
                    _segment(
                        f"e{e_index}",
                        desc,
                        ("embedded", e_index),
                        SegmentKind.HIDDEN,
                        "description de pièce jointe",
                    )
                )
            data = doc.embfile_get(name)
            ctx.budget.consume(len(data), depth=ctx.depth + 1)
            filename = str(info.get("ufilename") or info.get("filename") or name)
            child = ctx.extract_child(data, filename, document)
            embedded[child.id] = name
        return embedded
