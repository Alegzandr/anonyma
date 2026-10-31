"""OCR engine factory: RapidOCR on ONNX Runtime (CPU), fully offline.

``rapidocr_onnxruntime`` (Apache-2.0) ships its PP-OCRv4 detection, direction
classification and recognition models inside the wheel: nothing is downloaded at
runtime. Its default recognition model is the Chinese/English one, which reads
Latin letters but drops some French accents (detection still matches because the
pipeline compares accent-insensitively).

When the optional PP-OCRv5 Latin recognition model (Apache-2.0, 32 Latin-script
languages including French) has been installed by the model manager under
``models/ocr-latin-ppocrv5/`` (files ``languages/latin/rec.onnx`` and
``languages/latin/dict.txt`` of the Hugging Face repository
``monkt/paddleocr-onnx``), it replaces the default recognition model.

``create_engine(config)`` returns an :class:`~anonyma.models.OcrEngine` or None
when OCR is disabled or unavailable.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

from anonyma import paths
from anonyma.config import Config
from anonyma.logs import get_logger
from anonyma.models import BBox, OcrEngine, OcrLine

if TYPE_CHECKING:
    from PIL import Image

log = get_logger("ocr")

LATIN_MODEL_ID = "ocr-latin-ppocrv5"
LATIN_REC_MODEL = "languages/latin/rec.onnx"
LATIN_DICT = "languages/latin/dict.txt"
#: Lines recognised with a lower confidence are dropped (recall first: low value).
MIN_LINE_SCORE = 0.3


def latin_model_files(root: Path | None = None) -> tuple[Path, Path] | None:
    """``(rec.onnx, dict.txt)`` of the installed Latin model, or None."""
    folder = (root or paths.models_dir()) / LATIN_MODEL_ID
    model, keys = folder / LATIN_REC_MODEL, folder / LATIN_DICT
    if model.is_file() and keys.is_file():
        return model, keys
    return None


def _box(points: Any) -> BBox:
    xs = [float(p[0]) for p in points]
    ys = [float(p[1]) for p in points]
    return BBox(min(xs), min(ys), max(xs), max(ys))


def proportional_boxes(text: str, bbox: BBox) -> tuple[BBox, ...]:
    """Split a line box evenly between its characters."""
    if not text:
        return ()
    width = (bbox.x1 - bbox.x0) / len(text)
    return tuple(
        BBox(bbox.x0 + i * width, bbox.y0, bbox.x0 + (i + 1) * width, bbox.y1)
        for i in range(len(text))
    )


class RapidOcrEngine:
    """:class:`~anonyma.models.OcrEngine` backed by ``rapidocr_onnxruntime``."""

    def __init__(self, latin: tuple[Path, Path] | None = None) -> None:
        from rapidocr_onnxruntime import RapidOCR

        kwargs: dict[str, Any] = {"text_score": MIN_LINE_SCORE}
        if latin is not None:
            kwargs["rec_model_path"] = str(latin[0])
            kwargs["rec_keys_path"] = str(latin[1])
        self.latin = latin is not None
        self._engine = RapidOCR(**kwargs)
        self._lock = threading.Lock()

    def recognize(self, image: Image.Image) -> list[OcrLine]:
        import numpy as np

        rgb = image if image.mode == "RGB" else image.convert("RGB")
        array = np.ascontiguousarray(np.asarray(rgb)[:, :, ::-1])  # RGB -> BGR
        with self._lock:
            # Every detected box is returned (score 0 included): a box whose text
            # could not be read is re-read on its own instead of being dropped.
            result, _ = self._engine(array, return_word_box=True, text_score=0.0)
        lines: list[OcrLine] = []
        for item in result or []:
            points, text, score = item[0], str(item[1]), float(item[2])
            bbox = _box(points)
            reread = False
            if not text.strip() or score < MIN_LINE_SCORE:
                retry = self._reread(array, bbox)
                if retry is None:
                    continue
                text, score = retry
                reread = True
            char_boxes: tuple[BBox, ...] | None = None
            if not reread and len(item) > 3 and item[3] and len(item[3]) == len(text):
                char_boxes = tuple(_box(p) for p in item[3])
                # Stretch character cells vertically to the whole line.
                char_boxes = tuple(BBox(b.x0, bbox.y0, b.x1, bbox.y1) for b in char_boxes)
            lines.append(
                OcrLine(
                    text=text,
                    bbox=bbox,
                    score=score,
                    char_boxes=char_boxes or proportional_boxes(text, bbox),
                )
            )
        return lines

    def _reread(self, array: Any, bbox: BBox) -> tuple[str, float] | None:
        """Recognition alone on one detected box (long lines can fail in the full pass).

        The word-box pass of RapidOCR sometimes returns an empty text for a long
        line that the recognizer reads perfectly once the line is cropped.
        """
        height, width = array.shape[:2]
        pad = 6
        x0, y0 = max(0, int(bbox.x0) - pad), max(0, int(bbox.y0) - pad)
        x1, y1 = min(width, int(bbox.x1) + pad), min(height, int(bbox.y1) + pad)
        if x1 - x0 < 4 or y1 - y0 < 4:
            return None
        import numpy as np

        crop = np.ascontiguousarray(array[y0:y1, x0:x1])
        with self._lock:
            result, _ = self._engine(crop, use_det=False, use_cls=False, text_score=0.0)
        for item in result or []:
            text, score = str(item[0]), float(item[1])
            if text.strip() and score >= MIN_LINE_SCORE:
                return text, score
        return None


def create_engine(config: Config) -> OcrEngine | None:
    if not config.ocr_enabled:
        return None
    try:
        engine = RapidOcrEngine(latin_model_files())
    except ImportError:
        log.warning("OCR engine unavailable: rapidocr_onnxruntime not installed")
        return None
    log.info("OCR engine ready (latin model: %s)", engine.latin)
    return engine
