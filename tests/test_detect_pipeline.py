"""End-to-end LIGHT pipeline on rich synthetic text, in every replacement mode."""

from __future__ import annotations

from pathlib import Path

import pytest

from anonyma import checksums
from anonyma.config import Config
from anonyma.models import DocumentStatus, Profile, ReplacementMode
from anonyma.pipeline import Pipeline


def _iban() -> str:
    bank, branch, account = "30001", "00794", "00012345678"
    bban = bank + branch + account + checksums.rib_key(bank, branch, account)
    iban = "FR" + checksums.iban_check_digits("FR", bban) + bban
    return " ".join(iban[i : i + 4] for i in range(0, len(iban), 4))


def _nir() -> str:
    body = "2850478006084"
    return body + (checksums.nir_key(body) or "")


ORIGINALS = [
    "Bérangère",
    "Kervella",
    "kervella",
    "06 12 34 56 78",
    "0612345678",
    "rue des Lilas",
    "AB-123-CD",
    "03/04/1985",
    "192.168.10.20",
    "bkervella",
]


def _write_inputs(root: Path) -> Path:
    src = root / "Dossiers"
    (src / "Kervella").mkdir(parents=True)
    body = f"""Objet : dossier de Mme Bérangère KERVELLA

Madame Bérangère Kervella (b.kervella@exemple.test, 06 12 34 56 78), née le 03/04/1985,
demeure 12 rue des Lilas, 29000 Quimper. IBAN {_iban()}. N° SS {_nir()}.
Véhicule AB-123-CD. Connexion depuis 192.168.10.20 avec le login : bkervella.
Mme Kervella rappellera au 0612345678. Copie à B. Kervella.
"""
    (src / "Kervella" / "courrier.txt").write_text(body, encoding="utf-8")
    (src / "CV_Berangere_Kervella.txt").write_text("Expérience : 10 ans.\n", encoding="utf-8")
    (src / "notes bkervella.txt").write_text("RAS\n", encoding="utf-8")
    return src


@pytest.mark.parametrize(
    "mode", [ReplacementMode.TAG, ReplacementMode.REALISTIC, ReplacementMode.BLACKOUT]
)
def test_rich_document_is_anonymized_and_verified(tmp_path: Path, mode: ReplacementMode) -> None:
    src = _write_inputs(tmp_path)
    config = Config(profile=Profile.LIGHT, workers=1, mode=mode)
    pipeline = Pipeline(config)
    analysis = pipeline.analyze([src])
    result = pipeline.apply(analysis, tmp_path / "out")
    pipeline.close(analysis)
    assert len(result.results) == 3
    for top in result.results:
        residues = top.verification.residues if top.verification else []
        assert top.status is DocumentStatus.ANONYMIZED, (
            [i.message for i in top.issues],
            [(r.entity_type, r.reason, r.detector, r.segment_label) for r in residues],
        )
        assert top.output_path is not None
        text = top.output_path.read_text(encoding="utf-8")
        names = "/".join(top.output_path.relative_to(tmp_path / "out").parts)
        for original in ORIGINALS:
            assert original.casefold() not in text.casefold(), (mode, original)
            assert original.casefold() not in names.casefold(), (mode, original)
    if mode is ReplacementMode.REALISTIC:
        courrier = next(
            r for r in result.results if r.output_path and "courrier" in r.output_path.name
        )
        assert courrier.output_path is not None
        text = courrier.output_path.read_text(encoding="utf-8")
        assert "Madame " in text and "@" in text and "[" not in text


def test_review_and_reject_keep_values(tmp_path: Path) -> None:
    src = tmp_path / "in"
    src.mkdir()
    (src / "a.txt").write_text("Rendez-vous avec M. Kervella à Quimper.\n", encoding="utf-8")
    pipeline = Pipeline(Config(profile=Profile.LIGHT, workers=1))
    analysis = pipeline.analyze([src])
    person = next(c for c in analysis.clusters.values() if c.entity_type == "PERSONNE")
    from anonyma.models import ReviewDecisions

    result = pipeline.apply(
        analysis, tmp_path / "out", ReviewDecisions(rejected_clusters={person.id})
    )
    pipeline.close(analysis)
    (top,) = result.results
    assert top.status is DocumentStatus.ANONYMIZED
    assert top.output_path is not None
    assert "Kervella" in top.output_path.read_text(encoding="utf-8")
