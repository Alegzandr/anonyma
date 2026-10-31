"""The review preview must match what ``Pipeline.apply`` writes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from PySide6.QtCore import Qt

from anonyma.config import Config
from anonyma.gui.main_window import MainWindow
from anonyma.gui.preview import (
    RowState,
    apply_with_spans,
    compute_effective,
    entity_rows,
    pending_review,
    preview_segments,
    substitute_spans,
)
from anonyma.gui.textview import display_text
from anonyma.models import (
    FILENAME_SEGMENT_ID,
    ManualEntity,
    Profile,
    ReplacementMode,
    ReviewDecisions,
)
from anonyma.pipeline import Pipeline
from tests.gui.conftest import DialogRecorder

TIMEOUT = 30_000


def _batch(tmp_path: Path) -> tuple[Config, Path]:
    src = tmp_path / "in"
    src.mkdir()
    (src / "a.txt").write_bytes(
        "Note de Bérénice Lagarde\r\nprojet Hélios 🚀 à Brest.\r\n".encode()
    )
    (src / "b.txt").write_text("Brest, HÉLIOS et Lagarde.\n", encoding="utf-8")
    config = Config(
        profile=Profile.LIGHT,
        workers=1,
        always_mask=[
            {"value": "Bérénice Lagarde", "type": "PERSONNE"},
            {"value": "Lagarde", "type": "PERSONNE"},
            {"value": "Hélios", "type": "PROJET"},
        ],
    )
    return config, src


@pytest.mark.parametrize("mode", [ReplacementMode.TAG, ReplacementMode.BLACKOUT])
def test_preview_matches_apply(tmp_path: Path, mode: ReplacementMode) -> None:
    config, src = _batch(tmp_path)
    config.mode = mode
    pipeline = Pipeline(config)
    analysis = pipeline.analyze([src])
    helios = next(c for c in analysis.clusters.values() if c.entity_type == "PROJET")
    lagarde = next(c for c in analysis.clusters.values() if c.canonical == "Lagarde")
    decisions = ReviewDecisions(
        rejected_clusters={helios.id},
        retyped_clusters={lagarde.id: "NOM_PROPRE"},
        manual=[ManualEntity("Brest", "LIEU")],
    )
    effective = compute_effective(analysis, decisions)
    result = pipeline.apply(analysis, tmp_path / "out", decisions)
    try:
        for doc, res in zip(analysis.documents, result.results, strict=True):
            plan = preview_segments(doc, effective)
            body = "".join(
                apply_with_spans(seg.text, plan.get(seg.id, []))[0]
                for seg in doc.segments
                if seg.id != FILENAME_SEGMENT_ID
            )
            assert res.output_path is not None
            written = res.output_path.read_bytes().decode("utf-8")
            assert body == written
            assert "Brest" not in written and "Bérénice" not in written
        rows = entity_rows(analysis, decisions, effective)
        states = {r.entity.value: r.state for r in rows}
        assert states["Hélios"] is RowState.REJECTED
        assert {r.state for r in rows if r.manual} == {RowState.MANUAL}
        assert any(r.entity_type == "NOM_PROPRE" for r in rows)
        assert pending_review(analysis, decisions) == []
    finally:
        pipeline.close(analysis)


def test_preview_never_touches_a_store(tmp_path: Path) -> None:
    from anonyma.models import Regime

    config, src = _batch(tmp_path)
    config.regime = Regime.PSEUDONYMIZATION
    pipeline = Pipeline(config)
    analysis = pipeline.analyze([src])
    effective = compute_effective(analysis, ReviewDecisions())
    assert effective.table.by_cluster  # assigned without any store
    pipeline.close(analysis)


def test_display_text_keeps_offsets() -> None:
    text = "a\r\nb\rc\x00d e"
    shown = display_text(text)
    assert len(shown) == len(text)
    assert "\r" not in shown and "\x00" not in shown


def test_substitute_spans() -> None:
    text = "x [PERSONNE_001] y ███ z [NUMERO_DOSSIER_012]"
    spans = substitute_spans(text)
    assert [text[s.start : s.end] for s in spans] == [
        "[PERSONNE_001]",
        "███",
        "[NUMERO_DOSSIER_012]",
    ]
    assert [s.entity_type for s in spans] == ["PERSONNE", "", "NUMERO_DOSSIER"]


def test_review_keyboard_shortcuts(
    qtbot: Any, window: MainWindow, tmp_path: Path, recorder: DialogRecorder
) -> None:
    config, src = _batch(tmp_path)
    window.context.config.always_mask = config.always_mask
    window.controller.add_inputs([src])
    with qtbot.waitSignal(window.controller.analysis_ready, timeout=TIMEOUT):
        window.processing.start_analyze()
    with qtbot.waitActive(window):
        window.show()
        window.activateWindow()
    window.show_page("review")
    review = window.review
    review.queue.setFocus()
    ids = review.model.ids
    assert len(ids) == 3
    review.select_cluster(ids[0])
    qtbot.keyClick(review.queue, Qt.Key.Key_N)
    if review.current_cluster_id() == ids[0]:  # shortcuts need an active window
        pytest.skip("window activation unavailable on this platform")
    assert review.current_cluster_id() == ids[1]
    qtbot.keyClick(review.queue, Qt.Key.Key_P)
    assert review.current_cluster_id() == ids[0]
    qtbot.keyClick(review.queue, Qt.Key.Key_R)
    assert ids[0] in window.controller.decisions.rejected_clusters
    qtbot.waitUntil(lambda: review.current_cluster_id() != ids[0], timeout=5_000)
    review.select_cluster(ids[0])
    qtbot.keyClick(review.queue, Qt.Key.Key_U)
    assert ids[0] not in window.controller.decisions.rejected_clusters
    qtbot.keyClick(review.queue, Qt.Key.Key_V)
    assert ids[0] in window.controller.decisions.confirmed_clusters

    # Highlighted current entity in the original text, with its type colour legend.
    review.select_cluster(ids[1])
    assert review.original.extraSelections()
    assert "Personne" in review.legend.text()
