"""Image writer: opaque boxes over every replaced OCR span and barcode, all metadata
stripped.

EXIF orientation is applied to the pixels, every page/frame is kept, the format
and (as far as possible) the compression settings are preserved; only the ICC
profile survives. In tag mode the tag is drawn inside the box when it fits.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont, JpegImagePlugin

from anonyma.extractors.image import IMAGE_FORMATS, prepare_frame
from anonyma.models import (
    BBox,
    Document,
    Issue,
    ReplacementMode,
    ReplacementPlan,
    Segment,
    SegmentKind,
    WriteContext,
    Writer,
)
from anonyma.registry import register_writer

#: Box padding relative to the line height (OCR character cells are approximate).
PAD_X = 0.3
PAD_Y = 0.15

_PIL_FORMATS = {
    "png": "PNG",
    "jpeg": "JPEG",
    "tiff": "TIFF",
    "bmp": "BMP",
    "webp": "WEBP",
    "heic": "HEIF",
    "gif": "GIF",
}


def drawable(frame: Image.Image, fmt: str) -> Image.Image:
    """Convert to a mode that supports opaque drawing and the output format."""
    if frame.mode in ("P", "PA"):
        has_alpha = frame.mode == "PA" or "transparency" in frame.info
        return frame.convert("RGBA" if has_alpha and fmt != "jpeg" else "RGB")
    if frame.mode in ("YCbCr", "LAB", "HSV"):
        return frame.convert("RGB")
    if frame.mode.startswith("I;16"):
        return frame.convert("I")
    if fmt in ("jpeg", "bmp") and frame.mode in ("RGBA", "LA", "RGBa", "La"):
        return frame.convert("RGB" if frame.mode.startswith("RGB") else "L")
    if fmt in ("webp", "heic", "gif") and frame.mode not in ("RGB", "RGBA", "L"):
        return frame.convert("RGBA" if "A" in frame.mode else "RGB")
    return frame


def _colors(mode: str) -> tuple[Any, Any]:
    """(box fill, text fill) for ``mode``."""
    if mode == "1":
        return 0, 1
    if mode in ("L", "I", "F"):
        return 0, 255
    if mode == "LA":
        return (0, 255), (255, 255)
    if mode == "RGBA":
        return (0, 0, 0, 255), (255, 255, 255, 255)
    if mode == "CMYK":
        return (0, 0, 0, 255), (0, 0, 0, 0)
    return (0, 0, 0), (255, 255, 255)


def _word_span(text: str, start: int, end: int) -> tuple[int, int]:
    """``[start, end)`` widened to the whitespace-delimited words it touches."""
    while start > 0 and not text[start - 1].isspace():
        start -= 1
    while end < len(text) and not text[end].isspace():
        end += 1
    return start, end


def _padded(box: BBox, height: float, char_width: float) -> BBox:
    """``box`` with a safety margin: OCR character cells are approximate (the last
    characters of a long run are often placed short)."""
    dx = PAD_X * height + 0.5 * char_width
    dy = PAD_Y * height
    return BBox(box.x0 - dx, box.y0 - dy, box.x1 + dx, box.y1 + dy)


def _char_width(segment: Segment) -> float:
    line = segment.context.bbox
    if line is None or not segment.text:
        return 0.0
    return (line.x1 - line.x0) / len(segment.text)


def redaction_boxes(segment: Segment, plan: ReplacementPlan) -> list[tuple[BBox, str]]:
    """Boxes over the replaced spans of one OCR line, each widened to the whole OCR
    words it touches (a value is never partly masked) plus a safety margin."""
    boxes: list[tuple[BBox, str]] = []
    line = segment.context.bbox
    if segment.context.kind is SegmentKind.BARCODE:
        # The whole code (its bbox already includes a quiet-zone margin).
        return [(line, "")] if line is not None and plan.for_segment(segment.id) else []
    for rep in plan.for_segment(segment.id):
        start, end = _word_span(segment.text, rep.start, rep.end)
        box = segment.span_bbox(start, end) or line
        if box is None:
            continue
        if line is not None:
            box = BBox(box.x0, min(box.y0, line.y0), box.x1, max(box.y1, line.y1))
        height = max(box.y1 - box.y0, 1.0)
        boxes.append((_padded(box, height, _char_width(segment)), rep.substitute))
    return boxes


def _same_row(a: BBox, b: BBox) -> bool:
    overlap = min(a.y1, b.y1) - max(a.y0, b.y0)
    return overlap >= 0.5 * min(a.y1 - a.y0, b.y1 - b.y0)


def _continuations(segment: Segment, others: Sequence[Segment], *, after: bool) -> list[Segment]:
    """OCR lines that continue ``segment`` on the same row with no visible gap
    (the engine split one token into two boxes: ``...moreau(`` + ``@societe-exe``)."""
    line = segment.context.bbox
    if line is None:
        return []
    height = max(line.y1 - line.y0, 1.0)
    found = []
    for other in others:
        box = other.context.bbox
        if other is segment or box is None or not _same_row(line, box):
            continue
        gap = box.x0 - line.x1 if after else line.x0 - box.x1
        if -0.5 * height <= gap <= 0.6 * height:
            found.append(other)
    return found


def page_redaction_boxes(
    segments: Sequence[Segment], plan: ReplacementPlan
) -> list[tuple[BBox, str]]:
    """Redaction boxes of every OCR line and barcode of one image. A replaced word
    that touches the end (start) of its line also masks the first (last) word of an
    OCR line that continues it on the same row."""
    boxes: list[tuple[BBox, str]] = []
    ocr_lines = [s for s in segments if s.context.kind is SegmentKind.OCR]
    for segment in segments:
        boxes.extend(redaction_boxes(segment, plan))
        if segment.context.kind is not SegmentKind.OCR:
            continue
        text = segment.text
        for rep in plan.for_segment(segment.id):
            start, end = _word_span(text, rep.start, rep.end)
            sides = []
            if not text[end:].strip():
                sides.append(True)
            if not text[:start].strip():
                sides.append(False)
            for after in sides:
                for other in _continuations(segment, ocr_lines, after=after):
                    words = other.text.strip()
                    if not words:
                        continue
                    if after:
                        lead = len(other.text) - len(other.text.lstrip())
                        span = _word_span(other.text, lead, lead + 1)
                    else:
                        tail = len(other.text.rstrip())
                        span = _word_span(other.text, tail - 1, tail)
                    box = other.span_bbox(*span) or other.context.bbox
                    if box is None:
                        continue
                    height = max(box.y1 - box.y0, 1.0)
                    boxes.append((_padded(box, height, _char_width(other)), ""))
    return boxes


def redact(frame: Image.Image, boxes: list[tuple[BBox, str]], mode: ReplacementMode) -> None:
    draw = ImageDraw.Draw(frame)
    fill, text_fill = _colors(frame.mode)
    width, height = frame.size
    for box, label in boxes:
        x0, y0 = max(0, int(box.x0)), max(0, int(box.y0))
        x1, y1 = min(width, int(box.x1 + 0.999)), min(height, int(box.y1 + 0.999))
        if x1 <= x0 or y1 <= y0:
            continue
        draw.rectangle((x0, y0, x1 - 1, y1 - 1), fill=fill)
        if mode is ReplacementMode.TAG and label and frame.mode != "1":
            size = max(8, int((y1 - y0) * 0.55))
            try:
                font: Any = ImageFont.load_default(size=size)
            except (OSError, TypeError):
                continue
            text_box = draw.textbbox((0, 0), label, font=font)
            text_w, text_h = text_box[2] - text_box[0], text_box[3] - text_box[1]
            if text_w <= x1 - x0 and text_h <= y1 - y0:
                position = (x0 + (x1 - x0 - text_w) / 2, y0 + (y1 - y0 - text_h) / 2 - text_box[1])
                draw.text(position, label, fill=text_fill, font=font)


def _webp_lossless(source: Path) -> bool:
    with source.open("rb") as fh:
        head = fh.read(4096)
    return b"VP8L" in head[12:200] and b"VP8 " not in head[12:200]


def save_frames(
    frames: list[Image.Image], fmt: str, destination: Path, original: Image.Image, source: Path
) -> None:
    pil_format = _PIL_FORMATS[fmt]
    params: dict[str, Any] = {}
    icc = original.info.get("icc_profile")
    if icc and fmt != "bmp":
        params["icc_profile"] = icc
    first, rest = frames[0], frames[1:]
    if fmt == "jpeg":
        params.update(_jpeg_params(original, first))
    elif fmt == "tiff":
        compression = original.info.get("compression", "raw")
        params["compression"] = compression
    elif fmt == "webp":
        if _webp_lossless(source):
            params["lossless"] = True
        else:
            params["quality"] = 90
        if rest:
            params["duration"] = original.info.get("duration", 100)
            params["loop"] = original.info.get("loop", 0)
    elif fmt == "heic":
        params.update(quality=90, exif=None, xmp=None)
    elif fmt in ("png", "gif") and rest:
        params["duration"] = original.info.get("duration", 100)
        params["loop"] = original.info.get("loop", 0)
    if rest:
        params.update(save_all=True, append_images=rest)
    try:
        first.save(destination, pil_format, **params)
    except (OSError, ValueError, KeyError):
        if fmt != "tiff":
            raise
        params["compression"] = "tiff_lzw" if first.mode != "1" else "group4"
        first.save(destination, pil_format, **params)


def _jpeg_params(original: Image.Image, frame: Image.Image) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if original.format == "JPEG" and frame.mode == original.mode:
        quantization = getattr(original, "quantization", None)
        if quantization:
            params["qtables"] = quantization
        sampling = JpegImagePlugin.get_sampling(original)
        if sampling != -1:
            params["subsampling"] = sampling
    else:
        params["quality"] = 95
    if original.info.get("progressive") or original.info.get("progression"):
        params["progressive"] = True
    return params


@register_writer
class ImageWriter(Writer):
    name = "image"
    formats = IMAGE_FORMATS

    def write(
        self,
        document: Document,
        plan: ReplacementPlan,
        destination: Path,
        ctx: WriteContext,
    ) -> list[Issue]:
        by_page: dict[int, list[Segment]] = {}
        for segment in document.segments:
            if segment.context.kind in (SegmentKind.OCR, SegmentKind.BARCODE):
                by_page.setdefault(segment.context.page or 0, []).append(segment)
        with Image.open(document.source) as image:
            frames: list[Image.Image] = []
            for page in range(getattr(image, "n_frames", 1) or 1):
                image.seek(page)
                frame = drawable(prepare_frame(image), document.format)
                boxes = page_redaction_boxes(by_page.get(page, []), plan)
                if boxes:
                    redact(frame, boxes, ctx.mode)
                frame.info = {}
                frames.append(frame)
            image.seek(0)
            save_frames(frames, document.format, destination, image, document.source)
        return []
