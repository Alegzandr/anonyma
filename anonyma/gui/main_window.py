"""Main window: side navigation and the five pages."""

from __future__ import annotations

from typing import override

from PySide6.QtCore import QByteArray, QModelIndex, QPersistentModelIndex, QSize, Qt
from PySide6.QtGui import QCloseEvent, QKeySequence, QPainter, QShortcut, QShowEvent
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QStackedWidget,
    QStatusBar,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from anonyma.frenchtext import typo
from anonyma.gui import dialogs, icons, theme
from anonyma.gui.context import AppContext
from anonyma.gui.controller import BatchController, Phase
from anonyma.gui.pages.models import ModelsPage
from anonyma.gui.pages.processing import ProcessingPage
from anonyma.gui.pages.report import ReportPage
from anonyma.gui.pages.review import ReviewPage
from anonyma.gui.pages.settings import SettingsPage
from anonyma.gui.update import UpdateManager
from anonyma.logs import get_logger

log = get_logger("gui.window")

IndexLike = QModelIndex | QPersistentModelIndex

#: Navigation, in workflow order; Ctrl+1… follows this order. A gap separates the
#: batch pages from the configuration pages.
PAGES = (
    ("processing", "Traitement", "process"),
    ("review", "Aperçu", "review"),
    ("report", "Rapport", "report"),
    ("models", "Modèles", "models"),
    ("settings", "Réglages", "settings"),
)
_GAP_BEFORE = "models"
_GAP = 16
_GAP_ROLE = Qt.ItemDataRole.UserRole + 1


class _NavDelegate(QStyledItemDelegate):
    """Draws the first configuration page below a gap that separates it from the
    batch pages (the gap is part of its row, so keyboard navigation is unchanged)."""

    @override
    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: IndexLike) -> None:
        if index.data(_GAP_ROLE):
            option = QStyleOptionViewItem(option)
            option.rect = option.rect.adjusted(0, _GAP, 0, 0)
        super().paint(painter, option, index)


