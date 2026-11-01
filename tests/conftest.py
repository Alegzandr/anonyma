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


@pytest.fixture(autouse=True)
def plain_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Output as a redirected terminal gets it, on every CI: Typer forces colours (ANSI
    codes in help and errors) when ``GITHUB_ACTIONS``, ``FORCE_COLOR`` or ``PY_COLORS``
    is set, and reads that once, at import."""
    import typer.rich_utils

    for name in ("GITHUB_ACTIONS", "FORCE_COLOR", "PY_COLORS", "TTY_COMPATIBLE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(typer.rich_utils, "FORCE_TERMINAL", None)


class _BlankOcr:
    """An OCR engine that reads no text on any image."""

    def recognize(self, image: object) -> list[object]:
        return []


@pytest.fixture
def blank_ocr(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pages and images are read by an OCR that finds nothing, for tests about something
    else than OCR: the real engine costs about half a second per rendered page."""
    blank = _BlankOcr()
    monkeypatch.setattr(pipeline, "ocr_for", lambda config: blank if config.ocr_enabled else None)


@pytest.fixture
def canary_config() -> Config:
    """Light profile, inline execution, canary terms always masked."""
    return Config(profile=Profile.LIGHT, workers=1, always_mask=list(CANARY_TERMS))
