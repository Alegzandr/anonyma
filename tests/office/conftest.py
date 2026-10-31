"""Shared helpers for the Office/PDF/data format tests.

LibreOffice-only formats are handled natively by ``tests/test_contracts.py``.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from anonyma import libreoffice
from anonyma.config import Config
from anonyma.models import DocumentStatus
from anonyma.pipeline import BatchResult, Pipeline


def libreoffice_missing() -> bool:
    return libreoffice.find_soffice(Config()) is None


# ---------------------------------------------------------------------------
# Helpers for the format tests
# ---------------------------------------------------------------------------


def run_pipeline(source: Path, tmp_path: Path, config: Config) -> tuple[BatchResult, Path]:
    """Anonymize ``source`` (copied into an input folder) and return the result and
    the output path of the top-level document."""
    in_dir = tmp_path / "in"
    in_dir.mkdir(parents=True, exist_ok=True)
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
