"""docx / xlsx / pptx: run splitting, metadata purge, children, zip safety."""

from __future__ import annotations

from pathlib import Path

import pytest
from lxml import etree

from anonyma.config import Config
from anonyma.extractors._media import BLANKED_CODE, PLACEHOLDER_PNG
from anonyma.extractors._ooxml_flavors import (
    rename_sheet_refs,
    sanitize_sheet_name,
    split_header_footer,
)
from anonyma.extractors._xmlscan import redistribute
from anonyma.models import DocumentStatus, Profile, Replacement
from anonyma.pipeline import extract_file
from tests.canary import CANARY_EMAIL, CANARY_PERSON
from tests.fixtures.generators.office import (
    docx_entries,
    pptx_entries,
    write_zip,
    xlsx_entries,
)
from tests.office.conftest import run_pipeline
from tests.office.helpers import (
    MARKER_PNG,
    install_fake_images,
    png_bytes,
    remove_image_support,
    zip_names,
    zip_read,
)

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "contract"


def rep(start: int, end: int, substitute: str) -> Replacement:
    return Replacement(start, end, "x", substitute, "PERSONNE", "c1")


# -- redistribution ----------------------------------------------------------


def test_redistribute_keeps_substitute_in_starting_run() -> None:
    pieces = [("Par ", True), ("Zé", True), ("phyrin", True), (" Q.", True)]
    new = redistribute(pieces, [rep(4, 12, "[P]")])
    assert new == ["Par ", "[P]", "", " Q."]


def test_redistribute_skips_virtual_pieces() -> None:
    pieces = [("\t", False), ("Zé", True), ("\n", False), ("phyrin", True)]
    new = redistribute(pieces, [rep(0, 10, "[P]")])
    assert new == ["\t", "[P]", "\n", ""]


def test_redistribute_rejects_overlaps() -> None:
    with pytest.raises(ValueError):
        redistribute([("abcdef", True)], [rep(2, 4, "x"), rep(3, 5, "y")])


# -- docx -------------------------------------------------------------------


def test_docx_name_split_across_runs_is_found_and_replaced(
    tmp_path: Path, canary_config: Config
) -> None:
    doc = extract_file(FIXTURES / "rapport.docx", "rapport.docx", canary_config, tmp_path / "w")
    body = [s.text for s in doc.segments if s.locator[:1] == ("word/document.xml",)]
    assert f"Rapport rédigé par {CANARY_PERSON}. Voir note" in body

    _, output = run_pipeline(FIXTURES / "rapport.docx", tmp_path, canary_config)
    root = etree.fromstring(zip_read(output, "word/document.xml"))
    first = root.find(f".//{W}p")
    assert first is not None
    runs = first.findall(f"{W}r")
    bold = next(r for r in runs if r.find(f"{W}rPr/{W}b") is not None)
    assert bold.findtext(f"{W}t") == "[PERSONNE_001]"
    italic = next(r for r in runs if r.find(f"{W}rPr/{W}i") is not None)
    assert italic.findtext(f"{W}t") == ""


def test_docx_metadata_and_history_purged(tmp_path: Path, canary_config: Config) -> None:
    _, output = run_pipeline(FIXTURES / "rapport.docx", tmp_path, canary_config)
    names = zip_names(output)
    assert "docProps/custom.xml" not in names
    assert b"custom-properties" not in zip_read(output, "_rels/.rels")
    core = etree.fromstring(zip_read(output, "docProps/core.xml"))
    assert len(core) == 0
    assert b"Company" not in zip_read(output, "docProps/app.xml")
    document = zip_read(output, "word/document.xml")
    assert b"w:del " not in document and b"delText" not in document and b"w:ins " not in document
    assert b"ancien contact" not in document
    assert b"rsid" not in document
    comments = etree.fromstring(zip_read(output, "word/comments.xml"))
    comment = comments.find(f"{W}comment")
    assert comment is not None
    assert comment.get(f"{W}author") == "Anonyme"
    assert comment.get(f"{W}date") is None
    settings = zip_read(output, "word/settings.xml")
    assert b"docVars" not in settings and b"rsids" not in settings
    for name in names:
        data = zip_read(output, name)
        assert CANARY_PERSON.encode() not in data and CANARY_EMAIL.encode() not in data


def _docx_with_image(path: Path) -> None:
    write_zip(path, docx_entries(image=png_bytes()))


