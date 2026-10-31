"""PDF: real redaction, OCR of scanned pages, metadata/active content purge, children."""

from __future__ import annotations

import io
from pathlib import Path

import pymupdf
import pytest
from PIL import Image, ImageOps

from anonyma import pipeline as pipeline_module
from anonyma.config import Config
from anonyma.extractors._media import BLANKED_CODE
from anonyma.extractors._pdf import encode_pdf_string, iter_strings
from anonyma.models import BBox, DocumentStatus, OcrLine, ReplacementMode, SegmentKind
from anonyma.pipeline import Pipeline, extract_file
from tests.canary import CANARY_EMAIL, CANARY_PERSON
from tests.fixtures.generators.office import pdf_document
from tests.office.conftest import run_pipeline
from tests.office.helpers import png_bytes

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "contract"


def content_streams(doc: pymupdf.Document) -> bytes:
    data = b""
    for page in doc:
        for xref in page.get_contents():
            data += doc.xref_stream(xref) or b""
    return data


def test_redaction_removes_glyphs_and_keeps_neighbours(
    tmp_path: Path, canary_config: Config
) -> None:
    _, output = run_pipeline(FIXTURES / "rapport.pdf", tmp_path, canary_config)
    with pymupdf.open(output) as doc:
        text = "".join(page.get_text() for page in doc)
        assert "Quintavalle" not in text and "QUINTAVALLE" not in text
        assert CANARY_EMAIL not in text
        for kept in ("Ligne voisine conservée", "Rapport rédigé par", "Signé :", "a signé"):
            assert kept in text
        assert "[PERSONNE_001]" in text
        raw = content_streams(doc)
        for needle in ("Quintavalle", "phyrin"):
            assert needle.encode("latin-1") not in raw
            assert needle.encode("latin-1").hex().encode() not in raw.lower()


def _with_javascript(path: Path) -> None:
    doc = pymupdf.open(stream=pdf_document(), filetype="pdf")
    catalog = doc.pdf_catalog()
    doc.xref_set_key(
        catalog, "OpenAction", f"<< /S /JavaScript /JS (app.alert\\('{CANARY_EMAIL}'\\)) >>"
    )
    xref = doc.get_new_xref()
    doc.update_object(xref, f"<< /Alt {encode_pdf_string(CANARY_PERSON)} >>")
    doc.xref_set_key(catalog, "AnonymaTest", f"{xref} 0 R")
    doc.save(path)
    doc.close()


def test_metadata_and_active_content_purged(tmp_path: Path, canary_config: Config) -> None:
    source = tmp_path / "actif.pdf"
    _with_javascript(source)
    doc_in = extract_file(source, source.name, canary_config, tmp_path / "w")
    hidden = [s.text for s in doc_in.segments if s.context.kind is SegmentKind.HIDDEN]
    assert CANARY_PERSON in hidden  # /Alt string exposed
    _, output = run_pipeline(source, tmp_path, canary_config)
    with pymupdf.open(output) as doc:
        assert not any(v for k, v in doc.metadata.items() if k not in ("format", "encryption"))
        assert doc.get_xml_metadata() == ""
        assert doc.get_toc() == []
        for page in doc:
            assert [a.type[0] for a in page.annots()] == []
            assert page.get_links() == []
            for widget in page.widgets():
                assert widget.field_value == "[PERSONNE_001]"
        sources = "".join(doc.xref_object(x) for x in range(1, doc.xref_length()))
        assert "/JavaScript" not in sources and "/JS" not in sources
        strings = [s.value for s in iter_strings(sources)]
        assert "[PERSONNE_001]" in strings and CANARY_PERSON not in strings
        assert doc.embfile_names() == ["note.txt"]
        attached = doc.embfile_get("note.txt").decode("utf-8")
        assert "[PERSONNE_001]" in attached and CANARY_PERSON not in attached


def test_blackout_paints_black_boxes(tmp_path: Path, canary_config: Config) -> None:
    config = Config(
        profile=canary_config.profile,
        workers=1,
        always_mask=canary_config.always_mask,
        mode=ReplacementMode.BLACKOUT,
    )
    source_doc = extract_file(FIXTURES / "rapport.pdf", "rapport.pdf", config, tmp_path / "w")
    segment = next(s for s in source_doc.segments if s.id == "t0_0")
    start = segment.text.index("Quintavalle")
    box = segment.span_bbox(start, start + 11)
    assert box is not None
    _, output = run_pipeline(FIXTURES / "rapport.pdf", tmp_path, config)
    with pymupdf.open(output) as doc:
        pix = doc[0].get_pixmap(dpi=72)
        x, y = int((box.x0 + box.x1) / 2), int((box.y0 + box.y1) / 2)
        assert pix.pixel(x, y) == (0, 0, 0)
        assert "Quintavalle" not in doc[0].get_text()


# -- scanned pages ------------------------------------------------------------


class DarkBlockOcr:
    """Fake OCR: reports the canary on the dark block of the page, if still dark."""

    def __init__(self) -> None:
        self.calls = 0

    def recognize(self, image: Image.Image) -> list[OcrLine]:
        self.calls += 1
        gray = image.convert("L")
        bbox = ImageOps.invert(gray).point(lambda p: 255 if p > 128 else 0).getbbox()
        if bbox is None:
            return []
        pixels = gray.crop(bbox).tobytes()
        if sum(pixels) / len(pixels) > 100:
            return []
        return [OcrLine(CANARY_PERSON, BBox(*map(float, bbox)), 0.9)]


