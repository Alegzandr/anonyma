"""Reusable widgets: page header, banners, drop area, buttons and badge delegate."""

from __future__ import annotations

import html
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import override

from PySide6.QtCore import QModelIndex, QPersistentModelIndex, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QDragEnterEvent,
    QDragLeaveEvent,
    QDropEvent,
    QFontMetrics,
    QIcon,
    QPainter,
    QPaintEvent,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QAbstractSpinBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionButton,
    QStyleOptionViewItem,
    QStylePainter,
    QTableView,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from anonyma.frenchtext import typo
from anonyma.gui import icons, theme
from anonyma.gui.labels import entity_type_label

#: Item data role holding a badge level ("success", "warning"...).
BADGE_ROLE = Qt.ItemDataRole.UserRole + 10

IndexLike = QModelIndex | QPersistentModelIndex


def page_header(title: str) -> QWidget:
    """Page title. No subtitle: the page content explains itself."""
    box = QWidget()
    layout = QVBoxLayout(box)
    layout.setContentsMargins(0, 0, 0, 4)
    layout.setSpacing(2)
    label = QLabel(title)
    label.setObjectName("PageTitle")
    layout.addWidget(label)
    return box


def muted(text: str) -> QLabel:
    label = QLabel(typo(text))
    label.setProperty("muted", True)
    label.setWordWrap(True)
    return label


def section_title(text: str) -> QLabel:
    label = QLabel(typo(text))
    label.setObjectName("SectionTitle")
    return label


#: Button property holding the icon name, so the icon follows theme switches.
_ICON_PROPERTY = "anonymaIcon"
_retint_connected = False


def _icon_colour(b: QPushButton) -> str:
    t = theme.tokens()
    return t.accent_text if b.property("primary") else t.text


def _set_button_icon(b: QPushButton, icon_name: str) -> None:
    global _retint_connected
    b.setProperty(_ICON_PROPERTY, icon_name)
    b.setIcon(icons.icon(icon_name, _icon_colour(b)))
    if not _retint_connected:
        theme.notifier().changed.connect(_retint_buttons)
        _retint_connected = True


def set_button_icon(b: QPushButton, icon_name: str) -> None:
    """Change (or remove, with "") the themed icon of a button."""
    if icon_name:
        _set_button_icon(b, icon_name)
    else:
        b.setProperty(_ICON_PROPERTY, "")
        b.setIcon(QIcon())


def _retint_buttons() -> None:
    """Re-tint every themed button icon (dark icons vanish on the dark theme otherwise)."""
    from PySide6.QtWidgets import QApplication

    for widget in QApplication.allWidgets():
        if isinstance(widget, QPushButton):
            name = widget.property(_ICON_PROPERTY)
            if name:
                widget.setIcon(icons.icon(str(name), _icon_colour(widget)))


def _tab_focus(b: QPushButton) -> QPushButton:
    """Keyboard focus only: a click leaves no focus ring behind (as on Windows and
    macOS), Tab still reaches the button."""
    b.setFocusPolicy(Qt.FocusPolicy.TabFocus)
    return b


def primary_button(text: str, icon_name: str = "") -> QPushButton:
    """The page's main action (accent fill). Icons are reserved to primary actions,
    review decisions and the side navigation."""
    b = QPushButton(text)
    b.setProperty("primary", True)
    if icon_name:
        _set_button_icon(b, icon_name)
    return _tab_focus(b)


def button(text: str, icon_name: str = "") -> QPushButton:
    """Secondary action; ``icon_name`` only for icon-only buttons."""
    b = QPushButton(text)
    if icon_name:
        _set_button_icon(b, icon_name)
    return _tab_focus(b)


