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
        "pj.txt",
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
        assert data["parametres"]["Mode"] == "balises"
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
