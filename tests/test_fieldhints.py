"""Field hints: column headers and JSON keys typing their values."""

from __future__ import annotations

import json
from pathlib import Path

from anonyma.config import Config
from anonyma.fieldhints import field_hint, header_hints, hint_of, with_column_hint, with_key_hint
from anonyma.models import Profile
from anonyma.pipeline import Pipeline
from tests.fixtures.generators.office import (
    SML,
    XML_DECL,
    content_types,
    odf_package,
    package_rels,
    rels,
    s_doc,
    write_zip,
)


def test_field_hint_vocabulary() -> None:
    assert field_hint("Prénom") == "nom"
    assert field_hint("NOM_COMPLET") == "nom"
    assert field_hint("Téléphone portable") == "telephone"
    assert field_hint("login") == "identifiant"
    assert field_hint("Date de naissance") == "naissance"
    assert field_hint("Commentaire") is None
    assert field_hint("Jean Dupont") is None


def test_labels_carry_only_the_hint_word() -> None:
    label = with_column_hint("C4", "nom")
    assert label == "C4 (colonne nom)"
    assert hint_of(label) == "nom"
    assert hint_of(with_key_hint("$.*", "identifiant")) == "identifiant"
    assert with_column_hint("C4", None) == "C4"
    assert hint_of("nom de feuille") is None


def test_header_detection_is_strict() -> None:
    header = {0: {0: "id", 1: "Nom", 2: "Prénom", 3: "Courriel", 4: "Commentaire"}}
    assert header_hints({**header, 1: {1: "LEBRUN"}}) == {1: "nom", 2: "nom", 3: "courriel"}
    # A vertical key/value layout is not a header.
    assert header_hints({0: {0: "Nom", 1: "Zéphyrin Quintavalle"}, 1: {0: "Courriel"}}) == {}
    # Data in the first row: no header.
    assert header_hints({0: {0: "Nom", 1: "Prénom", 2: "jean@exemple.test"}}) == {}


def _values(path: Path) -> set[str]:
    pipe = Pipeline(Config(profile=Profile.LIGHT, workers=1))
    analysis = pipe.analyze([path])
    try:
        return {e.value for e in analysis.entities}
    finally:
        pipe.close(analysis)


def test_csv_name_columns(tmp_path: Path) -> None:
    path = tmp_path / "clients.csv"
    path.write_text(
        "id;nom;prenom;commentaire\n1;LEBRUN;Margaux;Un martin-pêcheur a été vu.\n",
        encoding="utf-8",
    )
    values = _values(path)
    assert {"LEBRUN", "Margaux"} <= values
    assert not {"nom", "prenom"} & values


def test_json_keys(tmp_path: Path) -> None:
    data = {"utilisateurs": [{"login": "jbkowal88", "telephone": 33639984697, "ville": "Metz"}]}
    path = tmp_path / "export.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    assert {"jbkowal88", "33639984697", "Metz"} <= _values(path)


def test_xlsx_header_row(tmp_path: Path) -> None:
    strings = ["Nom", "Prénom", "Téléphone", "LEBRUN", "Margaux"]
    sst = "".join(f"<si><t>{s}</t></si>" for s in strings)
    header = "".join(f'<c r="{c}1" t="s"><v>{i}</v></c>' for i, c in enumerate("ABC"))
    data = '<c r="A2" t="s"><v>3</v></c><c r="B2" t="s"><v>4</v></c>'
    sheet = f'<sheetData><row r="1">{header}</row><row r="2">{data}</row></sheetData>'
    path = tmp_path / "registre.xlsx"
    write_zip(
        path,
        {
            "[Content_Types].xml": content_types(
                {
                    "xl/workbook.xml": f"{SML}.sheet.main+xml",
                    "xl/worksheets/sheet1.xml": f"{SML}.worksheet+xml",
                    "xl/sharedStrings.xml": f"{SML}.sharedStrings+xml",
                }
            ),
            "_rels/.rels": package_rels("xl/workbook.xml"),
            "xl/workbook.xml": s_doc(
                "workbook", '<sheets><sheet name="Registre" sheetId="1" r:id="rId1"/></sheets>'
            ),
            "xl/_rels/workbook.xml.rels": rels(
                [
                    ("rId1", "worksheet", "worksheets/sheet1.xml", False),
                    ("rId2", "sharedStrings", "sharedStrings.xml", False),
                ]
            ),
            "xl/sharedStrings.xml": XML_DECL
            + '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            + f"{sst}</sst>",
            "xl/worksheets/sheet1.xml": s_doc("worksheet", sheet),
        },
    )
    assert {"LEBRUN", "Margaux"} <= _values(path)


def test_ods_header_row(tmp_path: Path) -> None:
    def cell(text: str) -> str:
        return (
            f'<table:table-cell office:value-type="string"><text:p>{text}</text:p>'
            "</table:table-cell>"
        )

    body = (
        '<office:spreadsheet><table:table table:name="Planning">'
        f"<table:table-row>{cell('Semaine')}{cell('Agent')}{cell('Téléphone')}</table:table-row>"
        f"<table:table-row>{cell('S10')}{cell('ROUSSEL')}{cell('aucun')}</table:table-row>"
        "</table:table></office:spreadsheet>"
    )
    path = tmp_path / "planning.ods"
    odf_package(path, "application/vnd.oasis.opendocument.spreadsheet", body)
    assert "ROUSSEL" in _values(path)
