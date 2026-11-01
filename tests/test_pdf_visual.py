"""PDF: images with transparency, invisible text, full-page OCR, vector glyphs,
barcodes, digital signatures and revisions (synthetic documents only)."""

from __future__ import annotations

import datetime as dt
import io
import re
from pathlib import Path

import numpy as np
import pymupdf
import pytest
import qrcode  # type: ignore[import-untyped]
from PIL import Image, ImageDraw

from anonyma import barcodes
from anonyma import pipeline as pipeline_module
from anonyma.config import Config
from anonyma.extractors._pdfobjects import strip_jpeg_metadata
from anonyma.models import BBox, DocumentStatus, OcrLine, Profile, SegmentKind
from anonyma.pipeline import Pipeline, extract_file
from tests.canary import CANARY_EMAIL, CANARY_PERSON, CANARY_TERMS

RED = (220, 20, 20)


class RedInkOcr:
    """Fake OCR: "reads" the canary person wherever red ink is visible.

    A uniform red rectangle (a stamp whose shape was lost with its soft mask) is
    unreadable, like real text drawn black on black.
    """

    def __init__(self) -> None:
        self.calls = 0
        self.fail = False

    def recognize(self, image: Image.Image) -> list[OcrLine]:
        self.calls += 1
        if self.fail:
            raise RuntimeError("panne simulée")
        array = np.asarray(image.convert("RGB")).astype(int)
        red = (array[:, :, 0] > 150) & (array[:, :, 1] < 90) & (array[:, :, 2] < 90)
        fraction = red.mean()
        if fraction < 0.0005 or fraction > 0.6:
            return []
        ys, xs = np.nonzero(red)
        box = BBox(float(xs.min()), float(ys.min()), float(xs.max() + 1), float(ys.max() + 1))
        return [OcrLine(CANARY_PERSON, box, 0.99)]


@pytest.fixture
def ink(monkeypatch: pytest.MonkeyPatch) -> RedInkOcr:
    engine = RedInkOcr()
    monkeypatch.setattr(pipeline_module, "ocr_for", lambda config: engine)
    return engine


@pytest.fixture
def config() -> Config:
    return Config(profile=Profile.LIGHT, workers=1, always_mask=list(CANARY_TERMS))


def _png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def _stamp() -> bytes:
    """Red text shape carried only by the alpha channel (base image uniformly red)."""
    alpha = Image.new("L", (300, 80), 0)
    ImageDraw.Draw(alpha).rectangle((20, 25, 280, 55), fill=255)
    stamp = Image.new("RGBA", (300, 80), (*RED, 255))
    stamp.putalpha(alpha)
    return _png(stamp)


def _logo() -> bytes:
    """Transparent blue disc: no text, must keep its exact look."""
    logo = Image.new("RGBA", (120, 120), (0, 0, 0, 0))
    ImageDraw.Draw(logo).ellipse((10, 10, 110, 110), fill=(30, 60, 200, 255))
    return _png(logo)


def _run(source: Path, tmp_path: Path, config: Config):  # type: ignore[no-untyped-def]
    pipeline = Pipeline(config)
    analysis = pipeline.analyze([source])
    try:
        result = pipeline.apply(analysis, tmp_path / "out")
    finally:
        pipeline.close(analysis)
    return result.results[0]


def _clip(page: pymupdf.Page, rect: pymupdf.Rect) -> Image.Image:
    pix = page.get_pixmap(dpi=72, clip=rect, alpha=False)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


def _red_fraction(image: Image.Image) -> float:
    a = np.asarray(image).astype(int)
    return float(((a[:, :, 0] > 150) & (a[:, :, 1] < 90) & (a[:, :, 2] < 90)).mean())


# -- images ---------------------------------------------------------------------


