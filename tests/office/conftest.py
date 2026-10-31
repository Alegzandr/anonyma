"""Shared helpers for the Office/PDF/data format tests, and contract-test
adjustments for the formats that need LibreOffice.

``.doc``/``.ppt`` contract fixtures can only be generated (from the OOXML ones)
and processed with LibreOffice. When LibreOffice is not installed:

* their round-trip contract tests are skipped with an explicit reason;
* ``test_every_format_has_a_fixture`` does not require a fixture for them.

This lives here because the shared ``tests/test_contracts.py`` is owned by the
orchestrator; the hook only touches LibreOffice-only formats.
"""

from __future__ import annotations

import functools
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from anonyma import libreoffice, registry
from anonyma.config import Config
from anonyma.models import DocumentStatus
from anonyma.pipeline import BatchResult, Pipeline

LIBREOFFICE_ONLY = frozenset({"doc", "ppt"})
REASON = "LibreOffice non installé : formats .doc/.ppt non testables sur cette machine"


def libreoffice_missing() -> bool:
    return libreoffice.find_soffice(Config()) is None


def _without_libreoffice_formats(test: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(test)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        original = registry.supported_formats

        def supported() -> dict[str, tuple[bool, bool]]:
            return {k: v for k, v in original().items() if k not in LIBREOFFICE_ONLY}

        registry.supported_formats = supported  # type: ignore[assignment]
        try:
            return test(*args, **kwargs)
        finally:
            registry.supported_formats = original  # type: ignore[assignment]

    return wrapper


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if not libreoffice_missing():
        return
    for item in items:
        if not isinstance(item, pytest.Function) or not item.nodeid.startswith(
            "tests/test_contracts.py"
        ):
            continue
        if item.originalname == "test_every_format_has_a_fixture":
            item.obj = _without_libreoffice_formats(item.obj)
        elif item.originalname == "test_fixture_roundtrip":
            fixture = item.callspec.params.get("fixture")
            if isinstance(fixture, Path) and fixture.suffix.lower() in (".doc", ".dot", ".ppt"):
                item.add_marker(pytest.mark.skip(reason=REASON))


# ---------------------------------------------------------------------------
# Helpers for the format tests
# ---------------------------------------------------------------------------


def run_pipeline(source: Path, tmp_path: Path, config: Config) -> tuple[BatchResult, Path]:
    """Anonymize ``source`` (copied into an input folder) and return the result and
    the output path of the top-level document."""
    in_dir = tmp_path / "in"
    in_dir.mkdir(exist_ok=True)
    copy = in_dir / source.name
    if source != copy:
        shutil.copy2(source, copy)
    pipeline = Pipeline(config)
    analysis = pipeline.analyze([copy])
    try:
        result = pipeline.apply(analysis, tmp_path / "out")
    finally:
        pipeline.close(analysis)
    top = result.results[0]
    assert top.output_path is not None, [i.message for i in top.issues]
    assert top.status in (DocumentStatus.ANONYMIZED, DocumentStatus.NEEDS_REVIEW), [
        i.message for i in top.issues
    ]
    return result, top.output_path
