from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from PySide6.QtCore import QMimeData, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import QCloseEvent, QDragEnterEvent, QDropEvent

from anonyma.gui import errors, icons, theme
from anonyma.gui.controller import Phase
from anonyma.gui.main_window import PAGES, MainWindow
from anonyma.pipeline import OUTPUT_CONFLICT_MESSAGE
from tests.gui.conftest import DialogRecorder


def test_window_construction(window: MainWindow) -> None:
    window.show()
    assert window.windowTitle() == "Anonyma"
    assert window.nav.count() == len(PAGES)
    for key, _label, _icon in PAGES:
        window.show_page(key)
        assert window.current_page() == key
        assert window.stack.currentWidget() is window.pages[key]


def test_navigation_order_shortcuts_and_gap(window: MainWindow) -> None:
    """Four pages; Ctrl+4 reaches « Réglages », the only page below the gap."""
    from anonyma.gui.main_window import _GAP_ROLE

    keys = [window.nav.item(r).data(Qt.ItemDataRole.UserRole) for r in range(window.nav.count())]
    assert keys == ["processing", "review", "report", "settings"]
    assert [window.nav.item(r).data(_GAP_ROLE) for r in range(len(keys))] == [
        False,
        False,
        False,
        True,
    ]
    assert window.nav.item(3).toolTip() == "Réglages (Ctrl+4)"


def test_themes_apply(window: MainWindow) -> None:
    for mode in ("sombre", "systeme", "clair"):
        tokens = theme.apply_theme(mode)
        assert tokens.dark is theme.resolve_dark(mode)
        assert tokens.dark is {"sombre": True, "clair": False}.get(mode, tokens.dark)


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
    assert page.output_error.text() == OUTPUT_CONFLICT_MESSAGE
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


def test_qt_texts_in_french(qapp: Any) -> None:
    from PySide6.QtCore import QCoreApplication, QLocale

    from anonyma.app import install_french

    assert install_french(qapp)
    assert QLocale().language() is QLocale.Language.French
    assert QCoreApplication.translate("QPlatformTheme", "Cancel") == "Annuler"


def test_status_badges_are_not_clipped(window: MainWindow) -> None:
    from PySide6.QtGui import QFontMetrics

    from anonyma.gui.widgets import badge_column_width

    longest = "Non pris en charge"
    for view, column in (
        (window.report.files_table, 2),
        (window.report.failures_table, 1),
        (window.review.tree, 2),
    ):
        view.ensurePolished()
        needed = QFontMetrics(view.font()).horizontalAdvance(longest) + 24  # pill padding
        assert view.columnWidth(column) >= needed
        assert view.columnWidth(column) == badge_column_width(view, [longest])


def test_output_folder_and_browse_start_are_remembered(
    qtbot: Any,
    window: MainWindow,
    recorder: DialogRecorder,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from anonyma.config import Config
    from anonyma.gui import dialogs
    from anonyma.gui.context import AppContext

    sources = tmp_path / "sources"
    sources.mkdir()
    (sources / "a.txt").write_text("texte", encoding="utf-8")
    out = tmp_path / "sortie"
    out.mkdir()
    recorder.files = [sources / "a.txt"]
    window.processing._pick_files()
    assert window.processing.set_output_dir(out)

    starts: list[Path | None] = []

    def fake_open_files(*args: Any, start: Path | None = None, **_k: Any) -> list[Path]:
        starts.append(start)
        return []

    monkeypatch.setattr(dialogs, "open_files", fake_open_files)
    window.processing._pick_files()
    assert starts == [sources.resolve()]

    # A new session (same data/config.json) restores the output folder.
    config_path = window.controller.context.config_path
    assert config_path is not None
    reopened = AppContext(Config.load(config_path), config_path=config_path)
    assert reopened.last_dir("entrees") == sources.resolve()
    second = MainWindow(reopened)
    qtbot.addWidget(second)
    assert second.processing.output_edit.text() == str(out.resolve())
    assert second.controller.output_dir == out.resolve()
    second.controller.shutdown(10_000)
    second.settings.models.runner.wait_all(10_000)


def test_missing_remembered_output_is_not_restored(
    qtbot: Any, context: Any, recorder: DialogRecorder, tmp_path: Path
) -> None:
    context.set_ui_pref("output_dir", str(tmp_path / "disparu"))
    context.remember_dir("entrees", tmp_path / "disparu")
    assert context.last_dir("entrees") is None
    win = MainWindow(context)
    qtbot.addWidget(win)
    assert win.processing.output_edit.text() == ""
    assert win.controller.output_dir is None
    win.controller.shutdown(10_000)
    win.settings.models.runner.wait_all(10_000)


def test_update_offer_skip_and_install(
    qtbot: Any,
    window: MainWindow,
    recorder: DialogRecorder,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from anonyma import updater
    from anonyma.gui import dialogs
    from anonyma.gui.update import SKIPPED_VERSION_KEY

    release = updater.Release("9.0.0", "", "a.zip", "https://x/a.zip", 1, "00" * 32)
    answers = ["ignorer", "installer"]
    asked: list[str] = []

    def ask(_parent: Any, current: str, available: str) -> str:
        asked.append(available)
        return answers.pop(0)

    monkeypatch.setattr(dialogs, "ask_update", ask)
    staged = tmp_path / "maj" / "Anonyma"
    monkeypatch.setattr(updater, "prepare", lambda _r, _p, _c, proxy="": staged)
    launched: list[Path] = []
    versions: list[str] = []

    def launch(path: Path, *, version: str) -> None:
        launched.append(path)
        versions.append(version)

    monkeypatch.setattr(updater, "launch_installer", launch)

    window.show()
    window.updates._checked(release)
    assert window.context.ui_pref(SKIPPED_VERSION_KEY) == "9.0.0"
    window.updates._checked(release)  # skipped version: not offered again
    assert asked == ["9.0.0"]

    window.updates.offer(release)
    qtbot.waitUntil(lambda: bool(launched), timeout=10_000)
    assert launched == [staged]
    assert versions == ["9.0.0"]
    assert not window.isVisible()
    assert "redémarrer" in recorder.confirms[-1]


def test_update_refused_while_busy(
    window: MainWindow, recorder: DialogRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    from anonyma import updater

    monkeypatch.setattr(window, "busy", lambda: True)
    window.updates.install(updater.Release("9.0.0", "", "a.zip", "https://x", 1, ""))
    assert recorder.warnings and "en cours" in recorder.warnings[-1]
    assert not window.updates.busy()


def test_update_check_disabled_or_from_sources(
    window: MainWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    from anonyma import updater

    calls: list[int] = []
    monkeypatch.setattr(updater, "check_latest", lambda proxy="": calls.append(1))
    window.updates.check_on_startup()  # sources: never
    monkeypatch.setattr(updater, "can_self_update", lambda: True)
    window.context.config.check_updates = False
    window.updates.check_on_startup()  # disabled in Réglages
    assert not window.updates.runner.running()
    assert calls == []
    assert window.settings.check_updates.isChecked() is True


def test_report_titles_are_never_cut(window: MainWindow) -> None:
    """« Résidus possibles » is wider than a default section: it keeps its width."""
    header = window.report.files_table.horizontalHeader()
    for c in range(1, header.count()):
        assert header.sectionSize(c) >= header.sectionSizeHint(c)
