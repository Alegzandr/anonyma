"""Light / dark / system themes, accent colour and entity-type colours.

Everything is drawn with the Fusion style, a ``QPalette`` and a small style sheet:
no external resource is ever loaded.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor, QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication

from anonyma.models import EntityType

#: Config.ui["theme"] value -> French label.
THEMES: dict[str, str] = {"systeme": "Système", "clair": "Clair", "sombre": "Sombre"}
DEFAULT_THEME = "systeme"


@dataclass(frozen=True, slots=True)
class Tokens:
    """Colour tokens of one theme."""

    dark: bool
    window: str
    base: str
    alt_base: str
    surface: str
    text: str
    muted: str
    border: str
    accent: str
    accent_hover: str
    accent_text: str
    selection: str
    sidebar: str
    sidebar_text: str
    sidebar_selected: str
    success: str
    warning: str
    danger: str
    info: str
    neutral: str
    control_border: str


LIGHT = Tokens(
    dark=False,
    window="#f4f5f7",
    base="#ffffff",
    alt_base="#f8f9fb",
    surface="#ffffff",
    text="#1d2330",
    muted="#5d6677",
    border="#d5d9e0",
    accent="#2f5f98",
    accent_hover="#274f80",
    accent_text="#ffffff",
    selection="#cfdcef",
    sidebar="#e9ecf1",
    sidebar_text="#2a3140",
    sidebar_selected="#d6dfec",
    success="#2e7d4f",
    warning="#a0670a",
    danger="#b3322c",
    info="#2f5f98",
    neutral="#6b7280",
    control_border="#9aa2b1",
)

DARK = Tokens(
    dark=True,
    window="#1d2027",
    base="#252932",
    alt_base="#2a2e38",
    surface="#262a33",
    text="#e4e7ec",
    muted="#9aa3b2",
    border="#3a3f4b",
    accent="#6c9bd4",
    accent_hover="#82acdf",
    accent_text="#0f1520",
    selection="#34496a",
    sidebar="#181b21",
    sidebar_text="#cfd5df",
    sidebar_selected="#2c3647",
    success="#5cb685",
    warning="#d9a443",
    danger="#e0706a",
    info="#6c9bd4",
    neutral="#9aa3b2",
    control_border="#6b7385",
)

_current: Tokens = LIGHT


class _Notifier(QObject):
    changed = Signal()


_notifier: _Notifier | None = None


def notifier() -> _Notifier:
    """Object emitting ``changed`` after every theme switch."""
    global _notifier
    if _notifier is None:
        _notifier = _Notifier()
    return _notifier


def tokens() -> Tokens:
    return _current


def is_dark() -> bool:
    return _current.dark


def system_is_dark() -> bool:
    app = QGuiApplication.instance()
    if not isinstance(app, QGuiApplication):
        return False
    return app.styleHints().colorScheme() == Qt.ColorScheme.Dark


def resolve_dark(mode: str) -> bool:
    if mode == "sombre":
        return True
    if mode == "clair":
        return False
    return system_is_dark()


def _palette(t: Tokens) -> QPalette:
    p = QPalette()
    roles = {
        QPalette.ColorRole.Window: t.window,
        QPalette.ColorRole.WindowText: t.text,
        QPalette.ColorRole.Base: t.base,
        QPalette.ColorRole.AlternateBase: t.alt_base,
        QPalette.ColorRole.ToolTipBase: t.surface,
        QPalette.ColorRole.ToolTipText: t.text,
        QPalette.ColorRole.PlaceholderText: t.muted,
        QPalette.ColorRole.Text: t.text,
        QPalette.ColorRole.Button: t.surface,
        QPalette.ColorRole.ButtonText: t.text,
        QPalette.ColorRole.BrightText: t.danger,
        QPalette.ColorRole.Highlight: t.accent,
        QPalette.ColorRole.HighlightedText: t.accent_text,
        QPalette.ColorRole.Link: t.accent,
        QPalette.ColorRole.Light: t.base,
        QPalette.ColorRole.Midlight: t.alt_base,
        QPalette.ColorRole.Mid: t.border,
        QPalette.ColorRole.Dark: t.border,
        QPalette.ColorRole.Shadow: "#000000",
    }
    for role, colour in roles.items():
        p.setColor(role, QColor(colour))
    disabled = QColor(t.muted)
    for role in (
        QPalette.ColorRole.WindowText,
        QPalette.ColorRole.Text,
        QPalette.ColorRole.ButtonText,
    ):
        p.setColor(QPalette.ColorGroup.Disabled, role, disabled)
    return p


_CHECK_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16"><path d="M3.5 8.5l3 3 6-7" '
    'fill="none" stroke="{c}" stroke-width="2" stroke-linecap="round" '
    'stroke-linejoin="round"/></svg>'
)


def _check_image(t: Tokens) -> str:
    """Path (forward slashes) of the check-mark image for this theme, written once
    inside the portable folder; "" when it cannot be written."""
    from anonyma import paths

    folder = paths.data_dir() / "qt" / "theme"
    target = folder / f"check-{t.accent_text.lstrip('#')}.svg"
    try:
        if not target.exists():
            folder.mkdir(parents=True, exist_ok=True)
            target.write_text(_CHECK_SVG.replace("{c}", t.accent_text), encoding="utf-8")
    except OSError:
        return ""
    return target.as_posix()


def _indicator_rules(t: Tokens, check: str) -> str:
    image = f"image: url({check});" if check else ""
    return f"""
