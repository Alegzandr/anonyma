"""Every segment an extractor exposes must be rewritten by its writer.

For each fixture of this workstream, the whole text of EVERY segment (documents
and their children) is replaced by a unique marker, the tree is written, and the
output is re-extracted. No original segment text may survive anywhere in the
output (a writer may purge a segment instead of rewriting it, but never leave it
untouched), and the markers must show up. This catches any locator a writer
silently ignores.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest

from anonyma import libreoffice, registry
from anonyma.config import Config
from anonyma.extractors._legacy import MODE_LIBREOFFICE, TO_LEGACY
from anonyma.models import (
    FILENAME_SEGMENT_ID,
    WRITER_PLACEHOLDERS,
    ChildOutput,
    Document,
    DocumentStatus,
    Profile,
    Replacement,
    ReplacementPlan,
    WriteContext,
    new_id,
)
from anonyma.pipeline import extract_file
from tests.office.conftest import libreoffice_missing

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "contract"
OWN_FORMATS = frozenset(
    {"docx", "xlsx", "pptx", "odt", "ods", "odp", "rtf", "pdf", "csv", "tsv", "json", "xml"}
    | {"doc", "xls", "ppt"}
)
LIBREOFFICE_FORMATS = frozenset({"doc", "ppt"})
#: Texts shorter than this are too common to be checked as substrings.
MIN_CHECKED = 4


def own_fixtures() -> list[Path]:
    registry.load_plugins()
    return sorted(
        p for p in FIXTURES.iterdir() if p.is_file() and registry.detect_format(p) in OWN_FORMATS
    )


class _Context(WriteContext):
    def __init__(self, config: Config, outputs: dict[str, ChildOutput], work: Path) -> None:
        self.config = config
        self.mode = config.mode
        self.child_outputs = outputs
        self.work = work

    def temp_path(self, suffix: str = "") -> Path:
        self.work.mkdir(parents=True, exist_ok=True)
        return self.work / f"{new_id()}{suffix}"


class Marker:
    def __init__(self) -> None:
        self.count = 0
        self.originals: dict[str, str] = {}
        self.places: dict[str, str] = {}

    def __call__(self, original: str, place: str = "nom") -> str:
        self.count += 1
        marker = f"QZ{self.count:05d}X"
        self.originals[marker] = original
        self.places.setdefault(original.strip(), place)
        return marker


def write_everything(doc: Document, config: Config, tmp: Path, marker: Marker) -> Path | None:
    outputs: dict[str, ChildOutput] = {}
    for child in doc.children:
        path = write_everything(child, config, tmp, marker)
        name = marker(child.name) + PurePosixPath(child.name).suffix
        outputs[child.id] = ChildOutput(child.id, path, name)
    if doc.status is not DocumentStatus.EXTRACTED:
        return None
    writer = registry.writer_for(doc.format)
    assert writer is not None
    plan = ReplacementPlan(doc.id)
    for seg in doc.segments:
        if seg.id == FILENAME_SEGMENT_ID or not seg.text.strip():
            continue
        plan.by_segment[seg.id] = [
            Replacement(
                0,
                len(seg.text),
                seg.text,
                marker(seg.text, f"{seg.context.kind}:{seg.context.label}"),
                "TEST",
                "c",
            )
        ]
    destination = tmp / new_id() / f"sortie{writer.output_extension(doc)}"
    destination.parent.mkdir(parents=True)
    writer.write(doc, plan, destination, _Context(config, outputs, tmp / "work"))
    return destination


def all_texts(doc: Document) -> list[str]:
    return [s.text for d in doc.walk() for s in d.segments if s.id != FILENAME_SEGMENT_ID]


#: Empty flat OpenDocument files (no text at all), per legacy format.
_BLANK = {
    "doc": ("fodt", "text", "<office:text/>"),
    "xls": (
        "fods",
        "spreadsheet",
        "<office:spreadsheet><table:table table:name='A'/></office:spreadsheet>",
    ),
    "ppt": (
        "fodp",
        "presentation",
        "<office:presentation><draw:page draw:name='A'/></office:presentation>",
    ),
}


def libreoffice_baseline(fmt: str, config: Config, tmp: Path) -> set[str]:
    """Texts LibreOffice writes into ANY document of this format by itself (built-in
    style and font names, master placeholders, generator, application version): it
    writes them again after the writer replaced them, so they are not leaks."""
    soffice = libreoffice.find_soffice(config)
    assert soffice is not None
    ext, kind, body = _BLANK[fmt]
    blank = tmp / f"vide.{ext}"
    blank.parent.mkdir(parents=True)
    blank.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<office:document xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
        'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
        'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" office:version="1.3" '
        f'office:mimetype="application/vnd.oasis.opendocument.{kind}">'
        f"<office:body>{body}</office:body></office:document>",
        encoding="utf-8",
    )
    legacy = libreoffice.convert(blank, TO_LEGACY[fmt], tmp / "lo", soffice)
    doc = extract_file(legacy, legacy.name, config, tmp / "x")
    assert doc.status is DocumentStatus.EXTRACTED, [i.message for i in doc.issues]
    return {t.strip() for t in all_texts(doc)}


@pytest.mark.parametrize("fixture", own_fixtures(), ids=lambda p: p.name)
def test_every_segment_is_rewritten(fixture: Path, tmp_path: Path) -> None:
    config = Config(profile=Profile.LIGHT, workers=1)
    fmt = registry.detect_format(fixture)
    if fmt in LIBREOFFICE_FORMATS and libreoffice_missing():
        pytest.skip("LibreOffice non installé")
    source = extract_file(fixture, fixture.name, config, tmp_path / "x")
    assert source.status is DocumentStatus.EXTRACTED, [i.message for i in source.issues]
    marker = Marker()
    output = write_everything(source, config, tmp_path, marker)
    assert output is not None
    reread = extract_file(output, output.name, config, tmp_path / "re")
    for d in reread.walk():
        assert d.status is DocumentStatus.EXTRACTED, [i.message for i in d.issues]
    texts = all_texts(reread)
    joined = "\n".join(texts)
    regenerated = (
        libreoffice_baseline(fmt, config, tmp_path / "vide")
        if source.properties.get("legacy_mode") == MODE_LIBREOFFICE
        else set()
    )
    survivors = sorted(
        {
            original.strip()
            for original in marker.originals.values()
            if len(original.strip()) >= MIN_CHECKED
            and original.strip() not in WRITER_PLACEHOLDERS
            and original.strip() not in regenerated
            and original.strip() in joined
        }
    )
    assert not survivors, f"segments left untouched by the {fmt} writer: " + ", ".join(
        f"{t!r} ({marker.places.get(t)})" for t in survivors
    )
    assert any(m in joined for m in marker.originals), "no marker visible in the output"
