"""Regression: the e-signed PDF incident (see ``tests/fixtures/generators/signed_pdf.py``).

The whole pipeline runs on a signed PDF, the same content as a docx, an eml
carrying the PDF and a zip of the three. The outputs are then re-read with
**independent** means only (never Anonyma's extractors or verifier):

* the readers of the benchmark (``tests/benchmark/measure.py``): PyMuPDF text
  (invisible text included), Info/XMP metadata, every PDF object and stream, raw
  bytes in several encodings, zip/eml structure, every file/member/attachment name;
* RapidOCR (called directly) on every rendered output page and every output image,
  at 0°, 90° and 270°, with fuzzy matching (OCR noise);
* zxing-cpp on the same images.

The signer is masked through a business list (detection of names is covered by
other tests): what is tested here is that every *rendering* of a detected value
disappears (images, codes, invisible text, signature, metadata, names) and that
the status is only ANONYMIZED / NEEDS_REVIEW when the output is truly clean.
"""

from __future__ import annotations

import difflib
import email
import email.policy
import hashlib
import io
import re
import unicodedata
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from anonyma import pipeline as pipeline_module
from anonyma.config import Config
from anonyma.models import DocumentResult, DocumentStatus, OcrLine, Profile
from anonyma.pipeline import BatchResult, Pipeline, is_quarantined
from tests.benchmark.measure import NAME, Hay, walk_bytes
from tests.fixtures.generators import signed_pdf as fx

CLEAN_STATUSES = {DocumentStatus.ANONYMIZED, DocumentStatus.NEEDS_REVIEW}


def _config(**overrides: Any) -> Config:
    config = Config(
        profile=Profile.LIGHT,
        workers=1,
        always_mask=[{"value": fx.SIGNER, "type": "PERSONNE"}],
        organization_whitelist=list(fx.PUBLISHER_WHITELIST),
    )
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


# ---------------------------------------------------------------------------
# Normalisation and fuzzy search
# ---------------------------------------------------------------------------


