"""PDF extractor (PyMuPDF).

Segments:

* one per text block (lines joined by ``\\n``), with per-character boxes;
* scanned pages (little text, large images) are rendered and OCR'd: one OCR
  segment per recognised line, boxes mapped back to page coordinates. Without an
  OCR engine such a page is an ERROR (the file cannot be verified);
* document information, XMP, bookmarks, annotations, link targets, form fields,
  embedded file descriptions and every textual string of every PDF object
  (structure tree alt text, names...);
* images of text pages and embedded files become children.
"""

from __future__ import annotations

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


def open_pdf(source: Path) -> pymupdf.Document:
    doc = pymupdf.open(source, filetype="pdf")
    if doc.needs_pass and not doc.authenticate(""):
        doc.close()
        raise PermissionError("PDF chiffré")
    return doc


def text_segments(page: pymupdf.Page) -> tuple[list[Segment], int]:
    """Block segments of ``page`` and the number of visible characters."""
    segments: list[Segment] = []
    visible = 0
    data = page.get_text("rawdict", flags=TEXT_FLAGS)
    for b_index, block in enumerate(data.get("blocks", [])):
        if block.get("type") != 0:
            continue
        chars: list[str] = []
        boxes: list[BBox] = []
        for line in block.get("lines", []):
            line_chars: list[str] = []
            line_boxes: list[BBox] = []
            for span in line.get("spans", []):
                for char in span.get("chars", []):
                    c = char.get("c", "")
                    if not c:
                        continue
                    box = to_bbox(char["bbox"])
                    line_chars.extend(c)
                    line_boxes.extend(split_box(box, len(c)) if len(c) > 1 else [box])
            if not any(c.isalnum() for c in line_chars):
                continue  # punctuation-only lines cannot carry personal data
            if chars:
                last = boxes[-1]
                chars.append("\n")
                boxes.append(BBox(last.x1, last.y0, last.x1, last.y1))
            chars.extend(line_chars)
            boxes.extend(line_boxes)
        text = "".join(chars)
        if not text.strip():
            continue
        visible += sum(1 for c in text if not c.isspace())
        segments.append(
            Segment(
                id=f"t{page.number}_{b_index}",
                text=text,
                locator=("text", page.number, b_index),
                context=SegmentContext(
                    SegmentKind.BODY,
                    label=f"page {page.number + 1}",
                    page=page.number,
                    bbox=to_bbox(block["bbox"]),
                ),
                char_boxes=tuple(boxes),
            )
        )
    return segments, visible


def ocr_segments(page: pymupdf.Page, engine: OcrEngine) -> list[Segment]:
    zoom = OCR_DPI / 72
    pixels = abs(page.rect) * zoom * zoom
    if pixels > MAX_RENDER_PIXELS:
        zoom *= (MAX_RENDER_PIXELS / pixels) ** 0.5
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
    image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    segments: list[Segment] = []
    for i, line in enumerate(engine.recognize(image)):
        if not line.text.strip():
            continue
        boxes = line.char_boxes
        if boxes is None or len(boxes) != len(line.text):
            boxes = tuple(split_box(line.bbox, len(line.text)))
        mapped = tuple(to_bbox(pixel_rect_to_page(page, box, zoom)) for box in boxes)
        segments.append(
            Segment(
                id=f"o{page.number}_{i}",
                text=line.text,
                locator=("ocr", page.number, i),
                context=SegmentContext(
                    SegmentKind.OCR,
                    label=f"page {page.number + 1} (OCR)",
                    page=page.number,
                    bbox=to_bbox(pixel_rect_to_page(page, line.bbox, zoom)),
                ),
                char_boxes=mapped,
            )
        )
    return segments


def _segment(
    seg_id: str, text: str, locator: tuple[str | int, ...], kind: SegmentKind, label: str
) -> Segment:
    return Segment(id=seg_id, text=text, locator=locator, context=SegmentContext(kind, label))


@register_extractor
class PdfExtractor(Extractor):
    name = "pdf"
    extensions = {".pdf": "pdf"}

    @classmethod
    def sniff(cls, head: bytes) -> str | None:
        return "pdf" if b"%PDF-" in head[:1024] else None

    def extract(self, source: Path, document: Document, ctx: ExtractionContext) -> None:
        try:
            doc = open_pdf(source)
        except PermissionError:
            document.add_issue(
                Severity.ERROR,
                "extract.encrypted",
                "PDF protégé par mot de passe : impossible à lire.",
            )
            return
        try:
            self._extract(doc, document, ctx)
        finally:
            doc.close()

    def _extract(self, doc: pymupdf.Document, document: Document, ctx: ExtractionContext) -> None:
        segments = document.segments
        images: dict[str, tuple[int, int]] = {}
        unextracted: list[tuple[int, int]] = []
        seen_images: set[int] = set()
        missing_ocr: list[int] = []
        for page in doc:
            ctx.check_cancelled()
            page_segments, visible = text_segments(page)
            segments.extend(page_segments)
            ocr_done = False
            if visible < MIN_TEXT_CHARS:
                scanned = page_coverage(page) >= SCAN_COVERAGE
                if scanned or (visible == 0 and has_drawings(page)):
                    engine = ctx.ocr
                    if engine is not None:
                        segments.extend(ocr_segments(page, engine))
                        ocr_done = True
                    elif scanned:
                        missing_ocr.append(page.number + 1)
            if ocr_done:
                continue
            for item in page.get_images(full=True):
                xref, width, height = item[0], item[2], item[3]
                if xref in seen_images or xref <= 0:
                    continue
                seen_images.add(xref)
                if width < MIN_IMAGE_SIDE or height < MIN_IMAGE_SIDE:
                    continue
                try:
                    info = doc.extract_image(xref)
                except Exception:
                    info = None
                if not info or not info.get("image"):
                    unextracted.append((xref, page.number))
                    continue
                data: bytes = info["image"]
                ctx.budget.consume(len(data), depth=ctx.depth + 1)
                child = ctx.extract_child(data, f"image{xref}.{info.get('ext', 'bin')}", document)
                images[child.id] = (xref, page.number)
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
        for xref, source in object_sources(doc):
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
        document.properties["pdf_images"] = images
        document.properties["pdf_unextracted"] = unextracted
        document.properties["pdf_attachments"] = attachments
        document.properties["pdf_embedded"] = embedded

    @staticmethod
    def _metadata(doc: pymupdf.Document, segments: list[Segment]) -> None:
        for key, value in (doc.metadata or {}).items():
            if key in ("format", "encryption") or not isinstance(value, str) or not value.strip():
                continue
            segments.append(_segment(f"m_{key}", value, ("meta", key), SegmentKind.METADATA, key))
        xmp = doc.get_xml_metadata() or ""
        for i, (_offset, chunk) in enumerate(chunk_text(xmp) if xmp.strip() else []):
            segments.append(_segment(f"xmp{i}", chunk, ("xmp", i), SegmentKind.METADATA, "XMP"))
        for i, entry in enumerate(doc.get_toc(simple=True)):
            title = str(entry[1])
            if title.strip():
                segments.append(
                    _segment(f"toc{i}", title, ("toc", i), SegmentKind.HIDDEN, "signet")
                )

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
