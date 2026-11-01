"""Work that must stay linear on big inputs (large sheets, many people, worker pools)."""

from __future__ import annotations

import time

import pytest

from anonyma import threads
from anonyma.config import Config
from anonyma.detectors.regex_fr import RegexDetector, RowLabels, _row_label_hint
from anonyma.models import Profile, Segment, SegmentContext, SegmentKind


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
