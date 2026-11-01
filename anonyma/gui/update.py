"""Startup update check and installation (see ``anonyma/updater.py``)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, override

from PySide6.QtCore import QCoreApplication, QObject, Qt, Signal
from PySide6.QtGui import QCloseEvent, QResizeEvent
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QProgressBar,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from anonyma import __version__, updater
from anonyma.gui import dialogs
from anonyma.gui.tasks import Report, TaskRunner
from anonyma.logs import get_logger

if TYPE_CHECKING:
    from anonyma.gui.main_window import MainWindow

log = get_logger("gui.update")

#: ``Config.ui`` key of the version the user chose to ignore.
SKIPPED_VERSION_KEY = "maj_ignoree"
TITLE = "Mise à jour d'Anonyma"


def _log_hint() -> str:
    return f"Le détail des étapes est consigné dans {Path('data') / 'logs' / updater.INSTALL_LOG}."


class UpdateProgressDialog(QDialog):
    """Download progress with a fixed size: whatever the message, the window never
    grows (``QProgressDialog`` widens itself to fit each new label text). Long
    messages are elided, the full text goes to the tooltip."""

    WIDTH = 440
    canceled = Signal()

    def __init__(self, parent: QWidget | None, text: str) -> None:
        super().__init__(parent)
        self.setWindowTitle(TITLE)
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self._text = ""
        self._finished = False
        layout = QVBoxLayout(self)
        self._layout = layout
        self.label = QLabel()
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        self.label.setWordWrap(False)
        # Ignored: the label's text never takes part in the dialog's width.
        self.label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        buttons = QDialogButtonBox()
        self.cancel_button = buttons.addButton("Annuler", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.rejected.connect(self.reject)
        layout.addWidget(self.label)
        layout.addWidget(self.bar)
        layout.addWidget(buttons)
        self.setFixedWidth(self.WIDTH)
        self.set_label_text(text)
        self.setFixedHeight(self.sizeHint().height())

    def label_text(self) -> str:
        return self._text

    def set_label_text(self, text: str) -> None:
        self._text = text
        self._elide()

    def set_value(self, value: int) -> None:
        self.bar.setValue(value)

    def set_busy(self) -> None:
        """Indeterminate bar (a step whose length is unknown)."""
        self.bar.setRange(0, 0)

    def finish(self) -> None:
        """Close for good (end of the download, success or not)."""
        self._finished = True
        self.close()

    def _elide(self) -> None:
        margins = self._layout.contentsMargins()
        width = self.WIDTH - margins.left() - margins.right()
        metrics = self.label.fontMetrics()
        shown = metrics.elidedText(self._text, Qt.TextElideMode.ElideRight, width)
        self.label.setText(shown)
        self.label.setToolTip(self._text if shown != self._text else "")

    @override
    def reject(self) -> None:
        if self._finished:
            super().reject()
            return
        # Cancelling takes effect at the next downloaded chunk: stay open until then.
        if self.cancel_button.isEnabled():
            self.cancel_button.setEnabled(False)
            self.set_label_text("Annulation de la mise à jour…")
            self.canceled.emit()

    @override
    def closeEvent(self, event: QCloseEvent) -> None:
        if self._finished:
            super().closeEvent(event)
            return
        event.ignore()
        self.reject()

    @override
    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._elide()


class UpdateManager(QObject):
    def __init__(self, window: MainWindow) -> None:
        super().__init__(window)
        self.window = window
        self.runner = TaskRunner(self)
        self.release: updater.Release | None = None
        self._progress: UpdateProgressDialog | None = None
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

    # -- outcome of the previous installation ---------------------------------------------

    def report_previous(self, state: updater.InstallState | None) -> None:
        """Tell the user how the installation started by the previous session ended."""
        if state is None or state.running:
            return
        if state.status == updater.STATE_OK:
            if not state.detail or state.detail == __version__:
                dialogs.info(
                    self.parent_widget, f"Anonyma a été mis à jour : version {__version__}.", TITLE
                )
                return
            message = (
                f"La mise à jour vers la version {state.detail} ne s'est pas appliquée : "
                f"la version {__version__} est toujours en place."
            )
        elif state.status == updater.STATE_STARTED:
            message = (
                "L'installation de la mise à jour a été interrompue avant la fin : "
                f"la version {__version__} est conservée."
            )
        elif state.detail == updater.STEP_WAIT:
            message = (
                "Anonyma ne s'est pas fermé à temps : la mise à jour n'a pas été installée "
                f"et la version {__version__} est conservée."
            )
        elif state.detail == updater.STEP_RESTORE:
            message = (
                "La mise à jour a échoué et la version précédente n'a pas pu être remise en "
                "place entièrement. Téléchargez Anonyma depuis la page des versions et "
                "remplacez le dossier du programme (gardez les dossiers data et models)."
            )
        else:
            message = (
                "Des fichiers d'Anonyma n'ont pas pu être remplacés (programme encore ouvert "
                "ou fichier verrouillé) : la mise à jour n'a pas été installée et la version "
                f"{__version__} est conservée."
            )
        log.warning("previous update failed: %s %s", state.status, state.detail)
        dialogs.error(self.parent_widget, f"{message} {_log_hint()}", TITLE)

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
        progress = UpdateProgressDialog(
            self.parent_widget, f"Téléchargement d'Anonyma {release.version}…"
        )
        progress.canceled.connect(self._cancel)
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
        if self._progress is None or self._cancelled or not isinstance(payload, tuple):
            return
        done, total = payload
        if not total:
            return
        if done >= total:
            # Download complete: the digest check and unpacking follow (no progress).
            self._progress.set_value(100)
            self._progress.set_busy()
            self._progress.set_label_text("Vérification et préparation de la mise à jour…")
            return
        self._progress.set_value(min(99, int(done * 100 / total)))
        version = self.release.version if self.release is not None else ""
        self._progress.set_label_text(
            f"Téléchargement d'Anonyma {version} : {done / 1024**2:.0f} / {total / 1024**2:.0f} Mo"
        )

    def _close_progress(self) -> None:
        if self._progress is not None:
            self._progress.canceled.disconnect(self._cancel)
            self._progress.finish()
            self._progress.deleteLater()
            self._progress = None

    def _failed(self, message: str, _type: str) -> None:
        self._downloading = False
        self._close_progress()
        dialogs.error(self.parent_widget, message, TITLE)

    def _finished(self, outcome: object) -> None:
        self._downloading = False
        self._close_progress()
        assert isinstance(outcome, tuple)
        kind, value = outcome
        if kind == "erreur":
            dialogs.error(self.parent_widget, str(value), TITLE)
        elif kind == "pret" and isinstance(value, Path):
            self._restart(value)

    def _restart(self, staged: Path) -> None:
        if not dialogs.confirm(
            self.parent_widget,
            "La mise à jour est prête. Anonyma va se fermer, installer la nouvelle version "
            "puis redémarrer.",
            title=TITLE,
            ok_label="Redémarrer maintenant",
            cancel_label="Plus tard",
        ):
            return
        if not self.window.close():  # the user kept the window open (unsaved settings…)
            return
        version = self.release.version if self.release is not None else ""
        try:
            # Returns once the installer runs: it then waits for this process to exit.
            updater.launch_installer(staged, version=version)
        except Exception as exc:
            log.warning("update installer not started: %s", type(exc).__name__)
            message = (
                str(exc)
                if isinstance(exc, updater.UpdateError)
                else "Impossible de lancer l'installation de la mise à jour "
                f"({type(exc).__name__})."
            )
            dialogs.error(
                None,
                f"{message} La version {__version__} est conservée : relancez Anonyma pour "
                f"réessayer. {_log_hint()}",
                TITLE,
            )
        # The installer replaces the program only once this process is gone.
        QCoreApplication.quit()
