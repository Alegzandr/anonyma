from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from anonyma import pipeline
from anonyma.config import Config
from anonyma.models import Profile
from tests.canary import CANARY_TERMS


@pytest.fixture(autouse=True)
def portable_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Every test gets its own portable application folder."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("ANONYMA_HOME", str(home))
    pipeline._detector_cache.clear()
    pipeline._ocr_cache.clear()
    yield home


@pytest.fixture
def canary_config() -> Config:
    """Light profile, inline execution, canary terms always masked."""
    return Config(profile=Profile.LIGHT, workers=1, always_mask=list(CANARY_TERMS))
