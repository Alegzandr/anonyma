from __future__ import annotations

import functools
import io
from pathlib import Path

import pillow_heif
import pytest
from PIL import Image, ImageCms
from PIL.PngImagePlugin import PngInfo

from anonyma import ocr
from anonyma.config import Config
from anonyma.extractors.image import metadata_items, sniff_image
from anonyma.models import (
    BBox,
    DocumentStatus,
    OcrLine,
    Replacement,
    ReplacementPlan,
    Segment,
    SegmentContext,
    SegmentKind,
)
from anonyma.pipeline import Pipeline, extract_file
from anonyma.writers.image import page_redaction_boxes
from tests.canary import CANARY_EMAIL, CANARY_PERSON

LINE = f"Bonjour {CANARY_PERSON}"
INK = 128  # grey "text" pixels


class FakeOcr:
    """Reads ``LINE`` at a fixed place; the canary disappears once covered in black."""

    def __init__(self) -> None:
        self.calls = 0

    def recognize(self, image: Image.Image) -> list[OcrLine]:
        self.calls += 1
        gray = image.convert("L")
        bbox = BBox(10, 20, 10 + 10 * len(LINE), 60)
        canary_x = int(bbox.x0 + 10 * (len("Bonjour ") + 4))
        visible = abs(int(gray.getpixel((canary_x, 40))) - INK) < 30  # type: ignore[arg-type]
        text = LINE if visible else "Bonjour"
        boxes = tuple(
            BBox(bbox.x0 + 10 * i, 20, bbox.x0 + 10 * (i + 1), 60) for i in range(len(text))
        )
        return [OcrLine(text=text, bbox=bbox, score=0.99, char_boxes=boxes)]


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeOcr:
    engine = FakeOcr()
    monkeypatch.setattr(ocr, "create_engine", lambda config: engine)
    return engine


def _canvas(mode: str = "RGB", size: tuple[int, int] = (400, 100)) -> Image.Image:
    image = Image.new("RGB", size, "white")
    image.paste((INK, INK, INK), (10, 25, 10 + 10 * len(LINE), 55))
    return image.convert(mode)


def _exif(orientation: int | None = None) -> Image.Exif:
    exif = Image.Exif()
    exif[0x013B] = CANARY_PERSON.encode()  # UTF-8 bytes, as most tools write
    exif[0x9C9D] = CANARY_PERSON.encode("utf-16-le") + b"\0\0"
    exif.get_ifd(0x8769)[0x9286] = b"ASCII\0\0\0" + CANARY_EMAIL.encode()
    gps = exif.get_ifd(0x8825)
    gps.update({1: "N", 2: (45.0, 30.0, 0.0), 3: "W", 4: (1.0, 15.0, 0.0)})
    if orientation:
        exif[0x0112] = orientation
    return exif


@functools.cache
def _icc() -> bytes:
    # Generated once: the profile header embeds a creation timestamp.
    return ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


def _run(tmp_path: Path, name: str, data: bytes, config: Config):  # type: ignore[no-untyped-def]
    src = tmp_path / "in"
    src.mkdir(exist_ok=True)
    (src / name).write_bytes(data)
    top = Pipeline(config).run([src / name], tmp_path / "out").results[0]
    assert top.status is DocumentStatus.ANONYMIZED, [i.message for i in top.issues]
    return top


def _png() -> bytes:
    info = PngInfo()
    info.add_text("Author", CANARY_PERSON)
    info.add_itxt("Comment", CANARY_EMAIL)
    buffer = io.BytesIO()
    _canvas().save(buffer, "PNG", pnginfo=info, exif=_exif(), icc_profile=_icc())
    return buffer.getvalue()


def test_sniff() -> None:
    assert sniff_image(_png()[:16]) == "png"
    assert sniff_image(b"\xff\xd8\xff\xe0") == "jpeg"
    assert sniff_image(b"II*\x00") == "tiff"
    assert sniff_image(b"RIFF\0\0\0\0WEBPVP8 ") == "webp"
    assert sniff_image(b"\0\0\0\x18ftypheic\0\0\0\0") == "heic"
    assert sniff_image(b"BMxxxx\0\0\0\0xxxx\x28\0\0\0") == "bmp"
    assert sniff_image(b"BM is not enough") is None


def test_metadata_items() -> None:
    with Image.open(io.BytesIO(_png())) as image:
        items = dict(metadata_items(image))
    assert items["EXIF Artist"] == CANARY_PERSON
    assert items["EXIF XPAuthor"] == CANARY_PERSON
    assert items["EXIF UserComment"] == CANARY_EMAIL
    assert items["EXIF GPS"] == "GPS 45.500000, -1.250000"
    assert items["PNG Author"] == CANARY_PERSON
    assert items["PNG Comment"] == CANARY_EMAIL


def test_extraction(tmp_path: Path, canary_config: Config, fake: FakeOcr) -> None:
    path = tmp_path / "a.png"
    path.write_bytes(_png())
    doc = extract_file(path, "a.png", canary_config, tmp_path / "w")
    assert doc.status is DocumentStatus.EXTRACTED
    ocr_segments = [s for s in doc.segments if s.context.kind is SegmentKind.OCR]
    assert [s.text for s in ocr_segments] == [LINE]
    assert ocr_segments[0].char_boxes is not None
    assert len(ocr_segments[0].char_boxes) == len(LINE)
    assert any(s.context.kind is SegmentKind.METADATA for s in doc.segments)
    assert any(i.code == "image.gps" for i in doc.issues)


