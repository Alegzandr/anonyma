"""Anonymisation benchmark (``tests/benchmark``): recall thresholds on the synthetic corpus.

Slow: the whole corpus goes through the command line, then the outputs are re-read
with independent means (see ``tests/benchmark/measure.py``). The STANDARD profile
needs the GLiNER model in ``models/`` (or ``ANONYMA_TEST_MODELS``).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

from anonyma import paths
from anonyma.config import Config
from anonyma.detectors.gliner import model_dir, model_spec
from anonyma.models import Profile
from tests.benchmark import generate, measure
from tests.benchmark.common import TRUTH

REPO_MODELS = Path(os.environ.get("ANONYMA_TEST_MODELS", Path(__file__).parents[1] / "models"))

#: Thresholds: >= 99 % of direct identifiers hidden in STANDARD (the target), LIGHT
#: (no model) a little lower; precision and untouched control sentences kept sane.
THRESHOLDS = {
    "leger": {"direct": 0.98, "precision": 0.85, "keep": 0.85},
    "standard": {"direct": 0.99, "precision": 0.85, "keep": 0.8},
}


def _ratio(pair: list[int]) -> float:
    ok, total = pair
    return ok / total if total else 1.0


def _link_models(home: Path) -> None:
    """``home/models`` pointing at the installed models (junction on Windows)."""
    target = home / "models"
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(REPO_MODELS.resolve()), str(target))
    else:
        target.symlink_to(REPO_MODELS.resolve(), target_is_directory=True)


def _run(tmp_path: Path, profile: str, with_models: bool) -> dict[str, Any]:
    home = tmp_path / "accueil"
    home.mkdir()
    if with_models:
        _link_models(home)
    argv = ["--profil", profile, "--travail", str(tmp_path / "travail"), "--sans-ecrire"]
    results = measure.main([*argv, "--accueil", str(home)])
    return results["profiles"][profile]  # type: ignore[no-any-return]


def _check(result: dict[str, Any], profile: str) -> None:
    agg = result["aggregate"]
    leaks = [
        f"{doc['slug']} {e['type']} {e['hits'][:2]}"
        for doc in result["docs"]
        for e in doc["entries"]
        if e["leaked"] and e["direct"]
    ]
    limits = THRESHOLDS[profile]
    assert _ratio(agg["direct"]) >= limits["direct"], leaks
    assert _ratio(agg["precision"]) >= limits["precision"]
    assert _ratio(agg["keep"]) >= limits["keep"]


@pytest.mark.slow
def test_generator_is_deterministic(tmp_path: Path) -> None:
    generate.main(tmp_path)
    produced = sorted(p.name for p in (tmp_path / "ground_truth").glob("*.json"))
    assert produced == sorted(p.name for p in TRUTH.glob("*.json"))
    for name in produced:
        fresh = (tmp_path / "ground_truth" / name).read_text(encoding="utf-8")
        assert fresh == (TRUTH / name).read_text(encoding="utf-8"), name


@pytest.mark.slow
def test_light_profile(tmp_path: Path) -> None:
    _check(_run(tmp_path, "leger", with_models=False), "leger")


@pytest.mark.slow
@pytest.mark.models
def test_standard_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with monkeypatch.context() as patched:
        patched.setattr(paths, "models_dir", lambda: REPO_MODELS)
        spec = model_spec(Config(profile=Profile.STANDARD))
        if spec is None or model_dir(spec) is None:
            pytest.skip("modèle GLiNER non installé")
    _check(_run(tmp_path, "standard", with_models=True), "standard")
