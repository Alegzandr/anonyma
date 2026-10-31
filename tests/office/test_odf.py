"""odt / ods / odp: paragraphs over spans, revisions, annotations, metadata, media."""

from __future__ import annotations

from pathlib import Path

import pytest

from anonyma.config import Config
from anonyma.extractors._media import BLANKED_CODE, PLACEHOLDER_PNG
from anonyma.extractors._odf import sniff_odf
from anonyma.pipeline import extract_file
from tests.canary import CANARY_EMAIL, CANARY_PERSON
from tests.fixtures.generators.office import odf_package, odt_body
from tests.office.conftest import run_pipeline
from tests.office.helpers import (
    MARKER_PNG,
    install_fake_images,
    png_bytes,
    remove_image_support,
    zip_names,
    zip_read,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "contract"
ODT = "application/vnd.oasis.opendocument.text"


def test_sniff_recognises_odf_packages() -> None:
    head = (FIXTURES / "rapport.odt").read_bytes()[:8192]
    assert sniff_odf(head) == "odt"
    assert sniff_odf((FIXTURES / "tableau.ods").read_bytes()[:8192]) == "ods"
    assert sniff_odf((FIXTURES / "rapport.docx").read_bytes()[:8192]) is None


def test_odt_paragraph_joins_spans(tmp_path: Path, canary_config: Config) -> None:
    doc = extract_file(FIXTURES / "rapport.odt", "rapport.odt", canary_config, tmp_path / "w")
    texts = [s.text for s in doc.segments]
    assert f"Rapport rédigé par {CANARY_PERSON}." in texts
    kinds = {s.context.label for s in doc.segments if CANARY_PERSON in s.text}
    assert {"note", "commentaire", "modification suivie", "en-tête"} <= kinds


def test_odt_output_purged(tmp_path: Path, canary_config: Config) -> None:
    _, output = run_pipeline(FIXTURES / "rapport.odt", tmp_path, canary_config)
    content = zip_read(output, "content.xml").decode()
    assert "tracked-changes" not in content and "Ancien contact" not in content
    assert "<dc:creator>" not in content and "<dc:date>" not in content
    assert '[PERSONNE_001]<text:span text:style-name="T1"></text:span>' in content
    assert "<text:s/>" not in content.split("Rapport")[1].split("</text:p>")[0]
    meta = zip_read(output, "meta.xml").decode()
    assert "<office:meta/>" in meta
    names = zip_names(output)
    assert names[0] == "mimetype"
    assert not any(n.startswith("Thumbnails/") for n in names)
    assert "Thumbnails" not in zip_read(output, "META-INF/manifest.xml").decode()
    assert "PrinterName" not in zip_read(output, "settings.xml").decode()
    for name in names:
        data = zip_read(output, name)
        assert CANARY_PERSON.encode() not in data and CANARY_EMAIL.encode() not in data


def test_ods_sheet_and_formula_renamed(tmp_path: Path, canary_config: Config) -> None:
    _, output = run_pipeline(FIXTURES / "tableau.ods", tmp_path, canary_config)
    content = zip_read(output, "content.xml").decode()
    assert 'table:name="(PERSONNE_001)"' in content
    assert "of:=['(PERSONNE_001)'.B1]" in content
    assert 'office:string-value="[EMAIL_001]"' in content


def test_ods_cell_labels(tmp_path: Path, canary_config: Config) -> None:
    doc = extract_file(FIXTURES / "tableau.ods", "tableau.ods", canary_config, tmp_path / "w")
    labels = {s.text: s.context.label for s in doc.segments if s.context.kind.value == "cell"}
    assert labels[CANARY_PERSON] in ("feuille 1!B1", "feuille 2!A1")
    assert labels[CANARY_EMAIL] == "feuille 1!C2"


def test_odp_slides_and_notes(tmp_path: Path, canary_config: Config) -> None:
    doc = extract_file(
        FIXTURES / "presentation.odp", "presentation.odp", canary_config, tmp_path / "w"
    )
    labels = {s.context.label for s in doc.segments}
    assert {"diapositive 1", "notes diapositive 1"} <= labels


def _odt_with_image(path: Path) -> None:
    odf_package(path, ODT, odt_body(image=True), extra={"Pictures/photo.png": png_bytes()})


def test_odt_image_blanked_without_image_support(
    tmp_path: Path, canary_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    remove_image_support(monkeypatch)
    source = tmp_path / "photo.odt"
    _odt_with_image(source)
    result, output = run_pipeline(source, tmp_path, canary_config)
    assert zip_read(output, "Pictures/photo.png") == PLACEHOLDER_PNG
    assert any(i.code == BLANKED_CODE for i in result.results[0].issues)
    content = zip_read(output, "content.xml").decode()
    assert "<svg:title>[PERSONNE_001]</svg:title>" in content


def test_odt_image_replaced_by_child_output(
    tmp_path: Path, canary_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_fake_images(monkeypatch)
    source = tmp_path / "photo.odt"
    _odt_with_image(source)
    _, output = run_pipeline(source, tmp_path, canary_config)
    assert zip_read(output, "Pictures/photo.png") == MARKER_PNG