def test_docx_image_without_image_support_is_blanked(
    tmp_path: Path, canary_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    remove_image_support(monkeypatch)
    source = tmp_path / "photo.docx"
    _docx_with_image(source)
    doc = extract_file(source, source.name, canary_config, tmp_path / "w")
    assert [c.status for c in doc.children] == [DocumentStatus.UNSUPPORTED]
    result, output = run_pipeline(source, tmp_path, canary_config)
    assert zip_read(output, "word/media/image1.png") == PLACEHOLDER_PNG
    assert any(i.code == BLANKED_CODE for i in result.results[0].issues)
    reread = extract_file(output, output.name, canary_config, tmp_path / "re")
    assert reread.children == []


def test_docx_image_replaced_by_child_output(
    tmp_path: Path, canary_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_fake_images(monkeypatch)
    source = tmp_path / "photo.docx"
    _docx_with_image(source)
    result, output = run_pipeline(source, tmp_path, canary_config)
    assert zip_read(output, "word/media/image1.png") == MARKER_PNG
    assert not any(i.code == BLANKED_CODE for i in result.results[0].issues)


def test_zip_bomb_is_refused(tmp_path: Path, canary_config: Config) -> None:
    entries = docx_entries()
    entries["word/media/bomb.bin"] = bytes(20_000_000)
    source = tmp_path / "bombe.docx"
    write_zip(source, entries)
    doc = extract_file(source, source.name, canary_config, tmp_path / "w")
    assert doc.status is DocumentStatus.FAILED
    assert any(i.code == "archive.bomb" for i in doc.issues)


def test_encrypted_ooxml_is_an_error(tmp_path: Path, canary_config: Config) -> None:
    source = tmp_path / "secret.docx"
    source.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + bytes(600))
    doc = extract_file(source, source.name, canary_config, tmp_path / "w")
    assert doc.status is DocumentStatus.FAILED
    assert any(i.code == "extract.encrypted" for i in doc.issues)


# -- xlsx -------------------------------------------------------------------


def test_xlsx_sheet_renamed_consistently(tmp_path: Path, canary_config: Config) -> None:
    _, output = run_pipeline(FIXTURES / "tableau.xlsx", tmp_path, canary_config)
    workbook = zip_read(output, "xl/workbook.xml").decode()
    assert 'name="(PERSONNE_001)"' in workbook
    assert "'(PERSONNE_001)'!$B$1" in workbook
    assert "'(PERSONNE_001)'!B1" in zip_read(output, "xl/worksheets/sheet2.xml").decode()
    comments = zip_read(output, "xl/comments1.xml").decode()
    assert "<author>Anonyme</author>" in comments


def test_xlsx_number_replaced_becomes_inline_string(tmp_path: Path) -> None:
    entries = xlsx_entries()
    entries["xl/worksheets/sheet2.xml"] = entries["xl/worksheets/sheet2.xml"].replace(  # type: ignore[union-attr]
        "</sheetData>", '<row r="2"><c r="B2"><v>612345678</v></c></row></sheetData>'
    )
    source = tmp_path / "nombres.xlsx"
    write_zip(source, entries)
    config = Config(
        profile=Profile.LIGHT,
        workers=1,
        always_mask=[
            {"value": CANARY_PERSON, "type": "PERSONNE"},
            {"value": "612345678", "type": "TELEPHONE"},
        ],
    )
    _, output = run_pipeline(source, tmp_path, config)
    sheet = zip_read(output, "xl/worksheets/sheet2.xml").decode()
    assert '<c r="B2" t="inlineStr"><is><t>[TELEPHONE_001]</t></is></c>' in sheet
    assert "612345678" not in sheet


def test_header_footer_codes_are_separators() -> None:
    pieces = split_header_footer('&L&"Arial,Gras"Zé&12phyrin && Cie&RPage &P')
    display = "".join(d for d, _raw, _editable in pieces)
    assert display == "\n\nZé\nphyrin & Cie\nPage \n"
    assert "".join(raw for _d, raw, _e in pieces) == '&L&"Arial,Gras"Zé&12phyrin && Cie&RPage &P'


def test_sheet_name_sanitizing() -> None:
    used: set[str] = set()
    assert sanitize_sheet_name("[PERSONNE_001]", used) == "(PERSONNE_001)"
    assert sanitize_sheet_name("[PERSONNE_001]", used) == "(PERSONNE_001) (2)"
    assert sanitize_sheet_name("a/b:c*d?e", set()) == "a_b_c_d_e"
    assert len(sanitize_sheet_name("x" * 40, set())) == 31
    renames = {"[P]": "(P)", "Feuil 1": "Feuil_1"}
    assert rename_sheet_refs("'[P]'!A1+'Feuil 1'!B2", renames) == "'(P)'!A1+'Feuil_1'!B2"


# -- pptx -------------------------------------------------------------------


def test_pptx_comment_authors_and_notes(tmp_path: Path, canary_config: Config) -> None:
    doc = extract_file(
        FIXTURES / "presentation.pptx", "presentation.pptx", canary_config, tmp_path / "w"
    )
    labels = {s.context.label for s in doc.segments}
    assert "notes diapositive 1" in labels and "diapositive 1" in labels
    _, output = run_pipeline(FIXTURES / "presentation.pptx", tmp_path, canary_config)
    authors = zip_read(output, "ppt/commentAuthors.xml").decode()
    assert 'name="Anonyme"' in authors and 'initials="A"' in authors
    slide = zip_read(output, "ppt/slides/slide1.xml").decode()
    assert 'descr="[PERSONNE_001]"' in slide


def test_pptx_image_blanked_then_replaced(
    tmp_path: Path, canary_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "photo.pptx"
    write_zip(source, pptx_entries(image=png_bytes()))
    remove_image_support(monkeypatch)
    _, output = run_pipeline(source, tmp_path / "a", canary_config)
    assert zip_read(output, "ppt/media/image1.png") == PLACEHOLDER_PNG
    install_fake_images(monkeypatch)
    _, output = run_pipeline(source, tmp_path / "b", canary_config)
    assert zip_read(output, "ppt/media/image1.png") == MARKER_PNG


def test_rename_part_updates_relationships(tmp_path: Path) -> None:
    from anonyma.extractors._ooxml import Package
    from anonyma.extractors._zip import read_package

    source = tmp_path / "x.docx"
    entries = docx_entries(image=png_bytes())
    entries["word/media/image1.emf"] = entries.pop("word/media/image1.png")
    entries["word/_rels/document.xml.rels"] = str(entries["word/_rels/document.xml.rels"]).replace(
        "media/image1.png", "media/image1.emf"
    )
    write_zip(source, entries)
    pkg = Package(read_package(source, None))
    pkg.rename_part("word/media/image1.emf", "word/media/image1.png")
    pkg.flush()
    rels = pkg.zip.entries["word/_rels/document.xml.rels"].decode()
    assert "media/image1.png" in rels and "image1.emf" not in rels
    assert "word/media/image1.png" in pkg.zip.entries
    assert b'Extension="png"' in pkg.zip.entries["[Content_Types].xml"]