def _stamp_pdf(path: Path) -> None:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Contrat de prestation", fontsize=12)
    page.insert_image(pymupdf.Rect(72, 100, 372, 180), stream=_stamp())
    page.insert_image(pymupdf.Rect(400, 100, 520, 220), stream=_logo())
    # The same stamp again inside a Form XObject.
    form = pymupdf.open()
    form.new_page(width=300, height=80).insert_image(pymupdf.Rect(0, 0, 300, 80), stream=_stamp())
    page.show_pdf_page(pymupdf.Rect(72, 300, 372, 380), form, 0)
    doc.save(path)


def test_soft_mask_stamp_is_read_and_redrawn(
    tmp_path: Path, config: Config, ink: RedInkOcr
) -> None:
    source = tmp_path / "tampon.pdf"
    _stamp_pdf(source)
    doc = extract_file(source, source.name, config, tmp_path / "w")
    assert doc.status is DocumentStatus.EXTRACTED
    image_ocr = [s for s in doc.segments if s.locator[:1] == ("image",)]
    # Read with its soft mask: without it the stamp is a uniform red block.
    assert image_ocr and all(s.text == CANARY_PERSON for s in image_ocr)
    first = image_ocr[0].context.bbox
    assert first is not None and 72 <= first.x0 < first.x1 <= 372
    # The page OCR sees the same text on the images: nothing duplicated.
    assert not [s for s in doc.segments if s.locator[:1] == ("ocr",)]

    with pymupdf.open(source) as original:
        logo_before = _clip(original[0], pymupdf.Rect(400, 100, 520, 220))
    result = _run(source, tmp_path, config)
    assert result.status is DocumentStatus.ANONYMIZED, [i.message for i in result.issues]
    with pymupdf.open(result.output_path) as out:
        page = out[0]
        for rect in (pymupdf.Rect(72, 100, 372, 180), pymupdf.Rect(72, 300, 372, 380)):
            assert _red_fraction(_clip(page, rect)) == 0  # masked in every placement
        # Transparency kept: the area around the box still shows the white page.
        corner = _clip(page, pymupdf.Rect(72, 100, 82, 105))
        assert corner.convert("L").getextrema() >= (240, 0)  # type: ignore[operator]
        # The logo (no replacement) keeps its exact look: no black rectangle.
        assert _clip(page, pymupdf.Rect(400, 100, 520, 220)).tobytes() == logo_before.tobytes()
        assert "Contrat de prestation" in page.get_text()


def test_jpeg_metadata_exposed_and_stripped(tmp_path: Path, config: Config, ink: RedInkOcr) -> None:
    exif = Image.Exif()
    exif[0x013B] = CANARY_PERSON.encode()
    buffer = io.BytesIO()
    Image.new("RGB", (64, 64), (200, 210, 220)).save(buffer, "JPEG", exif=exif)
    source = tmp_path / "photo.pdf"
    doc = pymupdf.open()
    doc.new_page().insert_image(pymupdf.Rect(50, 50, 200, 200), stream=buffer.getvalue())
    doc.save(source)
    extracted = extract_file(source, source.name, config, tmp_path / "w")
    meta = [s.text for s in extracted.segments if s.locator[:1] == ("image_meta",)]
    assert CANARY_PERSON in meta
    result = _run(source, tmp_path, config)
    assert result.status is DocumentStatus.ANONYMIZED
    data = result.output_path.read_bytes()
    assert b"Exif" not in data and b"Quintavalle" not in data


def test_strip_jpeg_metadata_is_lossless() -> None:
    exif = Image.Exif()
    exif[0x013B] = b"auteur"
    buffer = io.BytesIO()
    Image.new("RGB", (16, 16), (10, 120, 30)).save(buffer, "JPEG", exif=exif, comment=b"note")
    data = buffer.getvalue()
    stripped = strip_jpeg_metadata(data)
    assert b"Exif" not in stripped and b"note" not in stripped
    assert Image.open(io.BytesIO(stripped)).tobytes() == Image.open(io.BytesIO(data)).tobytes()
    assert strip_jpeg_metadata(b"not a jpeg") == b"not a jpeg"


# -- invisible text and layers -------------------------------------------------


