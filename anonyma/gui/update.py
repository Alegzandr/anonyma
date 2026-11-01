"""Startup update check and installation (see ``anonyma/updater.py``)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, Qt
from PySide6.QtWidgets import QProgressDialog, QWidget

from anonyma import __version__, updater
from anonyma.gui import dialogs
from anonyma.gui.tasks import Report, TaskRunner
from anonyma.logs import get_logger

if TYPE_CHECKING:
    from anonyma.gui.main_window import MainWindow

log = get_logger("gui.update")

#: ``Config.ui`` key of the version the user chose to ignore.
SKIPPED_VERSION_KEY = "maj_ignoree"


class UpdateManager(QObject):
    def __init__(self, window: MainWindow) -> None:
        super().__init__(window)
        self.window = window
        self.runner = TaskRunner(self)
        self.release: updater.Release | None = None
        self._progress: QProgressDialog | None = None
        self._cancelled = False
        self._downloading = False

    @property
    def parent_widget(self) -> QWidget:
        return self.window

    def busy(self) -> bool:
        """True while an update is being downloaded."""
        return self._downloading

    def shutdown(self, msecs: int = 10_000) -> None:
        self._cancelled = True
        self.runner.wait_all(msecs)

    # -- check ----------------------------------------------------------------------------

    def check_on_startup(self) -> None:
        """Ask GitHub for a newer release in the background (silent on failure)."""
        if not self.window.context.config.check_updates or not updater.can_self_update():
            return
        task = self.runner.create(lambda _report: updater.check_latest())
        task.succeeded.connect(self._checked)
        task.failed.connect(lambda _message, _type: None)  # offline: nothing to say
        task.start()

    def _checked(self, release: object) -> None:
        if not isinstance(release, updater.Release):
            return
        if self.window.context.ui_pref(SKIPPED_VERSION_KEY) == release.version:
            return
        self.offer(release)

    def offer(self, release: updater.Release) -> None:
        answer = dialogs.ask_update(self.parent_widget, __version__, release.version)
        if answer == "ignorer":
            self.window.context.set_ui_pref(SKIPPED_VERSION_KEY, release.version)
        elif answer == "installer":
            self.install(release)

    # -- installation ---------------------------------------------------------------------

    def install(self, release: updater.Release) -> None:
        if self.window.busy():
            dialogs.warning(
                self.parent_widget,
                "Un traitement ou un téléchargement est en cours : terminez-le ou annulez-le, "
                "puis relancez Anonyma pour installer la mise à jour.",
            )
            return
        self.release = release
        self._cancelled = False
        progress = QProgressDialog(
            f"Téléchargement d'Anonyma {release.version}…",
            "Annuler",
            0,
            100,
            self.parent_widget,
        )
        progress.setWindowTitle("Mise à jour d'Anonyma")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        progress.canceled.connect(self._cancel)
        progress.setValue(0)
        progress.show()
        self._progress = progress

        def work(report: Report) -> tuple[str, object]:
            # UpdateError messages are French and value-free: hand them to the UI as is.
            try:
                staged = updater.prepare(
                    release, lambda done, total: report((done, total)), lambda: self._cancelled
                )
            except updater.UpdateCancelledError:
                return ("annule", None)
            except updater.UpdateError as exc:
                return ("erreur", str(exc))
            return ("pret", staged)

        task = self.runner.create(work)
        task.progress.connect(self._on_progress)
        task.succeeded.connect(self._finished)
        task.failed.connect(self._failed)
        self._downloading = True
        task.start()

    def _cancel(self) -> None:
        self._cancelled = True

    def _on_progress(self, payload: object) -> None:
        if self._progress is None or not isinstance(payload, tuple):
            return
        done, total = payload
        if total:
            self._progress.setValue(min(99, int(done * 100 / total)))
            self._progress.setLabelText(
                f"Téléchargement d'Anonyma : {done / 1024**2:.0f} / {total / 1024**2:.0f} Mo"
            )

    def _close_progress(self) -> None:
        if self._progress is not None:
            self._progress.canceled.disconnect(self._cancel)
            self._progress.close()
            self._progress.deleteLater()
            self._progress = None

    def _failed(self, message: str, _type: str) -> None:
        self._downloading = False
        self._close_progress()
        dialogs.error(self.parent_widget, message, "Mise à jour d'Anonyma")

    def _finished(self, outcome: object) -> None:
        self._downloading = False
        self._close_progress()
        assert isinstance(outcome, tuple)
        kind, value = outcome
        if kind == "erreur":
            dialogs.error(self.parent_widget, str(value), "Mise à jour d'Anonyma")
        elif kind == "pret" and isinstance(value, Path):
            self._restart(value)

    def _restart(self, staged: Path) -> None:
        if not dialogs.confirm(
            self.parent_widget,
            "La mise à jour est prête. Anonyma va se fermer, installer la nouvelle version "
            "puis redémarrer.",
            title="Mise à jour d'Anonyma",
            ok_label="Redémarrer maintenant",
            cancel_label="Plus tard",
        ):
            return
        if not self.window.close():  # the user kept the window open (unsaved settings…)
            return
        try:
            updater.launch_installer(staged)
        except Exception as exc:
            log.warning("update installer not started: %s", type(exc).__name__)
            dialogs.error(
                None,
                f"Impossible de lancer l'installation de la mise à jour ({type(exc).__name__}).",
                "Mise à jour d'Anonyma",
            )
