"""LLM pass: answer validation, chunking, model selection; real model when installed."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from anonyma import llm, paths
from anonyma.config import Config
from anonyma.detectors.llm import LlmDetector, select_model
from anonyma.models import Profile, Segment, SegmentContext, SegmentKind

REPO_MODELS = Path(os.environ.get("ANONYMA_TEST_MODELS", Path(__file__).parents[1] / "models"))


def test_parse_answer_validates_schema() -> None:
    raw = json.dumps(
        {
            "entites": [
                {"texte": "la DRH de l'agence de Nantes", "type": "IDENTIFIANT_INDIRECT"},
                {"texte": "x", "type": "PERSONNE"},  # too short
                {"texte": "Paris", "type": "INVENTE"},  # unknown type
                {"texte": 3, "type": "PERSONNE"},
                "pas un objet",
            ]
        }
    )
    assert llm.parse_answer(raw) == [
        llm.LlmMention("la DRH de l'agence de Nantes", "IDENTIFIANT_INDIRECT")
    ]
    assert llm.parse_answer("<think>pas du JSON") == []
    assert llm.parse_answer('{"autre": []}') == []


def test_chunks_cover_text_with_overlap() -> None:
    text = " ".join(f"mot{i}" for i in range(1000))
    parts = llm.chunks(text, size=500, overlap=50)
    assert parts[0][0] == 0
    end = 0
    for offset, chunk in parts:
        assert text[offset : offset + len(chunk)] == chunk
        assert offset <= end  # no gap
        end = offset + len(chunk)
    assert end == len(text)


def test_no_model_means_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(paths, "models_dir", lambda: tmp_path)
    assert select_model(Config()) is None
    assert not LlmDetector.is_available(Config())
    assert select_model(Config(llm_model="gliner-multi-pii")) is None


def test_segment_targeting() -> None:
    detector = LlmDetector(Config())
    assert detector.wanted("La DRH de l'agence de Nantes a validé.")
    assert detector.wanted("Nous avons revu le dossier avec Kervella hier.")
    assert not detector.wanted("Le comité a validé le budget.")
    assert not detector.wanted("12 345,00")
    assert LlmDetector(Config(llm_all_segments=True)).wanted("Le comité a validé le budget.")


@pytest.fixture
def llm_config(monkeypatch: pytest.MonkeyPatch) -> Iterator[Config]:
    monkeypatch.setattr(paths, "models_dir", lambda: REPO_MODELS)
    config = Config(profile=Profile.FULL, workers=1)
    if not llm.available() or select_model(config) is None:
        pytest.skip("llama-cpp-python ou modèle Qwen3 non installé")
    yield config


@pytest.mark.models
@pytest.mark.slow
def test_llm_finds_indirect_identifiers(llm_config: Config) -> None:
    text = "Après l'entretien, la DRH de l'agence de Nantes a validé la mutation de Mme Kervella."
    detector = LlmDetector(llm_config)
    entities = detector.detect([Segment("s", text, (), SegmentContext(SegmentKind.BODY))], "d")
    for e in entities:
        assert e.value == text[e.start : e.end]
    values = " | ".join(e.value for e in entities)
    assert "Kervella" in values
    assert "DRH" in values or "agence de Nantes" in values
