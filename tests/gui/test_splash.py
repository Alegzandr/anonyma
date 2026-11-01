"""Startup splash: visible before the main window is built, gone once it shows."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from PySide6.QtWidgets import QApplication, QWidget
from pytestqt.qtbot import QtBot

from anonyma import app as app_module
from anonyma import logs, netguard, paths
from anonyma.gui import errors, main_window, splash


def test_splash_paints_its_status(qtbot: QtBot) -> None:
    widget = splash.show_splash()
    qtbot.addWidget(widget)
    assert widget.isVisible()
    assert widget.status.text() == splash.STARTING
    assert widget.progress.maximum() == 0  # indeterminate
    widget.set_status(splash.LOADING)
    assert widget.status.text() == splash.LOADING
    assert not widget.grab().isNull()
    widget.close()


class _FakeWindow(QWidget):
    """Stands in for the main window: records what the splash looked like."""

    instances: list[_FakeWindow] = []

    def __init__(self, _context: Any) -> None:
        super().__init__()
        self.updates = MagicMock()
        self.settings = MagicMock()
        self.models = MagicMock()
        self.splash_at_build = any(w.isVisible() for w in _splashes())
        self.splash_at_dialog: list[bool] = []
        self.updates.report_previous.side_effect = lambda _state: self.splash_at_dialog.append(
            any(w.isVisible() for w in _splashes())
        )
        _FakeWindow.instances.append(self)


def _splashes() -> list[splash.Splash]:
    return [w for w in QApplication.topLevelWidgets() if isinstance(w, splash.Splash)]


def test_main_shows_the_splash_first_and_closes_it(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    for module, name in (
        (paths, "configure_environment"),
        (paths, "clean_tmp"),
        (netguard, "install"),
        (logs, "setup_logging"),
        (errors, "install"),
        (app_module, "_configure_qt"),
    ):
        monkeypatch.setattr(module, name, lambda *_a, **_k: None)
    monkeypatch.setattr(main_window, "MainWindow", _FakeWindow)
    monkeypatch.setattr(QApplication, "exec", lambda *_a: 0)
    _FakeWindow.instances.clear()

    assert app_module.main([]) == 0

    (window,) = _FakeWindow.instances
    qtbot.addWidget(window)
    assert window.splash_at_build  # painted before the window is built
    assert window.splash_at_dialog == [False]  # never above a startup dialog
    assert window.isVisible()
    assert not any(w.isVisible() for w in _splashes())
