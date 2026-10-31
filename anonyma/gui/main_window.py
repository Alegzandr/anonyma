"""Main window: side navigation and the five pages."""

from __future__ import annotations

from typing import override

from PySide6.QtCore import QByteArray, QSize, Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from anonyma.gui import dialogs, icons, theme
from anonyma.gui.context import AppContext
from anonyma.gui.controller import BatchController, Phase
from anonyma.gui.pages.models import ModelsPage
from anonyma.gui.pages.processing import ProcessingPage
from anonyma.gui.pages.report import ReportPage
from anonyma.gui.pages.review import ReviewPage
from anonyma.gui.pages.settings import SettingsPage
from anonyma.logs import get_logger

log = get_logger("gui.window")

PAGES = (
    ("processing", "Traitement", "process"),
    ("review", "Revue", "review"),
    ("models", "Modèles", "models"),
    ("settings", "Réglages", "settings"),
    ("report", "Rapport", "report"),
)


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
        self.nav.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        side_layout.addWidget(self.nav, 1)
        self.footer = QLabel("Hors ligne · traitement local")
        self.footer.setObjectName("SidebarFooter")
        self.footer.setContentsMargins(20, 8, 12, 14)
        side_layout.addWidget(self.footer)
        root.addWidget(side)

        self.stack = QStackedWidget()
        root.addWidget(self.stack, 1)
        self.setCentralWidget(central)

        self.processing = ProcessingPage(self.controller)
        self.review = ReviewPage(self.controller)
        self.models = ModelsPage(self.context)
        self.settings = SettingsPage(self.context)
        self.report = ReportPage(self.controller)
        self.pages: dict[str, QWidget] = {
            "processing": self.processing,
            "review": self.review,
            "models": self.models,
            "settings": self.settings,
            "report": self.report,
        }
        for key, label, _icon in PAGES:
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, key)
            item.setSizeHint(QSize(180, 40))
            self.nav.addItem(item)
            self.stack.addWidget(self.pages[key])
        self.nav.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.nav.setCurrentRow(0)
        self._refresh_icons()

        self.processing.review_requested.connect(lambda: self.show_page("review"))
        self.processing.report_requested.connect(lambda: self.show_page("report"))
        self.review.apply_requested.connect(self._apply_from_review)
        self.review.pending_changed.connect(self._update_review_label)
        self.report.correct_requested.connect(self._correct_and_reapply)
        self.report.residue_requested.connect(self._show_residue)
        self.controller.result_ready.connect(self._result_ready)
        self.controller.phase_changed.connect(self._phase_changed)
        theme.notifier().changed.connect(self._refresh_icons)

        self.statusBar().showMessage("Prêt.")
        self._restore_geometry()

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

    def show_page(self, key: str) -> None:
        for row in range(self.nav.count()):
            item = self.nav.item(row)
            if item is not None and item.data(Qt.ItemDataRole.UserRole) == key:
                self.nav.setCurrentRow(row)
                return

    def current_page(self) -> str:
        item = self.nav.currentItem()
        return str(item.data(Qt.ItemDataRole.UserRole)) if item is not None else ""

    def _refresh_icons(self) -> None:
        colour = theme.tokens().sidebar_text
        for row, (_key, _label, icon_name) in enumerate(PAGES):
            item = self.nav.item(row)
            if item is not None:
                item.setIcon(icons.icon(icon_name, colour))

    def _update_review_label(self, pending: int) -> None:
        item = self.nav.item(1)
        if item is not None:
            item.setText(f"Revue ({pending})" if pending else "Revue")

    def _apply_from_review(self) -> None:
        self.show_page("processing")
        self.processing.start_apply()

    def _correct_and_reapply(self) -> None:
        self.show_page("review")
        self.review.banner.show_message(
            "Corrigez les décisions (validez l'entité oubliée, ajoutez une entité…), puis "
            "cliquez sur « Appliquer » : les fichiers produits précédemment seront remplacés.",
            "info",
        )

    def _show_residue(self, top_document_id: str, location: str, value: str) -> None:
        self.show_page("review")
        self.review.show_residue(top_document_id, location, value)

    def _result_ready(self) -> None:
        if self.current_page() == "review":
            return
        self.statusBar().showMessage("Traitement terminé : le rapport est disponible.", 8000)

    def _phase_changed(self, phase: str) -> None:
        messages = {
            Phase.ANALYZING.value: "Analyse en cours…",
            Phase.APPLYING.value: "Écriture et vérification en cours…",
        }
        self.statusBar().showMessage(messages.get(phase, "Prêt."))

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
        return self.controller.busy or self.models.busy()

    @override
    def closeEvent(self, event: QCloseEvent) -> None:
        if self.busy() and not dialogs.confirm(
            self,
            "Un traitement ou un téléchargement est en cours. Voulez-vous l'annuler et quitter ?",
            title="Quitter Anonyma",
            ok_label="Annuler et quitter",
            cancel_label="Continuer le traitement",
        ):
            event.ignore()
            return
        if self.settings.dirty and not dialogs.confirm(
            self,
            "Des réglages modifiés n'ont pas été enregistrés. Quitter sans les enregistrer ?",
            title="Quitter Anonyma",
            ok_label="Quitter",
        ):
            event.ignore()
            return
        self.models.cancel_download()
        self.controller.shutdown()
        self.models.runner.wait_all(10_000)
        try:
            self._save_geometry()
        except Exception as exc:
            log.warning("geometry not saved: %s", type(exc).__name__)
        event.accept()
