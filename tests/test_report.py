from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from anonyma.config import Config
from anonyma.models import (
    DocumentResult,
    DocumentStatus,
    Issue,
    Residue,
    Severity,
    VerificationResult,
)
from anonyma.pipeline import BatchResult, Pipeline
from anonyma.report import BatchReport
from tests.canary import CANARY_PERSON


def _result(tmp_path: Path) -> BatchResult:
    residue = Residue(
        location="dossier/[PERSONNE_001].txt",
        segment_label="corps",
        entity_type="PERSONNE",
        reason="original_value",
        detector="listes_metier",
        value=CANARY_PERSON,
    )
    leaky = DocumentResult(
        document_id="d1",
        output_name="dossier/<script>alert(1)</script>.txt",
        format="txt",
        output_format="txt",
        status=DocumentStatus.RESIDUE,
        entity_counts={"PERSONNE": 2},
        residue_count=1,
        issues=[Issue(Severity.ERROR, "verify.residue", "1 résidu(s) détecté(s).")],
        verification=VerificationResult(verified=True, residues=[residue]),
    )
    child = DocumentResult("c1", "pj.txt", "txt", "txt", DocumentStatus.FAILED, issues=[])
    ok = DocumentResult(
        "d2",
        "dossier/b.md",
        "md",
        "md",
        DocumentStatus.ANONYMIZED,
        entity_counts={"EMAIL": 1},
        children=[child],
        review_count=1,
        review_items=[("NIR", "[NIR_001]")],
    )
    return BatchResult(
        "20260101-000000-abcdef",
        tmp_path,
        [leaky, ok],
        started_at=datetime(2026, 1, 1, 10, 0, 0),
        finished_at=datetime(2026, 1, 1, 10, 1, 0),
    )


def test_from_result_counts(tmp_path: Path) -> None:
    report = BatchReport.from_result(_result(tmp_path))
    assert report.entity_counts == {"PERSONNE": 2, "EMAIL": 1}
    assert report.status_counts == {DocumentStatus.RESIDUE: 1, DocumentStatus.ANONYMIZED: 1}
    assert [f["fichier"] for f in report.failures] == [
        "dossier/<script>alert(1)</script>.txt",
        "dossier/b.md › pj.txt",  # an embedded item is located in its container
    ]
    assert report.residues[0]["type"] == "PERSONNE"
    assert report.review == [
        {"fichier": "dossier/b.md", "type": "NIR", "remplacement": "[NIR_001]"}
    ]
    assert "[NIR_001]" in report.to_markdown() and "[NIR_001]" in report.to_html()
    assert report.to_dict()["a_revoir"] == report.review
    assert report.quarantined  # the fixture's residue is certain
    report.residues[0]["certitude"] = "possible"
    assert not report.quarantined
    assert "| possible |" in report.to_markdown()


@pytest.mark.parametrize("suffix", [".json", ".md", ".html", ".htm", ".markdown"])
def test_save_dispatch_never_leaks(tmp_path: Path, suffix: str) -> None:
    report = BatchReport.from_result(_result(tmp_path))
    report.settings = {"Profil": "Léger", "Mode": "balises"}
    path = tmp_path / "sous" / f"rapport{suffix}"
    report.save(path)
    text = path.read_text(encoding="utf-8")
    assert CANARY_PERSON not in text
    assert "uintavalle" not in text
    if suffix == ".json":
        data = json.loads(text)
        assert data["lot"] == report.batch_id
        assert data["parametres_libelles"]["Mode"] == "balises"
    elif suffix in (".html", ".htm"):
        assert text.startswith("<!DOCTYPE html>")
        assert '<html lang="fr">' in text
        assert "<script>" not in text  # names are escaped
        assert "&lt;script&gt;" in text
        assert "http" not in text  # self-contained
        assert "Résidus" in text and "quarantaine" in text
    else:
        assert "## Résidus" in text and "## Échecs" in text


