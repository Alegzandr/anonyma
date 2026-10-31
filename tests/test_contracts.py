"""Contract tests every format plugin must pass.

Each workstream drops synthetic fixtures in ``tests/fixtures/contract/``. Every
fixture MUST contain the canary person and email (``tests/canary.py``) in its body
AND in every metadata field the format supports (author, subject, EXIF...). The
generator script of each fixture lives in ``tests/fixtures/generators/``.

For every fixture the full pipeline must: find the canaries, write a file in the
same family, re-read it, and find no canary anywhere (metadata purged or
anonymized), with status ANONYMIZED.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from anonyma import registry
from anonyma.config import Config
from anonyma.models import DocumentStatus, Extractor, Writer
from anonyma.pipeline import Pipeline, extract_file
from anonyma.textutils import find_all
from tests.canary import CANARY_EMAIL, CANARY_PERSON

FIXTURES = Path(__file__).parent / "fixtures" / "contract"
fixture_files = sorted(p for p in FIXTURES.iterdir() if p.is_file() and p.name != "README.md")


def test_registry_is_consistent() -> None:
    registry.load_plugins()
    for fmt, (has_extractor, has_writer) in registry.supported_formats().items():
        assert has_extractor and has_writer, f"format {fmt} needs both extractor and writer"
    for cls in registry._extractors.values():
        assert issubclass(cls, Extractor) and cls.name and cls.extensions
        assert all(ext.startswith(".") and ext == ext.lower() for ext in cls.extensions)
    for wcls in registry._writers.values():
        assert issubclass(wcls, Writer) and wcls.name and wcls.formats


def test_every_format_has_a_fixture() -> None:
    covered = {registry.detect_format(p) for p in fixture_files}
    missing = set(registry.supported_formats()) - covered
    assert not missing, f"formats without contract fixture: {sorted(missing)}"


@pytest.mark.parametrize("fixture", fixture_files, ids=lambda p: p.name)
def test_fixture_roundtrip(fixture: Path, tmp_path: Path, canary_config: Config) -> None:
    if registry.detect_format(fixture) is None and registry.unavailable:
        pytest.skip(f"format plugin unavailable here: {sorted(registry.unavailable.values())}")
    source_dir = tmp_path / "in"
    source_dir.mkdir()
    source = source_dir / fixture.name
    shutil.copy2(fixture, source)
    original_bytes = source.read_bytes()

    pipeline = Pipeline(canary_config)
    analysis = pipeline.analyze([source])
    doc = analysis.documents[0]
    assert doc.status is DocumentStatus.EXTRACTED, [i.message for i in doc.issues]
    for root in doc.walk():
        assert root.status is DocumentStatus.EXTRACTED, [i.message for i in root.issues]
    values = {e.value.casefold() for e in analysis.entities}
    assert any("quintavalle" in v for v in values), "canary person not found by extractor"

    result = pipeline.apply(analysis, tmp_path / "out")
    pipeline.close(analysis)
    top = result.results[0]
    assert top.status is DocumentStatus.ANONYMIZED, [i.message for i in top.issues]
    assert source.read_bytes() == original_bytes, "original modified"
    assert top.output_path and top.output_path.exists()

    reread = extract_file(top.output_path, top.output_name, canary_config, tmp_path / "re")
    for d in reread.walk():
        assert d.status is DocumentStatus.EXTRACTED
        for seg in d.segments:
            for canary in (CANARY_PERSON, CANARY_EMAIL, "Quintavalle"):
                assert not list(find_all(seg.text, canary)), (
                    f"canary left in {d.format} segment {seg.context.kind}/{seg.context.label}"
                )