def _invisible_pdf(path: Path) -> None:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Texte visible", fontsize=12)
    page.insert_text((72, 100), f"Mode3 {CANARY_EMAIL}", fontsize=12, render_mode=3)
    page.insert_text((72, 130), "Blanc TRX-9Qm4Lp", fontsize=12, color=(1, 1, 1))
    page.insert_text((700, 130), "Hors page TRX-9Qm4Lp", fontsize=12)
    xref = page.get_contents()[0]
    stream = doc.xref_stream(xref) + b"\nBT /helv 0 Tf 72 170 Td (Taille nulle TRX9Qm4Lp) Tj ET\n"
    doc.update_stream(xref, stream)
    layer = doc.add_ocg("Calque masqué", on=False)
    page.insert_text((72, 220), f"Calque {CANARY_PERSON}", fontsize=12, oc=layer)
    doc.save(path)


def _all_text(doc: pymupdf.Document) -> str:
    return " ".join(
        "".join(chr(c[0]) for c in span["chars"]) for page in doc for span in page.get_texttrace()
    )


def test_invisible_text_is_analysed_then_deleted(
    tmp_path: Path, config: Config, ink: RedInkOcr
) -> None:
    source = tmp_path / "invisible.pdf"
    _invisible_pdf(source)
    doc = extract_file(source, source.name, config, tmp_path / "w")
    hidden = " ".join(s.text for s in doc.segments if s.locator[:1] == ("hidden",))
    for needle in ("Mode3", "Blanc", "Hors page", "Taille nulle", "Calque"):
        assert needle in hidden
    assert CANARY_PERSON in hidden  # hidden layer analysed
    visible = " ".join(s.text for s in doc.segments if s.locator[:1] == ("text",))
    assert "Texte visible" in visible and "Mode3" not in visible

    result = _run(source, tmp_path, config)
    assert result.status is DocumentStatus.ANONYMIZED, [i.message for i in result.issues]
    with pymupdf.open(result.output_path) as out:
        text = _all_text(out)
        assert "Texte visible" in text
        for needle in ("Mode3", "Blanc", "Hors page", "Taille nulle", "Quintavalle", "TRX"):
            assert needle not in text
        # The layer is hidden again, as in the original.
        assert [v["on"] for v in out.get_ocgs().values()] == [False]


def test_page_ocr_keeps_only_purely_visual_text(
    tmp_path: Path, config: Config, ink: RedInkOcr
) -> None:
    source = tmp_path / "vecteurs.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Signataire :", fontsize=12)
    # Text converted to outlines: one red path per "glyph".
    for i in range(12):
        page.draw_rect(
            pymupdf.Rect(150 + i * 9, 62, 157 + i * 9, 74), color=None, fill=(0.86, 0.08, 0.08)
        )
    page.draw_rect(pymupdf.Rect(40, 40, 560, 800), color=(0, 0, 0), width=0.5)  # frame
    doc.save(source)
    extracted = extract_file(source, source.name, config, tmp_path / "w")
    ocr = [s for s in extracted.segments if s.context.kind is SegmentKind.OCR]
    assert [s.text for s in ocr] == [CANARY_PERSON]
    result = _run(source, tmp_path, config)
    assert result.status is DocumentStatus.ANONYMIZED, [i.message for i in result.issues]
    with pymupdf.open(result.output_path) as out:
        drawings = out[0].get_drawings()
        red = [d for d in drawings if d.get("fill") and d["fill"][1] < 0.3 and d["fill"][0] > 0.6]
        assert red == []  # the vector glyphs are gone, not just covered
        assert any(d["rect"].width > 400 for d in drawings)  # the frame survives
        assert "Signataire" in out[0].get_text()


def test_ocr_failure_is_an_error(tmp_path: Path, config: Config, ink: RedInkOcr) -> None:
    ink.fail = True
    source = tmp_path / "tampon.pdf"
    _stamp_pdf(source)
    doc = extract_file(source, source.name, config, tmp_path / "w")
    assert doc.status is DocumentStatus.FAILED
    assert any(i.code == "ocr.failed" for i in doc.issues)