def test_save_unknown_extension(tmp_path: Path) -> None:
    report = BatchReport.from_result(_result(tmp_path))
    with pytest.raises(ValueError, match=r"\.json, \.md ou \.html"):
        report.save(tmp_path / "rapport.pdf")


def test_markdown_escapes_pipes(tmp_path: Path) -> None:
    result = _result(tmp_path)
    result.results[1].output_name = "a|b.md"
    assert "a\\|b.md" in BatchReport.from_result(result).to_markdown()


def test_html_empty_batch(tmp_path: Path) -> None:
    empty = BatchResult("lot", tmp_path, [], started_at=datetime(2026, 1, 1))
    html = BatchReport.from_result(empty).to_html()
    assert "Aucun élément." in html


def test_report_from_real_pipeline(tmp_path: Path, canary_config: Config) -> None:
    src = tmp_path / "in"
    src.mkdir()
    (src / f"{CANARY_PERSON}.txt").write_text(f"Signé {CANARY_PERSON}.\n", encoding="utf-8")
    result = Pipeline(canary_config).run([src], tmp_path / "out")
    report = BatchReport.from_result(result)
    for render in (report.to_html(), report.to_markdown(), json.dumps(report.to_dict())):
        assert "uintavalle" not in render
    assert report.entity_counts == {"PERSONNE": 2}


def test_incomplete_verification_is_reported(tmp_path: Path) -> None:
    unread = DocumentResult(
        "d3",
        "_quarantaine/document_0001.pdf",
        "pdf",
        "pdf",
        DocumentStatus.NEEDS_REVIEW,
        verification=VerificationResult(verified=True, unread=2),
    )
    result = BatchResult(
        "20260101-000000-abcdef",
        tmp_path,
        [unread],
        started_at=datetime(2026, 1, 1, 10, 0, 0),
        finished_at=datetime(2026, 1, 1, 10, 1, 0),
    )
    report = BatchReport.from_result(result)
    assert report.to_dict()["verifications_incompletes"] == [
        {"fichier": "_quarantaine/document_0001.pdf", "non_relus": 2}
    ]
    assert "## Vérification incomplète" in report.to_markdown()
    assert "Vérification incomplète" in report.to_html()


def _batch(tmp_path: Path, *results: DocumentResult) -> BatchReport:
    return BatchReport.from_result(
        BatchResult(
            "lot",
            tmp_path,
            list(results),
            started_at=datetime(2026, 1, 1, 10, 0, 0),
            finished_at=datetime(2026, 1, 1, 10, 1, 0),
        )
    )


def _doc(status: DocumentStatus, output_path: Path | None = None) -> DocumentResult:
    return DocumentResult("d", "a.txt", "txt", "txt", status, output_path=output_path)


def test_verdict(tmp_path: Path) -> None:
    ok = _doc(DocumentStatus.ANONYMIZED)
    assert _batch(tmp_path, ok).verdict() == ("1 fichier anonymisé et vérifié.", "ok")
    assert _batch(tmp_path, ok, ok).verdict()[0] == "2 fichiers anonymisés et vérifiés."
    review = _doc(DocumentStatus.NEEDS_REVIEW)
    report = _batch(tmp_path, ok, ok, review)
    assert report.verdict() == ("2 anonymisés · 1 à revoir", "warn")
    assert report.verdict_code() == "a_revoir"
    # Unverified images: NEEDS_REVIEW, but in quarantine (a sibling of the output).
    unread = _doc(DocumentStatus.NEEDS_REVIEW, tmp_path.parent / f"{tmp_path.name}_quarantaine/x")
    failed = _doc(DocumentStatus.FAILED)
    report = _batch(tmp_path, ok, review, unread, failed)
    assert report.quarantined == 1
    assert report.verdict() == (
        "1 anonymisé · 1 à revoir · 1 en quarantaine (ne pas diffuser) · 1 en échec",
        "ko",
    )
    assert report.verdict_code() == "quarantaine"
    assert report.to_dict()["quarantaine"] == 1
    assert _batch(tmp_path).verdict() == ("Aucun fichier traité.", "warn")
    assert _batch(tmp_path, failed).verdict_code() == "echec"


