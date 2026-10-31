"""Legacy formats: LibreOffice detection, xlrd fallback, explicit errors."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from anonyma import libreoffice
from anonyma.config import Config
from anonyma.models import DocumentStatus
from anonyma.pipeline import extract_file
from tests.canary import CANARY_EMAIL, CANARY_PERSON
from tests.office.conftest import libreoffice_missing, run_pipeline
from tests.office.helpers import zip_read

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "contract"
EXE = "soffice.exe" if sys.platform == "win32" else "soffice"


def test_find_soffice_respects_settings(tmp_path: Path) -> None:
    install = tmp_path / "LibreOffice"
    exe = install / "program" / EXE
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    assert libreoffice.find_soffice(Config(libreoffice_path=str(exe))) == exe
    assert libreoffice.find_soffice(Config(libreoffice_path=str(install))) == exe
    assert (
        libreoffice.find_soffice(Config(use_libreoffice=False, libreoffice_path=str(exe))) is None
    )


def test_portable_copy_next_to_the_application_is_found(
    portable_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(libreoffice.shutil, "which", lambda name: None)
    exe = portable_home / "LibreOffice" / "program" / "soffice.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    assert libreoffice.find_soffice(Config()) == exe


def test_doc_without_libreoffice_is_an_explicit_error(
    tmp_path: Path, canary_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(libreoffice, "find_soffice", lambda config: None)
    source = tmp_path / "ancien.doc"
    source.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + bytes(1024))
    doc = extract_file(source, source.name, canary_config, tmp_path / "w")
    assert doc.status is DocumentStatus.FAILED
    assert [i.code for i in doc.issues] == ["libreoffice.required"]
    assert "LibreOffice requis" in doc.issues[0].message


def test_xls_without_libreoffice_becomes_xlsx(
    tmp_path: Path, canary_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(libreoffice, "find_soffice", lambda config: None)
    result, output = run_pipeline(FIXTURES / "tableau.xls", tmp_path, canary_config)
    assert output.suffix == ".xlsx"
    assert result.results[0].output_format == "xlsx"
    workbook = zip_read(output, "xl/workbook.xml").decode()
    assert 'name="(PERSONNE_001)"' in workbook and 'name="Synthèse"' in workbook
    sheet = zip_read(output, "xl/worksheets/sheet1.xml").decode()
    assert "[PERSONNE_001]" in sheet and "[EMAIL_001]" in sheet
    assert "<v>612345678.0</v>" in sheet  # numbers stay numbers
    for part in ("xl/worksheets/sheet1.xml", "xl/worksheets/sheet2.xml", "xl/workbook.xml"):
        data = zip_read(output, part).decode()
        assert CANARY_PERSON not in data and CANARY_EMAIL not in data
        assert "QUINTAVALLE" not in data


@pytest.mark.skipif(libreoffice_missing(), reason="LibreOffice non installé")
@pytest.mark.parametrize(
    ("modern", "target"),
    [("rapport.docx", "doc:MS Word 97"), ("presentation.pptx", "ppt:MS PowerPoint 97")],
)
def test_legacy_roundtrip_with_libreoffice(
    tmp_path: Path, canary_config: Config, modern: str, target: str
) -> None:
    soffice = libreoffice.find_soffice(canary_config)
    assert soffice is not None
    converted = libreoffice.convert(FIXTURES / modern, target, tmp_path / "conv", soffice)
    source = tmp_path / f"ancien{converted.suffix}"
    source.write_bytes(converted.read_bytes())
    result, output = run_pipeline(source, tmp_path, canary_config)
    assert output.suffix == converted.suffix
    assert result.results[0].status is DocumentStatus.ANONYMIZED