def scanned_pdf(path: Path, rotation: int = 0) -> None:
    image = Image.new("RGB", (600, 800), "white")
    image.paste((0, 0, 0), (100, 100, 400, 140))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_image(page.rect, stream=buffer.getvalue())
    page.set_rotation(rotation)
    doc.save(path)
    doc.close()


@pytest.mark.parametrize("rotation", [0, 90])
def test_scanned_page_is_ocred_and_pixels_redacted(
    tmp_path: Path, canary_config: Config, monkeypatch: pytest.MonkeyPatch, rotation: int
) -> None:
    engine = DarkBlockOcr()
    monkeypatch.setattr(pipeline_module, "ocr_for", lambda config: engine)
    source = tmp_path / "scan.pdf"
    scanned_pdf(source, rotation)
    doc = extract_file(source, source.name, canary_config, tmp_path / "w")
    ocr = [s for s in doc.segments if s.context.kind is SegmentKind.OCR]
    assert [s.text for s in ocr] == [CANARY_PERSON]
    assert doc.children == []  # the scan itself is not a child: the page is OCR'd
    result, output = run_pipeline(source, tmp_path, canary_config)
    assert result.results[0].status is DocumentStatus.ANONYMIZED
    with pymupdf.open(output) as out:
        page = out[0]
        rect = pymupdf.Rect(
            ocr[0].context.bbox.x0,
            ocr[0].context.bbox.y0,  # type: ignore[union-attr]
            ocr[0].context.bbox.x1,
            ocr[0].context.bbox.y1,
        )  # type: ignore[union-attr]
        pix = page.get_pixmap(dpi=72, clip=rect * page.rotation_matrix)
        samples = pix.samples
        mean = sum(samples) / len(samples)
        assert mean > 128  # the black block is gone (white box + small tag text)


def test_scanned_page_without_ocr_is_an_error(tmp_path: Path) -> None:
    source = tmp_path / "scan.pdf"
    scanned_pdf(source)
    config = Config(ocr_enabled=False, workers=1)
    doc = extract_file(source, source.name, config, tmp_path / "w")
    assert doc.status is DocumentStatus.FAILED
    assert any(i.code == "pdf.ocr_unavailable" for i in doc.issues)


# -- images ---------------------------------------------------------------------


def _pdf_with_image(path: Path) -> None:
    path.write_bytes(pdf_document(image=png_bytes(), attachment=False))


def test_pdf_image_deleted_without_ocr(tmp_path: Path, canary_config: Config) -> None:
    # Images are OCR'd inside the PDF (not as children): without OCR they cannot be
    # verified, so they are deleted and the missing OCR is reported.
    config = Config(
        profile=canary_config.profile,
        workers=1,
        always_mask=canary_config.always_mask,
        ocr_enabled=False,
    )
    source = tmp_path / "photo.pdf"
    _pdf_with_image(source)
    doc = extract_file(source, source.name, config, tmp_path / "w")
    assert doc.children == [c for c in doc.children if c.format != "png"]
    assert any(i.code == "ocr.unavailable" for i in doc.issues)
    pipeline = Pipeline(config)
    result = pipeline.run([source], tmp_path / "out").results[0]
    assert result.status is not DocumentStatus.ANONYMIZED
    assert any(i.code == BLANKED_CODE for i in result.issues)
    assert result.output_path is not None
    with pymupdf.open(result.output_path) as out:
        sizes = [(img[2], img[3]) for img in out[0].get_images(full=True)]
        assert all(w < 16 or h < 16 for w, h in sizes)


def test_pdf_image_without_text_is_kept_untouched(
    tmp_path: Path, canary_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = DarkBlockOcr()  # finds nothing on a light image
    monkeypatch.setattr(pipeline_module, "ocr_for", lambda config: engine)
    source = tmp_path / "photo.pdf"
    source.write_bytes(pdf_document(image=png_bytes((200, 220, 240)), attachment=False))
    with pymupdf.open(source) as original:
        xref = original[0].get_images(full=True)[0][0]
        before = pymupdf.Pixmap(original, xref).samples
    _, output = run_pipeline(source, tmp_path, canary_config)
    with pymupdf.open(output) as out:
        images = out[0].get_images(full=True)
        assert [(img[2], img[3]) for img in images] == [(40, 40)]
        assert pymupdf.Pixmap(out, images[0][0]).samples == before


def test_encrypted_pdf_is_an_error(tmp_path: Path, canary_config: Config) -> None:
    source = tmp_path / "secret.pdf"
    doc = pymupdf.open(stream=pdf_document(), filetype="pdf")
    doc.save(
        source,
        encryption=pymupdf.PDF_ENCRYPT_AES_256,
        user_pw="motdepasse",
        owner_pw="proprietaire",
    )
    doc.close()
    result = extract_file(source, source.name, canary_config, tmp_path / "w")
    assert result.status is DocumentStatus.FAILED
    assert any(i.code == "extract.encrypted" for i in result.issues)