def test_png_redacted_and_metadata_stripped(
    tmp_path: Path, canary_config: Config, fake: FakeOcr
) -> None:
    top = _run(tmp_path, "a.png", _png(), canary_config)
    with Image.open(top.output_path) as out:
        assert out.format == "PNG"
        assert not getattr(out, "text", {})
        assert not out.getexif()
        assert "exif" not in out.info
        assert out.info.get("icc_profile") == _icc()
        gray = out.convert("L")
        canary_x = 10 + 10 * (len("Bonjour ") + 4)
        assert gray.getpixel((canary_x, 40)) != INK  # covered
        assert gray.getpixel((15, 40)) == INK  # "Bonjour" untouched
        assert gray.getpixel((5, 5)) == 255


def test_ocr_disabled_is_an_error(tmp_path: Path, canary_config: Config) -> None:
    canary_config.ocr_enabled = False
    path = tmp_path / "a.png"
    path.write_bytes(_png())
    doc = extract_file(path, "a.png", canary_config, tmp_path / "w")
    assert doc.status is DocumentStatus.FAILED
    assert [i.code for i in doc.issues if i.code.startswith("ocr")] == ["ocr.unavailable"]


def test_jpeg_orientation_quality_and_exif(
    tmp_path: Path, canary_config: Config, fake: FakeOcr
) -> None:
    # Stored rotated: orientation 6 means "rotate 90° clockwise to display".
    stored = _canvas().transpose(Image.Transpose.ROTATE_90)
    buffer = io.BytesIO()
    stored.save(buffer, "JPEG", quality=80, exif=_exif(orientation=6))
    with Image.open(io.BytesIO(buffer.getvalue())) as original:
        tables = original.quantization
    top = _run(tmp_path, "p.jpg", buffer.getvalue(), canary_config)
    with Image.open(top.output_path) as out:
        assert out.format == "JPEG"
        assert out.size == (400, 100)
        assert not out.getexif()
        assert out.quantization == tables
        assert abs(out.convert("L").getpixel((10 + 10 * 12, 40)) - INK) > 40


def test_tiff_pages_are_all_kept(tmp_path: Path, canary_config: Config, fake: FakeOcr) -> None:
    buffer = io.BytesIO()
    first = _canvas()
    first.save(buffer, "TIFF", save_all=True, append_images=[_canvas("L")], compression="tiff_lzw")
    top = _run(tmp_path, "t.tiff", buffer.getvalue(), canary_config)
    assert fake.calls >= 4  # two pages, extraction and verification
    with Image.open(top.output_path) as out:
        assert out.n_frames == 2
        for page in range(2):
            out.seek(page)
            assert out.convert("L").getpixel((10 + 10 * 12, 40)) != INK


@pytest.mark.parametrize(
    ("fmt", "name"), [("WEBP", "w.webp"), ("HEIF", "h.heic"), ("BMP", "b.bmp")]
)
def test_other_formats_strip_metadata(
    tmp_path: Path, canary_config: Config, fake: FakeOcr, fmt: str, name: str
) -> None:
    pillow_heif.register_heif_opener()
    buffer = io.BytesIO()
    params = {} if fmt == "BMP" else {"exif": _exif().tobytes(), "quality": 95}
    _canvas().save(buffer, fmt, **params)
    top = _run(tmp_path, name, buffer.getvalue(), canary_config)
    with Image.open(top.output_path) as out:
        assert out.format == ("HEIF" if fmt == "HEIF" else fmt)
        assert not out.getexif()
        assert not out.info.get("xmp")


def test_transparent_png_is_flattened_for_ocr(
    tmp_path: Path, canary_config: Config, fake: FakeOcr
) -> None:
    image = _canvas("RGBA")
    image.putpixel((0, 0), (0, 0, 0, 0))
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    top = _run(tmp_path, "t.png", buffer.getvalue(), canary_config)
    with Image.open(top.output_path) as out:
        assert out.mode == "RGBA"
        assert out.getpixel((10 + 10 * 12, 40)) != (INK, INK, INK, 255)


def _ocr_segment(sid: str, text: str, x0: float, y0: float) -> tuple[Segment, list[BBox]]:
    cells = [BBox(x0 + 10 * i, y0, x0 + 10 * (i + 1), y0 + 40) for i in range(len(text))]
    line = BBox(x0, y0, cells[-1].x1, y0 + 40)
    context = SegmentContext(SegmentKind.OCR, bbox=line)
    return Segment(id=sid, text=text, locator=(), context=context, char_boxes=tuple(cells)), cells


def test_ocr_boxes_cover_whole_words_and_split_lines() -> None:
    # The OCR engine split « jean.dupont@societe-exemple.fr » over two line boxes,
    # and only the first part was detected.
    first, cells = _ocr_segment("a", "Contact : jean.dupont(", 0, 100)
    second, cont = _ocr_segment("b", "@societe-exemple.fr et suite", cells[-1].x1 - 4, 102)
    other_row, _ = _ocr_segment("c", "ligne suivante", 0, 200)
    start = first.text.index("jean")
    rep = Replacement(start, start + len("jean.dupont"), "jean.dupont", "[X]", "EMAIL", "k")
    plan = ReplacementPlan("d", {"a": [rep]})
    boxes = [box for box, _ in page_redaction_boxes([first, second, other_row], plan)]
    # The whole OCR word is masked (« ( » included), with a margin.
    assert any(b.x0 < cells[start].x0 and b.x1 > cells[-1].x1 for b in boxes)
    # The continuation word on the same row is masked, the rest of that line is not.
    word = len("@societe-exemple.fr")
    assert any(b.x0 <= cont[0].x0 and b.x1 >= cont[word - 1].x1 for b in boxes)
    assert all(b.x1 < cont[word + 4].x0 for b in boxes)
    assert all(b.y0 < 160 for b in boxes)  # nothing on the next row
