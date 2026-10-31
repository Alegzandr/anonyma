"""Raster images: OCR text (per line, with character boxes) and metadata.

Formats: png, jpeg, tiff (every page), gif (every frame), bmp, heic/heif, webp.
Text is recognised by ``ctx.ocr`` (vertical text included); without an OCR engine
the image cannot be verified and gets an ERROR, as does an OCR failure. QR codes,
Data Matrix and barcodes become BARCODE segments (``config.decode_barcodes``).
Metadata (EXIF incl. XP* and UserComment, GPS, XMP, IPTC, PNG text chunks, JPEG
comments, Photoshop resources) is exposed as METADATA segments; the writer strips
all of it anyway (ICC profiles excepted).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from PIL import ExifTags, Image, ImageOps, IptcImagePlugin

from anonyma.extractors._empty import empty_file
from anonyma.extractors._raster import BARCODE_LABEL, code_boxes, decode_codes, recognize
from anonyma.models import (
    Document,
    ExtractionContext,
    Extractor,
    Segment,
    SegmentContext,
    SegmentKind,
    Severity,
)
from anonyma.registry import register_extractor

try:  # HEIC/HEIF support (optional at import time, required for the heic format)
    import pillow_heif

    pillow_heif.register_heif_opener()
    HEIF_AVAILABLE = True
except ImportError:  # pragma: no cover - dependency declared in pyproject
    HEIF_AVAILABLE = False

IMAGE_EXTENSIONS = {
    ".png": "png",
    ".jpg": "jpeg",
    ".jpeg": "jpeg",
    ".jpe": "jpeg",
    ".jfif": "jpeg",
    ".tif": "tiff",
    ".tiff": "tiff",
    ".bmp": "bmp",
    ".dib": "bmp",
    ".gif": "gif",
    ".webp": "webp",
    **({".heic": "heic", ".heif": "heic", ".hif": "heic"} if HEIF_AVAILABLE else {}),
}
IMAGE_FORMATS = frozenset(IMAGE_EXTENSIONS.values())
_HEIF_BRANDS = (b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1")
_BMP_HEADERS = (12, 40, 52, 56, 64, 108, 124)

_XP_TAGS = {0x9C9B, 0x9C9C, 0x9C9D, 0x9C9E, 0x9C9F}
_SKIPPED_EXIF = {0x8769, 0x8825, 0xA005, 0x927C, 0x02BC, 0x83BB, 0x8649, 0x8773, 0x0112}
_IGNORED_INFO = {
    "icc_profile", "exif", "xmp", "XML:com.adobe.xmp", "dpi", "jfif", "jfif_version",
    "jfif_unit", "jfif_density", "transparency", "duration", "loop", "background",
    "compression", "progressive", "progression", "adobe", "adobe_transform", "photoshop",
    "mp", "mpoffset", "gamma", "srgb", "chromaticity", "aspect", "interlace", "resolution",
    "timestamp", "blend", "disposal", "default_image", "bitmap", "lossless", "version",
    "extension", "background_color", "frame_offset", "palette", "flags", "hdr", "depth",
    "bit_depth", "chroma", "primary", "thumbnails", "depth_images", "aux", "metadata",
    "original_orientation", "stride", "heif", "save_all", "exif_offset",
}  # fmt: skip
_PRINTABLE = re.compile(rb"[\x20-\x7e\xa0-\xff]{4,}")


def sniff_image(head: bytes) -> str | None:
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if HEIF_AVAILABLE and head[4:8] == b"ftyp" and head[8:12] in _HEIF_BRANDS:
        return "heic"
    if (
        head[:2] == b"BM"
        and len(head) >= 18
        and head[6:10] == b"\x00\x00\x00\x00"
        and int.from_bytes(head[14:18], "little") in _BMP_HEADERS
    ):
        return "bmp"
    return None


def prepare_frame(image: Image.Image) -> Image.Image:
    """The current frame, EXIF orientation applied, as a new image without info."""
    frame = image.copy()
    try:
        transposed = ImageOps.exif_transpose(frame)
        if transposed is not None:
            frame = transposed
    except Exception:  # broken EXIF: keep the raw orientation
        pass
    if frame.mode in ("P", "PA") and (frame.mode == "PA" or "transparency" in frame.info):
        frame = frame.convert("RGBA")
    frame.info = {}
    return frame


def ocr_view(frame: Image.Image) -> Image.Image:
    """RGB view for OCR: alpha flattened on white, high bit depths scaled."""
    if frame.mode in ("I;16", "I;16L", "I;16B", "I;16N", "I", "F"):
        gray = frame.convert("I") if frame.mode != "F" else frame
        extrema: Any = gray.getextrema()
        lo, hi = float(extrema[0]), float(extrema[1])
        span = (hi - lo) or 1.0
        frame = gray.point(lambda v: (v - lo) * 255.0 / span).convert("L")
    if frame.mode in ("RGBA", "LA", "PA") or (frame.mode == "P" and "transparency" in frame.info):
        rgba = frame.convert("RGBA")
        background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        return Image.alpha_composite(background, rgba).convert("RGB")
    return frame.convert("RGB")


def _demojibake(text: str) -> str:
    """Pillow decodes EXIF/TIFF ASCII values as latin-1; UTF-8 writers are common."""
    if text.isascii():
        return text
    try:
        return text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def _text_value(value: Any) -> str | None:
    if isinstance(value, str):
        return _demojibake(value).strip("\x00 ") or None
    if isinstance(value, bytes):
        try:
            text = value.decode("utf-8")
        except UnicodeDecodeError:
            text = value.decode("latin-1")
        text = text.strip("\x00 ")
        return text if text and text.isprintable() else None
    if (
        isinstance(value, (tuple, list))
        and value
        and all(isinstance(v, (str, bytes)) for v in value)
    ):
        parts = [t for v in value if (t := _text_value(v))]
        return " ; ".join(parts) or None
    return None


def _user_comment(value: Any) -> str | None:
    if not isinstance(value, bytes):
        return _text_value(value)
    code, body = value[:8], value[8:]
    if code.startswith(b"UNICODE"):
        for codec in ("utf-16-be", "utf-16-le"):
            try:
                text = body.decode(codec).strip("\x00 ")
            except UnicodeDecodeError:
                continue
            if text.isprintable():
                return text or None
        return None
    return _text_value(body if code.startswith((b"ASCII", b"JIS", b"\x00")) else value)


def _xp(value: Any) -> str | None:
    data = bytes(value) if isinstance(value, (bytes, tuple, list)) else None
    if data is None:
        return _text_value(value)
    try:
        return data.decode("utf-16-le").strip("\x00 ") or None
    except UnicodeDecodeError:
        return None


def _gps_text(gps: Any) -> str | None:
    try:

        def coord(values: Any, ref: Any) -> float:
            d, m, s = (float(v) for v in values)
            sign = -1 if str(ref).upper() in ("S", "W") else 1
            return sign * (d + m / 60 + s / 3600)

        lat = coord(gps[2], gps.get(1, "N"))
        lon = coord(gps[4], gps.get(3, "E"))
        return f"GPS {lat:.6f}, {lon:.6f}"
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        values = [str(v) for v in gps.values()] if hasattr(gps, "values") else []
        return ("GPS " + " ".join(values)) if values else None


def _xmp_text(data: Any) -> str | None:
    if isinstance(data, bytes):
        data = data.decode("utf-8", "replace")
    if not isinstance(data, str):
        return None
    values = re.findall(r">([^<>]+)<", data) + re.findall(r'\w+:\w+="([^"]*)"', data)
    import html

    text = "\n".join(html.unescape(v).strip() for v in values if v.strip())
    return text or None


def metadata_items(image: Image.Image) -> list[tuple[str, str]]:
    """Every textual metadata value as ``(label, text)``."""
    items: list[tuple[str, str]] = []
    try:
        exif = image.getexif()
    except Exception:
        exif = None
    if exif:
        for tag, value in exif.items():
            if tag in _SKIPPED_EXIF:
                continue
            name = ExifTags.TAGS.get(tag, f"0x{tag:04X}")
            text = _xp(value) if tag in _XP_TAGS else _text_value(value)
            if text:
                items.append((f"EXIF {name}", text))
        for ifd_tag in (0x8769, 0xA005):
            try:
                ifd = exif.get_ifd(ifd_tag)
            except Exception:
                continue
            for tag, value in ifd.items():
                name = ExifTags.TAGS.get(tag, f"0x{tag:04X}")
                if tag == 0x9286:
                    text = _user_comment(value)
                elif tag == 0x927C and isinstance(value, bytes):
                    text = "\n".join(m.decode("latin-1") for m in _PRINTABLE.findall(value)) or None
                else:
                    text = _text_value(value)
                if text:
                    items.append((f"EXIF {name}", text))
        try:
            gps = exif.get_ifd(0x8825)
        except Exception:
            gps = {}
        if gps:
            text = _gps_text(gps)
            if text:
                items.append(("EXIF GPS", text))
    xmp = image.info.get("xmp") or image.info.get("XML:com.adobe.xmp")
    if xmp is None and hasattr(image, "tag_v2"):
        xmp = image.tag_v2.get(700)
    xmp_text = _xmp_text(xmp)
    if xmp_text:
        items.append(("XMP", xmp_text))
    try:
        iptc = IptcImagePlugin.getiptcinfo(image)  # type: ignore[arg-type]
    except Exception:
        iptc = None
    for (record, dataset), value in (iptc or {}).items():
        text = _text_value(value)
        if text:
            items.append((f"IPTC {record}:{dataset}", text))
    text_chunks = getattr(image, "text", None)
    seen_keys: set[str] = set()
    if isinstance(text_chunks, dict):
        for key, value in text_chunks.items():
            if key in ("XML:com.adobe.xmp",):
                continue
            text = _text_value(str(value))
            if text:
                items.append((f"PNG {key}"[:60], text))
                seen_keys.add(key)
    photoshop = image.info.get("photoshop")
    if isinstance(photoshop, dict):
        for res_id, value in photoshop.items():
            if res_id == 0x0404 or not isinstance(value, bytes):
                continue  # IPTC handled above
            strings = [m.decode("latin-1") for m in _PRINTABLE.findall(value)]
            if strings:
                items.append((f"Photoshop 0x{res_id:04X}", "\n".join(strings)))
    for key, value in image.info.items():
        if not isinstance(key, str) or key in _IGNORED_INFO or key in seen_keys:
            continue
        text = _text_value(value)
        if text:
            items.append((f"info {key}"[:60], text))
    unique: list[tuple[str, str]] = []
    for item in items:
        if item not in unique:
            unique.append(item)
    return unique


@register_extractor
class ImageExtractor(Extractor):
    name = "image"
    extensions = IMAGE_EXTENSIONS

    @classmethod
    def sniff(cls, head: bytes) -> str | None:
        return sniff_image(head)

    def extract(self, source: Path, document: Document, ctx: ExtractionContext) -> None:
        if empty_file(source, document):
            return
        with Image.open(source) as image:
            for i, (label, text) in enumerate(metadata_items(image)):
                document.segments.append(
                    Segment(
                        id=f"m{i}",
                        text=text,
                        locator=("meta", i),
                        context=SegmentContext(SegmentKind.METADATA, label=label),
                    )
                )
                if label == "EXIF GPS":
                    document.add_issue(
                        Severity.INFO,
                        "image.gps",
                        "Coordonnées GPS présentes dans l'image : elles sont supprimées.",
                    )
            frames = getattr(image, "n_frames", 1) or 1
            document.properties["frames"] = frames
            engine = ctx.ocr
            if engine is None:
                document.add_issue(
                    Severity.ERROR,
                    "ocr.unavailable",
                    "OCR désactivé ou indisponible : le texte de l'image ne peut pas être vérifié.",
                )
            for page in range(frames):
                ctx.check_cancelled()
                image.seek(page)
                frame = prepare_frame(image)
                label = f"page {page + 1}" if frames > 1 else "image"
                view = ocr_view(frame)
                lines = recognize(engine, view, document) if engine is not None else None
                for i, line in enumerate(lines or []):
                    document.segments.append(
                        Segment(
                            id=f"o{page}.{i}",
                            text=line.text,
                            locator=("ocr", page, i),
                            context=SegmentContext(
                                SegmentKind.OCR, label=label, page=page, bbox=line.bbox
                            ),
                            char_boxes=line.char_boxes,
                        )
                    )
                for i, code in enumerate(decode_codes(view, ctx.config, document)):
                    document.segments.append(
                        Segment(
                            id=f"c{page}.{i}",
                            text=code.text,
                            locator=("code", page, i),
                            context=SegmentContext(
                                SegmentKind.BARCODE,
                                label=f"{BARCODE_LABEL} {label}",
                                page=page,
                                bbox=code.bbox,
                            ),
                            char_boxes=code_boxes(code),
                        )
                    )
