"""Outlook thread indexes: found anywhere, rebuilt consistently, never left as is."""

from __future__ import annotations

import base64
import dataclasses
import json
from datetime import timedelta
from pathlib import Path

from anonyma import threadindex
from anonyma.config import Config
from anonyma.detectors.threads import ThreadIndexDetector
from anonyma.models import (
    Document,
    DocumentStatus,
    EntityType,
    ReplacementMode,
    Segment,
    SegmentContext,
    SegmentKind,
)
from anonyma.pipeline import Pipeline
from anonyma.verifier import Verifier

#: Thread started 2026-09-01 08:14:29 UTC (synthetic GUID); a reply block.
PARENT = "AQHdOenojzqcIdTmS3+aHC49T1prfA=="
PARENT_HEX = base64.b64decode(PARENT).hex().upper()
CHILD_HEX = PARENT_HEX + "0000AB12C3"
KEY = b"k" * 32


def test_decode_and_find() -> None:
    assert threadindex.decode(PARENT) is not None
    assert threadindex.decode(PARENT_HEX) == (base64.b64decode(PARENT), "HEX")
    assert threadindex.decode(CHILD_HEX.lower()) is not None
    start = threadindex.start_time(PARENT_HEX)
    assert start is not None and (start.year, start.month, start.day) == (2026, 9, 1)
    text = f"index={CHILD_HEX}; en-tête {PARENT} fin"
    assert [text[s:e] for s, e in threadindex.find(text)] == [CHILD_HEX, PARENT]
    # A random hexadecimal run or a GUID is no index (implausible start, wrong size).
    assert threadindex.find("01" + "ff" * 21) == []
    assert threadindex.find("34be8221-5b2a-4e8a-b93c-e8b15d765216") == []
    assert threadindex.decode("01" + "00" * 26) is None  # start in 1601


def test_rebuild_keeps_threads_and_moves_the_start() -> None:
    parent = threadindex.rebuild_text(PARENT_HEX, KEY, 280)
    child = threadindex.rebuild_text(CHILD_HEX, KEY, 280)
    assert parent is not None and child is not None
    assert child.startswith(parent) and child.endswith("0000AB12C3")  # reply extends parent
    assert parent.isupper() and parent != PARENT_HEX  # same encoding, new value
    assert parent[12:44] != PARENT_HEX[12:44]  # keyed GUID
    old, new = threadindex.start_time(PARENT_HEX), threadindex.start_time(parent)
    assert old is not None and new is not None
    assert abs(new - old - timedelta(days=280)) < timedelta(seconds=2)
    # The base64 header of the eml and the hex column of the CRM agree.
    as_b64 = threadindex.rebuild_text(PARENT, KEY, 280)
    assert as_b64 is not None and base64.b64decode(as_b64).hex().upper() == parent
    neutral = threadindex.rebuild_text(CHILD_HEX, KEY, None)
    assert neutral is not None and neutral.endswith("00000000C3")  # no interval left
    start = threadindex.start_time(neutral)
    assert start is not None and start.year == 1970


def test_an_index_left_as_is_is_a_certain_residue() -> None:
    """The verifier catches an index nothing rebuilt (it gives the real date away),
    by the table of original values and by the detector alike."""
    doc = Document(source=Path("x"), name="o.json", format="json")
    doc.status = DocumentStatus.EXTRACTED
    doc.segments = [Segment("s0", CHILD_HEX, (0,), SegmentContext(SegmentKind.CELL, "$.x"))]
    detectors = [ThreadIndexDetector(Config())]
    by_table = Verifier(Config(), []).verify(doc, [(CHILD_HEX, EntityType.THREAD_INDEX)], set(), [])
    by_detector = Verifier(Config(), detectors).verify(doc, [], set(), [])
    for result in (by_table, by_detector):
        assert result.certain_residues, result.residues
    rebuilt = threadindex.rebuild_text(CHILD_HEX, KEY, 30)
    assert rebuilt is not None
    doc.segments[0] = dataclasses.replace(doc.segments[0], text=rebuilt)
    clean = Verifier(Config(), detectors).verify(
        doc, [(CHILD_HEX, EntityType.THREAD_INDEX)], {rebuilt}, []
    )
    assert clean.clean


def test_kept_identifiers_still_rebuild_indexes(tmp_path: Path, canary_config: Config) -> None:
    """--conserver-identifiants keeps hexadecimal ids, but not the dates inside an index."""
    src = tmp_path / "in"
    src.mkdir()
    records = [{"conversationindex": PARENT_HEX}, {"conversationindex": CHILD_HEX}]
    (src / "page.json").write_text(json.dumps({"value": records}), encoding="utf-8")
    for mode in ReplacementMode:
        config = dataclasses.replace(canary_config, mode=mode, mask_opaque_ids=False)
        out = tmp_path / f"out-{mode.value}"
        [top] = Pipeline(config).run([src / "page.json"], out).results
        assert top.status is DocumentStatus.ANONYMIZED, [i.message for i in top.issues]
        assert top.output_path is not None
        data = json.loads(top.output_path.read_text(encoding="utf-8"))["value"]
        parent, child = (r["conversationindex"] for r in data)
        assert parent != PARENT_HEX and threadindex.decode(parent) is not None, mode
        assert child.startswith(parent) and len(child) == len(CHILD_HEX), mode
