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
    sidebar_hover: str
    success: str
    warning: str
    danger: str
    info: str
    neutral: str
    control_border: str
    #: Badge text per level, drawn on a ``badge_fill`` alpha of the level colour
    #: (contrast >= 4.5:1, WCAG AA).
    badge_success: str
    badge_warning: str
    badge_danger: str
    badge_info: str
    badge_neutral: str
    badge_fill: float


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
    sidebar_hover="#dfe4eb",
    success="#2e7d4f",
    warning="#a0670a",
    danger="#b3322c",
    info="#2f5f98",
    neutral="#6b7280",
    control_border="#7d8696",
    badge_success="#1e5a37",
    badge_warning="#7a4c00",
    badge_danger="#922520",
    badge_info="#24497a",
    badge_neutral="#4b5263",
    badge_fill=0.14,
)

DARK = Tokens(
    dark=True,
    window="#1f1f1f",
    base="#272727",
    alt_base="#2c2c2c",
    surface="#282828",
    text="#e6e6e6",
    muted="#a4a4a4",
    border="#3e3e3e",
    accent="#6c9bd4",
    accent_hover="#82acdf",
    accent_text="#0f1520",
    selection="#3a4553",
    sidebar="#181818",
    sidebar_text="#d4d4d4",
    sidebar_selected="#333333",
    sidebar_hover="#262626",
    success="#5cb685",
    warning="#d9a443",
    danger="#e0706a",
    info="#6c9bd4",
    neutral="#a4a4a4",
    control_border="#7a7a7a",
    badge_success="#8fd6ad",
    badge_warning="#ecc47a",
    badge_danger="#f3a29d",
    badge_info="#a5c4ea",
    badge_neutral="#cccccc",
    badge_fill=0.22,
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


_SVG_HEAD = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">'
#: Images the style sheet needs: name -> (path data, stroke width).
_THEME_IMAGES: dict[str, tuple[str, str]] = {
    "check": ("M3.5 8.5l3 3 6-7", "2"),
    "chevron-down": ("M4 6l4 4 4-4", "1.6"),
    "chevron-up": ("M4 10l4-4 4 4", "1.6"),
}


def _theme_image(name: str, colour: str) -> str:
    """Path (forward slashes) of a style-sheet image tinted ``colour``, written once
    inside the portable folder; "" when it cannot be written."""
    from anonyma import paths

    path_data, width = _THEME_IMAGES[name]
    folder = paths.data_dir() / "qt" / "theme"
    target = folder / f"{name}-{colour.lstrip('#')}.svg"
    try:
        if not target.exists():
            folder.mkdir(parents=True, exist_ok=True)
            target.write_text(
                f'{_SVG_HEAD}<path d="{path_data}" fill="none" stroke="{colour}" '
                f'stroke-width="{width}" stroke-linecap="round" stroke-linejoin="round"/>'
                "</svg>",
                encoding="utf-8",
            )
    except OSError:
        return ""
    return target.as_posix()


def _image_rule(path: str) -> str:
    return f"image: url({path});" if path else ""


def _indicator_rules(t: Tokens, check: str) -> str:
    return f"""
QCheckBox::indicator {{
    width: 14px; height: 14px; border: 1px solid {t.control_border}; border-radius: 3px;
    background: {t.base};
}}
QCheckBox::indicator:hover {{ border-color: {t.muted}; }}
QCheckBox::indicator:focus {{ border-color: {t.accent}; }}
QCheckBox::indicator:checked {{
    background: {t.accent}; border-color: {t.accent}; {_image_rule(check)}
}}
QCheckBox::indicator:disabled {{ background: {t.alt_base}; border-color: {t.border}; }}
QCheckBox::indicator:checked:disabled {{ background: {t.border}; }}
"""


def _field_rules(t: Tokens, down: str, up: str) -> str:
    """Text fields, combo boxes and spin boxes: same height, radius and focus as buttons."""
    return f"""
QLineEdit, QComboBox, QAbstractSpinBox {{
    border: 1px solid {t.control_border}; border-radius: 4px; padding: 4px 8px;
    background: {t.base}; color: {t.text}; min-height: 20px;
    selection-background-color: {t.selection}; selection-color: {t.text};
}}
QLineEdit:hover, QComboBox:hover, QAbstractSpinBox:hover {{ border-color: {t.muted}; }}
QLineEdit:focus, QComboBox:focus, QComboBox:on, QAbstractSpinBox:focus {{
    border-color: {t.accent};
}}
QLineEdit:disabled, QComboBox:disabled, QAbstractSpinBox:disabled {{
    background: {t.alt_base}; border-color: {t.border}; color: {t.muted};
}}
QLineEdit[readOnly="true"] {{ background: {t.alt_base}; }}
QComboBox {{ padding: 4px 20px 4px 6px; }}
QComboBox::drop-down {{
    subcontrol-origin: padding; subcontrol-position: center right; width: 18px; border: none;
}}
QComboBox::down-arrow {{ {_image_rule(down)} width: 10px; height: 10px; }}
QComboBox QAbstractItemView {{
    border: 1px solid {t.border}; border-radius: 0; background: {t.base}; outline: 0;
    padding: 2px; selection-background-color: {t.selection}; selection-color: {t.text};
}}
QAbstractSpinBox {{ padding: 4px 24px 4px 8px; }}
QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {{
    subcontrol-origin: border; width: 20px; border: none; border-left: 1px solid {t.border};
    background: transparent;
}}
QAbstractSpinBox::up-button {{ subcontrol-position: top right; border-top-right-radius: 4px; }}
QAbstractSpinBox::down-button {{
    subcontrol-position: bottom right; border-bottom-right-radius: 4px;
}}
QAbstractSpinBox::up-button:hover, QAbstractSpinBox::down-button:hover {{
    background: {t.alt_base};
}}
QAbstractSpinBox::up-arrow {{ {_image_rule(up)} width: 10px; height: 10px; }}
QAbstractSpinBox::down-arrow {{ {_image_rule(down)} width: 10px; height: 10px; }}
"""


def _chrome_rules(t: Tokens) -> str:
    """Tabs and scroll bars."""
    return f"""
QTabWidget::pane {{ border: none; border-top: 1px solid {t.border}; top: -1px; }}
QTabBar::tab {{
    background: transparent; color: {t.muted}; border: none;
    border-bottom: 2px solid transparent; padding: 6px 12px 7px; margin-right: 4px;
}}
QTabBar::tab:selected {{ color: {t.text}; border-bottom-color: {t.accent}; }}
QTabBar::tab:hover:!selected {{ color: {t.text}; border-bottom-color: {t.border}; }}
QTabBar::tab:focus {{ color: {t.accent}; }}
QScrollBar:vertical {{ background: transparent; width: 12px; margin: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 12px; margin: 0; }}
QScrollBar::handle:vertical, QScrollBar::handle:horizontal {{
    background: {t.control_border}; border-radius: 3px; margin: 3px;
}}
QScrollBar::handle:vertical {{ min-height: 32px; }}
QScrollBar::handle:horizontal {{ min-width: 32px; }}
QScrollBar::handle:hover, QScrollBar::handle:pressed {{ background: {t.muted}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; border: none; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
"""


def style_sheet(t: Tokens, images: dict[str, str] | None = None) -> str:
    images = images or {}
    return (
        _indicator_rules(t, images.get("check", ""))
        + _field_rules(t, images.get("chevron-down", ""), images.get("chevron-up", ""))
        + _chrome_rules(t)
        + f"""
QWidget {{ font-size: 10pt; }}
QToolTip {{ color: {t.text}; background: {t.surface}; border: 1px solid {t.border}; }}
#SidebarBox {{ background: {t.sidebar}; border-right: 1px solid {t.border}; }}
#SidebarBox QLabel {{ background: transparent; }}
#Sidebar {{
    background: transparent; border: none; border-radius: 0;
    color: {t.sidebar_text}; outline: 0; padding: 4px 0;
}}
#Sidebar::item {{
    padding: 10px 14px; margin: 1px 8px; border-radius: 4px; border: 2px solid transparent;
}}
#Sidebar::item:selected {{ background: {t.sidebar_selected}; color: {t.text}; }}
#Sidebar::item:hover:!selected {{ background: {t.sidebar_hover}; }}
#Sidebar::item:focus {{ border-color: {t.accent}; }}
#AppTitle {{ font-size: 13pt; font-weight: 600; color: {t.text}; }}
#SidebarFooter {{ color: {t.muted}; font-size: 9pt; }}
#PageTitle {{ font-size: 15pt; font-weight: 600; color: {t.text}; }}
#PageSubtitle, .muted, QLabel[muted="true"] {{ color: {t.muted}; }}
#SectionTitle {{ font-weight: 600; color: {t.text}; }}
#Verdict {{ font-size: 12pt; font-weight: 600; color: {t.text}; }}
QFrame#Card {{
    background: {t.surface}; border: 1px solid {t.border}; border-radius: 4px;
}}
QFrame#DropArea {{
    background: {t.alt_base}; border: 1.5px dashed {t.border}; border-radius: 4px;
}}
QFrame#DropArea[active="true"] {{ border-color: {t.accent}; background: {t.selection}; }}
QFrame#Banner {{ border-radius: 4px; border: 1px solid {t.border}; background: {t.alt_base}; }}
QFrame#Banner[level="warning"] {{ border-color: {t.warning}; }}
QFrame#Banner[level="error"] {{ border-color: {t.danger}; }}
QFrame#Banner[level="success"] {{ border-color: {t.success}; }}
QPushButton {{
    padding: 5px 14px; border-radius: 4px; border: 1px solid {t.control_border};
    background: {t.surface}; color: {t.text}; min-height: 20px;
}}
QPushButton:hover {{ border-color: {t.muted}; background: {t.alt_base}; }}
QPushButton:pressed {{ background: {t.sidebar_selected}; }}
QPushButton:focus {{ border: 2px solid {t.accent}; padding: 4px 13px; }}
/* Segmented toggles (« Texte » / « Visuel »). */
QPushButton:checked {{
    background: {t.selection}; border-color: {t.accent}; color: {t.text}; font-weight: 600;
}}
QPushButton:disabled {{ color: {t.muted}; background: {t.alt_base}; border-color: {t.border}; }}
QPushButton:flat {{ border-color: transparent; background: transparent; }}
QPushButton:flat:hover {{ background: {t.alt_base}; }}
/* Menu buttons (« Actions groupées »): the push-button look plus a chevron. */
QToolButton[popupMode="2"] {{
    padding: 5px 22px 5px 12px; border-radius: 4px; border: 1px solid {t.control_border};
    background: {t.surface}; color: {t.text}; min-height: 20px;
}}
QToolButton[popupMode="2"]:hover {{ border-color: {t.muted}; background: {t.alt_base}; }}
QToolButton[popupMode="2"]:focus {{
    border: 2px solid {t.accent}; padding: 4px 21px 4px 11px;
}}
QToolButton[popupMode="2"]:disabled {{
    color: {t.muted}; background: {t.alt_base}; border-color: {t.border};
}}
QToolButton[popupMode="2"]::menu-indicator {{
    {_image_rule(images.get("chevron-down", ""))} subcontrol-origin: padding;
    subcontrol-position: center right; right: 6px; width: 10px; height: 10px;
}}
QPushButton[primary="true"] {{
    background: {t.accent}; color: {t.accent_text}; border-color: {t.accent}; font-weight: 600;
}}
QPushButton[primary="true"]:hover {{
    background: {t.accent_hover}; border-color: {t.accent_hover};
}}
QPushButton[primary="true"]:focus {{ border: 2px solid {t.text}; }}
QPushButton[primary="true"]:disabled {{
    background: {t.border}; border-color: {t.border}; color: {t.muted};
}}
QPushButton[danger="true"] {{ color: {t.danger}; }}
QPushButton[danger="true"]:disabled {{ color: {t.muted}; }}
QGroupBox {{
    border: 1px solid {t.border}; border-radius: 4px; margin-top: 14px; padding: 12px 10px 8px;
    background: {t.surface};
}}
QGroupBox::title {{
    subcontrol-origin: margin; left: 12px; padding: 0 4px; font-weight: 600; color: {t.text};
}}
QHeaderView::section {{
    background: {t.alt_base}; color: {t.muted}; border: none;
    border-bottom: 1px solid {t.border}; padding: 5px 8px; font-weight: 600;
}}
QTableView, QTreeView, QListView, QPlainTextEdit, QTextEdit {{
    border: 1px solid {t.border}; border-radius: 4px; background: {t.base};
    selection-background-color: {t.selection}; selection-color: {t.text};
}}
QProgressBar {{
    border: none; border-radius: 3px; background: {t.border};
    min-height: 6px; max-height: 6px;
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
        images = {
            "check": _theme_image("check", _current.accent_text),
            "chevron-down": _theme_image("chevron-down", _current.muted),
            "chevron-up": _theme_image("chevron-up", _current.muted),
        }
        app.setStyleSheet(style_sheet(_current, images))
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
    """Level colour (badge fill, before ``Tokens.badge_fill`` alpha)."""
    return QColor(getattr(_current, level if level in BADGE_LEVELS else "neutral"))


def badge_text(level: str) -> QColor:
    """Readable text colour of a badge of ``level`` on its tinted fill."""
    return QColor(getattr(_current, "badge_" + (level if level in BADGE_LEVELS else "neutral")))