def test_without_ocr_images_are_deleted_and_reported(tmp_path: Path, config: Config) -> None:
    config.ocr_enabled = False
    source = tmp_path / "tampon.pdf"
    _stamp_pdf(source)
    doc = extract_file(source, source.name, config, tmp_path / "w")
    assert any(i.code == "ocr.unavailable" for i in doc.issues)
    assert doc.properties["pdf_unextracted"]


# -- barcodes ---------------------------------------------------------------------


def _qr(text: str) -> Image.Image:
    image: Image.Image = qrcode.make(text, box_size=6).get_image().convert("RGB")
    return image


def test_barcode_decoder_finds_rotated_codes() -> None:
    canvas = Image.new("RGB", (900, 500), "white")
    canvas.paste(_qr("https://exemple.test/a"), (20, 20))
    canvas.paste(_qr("https://exemple.test/b").rotate(90, expand=True), (480, 20))
    found = sorted(c.text for c in barcodes.decode(canvas))
    assert found == ["https://exemple.test/a", "https://exemple.test/b"]


def test_qr_code_in_pdf_is_masked(tmp_path: Path, config: Config, ink: RedInkOcr) -> None:
    payload = f"mailto:{CANARY_EMAIL}?ref=9f3a7c21e8b04d56a1f2c3e4d5b6a7980c1d2e3f"
    source = tmp_path / "qr.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Vérifier le document", fontsize=12)
    page.insert_image(pymupdf.Rect(72, 100, 272, 300), stream=_png(_qr(payload)))
    doc.save(source)
    extracted = extract_file(source, source.name, config, tmp_path / "w")
    codes = [s for s in extracted.segments if s.context.kind is SegmentKind.BARCODE]
    assert [s.text for s in codes] == [payload]  # once: page decode deduplicated
    result = _run(source, tmp_path, config)
    assert result.status is DocumentStatus.ANONYMIZED, [i.message for i in result.issues]
    with pymupdf.open(result.output_path) as out:
        pix = out[0].get_pixmap(dpi=150)
        rendered = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        assert barcodes.decode(rendered) == []


def test_qr_code_in_png_is_masked(tmp_path: Path, config: Config, ink: RedInkOcr) -> None:
    source = tmp_path / "qr.png"
    canvas = Image.new("RGB", (500, 400), "white")
    canvas.paste(_qr(f"mailto:{CANARY_EMAIL}"), (40, 40))
    canvas.save(source)
    extracted = extract_file(source, source.name, config, tmp_path / "w")
    assert [s.context.kind for s in extracted.segments].count(SegmentKind.BARCODE) == 1
    result = _run(source, tmp_path, config)
    assert result.status is DocumentStatus.ANONYMIZED, [i.message for i in result.issues]
    assert barcodes.decode(Image.open(result.output_path)) == []


def test_gif_images_are_supported(tmp_path: Path, config: Config, ink: RedInkOcr) -> None:
    source = tmp_path / "anim.gif"
    frames = [Image.new("RGB", (200, 80), "white") for _ in range(2)]
    ImageDraw.Draw(frames[1]).rectangle((20, 20, 180, 50), fill=RED)
    frames[0].save(source, save_all=True, append_images=frames[1:], duration=100, loop=0)
    result = _run(source, tmp_path, config)
    assert result.status is DocumentStatus.ANONYMIZED, [i.message for i in result.issues]
    with Image.open(result.output_path) as out:
        assert getattr(out, "n_frames", 1) == 2
        out.seek(1)
        assert _red_fraction(out.convert("RGB")) == 0


# -- digital signatures and revisions ---------------------------------------------


def _pkcs7() -> bytes:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import pkcs7
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, CANARY_PERSON),
            x509.NameAttribute(NameOID.EMAIL_ADDRESS, CANARY_EMAIL),
        ]
    )
    start = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(4242)
        .not_valid_before(start)
        .not_valid_after(start + dt.timedelta(days=30))
        .sign(key, hashes.SHA256())
    )
    signed: bytes = (
        pkcs7.PKCS7SignatureBuilder()
        .set_data(b"contenu")
        .add_signer(cert, key, hashes.SHA256())
        .sign(serialization.Encoding.DER, [pkcs7.PKCS7Options.DetachedSignature])
    )
    return signed


