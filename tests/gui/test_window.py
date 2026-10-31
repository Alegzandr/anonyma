from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from PySide6.QtCore import QMimeData, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import QCloseEvent, QDragEnterEvent, QDropEvent

from anonyma.gui import errors, icons, theme
from anonyma.gui.controller import OUTPUT_CONFLICT, Phase
from anonyma.gui.main_window import PAGES, MainWindow
from tests.gui.conftest import DialogRecorder


def test_window_construction(window: MainWindow) -> None:
    window.show()
    assert window.windowTitle() == "Anonyma"
    assert window.nav.count() == len(PAGES)
    for key, _label, _icon in PAGES:
        window.show_page(key)
        assert window.current_page() == key
        assert window.stack.currentWidget() is window.pages[key]


@pytest.mark.parametrize("mode", ["clair", "sombre", "systeme"])
def test_themes_apply(window: MainWindow, mode: str) -> None:
    tokens = theme.apply_theme(mode)
    assert tokens.dark is theme.resolve_dark(mode)
    if mode == "sombre":
        assert tokens.dark
    if mode == "clair":
        assert not tokens.dark
    theme.apply_theme("clair")


def test_entity_colours_are_distinct_and_legible() -> None:
    for dark in (False, True):
        seen = set()
        for t in ("PERSONNE", "ORGANISATION", "LIEU", "EMAIL", "TELEPHONE", "IBAN"):
            bg, fg = theme.entity_colors(t, dark)
            seen.add(bg.name())
            assert abs(bg.lightnessF() - fg.lightnessF()) > 0.5
        assert len(seen) == 6
    # Custom types get a stable colour.
    assert theme.entity_colors("PROJET", False) == theme.entity_colors("PROJET", False)


def test_icons_render(qapp: Any) -> None:
    for name in icons.names():
        assert not icons.icon(name, "#000000").isNull()
    assert not icons.app_icon().isNull()


def _mime(paths: list[Path]) -> QMimeData:
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(p)) for p in paths])
    return mime


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_drag_and_drop_adds_files(window: MainWindow, tmp_path: Path, qtbot: Any) -> None:
    folder = tmp_path / "entrees"
    (folder / "sous").mkdir(parents=True)
    (folder / "a.txt").write_text("alpha", encoding="utf-8")
    (folder / "sous" / "b.md").write_text("beta", encoding="utf-8")
    single = tmp_path / "seul.log"
    single.write_text("gamma", encoding="utf-8")
    page = window.processing
    mime = _mime([folder, single])

    enter = QDragEnterEvent(
        QPoint(5, 5),
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    page.drop_area.dragEnterEvent(enter)
    assert enter.isAccepted()
    drop = QDropEvent(
        QPointF(5, 5),
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    page.drop_area.dropEvent(drop)

    assert [p.name for p in window.controller.inputs] == ["entrees", "seul.log"]
    # The folder inventory runs in the background.
    qtbot.waitUntil(lambda: page.model.rowCount() == 3, timeout=10_000)
    names = sorted(r.name for r in page.model.rows)
    assert names == ["entrees/a.txt", "entrees/sous/b.md", "seul.log"]
    formats = sorted(r.format for r in page.model.rows)
    assert formats == ["log", "md", "txt"]

    # Dropping again, or adding a file inside an added folder, adds nothing.
    page.drop_area.dropEvent(drop)
    window.controller.add_inputs([folder / "a.txt"])
    assert window.controller.inputs == [folder.resolve(), single.resolve()]

    row = next(i for i, r in enumerate(page.model.rows) if r.name == "seul.log")
    page.table.selectRow(row)
    page._remove_selected()
    qtbot.waitUntil(lambda: page.model.rowCount() == 2, timeout=10_000)


def test_add_buttons_use_file_chooser(
    window: MainWindow, recorder: DialogRecorder, tmp_path: Path
) -> None:
    f = tmp_path / "x.txt"
    f.write_text("x", encoding="utf-8")
    recorder.files = [f]
    window.processing.add_files_button.click()
    assert window.controller.inputs == [f.resolve()]
    recorder.folder = tmp_path
    window.processing.add_folder_button.click()
    # The folder replaces the file it contains.
    assert window.controller.inputs == [tmp_path.resolve()]


def test_output_folder_inside_inputs_is_refused(window: MainWindow, tmp_path: Path) -> None:
    src = tmp_path / "in"
    src.mkdir()
    (src / "a.txt").write_text("x", encoding="utf-8")
    window.controller.add_inputs([src])
    page = window.processing
    assert not page.set_output_dir(src / "sortie")
    assert page.output_error.text() == OUTPUT_CONFLICT
    assert window.controller.output_dir is None
    assert not page.set_output_dir(tmp_path)  # contains the inputs
    assert page.set_output_dir(tmp_path / "out")
    assert page.output_error.isHidden()
    assert window.controller.output_dir == (tmp_path / "out").resolve()


def test_close_confirmation_while_running(
    window: MainWindow, recorder: DialogRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(window.controller, "phase", Phase.ANALYZING)
    recorder.confirm_answer = False
    event = QCloseEvent()
    window.closeEvent(event)
    assert not event.isAccepted()
    assert recorder.confirms and "en cours" in recorder.confirms[0]
    monkeypatch.setattr(window.controller, "phase", Phase.IDLE)
    event = QCloseEvent()
    window.closeEvent(event)
    assert event.isAccepted()
    # Geometry is persisted in Config.ui, not in QSettings.
    assert isinstance(window.context.config.ui.get("window_geometry"), str)


def test_unexpected_exception_dialog_hides_values(
    qapp: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    shown: list[str] = []
    monkeypatch.setattr(errors.QMessageBox, "critical", lambda _p, _t, text: shown.append(text))
    try:
        raise RuntimeError("Zéphyrin Quintavalle")
    except RuntimeError as exc:
        with caplog.at_level("ERROR", logger="anonyma"):
            errors._excepthook(RuntimeError, exc, exc.__traceback__)
    assert shown and "RuntimeError" in shown[0]
    assert "Quintavalle" not in shown[0]
    assert "Quintavalle" not in caplog.text
