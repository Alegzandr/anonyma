"""Paranoid mode: proper nouns and suspicious tokens."""

from __future__ import annotations

from pathlib import Path

from anonyma.config import Config
from anonyma.detectors.paranoid import ParanoidDetector
from anonyma.models import DocumentStatus, Segment, SegmentContext, SegmentKind
from anonyma.pipeline import Pipeline


def flagged(text: str, config: Config | None = None) -> list[tuple[str, str]]:
    detector = ParanoidDetector(config or Config(paranoid=True))
    segment = Segment("s", text, (), SegmentContext(SegmentKind.BODY))
    return [(e.entity_type, e.value) for e in detector.detect([segment], "d")]


def test_inactive_without_flag() -> None:
    assert flagged("Rencontre avec Zorglub", Config(paranoid=False)) == []


def test_proper_nouns() -> None:
    hits = flagged("Hier, nous avons vu Kervella et Jean-Baptiste Quintavalle à Plougastel.")
    assert ("NOM_PROPRE", "Kervella") in hits
    assert ("NOM_PROPRE", "Jean-Baptiste Quintavalle") in hits
    assert ("NOM_PROPRE", "Plougastel") in hits
    assert ("NOM_PROPRE", "Hier") not in hits  # sentence start, common word


def test_capitalized_common_words_and_given_names() -> None:
    hits = flagged("Le Directeur a reçu Claire. Merci.")
    assert ("NOM_PROPRE", "Claire") in hits  # common word but frequent given name
    assert all(v not in ("Directeur", "Merci", "Le") for _, v in hits)


def test_unknown_word_at_sentence_start() -> None:
    assert ("NOM_PROPRE", "Zorglub") in flagged("Zorglub a appelé.")


def test_suspicious_tokens() -> None:
    hits = flagged("Réf AB12CD34, code x7k-92, dossier 1234567 et 2023.")
    assert ("SEQUENCE_SUSPECTE", "AB12CD34") in hits
    assert ("SEQUENCE_SUSPECTE", "x7k-92") in hits
    assert ("SEQUENCE_SUSPECTE", "1234567") in hits
    assert all(v != "2023" for _, v in hits)


def test_paranoid_pipeline_with_whitelist(tmp_path: Path) -> None:
    src = tmp_path / "in"
    src.mkdir()
    (src / "a.txt").write_text("Visite de Kervella chez Plougastel Industries.\n", encoding="utf-8")
    config = Config(workers=1, paranoid=True, whitelist=["Plougastel Industries"])
    config.profile = None
    from anonyma.models import Profile

    config.profile = Profile.LIGHT
    result = Pipeline(config).run([src], tmp_path / "out")
    (top,) = result.results
    assert top.status is DocumentStatus.ANONYMIZED, [i.message for i in top.issues]
    assert top.output_path is not None
    text = top.output_path.read_text(encoding="utf-8")
    assert "Kervella" not in text and "Plougastel Industries" in text


def test_capitalized_common_words_mid_sentence_are_names() -> None:
    hits = flagged("Réunion du projet Albatros avec les Tissages du Nord, URGENT, Madame.")
    assert ("NOM_PROPRE", "Albatros") in hits
    assert any(v.startswith("Tissages") for _, v in hits), hits
    assert all(v not in ("URGENT", "Madame", "Réunion") for _, v in hits)