def _signed_pdf(path: Path) -> bytes:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Formulaire signé", fontsize=12)
    widget = pymupdf.Widget()
    widget.field_type = pymupdf.PDF_WIDGET_TYPE_SIGNATURE
    widget.field_name = "Signature1"
    widget.rect = pymupdf.Rect(72, 600, 300, 680)
    page.add_widget(widget)
    doc.set_xml_metadata(f"<x:xmpmeta><dc:creator>{CANARY_PERSON}</dc:creator></x:xmpmeta>")
    doc.save(path)
    doc.close()
    # Signing = incremental update, as real signing tools do.
    doc = pymupdf.open(path)
    sig_widget = next(iter(doc[0].widgets()))
    der = _pkcs7()
    contents = der.hex().upper().ljust(8192, "0")
    sig = doc.get_new_xref()
    doc.update_object(
        sig,
        "<< /Type /Sig /Filter /Adobe.PPKLite /SubFilter /adbe.pkcs7.detached "
        f"/Name ({CANARY_PERSON.encode('latin-1').decode('latin-1')}) /Reason (Approbation) "
        f"/ContactInfo ({CANARY_EMAIL}) /M (D:20260314104207Z) "
        f"/ByteRange [0 0 0 0] /Contents <{contents}> >>",
    )
    doc.xref_set_key(sig_widget.xref, "V", f"{sig} 0 R")
    catalog = doc.pdf_catalog()
    doc.xref_set_key(catalog, "AcroForm/SigFlags", "3")
    cert_stream = doc.get_new_xref()
    doc.update_object(cert_stream, "<< >>")
    doc.update_stream(cert_stream, der)
    doc.xref_set_key(catalog, "DSS", f"<< /Certs [{cert_stream} 0 R] /VRI << >> >>")
    doc.xref_set_key(catalog, "Perms", f"<< /DocMDP {sig} 0 R >>")
    trailer_id = str(doc.xref_get_key(-1, "ID")[1])
    doc.saveIncr()
    doc.close()
    return trailer_id.encode()


def test_signature_removed_and_single_revision(
    tmp_path: Path, config: Config, ink: RedInkOcr
) -> None:
    source = tmp_path / "signe.pdf"
    _signed_pdf(source)
    raw = source.read_bytes()
    assert raw.count(b"%%EOF") == 2  # signed = two revisions
    original_ids = re.findall(rb"/ID\s*\[\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", raw)
    assert original_ids

    doc = extract_file(source, source.name, config, tmp_path / "w")
    signature = [s.text for s in doc.segments if s.context.kind is SegmentKind.SIGNATURE]
    assert CANARY_PERSON in signature and CANARY_EMAIL in signature  # dict + certificate
    assert "Approbation" in signature
    hidden = [s.text for s in doc.segments if s.context.kind is SegmentKind.HIDDEN]
    assert not any("\x00" in t for t in hidden)  # binary /Contents not exposed

    result = _run(source, tmp_path, config)
    assert result.status is DocumentStatus.ANONYMIZED, [i.message for i in result.issues]
    assert any(i.code == "pdf.signature_removed" for i in result.issues)
    out = result.output_path.read_bytes()
    assert out.count(b"%%EOF") == 1 and b"/Prev" not in out
    with pymupdf.open(result.output_path) as pdf:
        sources = " ".join(pdf.xref_object(x) for x in range(1, pdf.xref_length()))
        for token in ("/Sig", "/DSS", "/Perms", "/SigFlags", "/ByteRange", "/Metadata"):
            assert token not in sources
        assert pdf.get_xml_metadata() == ""
        assert list(pdf[0].widgets()) == []
        new_id = pdf.xref_get_key(-1, "ID")[1].upper()
        for first, second in original_ids:
            assert first.decode().upper() not in new_id
            assert second.decode().upper() not in new_id
        assert "Formulaire sign" in pdf[0].get_text()