QCheckBox::indicator {{
    width: 14px; height: 14px; border: 1px solid {t.control_border}; border-radius: 3px;
    background: {t.base};
}}
QCheckBox::indicator:hover {{ border-color: {t.accent}; }}
QCheckBox::indicator:checked {{ background: {t.accent}; border-color: {t.accent}; {image} }}
QCheckBox::indicator:disabled {{ background: {t.alt_base}; border-color: {t.border}; }}
QCheckBox::indicator:checked:disabled {{ background: {t.border}; }}
"""


def style_sheet(t: Tokens, check: str = "") -> str:
    return (
        _indicator_rules(t, check)
        + f"""
QWidget {{ font-size: 10pt; }}
QToolTip {{ color: {t.text}; background: {t.surface}; border: 1px solid {t.border}; }}
#Sidebar {{
    background: {t.sidebar}; border: none; border-right: 1px solid {t.border};
    color: {t.sidebar_text}; outline: 0; padding: 8px 0;
}}
#Sidebar::item {{ padding: 10px 14px; margin: 1px 8px; border-radius: 6px; }}
#Sidebar::item:selected {{ background: {t.sidebar_selected}; color: {t.text}; }}
#Sidebar::item:hover:!selected {{ background: {t.sidebar_selected}; }}
#AppTitle {{ font-size: 13pt; font-weight: 600; color: {t.text}; padding: 14px 18px 6px; }}
#PageTitle {{ font-size: 15pt; font-weight: 600; color: {t.text}; }}
#PageSubtitle, .muted, QLabel[muted="true"] {{ color: {t.muted}; }}
#SectionTitle {{ font-weight: 600; color: {t.text}; }}
QFrame#Card {{
    background: {t.surface}; border: 1px solid {t.border}; border-radius: 8px;
}}
QFrame#DropArea {{
    background: {t.alt_base}; border: 1.5px dashed {t.border}; border-radius: 8px;
}}
QFrame#DropArea[active="true"] {{ border-color: {t.accent}; background: {t.selection}; }}
QFrame#Banner {{ border-radius: 6px; border: 1px solid {t.border}; background: {t.alt_base}; }}
QFrame#Banner[level="warning"] {{ border-color: {t.warning}; }}
QFrame#Banner[level="error"] {{ border-color: {t.danger}; }}
QFrame#Banner[level="success"] {{ border-color: {t.success}; }}
QPushButton {{
    padding: 5px 14px; border-radius: 5px; border: 1px solid {t.border};
    background: {t.surface}; color: {t.text}; min-height: 20px;
}}
QPushButton:hover {{ border-color: {t.accent}; }}
QPushButton:disabled {{ color: {t.muted}; background: {t.alt_base}; }}
QPushButton[primary="true"] {{
    background: {t.accent}; color: {t.accent_text}; border-color: {t.accent}; font-weight: 600;
}}
QPushButton[primary="true"]:hover {{ background: {t.accent_hover}; }}
QPushButton[primary="true"]:disabled {{
    background: {t.border}; border-color: {t.border}; color: {t.muted};
}}
QPushButton[danger="true"] {{ color: {t.danger}; }}
QPushButton[danger="true"]:disabled {{ color: {t.muted}; }}
QGroupBox {{
    border: 1px solid {t.border}; border-radius: 8px; margin-top: 14px; padding: 12px 10px 8px;
    background: {t.surface};
}}
QGroupBox::title {{
    subcontrol-origin: margin; left: 12px; padding: 0 4px; font-weight: 600; color: {t.text};
}}
QHeaderView::section {{
    background: {t.alt_base}; color: {t.muted}; border: none;
    border-bottom: 1px solid {t.border}; padding: 5px 6px; font-weight: 600;
}}
QTableView, QTreeView, QListView, QPlainTextEdit, QTextEdit {{
    border: 1px solid {t.border}; border-radius: 6px; background: {t.base};
    selection-background-color: {t.selection}; selection-color: {t.text};
}}
QProgressBar {{
    border: 1px solid {t.border}; border-radius: 4px; background: {t.alt_base};
    text-align: center; min-height: 14px; color: {t.text};
}}
QProgressBar::chunk {{ background: {t.accent}; border-radius: 3px; }}
QScrollArea {{ border: none; background: transparent; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
"""
    )


def apply_theme(mode: str) -> Tokens:
    """Apply ``mode`` ("systeme" | "clair" | "sombre") to the running application."""
    global _current
    app = QApplication.instance()
    _current = DARK if resolve_dark(mode) else LIGHT
    if isinstance(app, QApplication):
        app.setStyle("Fusion")
        app.setPalette(_palette(_current))
        app.setStyleSheet(style_sheet(_current, _check_image(_current)))
    notifier().changed.emit()
    return _current


# ---------------------------------------------------------------------------
# Entity-type colours
# ---------------------------------------------------------------------------

#: Hue (degrees) per built-in type; chosen so that frequent types stay far apart.
_HUES: dict[str, int] = {
    EntityType.PERSON: 2,
    EntityType.ORGANIZATION: 28,
    EntityType.LOCATION: 105,
    EntityType.ADDRESS: 140,
    EntityType.JOB_TITLE: 48,
    EntityType.EMAIL: 188,
    EntityType.PHONE: 268,
    EntityType.IBAN: 318,
    EntityType.BIC: 334,
    EntityType.CREDIT_CARD: 300,
    EntityType.NIR: 348,
    EntityType.SIREN: 165,
    EntityType.SIRET: 172,
    EntityType.VAT_NUMBER: 158,
    EntityType.LICENSE_PLATE: 228,
    EntityType.POSTAL_CODE: 122,
    EntityType.IP_ADDRESS: 208,
    EntityType.URL: 198,
    EntityType.DATE_OF_BIRTH: 14,
    EntityType.DATE: 62,
    EntityType.CASE_NUMBER: 284,
    EntityType.USERNAME: 244,
    EntityType.MESSAGE_ID: 180,
    EntityType.INDIRECT: 38,
    EntityType.PROPER_NOUN: 8,
    EntityType.SUSPICIOUS_TOKEN: 76,
    EntityType.BUSINESS_TERM: 256,
}


def entity_hue(entity_type: str) -> int:
    hue = _HUES.get(entity_type)
    if hue is None:
        digest = hashlib.sha256(entity_type.encode()).digest()
        hue = int.from_bytes(digest[:2], "big") % 360
    return hue


def entity_colors(entity_type: str, dark: bool | None = None) -> tuple[QColor, QColor]:
    """(background, foreground) for highlighting a value of ``entity_type``.

    Pastel background with dark text in the light theme, deep background with light
    text in the dark theme: both keep a contrast ratio above 7:1.
    """
    dark = _current.dark if dark is None else dark
    hue = entity_hue(entity_type) / 360
    if dark:
        return QColor.fromHslF(hue, 0.42, 0.30), QColor("#f2f4f7")
    return QColor.fromHslF(hue, 0.75, 0.86), QColor("#151a24")


def entity_accent(entity_type: str, dark: bool | None = None) -> QColor:
    """Saturated colour of a type (legend swatch, underline)."""
    dark = _current.dark if dark is None else dark
    hue = entity_hue(entity_type) / 360
    return QColor.fromHslF(hue, 0.65, 0.62 if dark else 0.42)


#: Badge level -> token attribute name.
BADGE_LEVELS = ("success", "warning", "danger", "info", "neutral")


def badge_color(level: str) -> QColor:
    return QColor(getattr(_current, level if level in BADGE_LEVELS else "neutral"))