class KeyButton(QPushButton):
    """Button showing its keyboard shortcut after the label in muted text
    (« Masquer   V »): the keys are learnt by looking at the buttons."""

    _GAP = 10

    def __init__(self, text: str, key: str, icon_name: str = "") -> None:
        super().__init__(text)
        self.key = key
        if icon_name:
            _set_button_icon(self, icon_name)
        _tab_focus(self)

    def _key_width(self) -> int:
        return self.fontMetrics().horizontalAdvance(self.key) + self._GAP

    @override
    def sizeHint(self) -> QSize:
        size = super().sizeHint()
        return QSize(size.width() + self._key_width(), size.height())

    @override
    def minimumSizeHint(self) -> QSize:
        size = super().minimumSizeHint()
        return QSize(size.width() + self._key_width(), size.height())

    @override
    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QStylePainter(self)
        option = QStyleOptionButton()
        self.initStyleOption(option)
        option.text = ""
        option.icon = QIcon()
        painter.drawControl(QStyle.ControlElement.CE_PushButton, option)
        metrics = self.fontMetrics()
        has_icon = not self.icon().isNull()
        icon_size = self.iconSize() if has_icon else QSize(0, 0)
        icon_gap = 6 if has_icon else 0
        text_width = metrics.horizontalAdvance(self.text())
        total = icon_size.width() + icon_gap + text_width + self._key_width()
        x = max(4, (self.width() - total) // 2)
        enabled = self.isEnabled()
        t = theme.tokens()
        if has_icon:
            mode = QIcon.Mode.Normal if enabled else QIcon.Mode.Disabled
            pixmap = self.icon().pixmap(icon_size, self.devicePixelRatioF(), mode)
            painter.drawPixmap(x, (self.height() - icon_size.height()) // 2, pixmap)
            x += icon_size.width() + icon_gap
        flags = int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
        painter.setPen(QColor(t.text if enabled else t.muted))
        painter.drawText(QRect(x, 0, text_width + 2, self.height()), flags, self.text())
        x += text_width + self._GAP
        painter.setPen(QColor(t.muted))
        painter.drawText(QRect(x, 0, self._key_width(), self.height()), flags, self.key)


def pad_spin(*spins: QAbstractSpinBox) -> None:
    """Inset the value like other fields (the style sheet cannot reach the inner editor)."""
    for spin in spins:
        editor = spin.lineEdit()
        if editor is not None:
            editor.setTextMargins(6, 0, 0, 0)


def left_header(view: QAbstractItemView) -> None:
    """Column titles aligned like the cells below them (Qt centres them by default)."""
    header = getattr(view, "horizontalHeader", None)
    if header is not None:
        header().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)


def repolish(widget: QWidget) -> None:
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


class Banner(QFrame):
    """Inline message (level: info, success, warning, error). Hidden when empty."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Banner")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        self.label = QLabel()
        self.label.setWordWrap(True)
        self.label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.label, 1)
        self.actions_layout = QHBoxLayout()
        self.actions_layout.setSpacing(6)
        layout.addLayout(self.actions_layout)
        self.action_buttons: list[QPushButton] = []
        self.close_button = QPushButton("Fermer")
        self.close_button.setFlat(True)
        _tab_focus(self.close_button)
        self.close_button.clicked.connect(self.clear)
        layout.addWidget(self.close_button)
        self.level = "info"
        self.hide()

    def show_message(
        self,
        text: str,
        level: str = "info",
        actions: Sequence[tuple[str, Callable[[], None]]] = (),
    ) -> None:
        """Show ``text``; each action is a button that runs its callback (then hides)."""
        self.level = level
        self.setProperty("level", level)
        self.label.setText(typo(text))
        self._clear_actions()
        for label, callback in actions:
            action_button = button(label)
            action_button.clicked.connect(lambda _c=False, cb=callback: self._run(cb))
            self.actions_layout.addWidget(action_button)
            self.action_buttons.append(action_button)
        repolish(self)
        self.show()

    def _run(self, callback: Callable[[], None]) -> None:
        self.clear()
        callback()

    def _clear_actions(self) -> None:
        for action_button in self.action_buttons:
            self.actions_layout.removeWidget(action_button)
            action_button.deleteLater()
        self.action_buttons = []

    def clear(self) -> None:
        self.label.clear()
        self._clear_actions()
        self.hide()

    def text(self) -> str:
        return self.label.text()


class EmptyState(QWidget):
    def __init__(self, title: str, text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.addStretch(1)
        self.heading = section_title(title)
        self.heading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.body = muted(text)
        self.body.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.heading)
        layout.addWidget(self.body)
        layout.addStretch(2)

    def set_text(self, title: str, text: str) -> None:
        self.heading.setText(typo(title))
        self.body.setText(typo(text))


class DropArea(QFrame):
    """Accepts dropped files and folders (local paths only)."""

    dropped = Signal(list)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("DropArea")
        self.setAcceptDrops(True)
        self.setMinimumHeight(96)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.addStretch(1)
        self.title = section_title("Déposez ici des fichiers ou des dossiers")
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.hint = muted("")
        self.hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.title)
        layout.addWidget(self.hint)
        self.buttons = QHBoxLayout()
        self.buttons.addStretch(1)
        layout.addSpacing(6)
        layout.addLayout(self.buttons)
        layout.addStretch(1)

    def add_button(self, widget: QWidget) -> None:
        self.buttons.insertWidget(self.buttons.count(), widget)

    def finish_buttons(self) -> None:
        self.buttons.addStretch(1)

    @staticmethod
    def paths(event: QDropEvent | QDragEnterEvent) -> list[Path]:
        mime = event.mimeData()
        if not mime.hasUrls():
            return []
        return [Path(u.toLocalFile()) for u in mime.urls() if u.isLocalFile() and u.toLocalFile()]

    def set_active(self, active: bool) -> None:
        self.setProperty("active", active)
        repolish(self)

    @override
    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if self.paths(event) and self.isEnabled():
            event.acceptProposedAction()
            self.set_active(True)
        else:
            event.ignore()

    @override
    def dragLeaveEvent(self, event: QDragLeaveEvent) -> None:
        self.set_active(False)
        super().dragLeaveEvent(event)

    @override
    def dropEvent(self, event: QDropEvent) -> None:
        self.set_active(False)
        found = self.paths(event)
        if found:
            event.acceptProposedAction()
            self.dropped.emit(found)


def _pill(painter: QPainter, rect: QRect, text: str, level: str) -> None:
    metrics = QFontMetrics(painter.font())
    width = min(rect.width() - 4, metrics.horizontalAdvance(text) + 16)
    height = min(rect.height() - 6, metrics.height() + 4)
    pill = QRectF(rect.x() + 4, rect.center().y() - height / 2 + 1, width, height)
    fill = theme.badge_color(level)
    fill.setAlphaF(theme.tokens().badge_fill)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(fill)
    painter.drawRoundedRect(pill, height / 2, height / 2)
    painter.setPen(theme.badge_text(level))
    painter.drawText(
        pill,
        Qt.AlignmentFlag.AlignCenter,
        metrics.elidedText(text, Qt.TextElideMode.ElideRight, int(width) - 8),
    )


class BadgeDelegate(QStyledItemDelegate):
    """Draws the display text as a coloured pill when the item has a BADGE_ROLE and
    some text: an empty cell stays empty (pills only where the value varies)."""

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: IndexLike) -> None:
        level = index.data(BADGE_ROLE)
        text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        if not level or not text:
            super().paint(painter, option, index)
            return
        self.initStyleOption(option, index)
        option.text = ""
        style = option.widget.style() if option.widget else None
        if style is not None:
            style.drawControl(QStyle.ControlElement.CE_ItemViewItem, option, painter, option.widget)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        _pill(painter, option.rect, text, str(level))
        painter.restore()

    @override
    def sizeHint(self, option: QStyleOptionViewItem, index: IndexLike) -> QSize:
        size = super().sizeHint(option, index)
        return QSize(size.width() + 20, max(size.height(), 26))


#: Horizontal room a pill needs around its text: inner padding (16), left offset
#: (4) and a margin on the right so that it never touches the next column.
_PILL_ROOM = 32


def badge_column_width(view: QAbstractItemView, labels: Iterable[str]) -> int:
    """Width of a badge column whose pills must show the longest of ``labels`` in
    full (« Non pris en charge »), in the font the view will paint with."""
    view.ensurePolished()  # the application style sheet sets the font size
    metrics = QFontMetrics(view.font())
    return max((metrics.horizontalAdvance(t) for t in labels), default=0) + _PILL_ROOM


def fit_badge_column(view: QTableView | QTreeView, column: int, labels: Iterable[str]) -> None:
    """Give ``column`` of ``view`` a fixed width that fits every badge of ``labels``."""
    header = view.horizontalHeader() if isinstance(view, QTableView) else view.header()
    header.setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
    header.resizeSection(column, badge_column_width(view, labels))


def legend_html(types: Iterable[str]) -> str:
    """Rich-text legend: one coloured chip per entity type. Spaces inside a chip are
    unbreakable, so lines only wrap between chips."""
    chips = []
    for t in sorted(set(types)):
        bg, fg = theme.entity_colors(t)
        label = html.escape(entity_type_label(t)).replace(" ", "&nbsp;")
        chips.append(
            f'<span style="background-color:{bg.name()}; color:{fg.name()};">'
            f"&nbsp;{label}&nbsp;</span>"
        )
    return " &nbsp;".join(chips)
