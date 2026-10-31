"""Embedded media handling shared by container formats (OOXML, ODF, RTF).

A container writer must never embed original bytes of a child that could not be
anonymized. Images are replaced by :data:`PLACEHOLDER_PNG` (a blank 1x1 PNG),
other objects by empty bytes. Extractors skip both, so that re-reading an output
never turns a placeholder into an unsupported child.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import PurePosixPath


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    body = kind + data
    return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)


def _blank_png() -> bytes:
    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)  # 1x1, 8-bit RGB
    raw = b"\x00\xff\xff\xff"  # filter byte + one white pixel
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(raw, 9))
        + _png_chunk(b"IEND", b"")
    )


#: Blank 1x1 white PNG used in place of images that could not be anonymized.
PLACEHOLDER_PNG = _blank_png()

IMAGE_EXTENSIONS = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".jpe",
        ".jfif",
        ".gif",
        ".bmp",
        ".dib",
        ".tif",
        ".tiff",
        ".emf",
        ".wmf",
        ".emz",
        ".wmz",
        ".svg",
        ".svm",
        ".wdp",
        ".jxr",
        ".hdp",
        ".pcx",
        ".pict",
        ".pct",
        ".webp",
        ".heic",
        ".heif",
    }
)

#: Content types of the extensions a writer may introduce when renaming parts.
CONTENT_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".emf": "image/x-emf",
    ".wmf": "image/x-wmf",
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".xml": "application/xml",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".odt": "application/vnd.oasis.opendocument.text",
    ".ods": "application/vnd.oasis.opendocument.spreadsheet",
    ".odp": "application/vnd.oasis.opendocument.presentation",
}


def is_placeholder(data: bytes) -> bool:
    """True for bytes a writer put in place of an omitted child."""
    return not data or data == PLACEHOLDER_PNG


def is_image_name(name: str) -> bool:
    return PurePosixPath(name).suffix.lower() in IMAGE_EXTENSIONS


def suffix(name: str) -> str:
    """Lower-case extension, dotfiles included (``_rels/.rels`` -> ``.rels``)."""
    base = PurePosixPath(name).name
    dot = base.rfind(".")
    return base[dot:].lower() if dot >= 0 else ""


BLANKED_CODE = "office.media_blanked"


def blanked_message(count: int) -> str:
    return (
        f"{count} élément(s) intégré(s) (image, objet) non anonymisable(s) : remplacé(s) "
        "par un contenu vide."
    )
