"""A double click on a file opens its preview (« Aperçu »), never its folder."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from anonyma.gui.main_window import MainWindow
from anonyma.gui.pages import report as report_module
from tests.gui.conftest import analyze, prepare

TIMEOUT = 30_000


def test_processing_double_click_opens_the_preview_of_that_file(
    qtbot: Any, window: MainWindow, batch: Path, tmp_path: Path
) -> None:
    prepare(qtbot, window, batch, tmp_path / "sortie")
    table = window.processing.table
    # Before the analysis there is nothing to preview: the page does not change.
    table.doubleClicked.emit(table.model().index(1, 0))
    assert window.current_page() == "processing"
    analyze(qtbot, window)
    analysis = window.controller.analysis
    assert analysis is not None
    table.doubleClicked.emit(table.model().index(1, 0))
    assert window.current_page() == "review"
    assert window.review.current_doc_id == analysis.documents[1].id


def test_report_double_click_opens_the_preview_not_the_folder(
    qtbot: Any,
    window: MainWindow,
    batch: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[Path] = []
    monkeypatch.setattr(report_module.ReportPage, "_open", staticmethod(opened.append))
    prepare(qtbot, window, batch, tmp_path / "sortie")
    c = window.controller
    with qtbot.waitSignal(c.result_ready, timeout=TIMEOUT):
        window.processing.run_button.click()
    assert c.result is not None
    window.show_page("report")
    table = window.report.files_table
    table.setCurrentCell(1, 0)
    table.doubleClicked.emit(table.model().index(1, 0))
    assert window.current_page() == "review"
    assert window.review.current_doc_id == c.result.results[1].document_id
    assert opened == []
