"""RTF: tokenizer, splice writer, purge, pictures."""

from __future__ import annotations

from pathlib import Path

import pytest

from anonyma.config import Config
from anonyma.extractors._media import BLANKED_CODE, PLACEHOLDER_PNG
from anonyma.extractors._rtf import encode_text, parse_rtf
from anonyma.models import SegmentKind
from anonyma.pipeline import extract_file
from anonyma.writers.rtf import apply_edits
from tests.canary import CANARY_EMAIL, CANARY_PERSON
from tests.fixtures.generators.office import rtf_document
from tests.office.conftest import run_pipeline
from tests.office.helpers import MARKER_PNG, install_fake_images, png_bytes, remove_image_support

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "contract"


def texts(raw: bytes) -> list[str]:
    return [u.text for u in parse_rtf(raw).units]


def test_unicode_hex_and_groups_are_joined() -> None:
    raw = rb"{\rtf1\ansi\ansicpg1252 Par {\b Z\u233?}phyrin Q\'e9.\par}"
    assert texts(raw) == ["Par Zéphyrin Qé."]


def test_uc_fallback_and_surrogates() -> None:
    raw = rb"{\rtf1\uc2 a\u233\'e9\'e9 b\uc0\u-10179\u-8704 c\par}"
    assert texts(raw) == ["aé b😀c"]


def test_codepage_from_font_charset() -> None:
    raw = rb"{\rtf1\ansi\deff0{\fonttbl{\f0\fcharset204 Arial;}}\f0 \'cf\'e5\'f2\'f0\par}"
    assert texts(raw) == ["Петр"]


def test_destinations_kinds() -> None:
    parsed = parse_rtf((FIXTURES / "lettre.rtf").read_bytes())
    kinds = {u.label: u.kind for u in parsed.units}
    assert kinds["author"] is SegmentKind.METADATA
    assert kinds["en-tête"] is SegmentKind.HEADER
    assert kinds["note de bas de page"] is SegmentKind.NOTE
    assert kinds["commentaire"] is SegmentKind.COMMENT
    assert kinds["code de champ"] is SegmentKind.ATTRIBUTE
    assert kinds["tableau"] is SegmentKind.CELL
    deleted = [u for u in parsed.units if u.deleted]
    assert deleted and CANARY_EMAIL in deleted[0].text


def test_encode_text() -> None:
    assert encode_text("a{b}\\é", 1) == rb"a\{b\}\\\u233?"
    assert encode_text("é", 0) == rb"\u233 "
    assert encode_text("😀", 1) == rb"\u-10179?\u-8704?"


def test_edits_keep_control_word_delimiters() -> None:
    raw = rb"\plain\insrsid1 Texte"
    assert apply_edits(raw, [(6, 16, b"")]) == rb"\plain Texte"
    raw = rb"\b{\*\x y} suite"
    assert apply_edits(raw, [(2, 10, b"")]) == rb"\b  suite"


def test_rtf_output_purged(tmp_path: Path, canary_config: Config) -> None:
    _, output = run_pipeline(FIXTURES / "lettre.rtf", tmp_path, canary_config)
    data = output.read_bytes()
    assert rb"{\info}" in data
    for marker in (rb"\userprops", rb"\docvar", rb"\rsid", rb"\revauth", rb"\deleted", b"ancien"):
        assert marker not in data
    assert rb"{\*\atnauthor Anonyme}" in data
    assert rb"\pard\plain Rapport" in data
    assert rb"{\b [PERSONNE_001]}." in data
    assert CANARY_EMAIL.encode() not in data and b"Quintavalle" not in data


def test_rtf_picture_blanked_then_replaced(
    tmp_path: Path, canary_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "photo.rtf"
    source.write_bytes(rtf_document(picture=png_bytes()))
    doc = extract_file(source, source.name, canary_config, tmp_path / "w")
    assert len(doc.children) == 1
    remove_image_support(monkeypatch)
    result, output = run_pipeline(source, tmp_path / "a", canary_config)
    assert PLACEHOLDER_PNG.hex().encode() in unwrap(output.read_bytes())
    assert any(i.code == BLANKED_CODE for i in result.results[0].issues)
    assert CANARY_PERSON.encode("cp1252") not in output.read_bytes()
    install_fake_images(monkeypatch)
    _, output = run_pipeline(source, tmp_path / "b", canary_config)
    assert MARKER_PNG.hex().encode() in unwrap(output.read_bytes())


def unwrap(data: bytes) -> bytes:
    return data.replace(b"\r", b"").replace(b"\n", b"")
