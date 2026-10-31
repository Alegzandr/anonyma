"""Background tasks: run blocking work in a QThread and report through signals.

The UI thread never runs pipeline, hardware or download work. Progress callbacks
are invoked in the worker thread and re-emitted as Qt signals, which Qt delivers
(queued) to receivers living in the UI thread.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import CancelledError
from typing import Any

from PySide6.QtCore import QObject, QThread, Signal

from anonyma.logs import get_logger
from anonyma.model_manager import ModelError
from anonyma.store import StoreError

log = get_logger("gui.tasks")

#: Callback given to the task function to report progress (any picklable payload).
Report = Callable[[object], None]


def user_message(exc: BaseException) -> str:
    """French, value-free message for an exception.

    ``ModelError``/``StoreError``/``ValueError`` messages are user-facing by
    contract; anything else gets a generic message naming only the type.
    """
    if isinstance(exc, NotImplementedError):
        return "Cette fonction n'est pas encore disponible dans cette version d'Anonyma."
    if isinstance(exc, ModelError | StoreError | ValueError) and str(exc):
        return str(exc)
    if isinstance(exc, PermissionError):
        return "Accès refusé : vérifiez les droits sur les fichiers et dossiers choisis."
    if isinstance(exc, FileNotFoundError):
        return "Un fichier ou un dossier est introuvable."
    if isinstance(exc, OSError):
        return f"Erreur d'accès au disque ({type(exc).__name__})."
    return f"Une erreur inattendue s'est produite ({type(exc).__name__})."


class Task(QThread):
    """Run ``fn(report)`` in a worker thread.

    Signals: ``progress(payload)``, ``succeeded(result)``, ``failed(message, type)``,
    ``cancelled()``. Exactly one of the last three is emitted, then ``finished``.
    """

    progress = Signal(object)
    succeeded = Signal(object)
    failed = Signal(str, str)
    cancelled = Signal()

    def __init__(self, fn: Callable[[Report], Any], parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._fn = fn

    def run(self) -> None:
        try:
            result = self._fn(self.progress.emit)
        except CancelledError:
            self.cancelled.emit()
        except Exception as exc:
            # Type only: exception messages may contain sensitive values.
            log.warning("background task failed: %s", type(exc).__name__)
            self.failed.emit(user_message(exc), type(exc).__name__)
        else:
            self.succeeded.emit(result)


class TaskRunner(QObject):
    """Keeps references to running tasks so that they are not garbage-collected."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._tasks: set[Task] = set()

    def create(self, fn: Callable[[Report], Any]) -> Task:
        """A task owned by this runner; connect its signals, then call ``start()``."""
        task = Task(fn, self)
        self._tasks.add(task)
        task.finished.connect(lambda: self._forget(task))
        return task

    def _forget(self, task: Task) -> None:
        self._tasks.discard(task)
        task.deleteLater()

    def running(self) -> bool:
        # A task counts until its ``finished`` slot has run: its result signal is
        # delivered (queued) before that, whereas ``isRunning()`` turns false first.
        return bool(self._tasks)

    def wait_all(self, msecs: int = 30_000) -> bool:
        return all(t.wait(msecs) for t in list(self._tasks))
