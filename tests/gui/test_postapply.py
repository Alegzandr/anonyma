"""Post-apply review: quarantine explained, residue jump, correct and re-apply."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from anonyma import registry
from anonyma.gui.main_window import MainWindow
from anonyma.gui.pages import report as report_module
from anonyma.models import DocumentStatus
from anonyma.pipeline import quarantine_dir
from anonyma.writers.text import TextWriter
from tests.gui.conftest import DialogRecorder, prepare

TIMEOUT = 30_000


class _LeakyWriter(TextWriter):
    def write(self, document, plan, destination, ctx):  # type: ignore[no-untyped-def]
        destination.write_bytes(document.source.read_bytes())
        return []


def test_quarantine_residue_jump_and_reapply(
    qtbot: Any,
    window: MainWindow,
    batch: Path,
    tmp_path: Path,
    recorder: DialogRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry.load_plugins()
    monkeypatch.setitem(registry._writers, "txt", _LeakyWriter)
    out = tmp_path / "sortie"
    prepare(qtbot, window, batch, out)
    c = window.controller
    with qtbot.waitSignal(c.result_ready, timeout=TIMEOUT):
        window.processing.run_button.click()
    assert c.result is not None
    assert {r.status for r in c.result.results} == {DocumentStatus.RESIDUE}

    report = window.report
    assert len(report.quarantined) == 2
    assert report.quarantine_table.rowCount() == 2
    reason = report.quarantine_table.item(0, 2).text()
    assert "sensible" in reason and "encore présente" in reason and "(s)" not in reason
    assert report.residue_rows
    assert "quarantaine" in report.banner.text()
    assert [b.text() for b in report.banner.action_buttons] == [
        "Voir pourquoi",
        "Corriger et réappliquer",
    ]

    # Jump from a residue to its place in the produced file.
    window.show()
    report.residues_table.selectRow(0)
    report.show_selected_residue()
    assert window.current_page() == "review"
    review = window.review
    qtbot.waitUntil(lambda: "Résidu repéré" in review.banner.text(), timeout=TIMEOUT)
    assert review.anonymized.extraSelections()
    assert review.source_combo.currentData() == "output"

    # « Corriger et réappliquer »: back to review, then apply replaces the old outputs.
    report.correct_button.click()
    assert window.current_page() == "review"
    monkeypatch.setitem(registry._writers, "txt", TextWriter)
    previous = [r.output_path for r in c.result.results]
    with qtbot.waitSignal(c.result_ready, timeout=TIMEOUT):
        review.apply_button.click()
    assert any("remplacés" in text for text in recorder.confirms)
    assert {r.status for r in c.result.results} == {DocumentStatus.ANONYMIZED}
    assert all(p is not None and not p.exists() for p in previous)
    assert not list(quarantine_dir(out).rglob("*.txt"))
    names = sorted(p.name for p in out.rglob("*.txt"))
    assert names == ["a.txt", "b.txt"]  # replaced, no « (2) » duplicates


def test_report_opens_folder_outputs(
    qtbot: Any,
    window: MainWindow,
    batch: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepare(qtbot, window, batch, tmp_path / "sortie")
    c = window.controller
    with qtbot.waitSignal(c.result_ready, timeout=TIMEOUT):
        window.processing.run_button.click()
    assert c.result is not None
    opened: list[Path] = []
    monkeypatch.setattr(report_module.ReportPage, "_open", staticmethod(opened.append))
    report = window.report
    report.files_table.selectRow(0)
    report.open_selected_output()
    first = c.result.results[0].output_path
    assert first is not None and opened == [first.parent]
    # A PST restored as a folder of eml: the folder itself is opened.
    folder = tmp_path / "sortie" / "boite"
    folder.mkdir()
    c.result.results[0].output_path = folder
    report.open_selected_output()
    assert opened[-1] == folder


def test_report_verdict_and_breakdown() -> None:
    counts = {DocumentStatus.ANONYMIZED: 6, DocumentStatus.NEEDS_REVIEW: 1}
    assert report_module.verdict(counts) == (
        "6 fichiers sur 7 sont prêts à être diffusés.",
        "warning",
    )
    assert report_module.breakdown(counts).replace(" ", " ") == (
        "7 fichiers : 6 anonymisés · 1 à revoir"
    )
    counts = {DocumentStatus.ANONYMIZED: 1, DocumentStatus.RESIDUE: 1}
    assert report_module.verdict(counts)[1] == "danger"
    assert report_module.verdict({DocumentStatus.ANONYMIZED: 1}) == (
        "Le fichier est anonymisé et vérifié.",
        "success",
    )
    assert report_module.batch_period("2026-10-31T14:08:25", "2026-10-31T14:09:02") == (
        "Traité le 31/10/2026 de 14:08 à 14:09"
    )
