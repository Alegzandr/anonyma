"""Icons embedded as SVG strings (stroke style, 24x24), tinted to the theme.

Nothing is read from disk or downloaded.
"""

from __future__ import annotations

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

from anonyma.brand import APP_SVG
from anonyma.gui import theme

_STROKE = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
    'stroke="{c}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'
)

_PATHS: dict[str, str] = {
    "process": '<path d="M4 6h10M4 12h7M4 18h10"/><path d="M16 9l5 3-5 3z" fill="{c}"/>',
    "review": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M12 4v16"/>'
    '<path d="M6 9h3M6 13h3M15 9h3M15 13h3"/>',
    "settings": '<path d="M4 7h10M18 7h2M4 17h4M12 17h8"/><circle cx="16" cy="7" r="2"/>'
    '<circle cx="10" cy="17" r="2"/>',
    "report": '<path d="M6 3h9l4 4v14H6z"/><path d="M15 3v4h4"/><path d="M9 17v-3M12 17v-6M15 '
    '17v-4"/>',
    "file": '<path d="M6 3h9l4 4v14H6z"/><path d="M15 3v4h4"/>',
    "folder": '<path d="M3 6.5A1.5 1.5 0 0 1 4.5 5H10l2 2h7.5A1.5 1.5 0 0 1 21 8.5v9A1.5 1.5 '
    '0 0 1 19.5 19h-15A1.5 1.5 0 0 1 3 17.5z"/>',
    "add": '<path d="M12 5v14M5 12h14"/>',
    "cancel": '<circle cx="12" cy="12" r="9"/><path d="M9 9l6 6M15 9l-6 6"/>',
    "check": '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
    "reject": '<circle cx="12" cy="12" r="9"/><path d="M6 6l12 12"/>',
    "download": '<path d="M12 4v11M7 10l5 5 5-5"/><path d="M5 20h14"/>',
    "import": '<path d="M12 15V4M7 9l5-5 5 5"/><path d="M5 20h14"/>',
    "trash": '<path d="M5 7h14M10 7V4h4v3M7 7l1 13h8l1-13"/>',
    "export": '<path d="M14 4h6v6M20 4l-9 9"/><path d="M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7'
    'a1 1 0 0 1 1-1h5"/>',
    "play": '<path d="M7 5l12 7-12 7z" fill="{c}"/>',
    "next": '<path d="M9 6l6 6-6 6"/>',
    "previous": '<path d="M15 6l-6 6 6 6"/>',
    "tag": '<path d="M3 12V4h8l10 10-8 8z"/><circle cx="7.5" cy="8.5" r="1.3"/>',
    "undo": '<path d="M8 8H4V4"/><path d="M4.5 8A8 8 0 1 1 6 17"/>',
    "shield": '<path d="M12 3l7 3v5c0 5-3.2 8.4-7 10-3.8-1.6-7-5-7-10V6z"/>',
}


def _render(svg: str, size: int) -> QPixmap:
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(painter, QRectF(0, 0, size, size))
    painter.end()
    return pixmap


def svg_for(name: str, colour: str) -> str:
    body = _PATHS[name].replace("{c}", colour)
    return _STROKE.replace("{c}", colour) + body + "</svg>"


def icon(name: str, colour: str | QColor | None = None) -> QIcon:
    """Theme-tinted icon (defaults to the current text colour)."""
    if isinstance(colour, QColor):
        colour = colour.name()
    colour = colour or theme.tokens().text
    result = QIcon()
    for size in (16, 24, 32, 48):
        result.addPixmap(_render(svg_for(name, colour), size))
    return result


def app_icon() -> QIcon:
    result = QIcon()
    for size in (16, 20, 24, 32, 40, 48, 64, 128, 256):
        result.addPixmap(_render(APP_SVG, size))
    return result


def app_pixmap(size: int, ratio: float = 1.0) -> QPixmap:
    """The application mark at ``size`` logical pixels (sharp on high-DPI screens)."""
    pixmap = _render(APP_SVG, max(1, round(size * ratio)))
    pixmap.setDevicePixelRatio(ratio)
    return pixmap


def names() -> list[str]:
    return sorted(_PATHS)
