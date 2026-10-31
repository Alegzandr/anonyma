"""GLiNER ONNX detector. Model tests run when the model is installed in ``models/``
(or ``ANONYMA_TEST_MODELS``) and are skipped otherwise."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from anonyma import paths
from anonyma.config import Config
from anonyma.detectors.gliner import GlinerDetector, model_dir, model_spec
from anonyma.gliner_onnx import GlinerOnnx, default_threads, split_words
from anonyma.models import DocumentStatus, Profile, Segment, SegmentContext, SegmentKind
from anonyma.pipeline import Pipeline

REPO_MODELS = Path(os.environ.get("ANONYMA_TEST_MODELS", Path(__file__).parents[1] / "models"))


@pytest.fixture
def installed(monkeypatch: pytest.MonkeyPatch) -> Iterator[Config]:
    monkeypatch.setattr(paths, "models_dir", lambda: REPO_MODELS)
    config = Config(profile=Profile.STANDARD, workers=1)
    spec = model_spec(config)
    if spec is None or model_dir(spec) is None:
        pytest.skip("modèle GLiNER non installé")
    yield config


def test_split_words_matches_gliner_splitter() -> None:
    text = "Jean-Pierre (DRH) écrit à j.dupont@x.fr, 12h30."
    words = [text[s:e] for s, e in split_words(text)]
    assert words[:4] == ["Jean-Pierre", "(", "DRH", ")"]
    assert "j" in words and "12h30" in words


def test_windows_overlap_and_respect_budget() -> None:
    model = GlinerOnnx.__new__(GlinerOnnx)
    model.max_words = 10
    model.window_overlap = 3
    pieces = [[1, 2]] * 25  # two sub-tokens per word
    windows = model._windows(0, pieces, budget=12)
    assert all(w.word_count <= 6 for w in windows)
    covered = set()
    for w in windows:
        covered |= set(range(w.first_word, w.first_word + w.word_count))
    assert covered == set(range(25))
    assert windows[1].first_word < windows[0].first_word + windows[0].word_count  # overlap


def test_unavailable_without_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(paths, "models_dir", lambda: tmp_path)
    assert not GlinerDetector.is_available(Config())
    assert not GlinerDetector.is_available(Config(gliner_model="inconnu"))


def test_custom_labels_and_threads() -> None:
    detector = GlinerDetector(Config(custom_labels={"nom de projet": "projet"}))
    assert detector.labels["nom de projet"] == "PROJET"
    assert default_threads(0) >= 1 and default_threads(64) == 1


@pytest.mark.models
def test_gliner_detects_french_entities(installed: Config) -> None:
    text = (
        "Bonjour, je suis Bérangère Kervella, responsable des achats chez Exemplia Conseil "
        "à Quimper. Mon collègue Thibault Morvan vous rappellera."
    )
    detector = GlinerDetector(installed)
    entities = detector.detect([Segment("s", text, (), SegmentContext(SegmentKind.BODY))], "d")
    by_type: dict[str, set[str]] = {}
    for e in entities:
        assert e.value == text[e.start : e.end]
        by_type.setdefault(e.entity_type, set()).add(e.value)
    assert {"Bérangère Kervella", "Thibault Morvan"} <= by_type.get("PERSONNE", set())
    assert "Quimper" in by_type.get("LIEU", set()) | by_type.get("ADRESSE", set())
    assert any("Exemplia" in v for v in by_type.get("ORGANISATION", set()))


@pytest.mark.models
def test_gliner_long_segments_are_chunked(installed: Config) -> None:
    filler = "Le comité a examiné les points inscrits à l'ordre du jour sans observation. " * 60
    text = filler + "Le rapport final a été rédigé par Gwenaëlle Le Bihan."
    detector = GlinerDetector(installed)
    entities = detector.detect([Segment("s", text, (), SegmentContext(SegmentKind.BODY))], "d")
    assert any(e.entity_type == "PERSONNE" and "Bihan" in e.value for e in entities)


@pytest.mark.models
def test_standard_profile_pipeline(installed: Config, tmp_path: Path) -> None:
    src = tmp_path / "in"
    src.mkdir()
    (src / "note.txt").write_text(
        "Compte rendu : Bérangère Kervella a rencontré Thibault Morvan à Quimper.\n",
        encoding="utf-8",
    )
    result = Pipeline(installed).run([src], tmp_path / "out")
    (top,) = result.results
    assert top.status in (DocumentStatus.ANONYMIZED, DocumentStatus.NEEDS_REVIEW), [
        i.message for i in top.issues
    ]
    assert top.output_path is not None
    text = top.output_path.read_text(encoding="utf-8")
    assert "Kervella" not in text and "Morvan" not in text
