"""Text and codes of a raster image, shared by the image and PDF extractors.

OCR or barcode failures are never swallowed: they become ERROR issues on the
document (the text of the image could not be analysed, so the file cannot be
considered anonymized).
"""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import TYPE_CHECKING

from anonyma import barcodes
from anonyma.logs import get_logger
from anonyma.models import BBox, Document, OcrEngine, OcrLine, Severity
from anonyma.ocr import proportional_boxes

if TYPE_CHECKING:
    from PIL import Image

    from anonyma.config import Config

log = get_logger("raster")

OCR_FAILED = "ocr.failed"
BARCODE_FAILED = "barcode.failed"
BARCODE_UNAVAILABLE = "barcode.unavailable"
#: Label of barcode segments (no sensitive value).
BARCODE_LABEL = "code-barres"


@dataclass(frozen=True, slots=True)
class TextLine:
    """An OCR line with exactly one box per character."""

    text: str = field(repr=False)
    bbox: BBox
    char_boxes: tuple[BBox, ...] = field(repr=False)


def _issue_once(document: Document, severity: Severity, code: str, message: str) -> None:
    if not any(i.code == code for i in document.issues):
        document.add_issue(severity, code, message)


def recognize(engine: OcrEngine, image: Image.Image, document: Document) -> list[TextLine] | None:
    """OCR lines of ``image`` (RGB), or None (and an ERROR issue) on failure."""
    try:
        lines: Sequence[OcrLine] = engine.recognize(image)
    except Exception as exc:
        log.warning("OCR failed on %s: %s", document.id, type(exc).__name__)
        _issue_once(
            document,
            Severity.ERROR,
            OCR_FAILED,
            "Échec de l'OCR sur une image : son texte n'a pas pu être analysé.",
        )
        return None
    result: list[TextLine] = []
    for line in lines:
        if not line.text.strip():
            continue
        boxes = line.char_boxes
        if not boxes or len(boxes) != len(line.text):
            boxes = proportional_boxes(line.text, line.bbox)
        result.append(TextLine(line.text, line.bbox, tuple(boxes)))
    return result


def decode_codes(image: Image.Image, config: Config, document: Document) -> list[barcodes.Barcode]:
    """QR codes / Data Matrix / barcodes of ``image``."""
    try:
        return barcodes.decode(image)
    except ImportError:
        _issue_once(
            document,
            Severity.WARNING,
            BARCODE_UNAVAILABLE,
            "Lecture des codes-barres indisponible : les QR codes ne sont pas analysés.",
        )
    except Exception as exc:
        log.warning("barcode decoding failed on %s: %s", document.id, type(exc).__name__)
        _issue_once(
            document,
            Severity.ERROR,
            BARCODE_FAILED,
            "Échec de la lecture des codes-barres d'une image : contenu non vérifiable.",
        )
    return []


def code_boxes(code: barcodes.Barcode) -> tuple[BBox, ...]:
    """Every character box of a barcode segment is the whole code region."""
    return (code.bbox,) * len(code.text)


# ---------------------------------------------------------------------------
# Deduplication (OCR of a rendered page against text already extracted)
# ---------------------------------------------------------------------------


def normalized(text: str) -> str:
    """Case- and accent-insensitive alphanumeric skeleton (OCR drops accents)."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in decomposed if c.isalnum())


def covered(text: str, reference: str, threshold: float = 0.85) -> bool:
    """True when (almost) every character of ``text`` appears, in order, in
    ``reference`` (both normalized)."""
    if not text:
        return True
    if not reference:
        return False
    if text in reference:
        return True
    if len(text) < 4:
        return False
    matcher = SequenceMatcher(None, text, reference, autojunk=False)
    matched = sum(block.size for block in matcher.get_matching_blocks())
    return matched >= threshold * len(text)
