"""Update dialog and installation outcome (no network: every updater call is faked)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from anonyma import __version__, updater
from anonyma.gui.main_window import MainWindow
from anonyma.gui.update import UpdateProgressDialog
from tests.gui.conftest import DialogRecorder

LONG = "Téléchargement d'Anonyma 123.456.789 : " + "très long message " * 40


def test_progress_dialog_keeps_its_size(qtbot: Any) -> None:
    dialog = UpdateProgressDialog(None, "Téléchargement d'Anonyma 9.0.0…")
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitExposed(dialog)
    size = dialog.size()
    assert size.width() == UpdateProgressDialog.WIDTH
    for text in (LONG, "court", LONG * 3, "Vérification et préparation de la mise à jour…"):
        dialog.set_label_text(text)
        qtbot.wait(10)
        assert dialog.size() == size
        assert dialog.label.width() <= size.width()
    dialog.set_label_text(LONG)
    assert dialog.label_text() == LONG
    assert dialog.label.text() != LONG and dialog.label.text().endswith("…")
    assert dialog.label.toolTip() == LONG
    dialog.set_busy()
    assert dialog.size() == size
    dialog.finish()


def test_progress_dialog_cancel_stays_open_until_finished(qtbot: Any) -> None:
    dialog = UpdateProgressDialog(None, "Téléchargement…")
    qtbot.addWidget(dialog)
    dialog.show()
    with qtbot.waitSignal(dialog.canceled, timeout=1000):
        dialog.cancel_button.click()
    assert dialog.isVisible() and not dialog.cancel_button.isEnabled()
    dialog.close()  # window close button: still waiting for the download to stop
    assert dialog.isVisible()
    dialog.finish()
    assert not dialog.isVisible()


def test_download_progress_never_widens_the_dialog(
    qtbot: Any, window: MainWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = window.updates
    release = updater.Release("9.0.0", "", "a.zip", "https://x/a.zip", 1, "00" * 32)
    monkeypatch.setattr(manager.runner, "create", lambda _fn: _NoTask())
    window.show()
    manager.install(release)
    progress = manager._progress
    assert progress is not None
    qtbot.waitExposed(progress)
    width = progress.width()
    total = 987_654_321_000
    for done in (0, 1024**2, total // 3, total - 1, total):
        manager._on_progress((done, total))
        qtbot.wait(5)
        assert progress.width() == width
    assert "Vérification" in progress.label_text()
    manager._downloading = False
    manager._close_progress()


class _NoTask:
    """Stands in for the download task (never started: the test drives progress)."""

    class _Signal:
        def connect(self, _slot: object) -> None:
            pass

    progress = succeeded = failed = _Signal()

    def start(self) -> None:
        pass


def _state(status: str, detail: str) -> updater.InstallState:
    return updater.InstallState(status, detail, age=10_000.0)


def test_previous_installation_is_reported(window: MainWindow, recorder: DialogRecorder) -> None:
    manager = window.updates
    manager.report_previous(None)
    manager.report_previous(updater.InstallState("demarre", "9.0.0", age=1.0))  # running
    assert not recorder.infos and not recorder.errors

    manager.report_previous(_state("ok", __version__))
    assert recorder.infos == [f"Anonyma a été mis à jour : version {__version__}."]

    # Reported installed, yet another version runs: the update did not take.
    manager.report_previous(_state("ok", "99.0.0"))
    assert "ne s'est pas appliquée" in recorder.errors[-1]
    for status, detail, expected in (
        ("echec", "attente", "ne s'est pas fermé à temps"),
        ("echec", "remplacement", "n'ont pas pu être remplacés"),
        ("echec", "restauration", "n'a pas pu être remise"),
        ("demarre", "9.0.0", "interrompue"),
    ):
        manager.report_previous(_state(status, detail))
        assert expected in recorder.errors[-1]
        assert updater.INSTALL_LOG in recorder.errors[-1]


def test_installer_failure_is_shown(
    window: MainWindow, recorder: DialogRecorder, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def refuse(_path: Path, *, version: str) -> None:
        raise updater.UpdateError("Le programme d'installation de la mise à jour n'a pas démarré.")

    monkeypatch.setattr(updater, "launch_installer", refuse)
    quits: list[int] = []
    monkeypatch.setattr("anonyma.gui.update.QCoreApplication.quit", lambda: quits.append(1))
    window.show()
    window.updates._restart(tmp_path / "Anonyma")
    assert "n'a pas démarré" in recorder.errors[-1]
    assert "relancez Anonyma" in recorder.errors[-1]
    assert quits == [1]