def _plain(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def _compact(text: str) -> str:
    return re.sub(r"[^0-9a-z]+", "", _plain(text))


def _digits(text: str) -> str:
    return re.sub(r"\D+", "", text)


def fuzzy_in(haystack: str, needle: str, ratio: float = 0.85) -> bool:
    """``needle`` (compact) in ``haystack`` (compact), allowing OCR misreads."""
    if needle in haystack:
        return True
    if len(needle) < 10 or len(haystack) < len(needle) - 2:
        return False
    for size in (len(needle) - 1, len(needle), len(needle) + 1):
        for start in range(0, max(1, len(haystack) - size + 1)):
            window = haystack[start : start + size]
            if difflib.SequenceMatcher(None, window, needle).ratio() >= ratio:
                return True
    return False


#: Values that must not survive, as text renderings (accent/case-insensitive).
TEXT_FORMS = {
    "signataire": [_plain(f) for f in fx.SIGNER_FORMS],
    "horodatage": [_plain("2026-03-14 10:42:07"), _plain("10:42:07 UTC")],
    "reference": [fx.HEX_REF],
    "transaction": [_plain(fx.TRANSACTION_ID), _compact(fx.TRANSACTION_ID)],
}
#: The same values as OCR may read them (compact, fuzzy for long ones).
OCR_FORMS = {
    "signataire": [_compact(fx.SURNAME), "quintavalle", "brezac"],
    "horodatage": ["20260314104207"],
    "reference": [fx.HEX_REF],
    "transaction": [_compact(fx.TRANSACTION_ID)],
}
NAME_FORMS = ["quintavalle", "brezac", "zephyrin"]


# ---------------------------------------------------------------------------
# Independent reading of an output tree
# ---------------------------------------------------------------------------


@dataclass
class Visual:
    label: str
    image: Image.Image


@dataclass
class Reading:
    hay: Hay = field(default_factory=Hay)
    visuals: list[Visual] = field(default_factory=list)
    names: list[str] = field(default_factory=list)
    pdfs: list[bytes] = field(default_factory=list)


def read_output(path: Path, name: str) -> Reading:
    reading = Reading()
    reading.names.append(name)
    data = path.read_bytes()
    walk_bytes(reading.hay, data, name, name)
    _collect(reading, data, name, name, 0)
    reading.names += [i.text for i in reading.hay.items if i.kind == NAME]
    return reading


def _collect(reading: Reading, data: bytes, name: str, label: str, depth: int) -> None:
    """Images, rendered pages, names and PDFs of ``data`` (recursively)."""
    if depth > 8:
        return
    if data.startswith(b"%PDF"):
        reading.pdfs.append(data)
        _collect_pdf(reading, data, label, depth)
    elif data.startswith(b"PK\x03\x04"):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for info in archive.infolist():
                reading.names.append(info.filename)
                if not info.is_dir():
                    member = archive.read(info)
                    _collect(reading, member, info.filename, f"{label}!{info.filename}", depth + 1)
    elif name.lower().endswith(".eml") or re.match(rb"^[A-Za-z-]+: ", data[:200]):
        msg = email.message_from_bytes(data, policy=email.policy.default)
        for part in msg.walk():
            if part.is_multipart():
                continue
            filename = part.get_filename() or ""
            if filename:
                reading.names.append(filename)
            payload = part.get_payload(decode=True)
            if isinstance(payload, bytes) and not part.get_content_type().startswith("text/"):
                _collect(reading, payload, filename or "x.bin", f"{label}#{filename}", depth + 1)
    else:
        try:
            image = Image.open(io.BytesIO(data))
            image.load()
        except Exception:
            return
        reading.visuals.append(Visual(label, _flatten(image)))


def _flatten(image: Image.Image) -> Image.Image:
    """RGB on a white background (transparent stamps)."""
    if image.mode in ("RGBA", "LA", "P"):
        rgba = image.convert("RGBA")
        background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        return Image.alpha_composite(background, rgba).convert("RGB")
    return image.convert("RGB")


def _collect_pdf(reading: Reading, data: bytes, label: str, depth: int) -> None:
    import pymupdf

    doc = pymupdf.open(stream=data, filetype="pdf")
    try:
        for page in doc:
            pix = page.get_pixmap(dpi=150)
            image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            reading.visuals.append(Visual(f"{label} (page {page.number + 1})", image))
            for info in page.get_images(full=True):
                xref, smask = info[0], info[1]
                try:
                    pix = pymupdf.Pixmap(doc, xref)
                    if smask:
                        pix = pymupdf.Pixmap(pix, pymupdf.Pixmap(doc, smask))
                    image = Image.open(io.BytesIO(pix.tobytes("png")))
                    image.load()
                except Exception:
                    continue
                reading.visuals.append(Visual(f"{label} (image {xref})", _flatten(image)))
        for embedded in doc.embfile_names():
            meta = doc.embfile_info(embedded)
            reading.names += [
                embedded,
                str(meta.get("filename", "")),
                str(meta.get("ufilename", "")),
            ]
            _collect(
                reading,
                doc.embfile_get(embedded),
                str(meta.get("filename") or embedded),
                f"{label}!{embedded}",
                depth + 1,
            )
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# OCR and barcodes (independent engines, called directly)
# ---------------------------------------------------------------------------

_OCR: Any = None
_OCR_CACHE: dict[str, str] = {}


def ocr_text(image: Image.Image) -> str:
    """Compact text read at 0°, 90° and 270° (rotated stamps, vertical timestamps)."""
    global _OCR
    import numpy as np

    key = hashlib.sha256(image.tobytes() + repr(image.size).encode()).hexdigest()
    if key in _OCR_CACHE:
        return _OCR_CACHE[key]
    if _OCR is None:
        from rapidocr_onnxruntime import RapidOCR

        _OCR = RapidOCR()
    texts: list[str] = []
    for angle in (0, 90, 270):
        view = image.rotate(angle, expand=True, fillcolor="white") if angle else image
        array = np.ascontiguousarray(np.asarray(view.convert("RGB"))[:, :, ::-1])
        result, _ = _OCR(array)
        texts += [str(item[1]) for item in result or []]
    _OCR_CACHE[key] = _compact(" ".join(texts))
    return _OCR_CACHE[key]


def barcodes(image: Image.Image) -> list[str]:
    import zxingcpp

    return [b.text for b in zxingcpp.read_barcodes(image)]


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------


def text_findings(reading: Reading) -> list[str]:
    found: list[str] = []
    for item in reading.hay.items:
        plain = _plain(item.text)
        digits = _digits(item.text)
        for what, forms in TEXT_FORMS.items():
            if any(form in plain for form in forms):
                found.append(f"{what} dans {item.label}")
        if "20260314104207" in digits:
            found.append(f"horodatage dans {item.label}")
    return sorted(set(found))


def visual_findings(reading: Reading) -> list[str]:
    found: list[str] = []
    for visual in reading.visuals:
        text = ocr_text(visual.image)
        for what, forms in OCR_FORMS.items():
            if any(fuzzy_in(text, form) for form in forms):
                found.append(f"{what} (OCR) dans {visual.label}")
        for decoded in barcodes(visual.image):
            if fx.HEX_REF in decoded.casefold() or _compact(fx.SURNAME) in _compact(decoded):
                found.append(f"code-barres lisible dans {visual.label}")
    return sorted(set(found))


def name_findings(reading: Reading) -> list[str]:
    return sorted(
        {
            f"nom de fichier : forme {form}"
            for n in reading.names
            for form in NAME_FORMS
            if form in _plain(n)
        }
    )


def pdf_findings(reading: Reading, original_ids: list[str]) -> list[str]:
    found: list[str] = []
    for data in reading.pdfs:
        if re.search(rb"/Type\s*/Sig\b", data) or b"/ByteRange" in data or b"pkcs7" in data:
            found.append("signature numérique présente")
        lowered = data.lower()
        if any(i.lower().encode() in lowered for i in original_ids):
            found.append("/ID d'origine présent")
    return sorted(set(found))


def kept_publisher(reading: Reading) -> list[str]:
    """What is missing of the whitelisted publisher in the output (empty = kept)."""
    missing: list[str] = []
    texts = " ".join(_plain(i.text) for i in reading.hay.items if i.kind == "clean")
    if _plain(fx.PUBLISHER) not in texts:
        missing.append("nom de l'éditeur")
    if _plain(fx.PUBLISHER_URL) not in texts:
        missing.append("URL de l'éditeur")
    return missing


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def inputs(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    return fx.write_fixtures(tmp_path_factory.mktemp("signe") / "entrees")


def _by_input(result: BatchResult, paths: dict[str, Path]) -> dict[str, DocumentResult]:
    """Top-level results keyed like ``paths`` (results keep the input order)."""
    return dict(zip(paths, result.results, strict=True))


def _describe(result: DocumentResult) -> str:
    def walk(r: DocumentResult) -> Iterator[DocumentResult]:
        yield r
        for c in r.children:
            yield from walk(c)

    return "; ".join(
        f"{r.output_name}={r.status.value} [{', '.join(i.code for i in r.issues)}]"
        for r in walk(result)
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_fixture_is_readable_independently(inputs: dict[str, Path]) -> None:
    """Sanity of the checks themselves: every value is found in the inputs."""
    reading = read_output(inputs["pdf"], inputs["pdf"].name)
    assert {f.split(" ")[0] for f in text_findings(reading)} >= {
        "signataire",
        "horodatage",
        "reference",
        "transaction",
    }
    assert pdf_findings(reading, fx.original_pdf_id(inputs["pdf"].read_bytes()))
    assert name_findings(reading)
    assert not kept_publisher(reading)
    assert any(fx.HEX_REF in code for v in reading.visuals for code in barcodes(v.image))


@pytest.mark.slow
def test_fixture_values_readable_by_ocr(inputs: dict[str, Path]) -> None:
    reading = read_output(inputs["pdf"], inputs["pdf"].name)
    found = " ".join(visual_findings(reading))
    for what in ("signataire (OCR)", "horodatage (OCR)", "reference (OCR)", "code-barres"):
        assert what in found, found


@pytest.mark.slow
def test_signed_pdf_end_to_end(inputs: dict[str, Path], tmp_path: Path) -> None:
    result = Pipeline(_config()).run(list(inputs.values()), tmp_path / "sortie")
    original_ids = fx.original_pdf_id(inputs["pdf"].read_bytes())
    problems: list[str] = []
    for kind, top in _by_input(result, inputs).items():
        if top.output_path is None:
            problems.append(f"{kind}: aucune sortie ({_describe(top)})")
            continue
        reading = read_output(top.output_path, top.output_name)
        findings = (
            text_findings(reading)
            + visual_findings(reading)
            + name_findings(reading)
            + pdf_findings(reading, original_ids)
        )
        if kind in ("pdf", "docx"):
            findings += [f"éditeur supprimé : {m}" for m in kept_publisher(reading)]
            findings += [
                f"logo de l'éditeur illisible dans {v.label}"
                for v in reading.visuals[:1]
                if "parapheo" not in ocr_text(v.image)
            ]
        clean_status = top.status in CLEAN_STATUSES
        if findings:
            problems.append(f"{kind} ({top.status.value}): {findings}")
            if clean_status:
                problems.append(f"{kind}: statut « propre » malgré des résidus")
        elif not clean_status:
            problems.append(f"{kind}: sortie propre mais statut {_describe(top)}")
    assert not problems, "\n".join(problems)


def test_never_anonymized_without_ocr(inputs: dict[str, Path], tmp_path: Path) -> None:
    """OCR disabled for the whole run: images cannot be checked, never ANONYMIZED."""
    selected = [inputs["pdf"], inputs["docx"]]
    result = Pipeline(_config(ocr_enabled=False)).run(selected, tmp_path / "sortie")
    for top in result.results:
        assert top.status is not DocumentStatus.ANONYMIZED, _describe(top)


class _BlindOcr:
    """An OCR engine that reads nothing: images stay unmasked in the output."""

    def recognize(self, image: Image.Image) -> list[OcrLine]:
        return []


def test_never_anonymized_when_verification_cannot_ocr(
    inputs: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Images left unmasked, OCR unavailable only when verifying: not ANONYMIZED."""
    blind = _BlindOcr()
    monkeypatch.setattr(pipeline_module, "ocr_for", lambda config: blind)
    original_verify = pipeline_module._verify_output

    def verify_without_ocr(*args: Any, **kwargs: Any) -> Any:
        with monkeypatch.context() as patch:
            patch.setattr(pipeline_module, "ocr_for", lambda config: None)
            return original_verify(*args, **kwargs)

    monkeypatch.setattr(pipeline_module, "_verify_output", verify_without_ocr)
    selected = [inputs["pdf"], inputs["docx"]]
    result = Pipeline(_config()).run(selected, tmp_path / "sortie")
    for top in result.results:
        assert top.status is not DocumentStatus.ANONYMIZED, _describe(top)
        assert top.output_path is None or is_quarantined(top, tmp_path / "sortie")
