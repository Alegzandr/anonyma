"""Where the ``models`` tests find real models: only where ``ANONYMA_TEST_MODELS`` says.

There is no fallback to the repository's ``models/`` folder: real-model tests take
minutes, so a plain ``uv run pytest`` never runs them by accident. Opt in with
``ANONYMA_TEST_MODELS=<dossier des modèles> uv run pytest -m models``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

#: The folder holding the installed models (one sub-folder per model), or None.
TEST_MODELS: Path | None = (
    Path(os.environ["ANONYMA_TEST_MODELS"]) if os.environ.get("ANONYMA_TEST_MODELS") else None
)


def models_dir_or_skip() -> Path:
    """The models folder, or skip the test when ``ANONYMA_TEST_MODELS`` is not set."""
    if TEST_MODELS is None:
        pytest.skip("ANONYMA_TEST_MODELS non défini : tests sur modèles réels désactivés")
    return TEST_MODELS
