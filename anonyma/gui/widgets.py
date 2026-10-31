"""Reusable widgets: page header, banners, drop area, badge and progress delegates."""

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
    QPainter,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QAbstractSpinBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from anonyma.gui import icons, theme
from anonyma.gui.labels import entity_type_label

#: Item data role holding a badge level ("success", "warning"...).
BADGE_ROLE = Qt.ItemDataRole.UserRole + 10
#: Item data role holding a progress value 0..100 (or None).
PROGRESS_ROLE = Qt.ItemDataRole.UserRole + 11

IndexLike = QModelIndex | QPersistentModelIndex


def page_header(title: str, subtitle: str = "") -> QWidget:
    box = QWidget()
    layout = QVBoxLayout(box)
    layout.setContentsMargins(0, 0, 0, 6)
    layout.setSpacing(2)
    label = QLabel(title)
    label.setObjectName("PageTitle")
    layout.addWidget(label)
    if subtitle:
        sub = QLabel(subtitle)
        sub.setObjectName("PageSubtitle")
        sub.setWordWrap(True)
        layout.addWidget(sub)
    return box


def muted(text: str) -> QLabel:
    label = QLabel(text)
    label.setProperty("muted", True)
    label.setWordWrap(True)
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


def _retint_buttons() -> None:
    """Re-tint every themed button icon (dark icons vanish on the dark theme otherwise)."""
    from PySide6.QtWidgets import QApplication

    for widget in QApplication.allWidgets():
        if isinstance(widget, QPushButton):
            name = widget.property(_ICON_PROPERTY)
            if name:
                widget.setIcon(icons.icon(str(name), _icon_colour(widget)))


def primary_button(text: str, icon_name: str = "") -> QPushButton:
    button = QPushButton(text)
    button.setProperty("primary", True)
    if icon_name:
        _set_button_icon(button, icon_name)
    return button


def button(text: str, icon_name: str = "") -> QPushButton:
    b = QPushButton(text)
    if icon_name:
        _set_button_icon(b, icon_name)
    return b


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
        self.close_button = QPushButton("Masquer")
        self.close_button.setFlat(True)
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
        self.label.setText(text)
        self._clear_actions()
        for label, callback in actions:
            action_button = QPushButton(label)
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
        heading = QLabel(title)
        heading.setObjectName("SectionTitle")
        heading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        body = muted(text)
        body.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(heading)
        layout.addWidget(body)
        layout.addStretch(2)


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
        self.title = QLabel("Déposez ici des fichiers ou des dossiers")
        self.title.setObjectName("SectionTitle")
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.hint = muted("")
        self.hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.title)
        layout.addWidget(self.hint)
        self.buttons = QHBoxLayout()
        self.buttons.addStretch(1)
        layout.addLayout(self.buttons)

    def add_button(self, widget: QWidget) -> None:
        self.buttons.insertWidget(self.buttons.count(), widget)

    def finish_buttons(self) -> None:
        self.buttons.addStretch(1)

    @staticmethod
    def _paths(event: QDropEvent | QDragEnterEvent) -> list[Path]:
        mime = event.mimeData()
        if not mime.hasUrls():
            return []
        return [Path(u.toLocalFile()) for u in mime.urls() if u.isLocalFile() and u.toLocalFile()]

    def _set_active(self, active: bool) -> None:
        self.setProperty("active", active)
        repolish(self)

    @override
    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if self._paths(event) and self.isEnabled():
            event.acceptProposedAction()
            self._set_active(True)
        else:
            event.ignore()

    @override
    def dragLeaveEvent(self, event: QDragLeaveEvent) -> None:
        self._set_active(False)
        super().dragLeaveEvent(event)

    @override
    def dropEvent(self, event: QDropEvent) -> None:
        self._set_active(False)
        found = self._paths(event)
        if found:
            event.acceptProposedAction()
            self.dropped.emit(found)


def _pill(painter: QPainter, rect: QRect, text: str, colour: QColor) -> None:
    metrics = QFontMetrics(painter.font())
    width = min(rect.width() - 4, metrics.horizontalAdvance(text) + 16)
    height = min(rect.height() - 6, metrics.height() + 4)
    pill = QRectF(rect.x() + 4, rect.center().y() - height / 2 + 1, width, height)
    fill = QColor(colour)
    fill.setAlphaF(0.18 if not theme.is_dark() else 0.28)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(fill)
    painter.drawRoundedRect(pill, height / 2, height / 2)
    painter.setPen(colour if not theme.is_dark() else colour.lighter(115))
    painter.drawText(
        pill,
        Qt.AlignmentFlag.AlignCenter,
        metrics.elidedText(text, Qt.TextElideMode.ElideRight, int(width) - 8),
    )


class BadgeDelegate(QStyledItemDelegate):
    """Draws the display text as a coloured pill when the item has a BADGE_ROLE."""

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: IndexLike) -> None:
        level = index.data(BADGE_ROLE)
        if not level:
            super().paint(painter, option, index)
            return
        self.initStyleOption(option, index)
        text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        option.text = ""
        style = option.widget.style() if option.widget else None
        if style is not None:
            style.drawControl(QStyle.ControlElement.CE_ItemViewItem, option, painter, option.widget)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        _pill(painter, option.rect, text, theme.badge_color(str(level)))
        painter.restore()

    @override
    def sizeHint(self, option: QStyleOptionViewItem, index: IndexLike) -> QSize:
        size = super().sizeHint(option, index)
        return QSize(size.width() + 20, max(size.height(), 26))


class ProgressDelegate(QStyledItemDelegate):
    """Thin progress bar for items carrying PROGRESS_ROLE (0..100)."""

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: IndexLike) -> None:
        value = index.data(PROGRESS_ROLE)
        if value is None:
            super().paint(painter, option, index)
            return
        self.initStyleOption(option, index)
        option.text = ""
        style = option.widget.style() if option.widget else None
        if style is not None:
            style.drawControl(QStyle.ControlElement.CE_ItemViewItem, option, painter, option.widget)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = option.rect.adjusted(6, 0, -6, 0)
        bar = QRectF(rect.x(), rect.center().y() - 3, rect.width(), 6)
        t = theme.tokens()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(t.border))
        painter.drawRoundedRect(bar, 3, 3)
        done = QRectF(bar.x(), bar.y(), bar.width() * max(0, min(100, int(value))) / 100, 6)
        painter.setBrush(QColor(t.accent))
        painter.drawRoundedRect(done, 3, 3)
        painter.restore()


def legend_html(types: Iterable[str]) -> str:
    """Rich-text legend: one coloured chip per entity type."""
    chips = []
    for t in sorted(set(types)):
        bg, fg = theme.entity_colors(t)
        chips.append(
            f'<span style="background-color:{bg.name()}; color:{fg.name()};">'
            f"&nbsp;{html.escape(entity_type_label(t))}&nbsp;</span>"
        )
    return "&nbsp;&nbsp;".join(chips)
