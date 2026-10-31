"""GLiNER ONNX detector. Model tests run only when ``ANONYMA_TEST_MODELS`` points at
the installed models (see ``tests/modelpaths.py``) and are skipped otherwise."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

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
from tests.modelpaths import models_dir_or_skip


@pytest.fixture
def installed(monkeypatch: pytest.MonkeyPatch) -> Iterator[Config]:
    models = models_dir_or_skip()
    monkeypatch.setattr(paths, "models_dir", lambda: models)
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
@pytest.mark.slow
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
@pytest.mark.slow
def test_gliner_long_segments_are_chunked(installed: Config) -> None:
    filler = "Le comité a examiné les points inscrits à l'ordre du jour sans observation. " * 60
    text = filler + "Le rapport final a été rédigé par Gwenaëlle Le Bihan."
    detector = GlinerDetector(installed)
    entities = detector.detect([Segment("s", text, (), SegmentContext(SegmentKind.BODY))], "d")
    assert any(e.entity_type == "PERSONNE" and "Bihan" in e.value for e in entities)


@pytest.mark.models
@pytest.mark.slow
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


class _FakeEncoding:
    def __init__(self, ids: list[int], word_ids: list[int | None]) -> None:
        self.ids, self.word_ids = ids, word_ids


class _FakeTokenizer:
    """One token per word: ``1000 + first letter`` for capitalized words, else 500."""

    def encode(
        self, words: list[str], *, is_pretokenized: bool, add_special_tokens: bool
    ) -> _FakeEncoding:
        ids = [1000 + ord(w[0]) if w[:1].isupper() else 500 for w in words]
        word_ids: list[int | None] = list(range(len(words)))
        if add_special_tokens:
            return _FakeEncoding([1, *ids, 2], [None, *word_ids, None])
        return _FakeEncoding(ids, word_ids)

    def token_to_id(self, token: str) -> int:
        return 3


class _FakeSession:
    """Scores a span by its first and last words only (capitalized: a name), so that
    a span across two packed texts (« Alice . Bob ») would score too."""

    def __init__(self) -> None:
        self.calls = 0

    def run(self, names: list[str], feeds: dict[str, Any]) -> list[Any]:
        import numpy as np

        self.calls += 1
        ids, mask, spans = feeds["input_ids"], feeds["words_mask"], feeds["span_idx"]
        out = np.full((ids.shape[0], spans.shape[1], 1), -10.0, dtype=np.float32)
        for k in range(ids.shape[0]):
            words = {int(mask[k, j]) - 1: int(ids[k, j]) for j in range(ids.shape[1]) if mask[k, j]}
            for n, (a, b) in enumerate(spans[k]):
                if feeds["span_mask"][k, n] and words[int(a)] > 1000 and words[int(b)] > 1000:
                    out[k, n, 0] = 5.0 + (int(b) - int(a))
        return [out]


def _fake_model() -> tuple[GlinerOnnx, _FakeSession]:
    import threading

    model = GlinerOnnx.__new__(GlinerOnnx)
    model.max_width, model.max_words, model.max_tokens = 3, 384, 512
    model.ent_token, model.sep_token = "<<ENT>>", "<<SEP>>"
    model.batch_size, model.window_overlap, model.pad_id = 8, 48, 0
    model.tokenizer = _FakeTokenizer()  # type: ignore[assignment]
    model._suffix = [2]
    session = _FakeSession()
    model.session = session  # type: ignore[assignment]
    model.input_names = {"input_ids", "attention_mask", "words_mask", "span_idx", "span_mask"}
    model._lock = threading.Lock()
    model._prompt_cache = {}
    return model, session


def test_packed_short_texts_predict_like_one_by_one() -> None:
    texts = ["Alice", "Bob", "rien ici", "Jean Dupont est là", "", "Alice Bob Carole", "x"] * 30
    model, session = _fake_model()
    packed = model.predict(texts, ["personne"], flat=False)
    alone = [model.predict([t], ["personne"], flat=False)[0] for t in texts]
    assert packed == alone
    # No span joins two texts (« Alice . Bob » would score in this fake model).
    assert all(s.end <= len(t) for t, spans in zip(texts, packed, strict=True) for s in spans)
    # Thousands of tiny segments: a handful of model calls, not one window each.
    model, session = _fake_model()
    model.predict(["@odata.context", "Zéphyrin"] * 2000, ["personne"])
    assert session.calls <= 10


def test_segments_without_words_skip_the_model() -> None:
    from anonyma.detectors.gliner import has_words

    for text in ("3f2504e0-4f89-11d3-9a0c-0305e82c3301", "2026-01-14T10:12:00Z", "12 345,6", "a1"):
        assert not has_words(text), text
    assert has_words("@odata.context") and has_words("Jean")


def test_short_windows_overlap_by_half_at_most() -> None:
    """A long label prompt leaves a small budget: windows must still advance by half
    their length, not one word at a time (quadratic number of windows)."""
    model = GlinerOnnx.__new__(GlinerOnnx)
    model.max_words, model.window_overlap = 384, 48
    windows = model._windows(0, [[1]] * 2000, budget=20)
    assert len(windows) <= 2 * 2000 // 20 + 1
    covered = set()
    for w in windows:
        covered |= set(range(w.first_word, w.first_word + w.word_count))
    assert covered == set(range(2000))


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
