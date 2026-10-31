"""Unexpected exceptions: logged by type only, shown as a French dialog."""

from __future__ import annotations

import sys
import threading
from types import TracebackType

from PySide6.QtCore import QMessageLogContext, QtMsgType, qInstallMessageHandler
from PySide6.QtWidgets import QApplication, QMessageBox

from anonyma.logs import get_logger

log = get_logger("gui.errors")

_showing = False


def unexpected_message(exc_type: type[BaseException]) -> str:
    return (
        "Une erreur inattendue s'est produite "
        f"({exc_type.__name__}). L'opération en cours a été interrompue.\n\n"
        "Aucune donnée n'a été envoyée. Le journal (data/logs) indique le type d'erreur, "
        "sans aucune valeur sensible."
    )


def _show(exc_type: type[BaseException]) -> None:
    global _showing
    app = QApplication.instance()
    if _showing or not isinstance(app, QApplication):
        return
    _showing = True
    try:
        QMessageBox.critical(app.activeWindow(), "Erreur inattendue", unexpected_message(exc_type))
    finally:
        _showing = False


def _excepthook(
    exc_type: type[BaseException], exc: BaseException, tb: TracebackType | None
) -> None:
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc, tb)
        return
    # Type only: messages and tracebacks may contain file names or values.
    log.error("unhandled exception: %s", exc_type.__name__)
    _show(exc_type)


def _thread_hook(args: threading.ExceptHookArgs) -> None:
    log.error("unhandled exception in thread: %s", args.exc_type.__name__)


def _qt_messages(kind: QtMsgType, _context: QMessageLogContext, _message: str) -> None:
    # Qt messages can quote file paths: keep only their severity.
    if kind in (QtMsgType.QtCriticalMsg, QtMsgType.QtFatalMsg):
        log.warning("Qt reported a critical message")


def install() -> None:
    sys.excepthook = _excepthook
    threading.excepthook = _thread_hook
    qInstallMessageHandler(_qt_messages)
