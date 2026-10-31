"""GLiNER ONNX detector. Model tests run when the model is installed in ``models/``
(or ``ANONYMA_TEST_MODELS``) and are skipped otherwise."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from anonyma import paths
from anonyma.config import Config
from anonyma.detectors.gliner import (
    GlinerDetector,
    _skip_leading_words,
    job_titles_near_people,
    model_dir,
    model_spec,
)
from anonyma.gliner_onnx import GlinerOnnx, default_threads, split_words
from anonyma.models import (
    DocumentStatus,
    Entity,
    Profile,
    Segment,
    SegmentContext,
    SegmentKind,
)
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


def _span(entity_type: str, text: str, value: str) -> Entity:
    start = text.index(value)
    return Entity(entity_type, value, "d", "s", start, start + len(value), 0.8, "gliner")


def test_job_titles_need_a_person_nearby() -> None:
    text = (
        "Séance présidée par Eléonore VIDAL, présidente de l'association.\n\n"
        "Le boucher du marché couvert propose désormais des produits bio, comme chaque été."
    )
    found = job_titles_near_people(
        text,
        [
            _span("PERSONNE", text, "Eléonore VIDAL"),
            _span("FONCTION", text, "présidente"),
            _span("FONCTION", text, "boucher du marché couvert"),
        ],
    )
    assert [e.value for e in found] == ["Eléonore VIDAL", "présidente"]
    # A cell or a signature line made of the title is kept as it is.
    cell = "Responsable RH"
    assert job_titles_near_people(cell, [_span("FONCTION", cell, cell)])


def test_person_spans_drop_greetings_and_verbs() -> None:
    text = "Bonjour Rémi, Rappeler M. Joubert"
    assert text[_skip_leading_words(text, 0, 12) : 12] == "Rémi"
    start = text.index("Rappeler")
    assert text[_skip_leading_words(text, start, len(text)) :] == "M. Joubert"
    other = "Martin Dupont"
    assert _skip_leading_words(other, 0, len(other)) == 0


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


def test_onnx_telemetry_is_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ONNX Runtime telemetry (ETW events on Windows) is turned off before loading."""
    import sys
    import types

    calls: list[str] = []

    class Encoding:
        ids = [1, 5, 2]
        word_ids = [None, 0, None]

    class FakeTokenizer:
        @staticmethod
        def from_file(path: str) -> FakeTokenizer:
            return FakeTokenizer()

        def no_truncation(self) -> None: ...
        def no_padding(self) -> None: ...

        def encode(self, *args: object, **kwargs: object) -> Encoding:
            return Encoding()

    class FakeSession:
        def __init__(self, *args: object, **kwargs: object) -> None:
            calls.append("session")

        def get_inputs(self) -> list[object]:
            return []

    ort = types.SimpleNamespace(
        SessionOptions=types.SimpleNamespace,
        GraphOptimizationLevel=types.SimpleNamespace(ORT_ENABLE_ALL=99),
        ExecutionMode=types.SimpleNamespace(ORT_SEQUENTIAL=0),
        InferenceSession=FakeSession,
        disable_telemetry_events=lambda: calls.append("telemetry off"),
    )
    monkeypatch.setitem(sys.modules, "onnxruntime", ort)
    monkeypatch.setitem(sys.modules, "tokenizers", types.SimpleNamespace(Tokenizer=FakeTokenizer))
    GlinerOnnx(tmp_path)
    assert calls == ["telemetry off", "session"]


def test_contentless_spans_are_not_plausible() -> None:
    """GLiNER spans seen on contentless cells: « Bien à vous » (PERSONNE, 0.31) and
    « Lundi » (LIEU, 0.71); a month that is a first name may still be a person."""
    from anonyma.detectors._filters import plausible

    assert not plausible("PERSONNE", "Bien à vous", 0.306)
    assert not plausible("LIEU", "Lundi", 0.714)
    assert not plausible("ORGANISATION", "Mars 2024", 0.9)
    assert plausible("PERSONNE", "Avril Kervella", 0.6)
    assert plausible("PERSONNE", "M. Berger", 0.5)
    assert plausible("LIEU", "Quimper", 0.5)
