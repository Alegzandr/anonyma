"""Startup splash: shown as soon as the ``QApplication`` exists, before the heavy imports
(pipeline, detectors, pages) and the construction of the main window.

Light on purpose: Qt, the theme tokens and the brand mark only. A plain frameless
widget rather than ``QSplashScreen``, whose ``show()`` blocks until a timeout expires
in the bundled Qt, which would delay the very first pixel it is meant to bring forward.
"""

from __future__ import annotations

from typing import override

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QGuiApplication, QPainter, QPaintEvent, QPen
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication, QLabel, QProgressBar, QVBoxLayout, QWidget

from anonyma import __version__
from anonyma.brand import APP_SVG
from anonyma.gui import theme

#: Logical size of the splash (device-independent pixels).
WIDTH, HEIGHT = 420, 210
_LOGO = 64
_MARGIN = 28
_GAP = 18

STARTING = "Démarrage d’Anonyma…"
LOADING = "Chargement des modules…"
OPENING = "Ouverture de la fenêtre…"


class Splash(QWidget):
    """Brand mark, a status line and an indeterminate progress bar.

    The bar only moves when events are processed: :meth:`set_status` repaints, and
    the entry point calls it between the startup stages.
    """

    def __init__(self) -> None:
        super().__init__(
            None,
            Qt.WindowType.SplashScreen
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint,
        )
        self.setObjectName("splash")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowTitle("Anonyma")
        self.setFixedSize(WIDTH, HEIGHT)
        self._tokens = theme.tokens()
        self._logo = QSvgRenderer(QByteArray(APP_SVG.encode("utf-8")), self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(_MARGIN, 0, _MARGIN, _MARGIN)
        layout.setSpacing(10)
        layout.addStretch(1)
        self.status = QLabel(STARTING)
        self.status.setStyleSheet(f"color: {self._tokens.muted}; background: transparent;")
        layout.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)  # indeterminate
        self.progress.setTextVisible(False)
        layout.addWidget(self.progress)
        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            frame = self.frameGeometry()
            frame.moveCenter(screen.availableGeometry().center())
            self.move(frame.topLeft())

    @override
    def paintEvent(self, event: QPaintEvent) -> None:
        t = self._tokens
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        painter.fillRect(self.rect(), QColor(t.surface))
        painter.setPen(QPen(QColor(t.border), 1))
        painter.drawRect(QRectF(0.5, 0.5, self.width() - 1, self.height() - 1))
        self._logo.render(painter, QRectF(_MARGIN, _MARGIN, _LOGO, _LOGO))
        left = _MARGIN + _LOGO + _GAP
        width = self.width() - left - _MARGIN
        title = QFont(self.font())
        title.setPointSizeF(self.font().pointSizeF() * 1.9)
        title.setWeight(QFont.Weight.DemiBold)
        painter.setFont(title)
        painter.setPen(QColor(t.text))
        painter.drawText(
            QRectF(left, _MARGIN, width, _LOGO * 0.6),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom,
            "Anonyma",
        )
        painter.setFont(self.font())
        painter.setPen(QColor(t.muted))
        painter.drawText(
            QRectF(left, _MARGIN + _LOGO * 0.64, width, _LOGO * 0.4),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
            f"Anonymisation hors ligne · version {__version__}",
        )
        painter.end()

    def set_status(self, message: str) -> None:
        self.status.setText(message)
        self.repaint()
        QApplication.processEvents()


def show_splash() -> Splash:
    """Create, show and paint the splash right away (before the event loop runs)."""
    splash = Splash()
    splash.show()
    splash.raise_()
    splash.repaint()
    QApplication.processEvents()
    return splash
