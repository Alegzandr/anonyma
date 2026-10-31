"""QR codes, Data Matrix and 1D barcodes (zxing-cpp, Apache-2.0), fully offline.

``decode(image)`` returns every code found in a PIL image, in any orientation,
with its region in image pixels. Extractors turn each code into a
``SegmentKind.BARCODE`` segment whose every character box is the code region:
the "codes" detector masks the whole segment, so writers paint over the code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from anonyma.logs import get_logger
from anonyma.models import BBox

if TYPE_CHECKING:
    from PIL import Image

log = get_logger("barcodes")

#: Images smaller than this (pixels, either side) cannot hold a readable code.
MIN_SIDE = 21
#: Images are scanned at most at this size (zxing also tries downscaled copies).
MAX_SIDE = 4000
#: Fraction of the code size added around the reported corners (quiet zone).
MARGIN = 0.08


@dataclass(frozen=True, slots=True)
class Barcode:
    text: str = field(repr=False)
    bbox: BBox  # image pixels, margin included
    format: str


def available() -> bool:
    try:
        import zxingcpp  # noqa: F401
    except ImportError:
        return False
    return True


def _bbox(position: Any, width: int, height: int, scale: float) -> BBox:
    points = [position.top_left, position.top_right, position.bottom_right, position.bottom_left]
    xs = [float(p.x) / scale for p in points]
    ys = [float(p.y) / scale for p in points]
    x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
    # 1D codes report a thin scan line: grow them to a band at least 15 % as tall.
    side = max(x1 - x0, y1 - y0, 1.0)
    if y1 - y0 < 0.15 * side and x1 - x0 >= y1 - y0:
        cy = (y0 + y1) / 2
        y0, y1 = cy - 0.15 * side, cy + 0.15 * side
    elif x1 - x0 < 0.15 * side:
        cx = (x0 + x1) / 2
        x0, x1 = cx - 0.15 * side, cx + 0.15 * side
    pad = MARGIN * side
    return BBox(max(0.0, x0 - pad), max(0.0, y0 - pad), min(width, x1 + pad), min(height, y1 + pad))


def decode(image: Image.Image) -> list[Barcode]:
    """Every code readable in ``image`` (any orientation, inverted codes included).

    Raises ``ImportError`` when zxing-cpp is missing; decoding errors propagate
    (callers report them).
    """
    import zxingcpp

    width, height = image.size
    if min(width, height) < MIN_SIDE:
        return []
    gray = image.convert("L") if image.mode != "L" else image
    scale = 1.0
    if max(width, height) > MAX_SIDE:
        scale = MAX_SIDE / max(width, height)
        gray = gray.resize((max(1, int(width * scale)), max(1, int(height * scale))))
    found: list[Barcode] = []
    seen: set[tuple[str, int, int]] = set()
    for code in zxingcpp.read_barcodes(gray, try_rotate=True, try_invert=True):
        if not code.valid:
            continue
        text = str(code.text or "")
        if not text.strip():
            text = bytes(code.bytes).hex()  # binary payload: still sensitive
        if not text:
            continue
        bbox = _bbox(code.position, width, height, scale)
        key = (text, int(bbox.x0) // 8, int(bbox.y0) // 8)
        if key in seen:
            continue
        seen.add(key)
        found.append(Barcode(text=text, bbox=bbox, format=str(code.format.name)))
    return found
