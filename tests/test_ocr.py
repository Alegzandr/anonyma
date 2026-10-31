from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from anonyma import ocr
from anonyma.config import Config
from anonyma.models import BBox, OcrEngine


def test_disabled_returns_none() -> None:
    assert ocr.create_engine(Config(ocr_enabled=False)) is None


def test_proportional_boxes() -> None:
    boxes = ocr.proportional_boxes("abcd", BBox(0, 0, 40, 10))
    assert boxes == (
        BBox(0, 0, 10, 10),
        BBox(10, 0, 20, 10),
        BBox(20, 0, 30, 10),
        BBox(30, 0, 40, 10),
    )
    assert ocr.proportional_boxes("", BBox(0, 0, 1, 1)) == ()


def test_latin_model_detection(tmp_path: Path) -> None:
    assert ocr.latin_model_files(tmp_path) is None
    folder = tmp_path / ocr.LATIN_MODEL_ID / "languages" / "latin"
    folder.mkdir(parents=True)
    (folder / "rec.onnx").write_bytes(b"x")
    (folder / "dict.txt").write_text("a\n", encoding="utf-8")
    found = ocr.latin_model_files(tmp_path)
    assert found is not None
    assert found[0].name == "rec.onnx"


@pytest.mark.slow
def test_bundled_engine_reads_text() -> None:
    engine = ocr.create_engine(Config())
    assert isinstance(engine, OcrEngine)
    image = Image.new("RGB", (900, 160), "white")
    ImageDraw.Draw(image).text(
        (30, 40), "Contact 2026 exemple", fill="black", font=ImageFont.load_default(size=56)
    )
    lines = engine.recognize(image)
    text = " ".join(line.text for line in lines)
    assert "Contact" in text and "2026" in text
    for line in lines:
        assert line.char_boxes is not None and len(line.char_boxes) == len(line.text)
        assert line.bbox.x0 >= 0 and line.bbox.x1 <= 900