class MainWindow(QMainWindow):
    def __init__(self, context: AppContext | None = None) -> None:
        super().__init__()
        self.context = context or AppContext()
        self.controller = BatchController(self.context, self)
        self.setWindowTitle("Anonyma")
        self.setWindowIcon(icons.app_icon())
        # 1060: the review page's two panes stay legible (fits 1366 px at 125 %).
        self.setMinimumSize(QSize(1060, 640))

        central = QWidget()
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        side = QWidget()
        side.setObjectName("SidebarBox")
        side.setFixedWidth(200)
        side_layout = QVBoxLayout(side)
        side_layout.setContentsMargins(0, 0, 0, 0)
        side_layout.setSpacing(0)
        side_layout.addWidget(self._brand())
        self.nav = QListWidget()
        self.nav.setObjectName("Sidebar")
        self.nav.setIconSize(QSize(18, 18))
        self.nav.setItemDelegate(_NavDelegate(self.nav))
        # Tab reaches the list (focus ring on the current page); a click does not
        # leave a ring behind.
        self.nav.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        side_layout.addWidget(self.nav, 1)
        self.footer = QLabel(typo("Hors ligne : aucune donnée ne quitte ce poste"))
        self.footer.setObjectName("SidebarFooter")
        self.footer.setWordWrap(True)
        self.footer.setContentsMargins(20, 8, 12, 14)
        side_layout.addWidget(self.footer)
        root.addWidget(side)

        # The status bar belongs to the page column: the side column runs to the
        # bottom edge of the window (QMainWindow's own bar spans the full width).
        main = QWidget()
        main_layout = QVBoxLayout(main)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        self.stack = QStackedWidget()
        main_layout.addWidget(self.stack, 1)
        self.status_bar = QStatusBar()
        self.status_bar.setObjectName("PageStatusBar")
        status_row = QHBoxLayout()
        status_row.setContentsMargins(18, 0, 0, 0)  # aligned with the pages' margin
        status_row.addWidget(self.status_bar)
        main_layout.addLayout(status_row)
        root.addWidget(main, 1)
        self.setCentralWidget(central)

        self.processing = ProcessingPage(self.controller)
        self.review = ReviewPage(self.controller)
        self.models = ModelsPage(self.context)
        self.settings = SettingsPage(self.context)
        self.report = ReportPage(self.controller)
        self.pages: dict[str, QWidget] = {
            "processing": self.processing,
            "review": self.review,
            "report": self.report,
            "models": self.models,
            "settings": self.settings,
        }
        for number, (key, label, _icon) in enumerate(PAGES, start=1):
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, key)
            item.setData(_GAP_ROLE, key == _GAP_BEFORE)
            item.setSizeHint(QSize(180, 40 + (_GAP if key == _GAP_BEFORE else 0)))
            item.setToolTip(f"{label} (Ctrl+{number})")
            self.nav.addItem(item)
            self.stack.addWidget(self.pages[key])
            shortcut = QShortcut(QKeySequence(f"Ctrl+{number}"), self)
            shortcut.activated.connect(lambda k=key: self.show_page(k))
        self.nav.currentItemChanged.connect(self._nav_changed)
        self.nav.setCurrentRow(0)
        self._refresh_icons()
        QShortcut(QKeySequence("Ctrl+O"), self).activated.connect(self._add_files)

        self.processing.review_requested.connect(lambda: self.show_page("review"))
        self.processing.report_requested.connect(lambda: self.show_page("report"))
        self.processing.document_requested.connect(self._show_document)
        self.report.document_requested.connect(self._show_document)
        self.review.apply_requested.connect(self._apply_from_review)
        self.report.correct_requested.connect(self._correct_and_reapply)
        self.report.residue_requested.connect(self._show_residue)
        self.updates = UpdateManager(self)
        self.controller.result_ready.connect(self._result_ready)
        self.controller.phase_changed.connect(self._phase_changed)
        theme.notifier().changed.connect(self._refresh_icons)

        self._restore_geometry()
        self._first_show = True

    @override
    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        if self._first_show:
            # Start without a focus ring on the side navigation: Tab reaches it.
            self._first_show = False
            self.setFocus(Qt.FocusReason.OtherFocusReason)

    def _brand(self) -> QWidget:
        """Application mark and name at the top of the side column."""
        box = QWidget()
        row = QHBoxLayout(box)
        row.setContentsMargins(18, 18, 12, 14)
        row.setSpacing(10)
        self.logo = QLabel()
        self.logo.setObjectName("AppLogo")
        self.logo.setFixedSize(28, 28)
        self.logo.setPixmap(icons.app_pixmap(28, self.devicePixelRatioF()))
        row.addWidget(self.logo)
        title = QLabel("Anonyma")
        title.setObjectName("AppTitle")
        row.addWidget(title, 1)
        return box

    # -- navigation ---------------------------------------------------------------------

    def _nav_item(self, key: str) -> QListWidgetItem | None:
        for row in range(self.nav.count()):
            item = self.nav.item(row)
            if item is not None and item.data(Qt.ItemDataRole.UserRole) == key:
                return item
        return None

    def show_page(self, key: str) -> None:
        item = self._nav_item(key)
        if item is not None:
            self.nav.setCurrentItem(item)

    def current_page(self) -> str:
        item = self.nav.currentItem()
        return str(item.data(Qt.ItemDataRole.UserRole) or "") if item is not None else ""

    def _nav_changed(self, current: QListWidgetItem | None, _previous: object) -> None:
        key = str(current.data(Qt.ItemDataRole.UserRole) or "") if current is not None else ""
        if key in self.pages:
            self.stack.setCurrentWidget(self.pages[key])
            self._phase_changed(self.controller.phase.value)

    def _add_files(self) -> None:
        self.show_page("processing")
        self.processing.add_files_button.click()

    def _refresh_icons(self) -> None:
        colour = theme.tokens().sidebar_text
        for key, _label, icon_name in PAGES:
            item = self._nav_item(key)
            if item is not None:
                item.setIcon(icons.icon(icon_name, colour))

    def _apply_from_review(self) -> None:
        self.show_page("processing")
        self.processing.start_apply()

    def _correct_and_reapply(self) -> None:
        self.show_page("review")
        self.review.banner.show_message(
            "Corrigez les décisions (masquez l'entité oubliée, ajoutez une entité…), puis "
            "cliquez sur « Appliquer les décisions » : les fichiers produits précédemment "
            "seront remplacés.",
            "info",
        )

    def _show_document(self, document_id: str) -> None:
        self.show_page("review")
        self.review.select_document(document_id)

    def _show_residue(self, top_document_id: str, location: str, value: str) -> None:
        self.show_page("review")
        self.review.show_residue(top_document_id, location, value)

    def _result_ready(self) -> None:
        # « Traitement » already says it in its banner: only other pages need the news.
        if self.current_page() in ("processing", "review"):
            return
        self.status_bar.showMessage(typo("Traitement terminé : le rapport est disponible."), 8000)

    def _phase_changed(self, phase: str) -> None:
        """The status bar only speaks while something runs, and only away from
        « Traitement », which shows the progress itself (no idle « Prêt. »)."""
        messages = {
            Phase.ANALYZING.value: "Analyse en cours…",
            Phase.APPLYING.value: "Écriture et vérification en cours…",
        }
        message = messages.get(phase) if self.current_page() != "processing" else None
        if message:
            self.status_bar.showMessage(message)
        else:
            self.status_bar.clearMessage()

    # -- geometry (kept in Config.ui, never in QSettings) -----------------------------------

    def _restore_geometry(self) -> None:
        raw = self.context.ui_pref("window_geometry")
        if isinstance(raw, str) and raw:
            try:
                self.restoreGeometry(QByteArray.fromHex(raw.encode("ascii")))
                return
            except (ValueError, UnicodeEncodeError):
                pass
        self.resize(1240, 800)

    def _save_geometry(self) -> None:
        data = bytes(self.saveGeometry().toHex().data()).decode("ascii")
        self.context.set_ui_pref("window_geometry", data)

    # -- closing -------------------------------------------------------------------------------

    def busy(self) -> bool:
        """A batch or an update download runs. The automatic model installation does
        not count: closing interrupts it and the next start resumes it."""
        return self.controller.busy or self.updates.busy()

    @override
    def closeEvent(self, event: QCloseEvent) -> None:
        if self.busy() and not dialogs.confirm(
            self,
            "Un traitement ou un téléchargement est en cours. Voulez-vous l'annuler et quitter ?",
            title="Quitter Anonyma",
            ok_label="Annuler et quitter",
            cancel_label="Continuer le traitement",
            warn=True,
        ):
            event.ignore()
            return
        if self.settings.dirty and not dialogs.confirm(
            self,
            "Des réglages modifiés n'ont pas été enregistrés. Quitter sans les enregistrer ?",
            title="Quitter Anonyma",
            ok_label="Quitter sans enregistrer",
            cancel_label="Rester",
            warn=True,
        ):
            event.ignore()
            return
        self.models.cancel_download()
        self.updates.shutdown()
        self.controller.shutdown()
        self.models.runner.wait_all(10_000)
        try:
            self._save_geometry()
        except Exception as exc:
            log.warning("geometry not saved: %s", type(exc).__name__)
        event.accept()