def test_wording_occurrences_labels_dates(tmp_path: Path) -> None:
    report = BatchReport.from_result(_result(tmp_path))
    markdown = report.to_markdown()
    html = report.to_html()
    for text in (markdown, html):
        # The issue message comes from the verifier, not from the report.
        assert "(s)" not in text.replace("1 résidu(s) détecté(s).", "")
        assert "Nombre" not in text
        assert "Occurrences" in text
        assert "Personne" in text and "PERSONNE" in text  # GUI label and code
        assert "Le 01/01/2026, de 10:00:00 à 10:01:00" in text
    assert "**1 anonymisé · 1 en quarantaine (ne pas diffuser)**" in markdown
    assert '<p class="verdict ko">' in html
    assert "2 fichiers · 3 occurrences masquées" in html
    incomplete = DocumentResult(
        "d3",
        "x.pdf",
        "pdf",
        "pdf",
        DocumentStatus.NEEDS_REVIEW,
        verification=VerificationResult(verified=True, unread=1),
    )
    assert "1 image ou page non relue" in _batch(tmp_path, incomplete).to_markdown()


def test_possible_residue_is_to_review_not_quarantine(tmp_path: Path) -> None:
    report = BatchReport.from_result(_result(tmp_path))
    report.residues[0]["certitude"] = "possible"
    assert report.quarantined == 0
    assert report.verdict() == ("1 anonymisé · 1 à revoir", "warn")
    assert report.verdict_code() == "a_revoir"
    assert report.to_dict()["quarantaine"] == 0
    assert "Résidu possible" in report.to_markdown()


def test_residues_are_located_and_labelled_in_french(tmp_path: Path) -> None:
    report = BatchReport.from_result(_result(tmp_path))
    html = report.to_html()
    markdown = report.to_markdown()
    import html as html_module

    for text in (html_module.unescape(html), markdown):
        assert "Fichier" in text.split("Résidus", 2)[-1]  # residue table has the file
        assert "valeur d'origine" in text and "Listes métier" in text
        assert "original_value" not in text and "listes_metier" not in text
        assert "child.omitted" not in text and "verify.residue" not in text
    assert "<td>dossier/&lt;script&gt;alert(1)&lt;/script&gt;.txt</td><td>dossier/" in html
    record = report.to_dict()["residus"][0]
    assert record["origine"] == "valeur_origine" and record["origine_libelle"] == "valeur d'origine"
    assert record["detecteur_libelle"] == "Listes métier" and record["type_libelle"] == "Personne"


def test_json_report_uses_codes_and_shares_the_exit_code(tmp_path: Path) -> None:
    data = BatchReport.from_result(_result(tmp_path)).to_dict()
    assert data["schema"] == 2
    assert data["verdict"] == "quarantaine" and data["code_sortie"] == 1
    assert [f["statut"] for f in data["fichiers"]] == ["residu", "anonymise"]
    assert [f["statut_libelle"] for f in data["fichiers"]] == ["Résidus", "Anonymisé"]
    assert data["echecs"][1] == {
        "fichier": "dossier/b.md › pj.txt",
        "statut": "echec",
        "statut_libelle": "Échec",
        "problemes": [],
        "codes": [],
    }
    assert data["nombre_fichiers"] == 2 and data["nombre_elements"] == 3
    assert data["nombre_elements_inclus"] == 1
    assert data["statuts_elements"] == {"anonymise": 1, "echec": 1, "residu": 1}
    assert data["occurrences"] == 3 and data["residus_certains"] == 1


def test_pseudonymization_wording(tmp_path: Path) -> None:
    from anonyma.models import Regime

    result = _result(tmp_path)
    result.regime = Regime.PSEUDONYMIZATION
    report = BatchReport.from_result(result)
    assert report.verdict()[0] == "1\xa0pseudonymisé · 1\xa0en quarantaine (ne pas diffuser)"
    assert "Pseudonymisé" in report.to_markdown()
    assert report.to_dict()["fichiers"][1]["statut_libelle"] == "Pseudonymisé"
