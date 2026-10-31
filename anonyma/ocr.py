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


#: A line box at least this much taller than wide holds vertical text.
VERTICAL_RATIO = 1.5


def is_vertical(bbox: BBox) -> bool:
    return (bbox.y1 - bbox.y0) >= VERTICAL_RATIO * max(bbox.x1 - bbox.x0, 1.0)


def vertical_boxes(text: str, bbox: BBox, *, upward: bool) -> tuple[BBox, ...]:
    """Split a vertical line box evenly between its characters (``upward``: the
    text reads from bottom to top)."""
    if not text:
        return ()
    step = (bbox.y1 - bbox.y0) / len(text)
    boxes = []
    for i in range(len(text)):
        if upward:
            boxes.append(BBox(bbox.x0, bbox.y1 - (i + 1) * step, bbox.x1, bbox.y1 - i * step))
        else:
            boxes.append(BBox(bbox.x0, bbox.y0 + i * step, bbox.x1, bbox.y0 + (i + 1) * step))
    return tuple(boxes)


def _mostly_inside(inner: BBox, outer: BBox) -> bool:
    w = min(inner.x1, outer.x1) - max(inner.x0, outer.x0)
    h = min(inner.y1, outer.y1) - max(inner.y0, outer.y0)
    if w <= 0 or h <= 0:
        return False
    area = max((inner.x1 - inner.x0) * (inner.y1 - inner.y0), 1e-6)
    return w * h >= 0.5 * area


class RapidOcrEngine:
    """:class:`~anonyma.models.OcrEngine` backed by ``rapidocr_onnxruntime``.

    Vertical text (rotated 90° or 270°, e.g. timestamps along a margin) is read
    again on rotated strips around the vertical boxes of the first pass: the
    detector alone fragments such text into partial, unreliable boxes.
    """

    #: Second pass on rotated strips (vertical text).
    rotated = True

    def __init__(self, latin: tuple[Path, Path] | None = None) -> None:
        import onnxruntime as ort
        from rapidocr_onnxruntime import RapidOCR

        # rapidocr opens its own ONNX Runtime sessions: telemetry (ETW on Windows) off first.
        disable = getattr(ort, "disable_telemetry_events", None)
        if disable is not None:
            disable()
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
        lines = self._pass(array, vertical=True)
        if self.rotated:
            lines = self._vertical_pass(array, lines)
        return lines

    def _pass(self, array: Any, *, vertical: bool) -> list[OcrLine]:
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
            if is_vertical(bbox) and len(text) > 1:
                if vertical:  # RapidOCR's word boxes are wrong for vertical lines
                    char_boxes = vertical_boxes(text, bbox, upward=self._upward(array, bbox))
            elif not reread and len(item) > 3 and item[3] and len(item[3]) == len(text):
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

    def _vertical_pass(self, array: Any, lines: list[OcrLine]) -> list[OcrLine]:
        """Read vertical text again on rotated strips; its fragments are replaced."""
        import numpy as np

        height, width = array.shape[:2]
        flat = sorted(ln.bbox.y1 - ln.bbox.y0 for ln in lines if not is_vertical(ln.bbox))
        typical = flat[len(flat) // 2] if flat else 0.0
        ranges: list[tuple[int, int, int, int]] = []
        for line in lines:
            box = line.bbox
            if not is_vertical(box):
                continue
            if len(line.text.strip()) < 3 and box.y1 - box.y0 < 2.5 * typical:
                continue  # "1", "l"...: ordinary narrow glyphs
            side, tall = box.x1 - box.x0, box.y1 - box.y0
            margin = max(tall, 100.0)
            ranges.append(
                (
                    max(0, int(box.x0 - 2 * side - 8)),
                    min(width, int(box.x1 + 2 * side + 8)),
                    max(0, int(box.y0 - margin)),
                    min(height, int(box.y1 + margin)),
                )
            )
        if not ranges:
            return lines
        ranges.sort()
        merged = [ranges[0]]
        for x0, x1, y0, y1 in ranges[1:]:
            last = merged[-1]
            if x0 <= last[1] and y0 <= last[3] and last[2] <= y1:
                merged[-1] = (last[0], max(last[1], x1), min(last[2], y0), max(last[3], y1))
            else:
                merged.append((x0, x1, y0, y1))
        found: list[OcrLine] = []
        for x0, x1, y0, y1 in merged:
            strip_width = x1 - x0
            if strip_width < 4 or y1 - y0 < 4:
                continue
            strip = array[y0:y1, x0:x1]
            rotated = np.ascontiguousarray(np.rot90(strip))  # counter-clockwise
            for line in self._pass(rotated, vertical=False):
                b = line.bbox
                # Rotated pixel (rx, ry) comes from strip pixel (strip_width - ry, rx).
                mapped = BBox(
                    x0 + strip_width - b.y1, y0 + b.x0, x0 + strip_width - b.y0, y0 + b.x1
                )
                if not is_vertical(mapped):
                    continue  # horizontal text seen sideways: fragments
                upward = self._upward(array, mapped)
                found.append(
                    OcrLine(
                        line.text,
                        mapped,
                        line.score,
                        vertical_boxes(line.text, mapped, upward=upward),
                    )
                )
        if not found:
            return lines
        kept = [ln for ln in lines if not any(_mostly_inside(ln.bbox, f.bbox) for f in found)]
        return kept + found

    def _upward(self, array: Any, bbox: BBox) -> bool:
        """True when the vertical text in ``bbox`` reads from bottom to top."""
        import numpy as np

        height, width = array.shape[:2]
        x0, y0 = max(0, int(bbox.x0)), max(0, int(bbox.y0))
        x1, y1 = min(width, int(bbox.x1 + 0.999)), min(height, int(bbox.y1 + 0.999))
        classifier = getattr(self._engine, "text_cls", None)
        if classifier is None or x1 - x0 < 2 or y1 - y0 < 2:
            return False
        # Counter-clockwise rotation: text reading downwards becomes upright.
        crop = np.ascontiguousarray(np.rot90(array[y0:y1, x0:x1]))
        with self._lock:
            _, results, _ = classifier([crop])
        label, score = results[0] if results else ("0", 0.0)
        return "180" in str(label) and float(score) > float(getattr(classifier, "cls_thresh", 0.9))

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
        vertical = is_vertical(bbox)
        if vertical:
            crop = np.ascontiguousarray(np.rot90(crop))
        with self._lock:
            result, _ = self._engine(crop, use_det=False, use_cls=vertical, text_score=0.0)
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
