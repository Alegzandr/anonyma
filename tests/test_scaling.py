"""Work that must stay linear on big inputs (large sheets, many people, worker pools)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from anonyma import threads
from anonyma.config import Config
from anonyma.detectors.regex_fr import RegexDetector, RowLabels, _row_label_hint
from anonyma.models import Profile, Segment, SegmentContext, SegmentKind
from anonyma.pipeline import Pipeline


def _cell(ref: str, text: str) -> Segment:
    return Segment(ref, text, (), SegmentContext(SegmentKind.CELL, ref))


def test_row_labels_count_distinct_label_columns() -> None:
    labels = RowLabels()
    labels.setdefault(("", 1, 0), "1")
    labels[("", 1, 1)] = "nom"
    labels[("", 1, 1)] = "nom"  # the same cell twice counts once
    labels[("", 2, 3)] = "courriel"
    assert labels.labels("", 1) == 1
    assert labels.labels("", 2) == 1
    assert labels.labels("", 3) == 0
    assert labels.get(("", 1, 0)) == "1"


def test_vertical_label_still_hints_its_value() -> None:
    labels = RowLabels()
    assert _row_label_hint(_cell("A1", "Nom"), labels) is None
    assert _row_label_hint(_cell("B1", "Zéphyrin Quintavalle"), labels) == "nom"
    # A header row (several labels): its cells are labels, no hint.
    assert _row_label_hint(_cell("A2", "Nom"), labels) is None
    assert _row_label_hint(_cell("B2", "Courriel"), labels) is None
    assert _row_label_hint(_cell("C2", "zq@exemple.test"), labels) is None


def test_large_sheet_detection_is_linear() -> None:
    """Each cell used to rescan every label seen before it (minutes on 100k cells)."""
    detector = RegexDetector(Config(profile=Profile.LIGHT))

    def run(rows: int) -> float:
        segments = [
            _cell(f"{col}{row}", text)
            for row in range(1, rows + 1)
            for col, text in (("A", "Nom"), ("B", "Commentaire"), ("C", "texte libre"))
        ]
        started = time.perf_counter()
        detector.detect(segments, "doc")
        return time.perf_counter() - started

    small, large = run(1000), run(8000)
    assert large < small * 8 * 3  # linear, with room for noise


def test_model_threads_share_the_cpus_of_a_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("os.cpu_count", lambda: 16)
    monkeypatch.setattr(threads, "_pool_size", 0)
    assert threads.model_threads() == 0  # alone: the runtime's default
    threads.set_pool_size(1)
    assert threads.model_threads() == 0
    threads.set_pool_size(7)
    assert threads.model_threads() == 2
    threads.set_pool_size(64)
    assert threads.model_threads() == 1


def test_element_paths_match_libxml2() -> None:
    from anonyma.extractors._xml import parse_xml
    from anonyma.extractors.json_xml import element_paths

    data = (
        b'<r xmlns="urn:d" xmlns:a="urn:a" xmlns:b="urn:a"><x/><a:y/><b:y/><a:y/>'
        b'<z xmlns=""><q/><q/><w/><c xmlns="urn:e"/></z><x/><!--c--><?pi x?>'
        b'<n:k xmlns:n="urn:n"/></r>'
    )
    for sample in (data, b"<root><a/><b/><a><c/><c/></a><!-- x --><a/></root>"):
        tree = parse_xml(sample, doctype=True)
        paths = element_paths(tree.getroot())
        for element in tree.getroot().iter():
            if isinstance(element.tag, str):
                assert paths[element] == tree.getpath(element)


def test_flat_xml_is_linear(tmp_path: Path) -> None:
    def run(items: int) -> float:
        path = tmp_path / f"flat{items}.xml"
        body = "".join(f"<item><nom>Valeur {i}</nom><code>{i}</code></item>" for i in range(items))
        path.write_text(f"<liste>{body}</liste>", encoding="utf-8")
        started = time.perf_counter()
        Pipeline(Config(profile=Profile.LIGHT, workers=1)).analyze([path])
        return time.perf_counter() - started

    small, large = run(500), run(4000)
    assert large < small * 8 * 3


def test_chunked_inflate_matches_zlib() -> None:
    import os
    import zlib

    from anonyma.rawscan import _inflate

    payload = zlib.compress(os.urandom(200_000) + b"texte " * 50_000)
    for data in (payload + b"suite", payload[: len(payload) // 2], b"xx" + payload):
        view = memoryview(data)
        for start in (0, 2):
            for limit in (1, 70_000, 10**9):
                try:
                    expected: bytes | None = zlib.decompressobj().decompress(view[start:], limit)
                except zlib.error:
                    expected = None
                try:
                    got: bytes | None = _inflate(view, start, limit)
                except zlib.error:
                    got = None
                assert got == expected
