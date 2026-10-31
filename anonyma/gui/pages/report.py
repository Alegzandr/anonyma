"""« Rapport » page: end-of-batch summary and export."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from anonyma.gui import dialogs, theme
from anonyma.gui.controller import BatchController
from anonyma.gui.labels import STATUS_LEVELS, entity_type_label
from anonyma.gui.tasks import user_message
from anonyma.gui.widgets import (
    BADGE_ROLE,
    BadgeDelegate,
    Banner,
    EmptyState,
    button,
    page_header,
)
from anonyma.logs import get_logger
from anonyma.models import DocumentStatus
from anonyma.pipeline import QUARANTINE_DIR
from anonyma.report import STATUS_LABELS, BatchReport

log = get_logger("gui.report")

#: Summary cards, in display order.
CARDS = (
    DocumentStatus.ANONYMIZED,
    DocumentStatus.NEEDS_REVIEW,
    DocumentStatus.RESIDUE,
    DocumentStatus.FAILED,
    DocumentStatus.UNSUPPORTED,
    DocumentStatus.CANCELLED,
)

_LABEL_TO_LEVEL = {STATUS_LABELS[s]: STATUS_LEVELS.get(s, "neutral") for s in STATUS_LABELS}


def _table(headers: list[str]) -> QTableWidget:
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.verticalHeader().hide()
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setAlternatingRowColors(True)
    table.setShowGrid(False)
    table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
    return table


def _fill(table: QTableWidget, rows: list[list[str]], badge_col: int = -1) -> None:
    table.setRowCount(len(rows))
    for r, values in enumerate(rows):
        for c, value in enumerate(values):
            item = QTableWidgetItem(value)
            item.setToolTip(value)
            if c == badge_col:
                item.setData(BADGE_ROLE, _LABEL_TO_LEVEL.get(value, "neutral"))
            table.setItem(r, c, item)


class _Card(QFrame):
    def __init__(self, title: str, level: str) -> None:
        super().__init__()
        self.setObjectName("Card")
        self.level = level
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        self.value = QLabel("0")
        self.value.setStyleSheet("font-size: 18pt; font-weight: 600;")
        self.title = QLabel(title)
        self.title.setProperty("muted", True)
        layout.addWidget(self.value)
        layout.addWidget(self.title)

    def set_value(self, value: int) -> None:
        colour = theme.badge_color(self.level).name() if value else theme.tokens().muted
        self.value.setText(str(value))
        self.value.setStyleSheet(f"font-size: 18pt; font-weight: 600; color: {colour};")


class ReportPage(QWidget):
    def __init__(self, controller: BatchController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.controller = controller
        self.report: BatchReport | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)
        head = QHBoxLayout()
        head.addWidget(
            page_header(
                "Rapport",
                "Bilan du dernier lot. Le rapport ne contient que des noms anonymisés, des "
                "nombres et des codes : aucune valeur sensible.",
            ),
            1,
        )
        layout.addLayout(head)
        self.banner = Banner()
        layout.addWidget(self.banner)

        self.stack = QStackedWidget()
        layout.addWidget(self.stack, 1)
        self.stack.addWidget(
            EmptyState("Aucun rapport", "Le rapport apparaît ici à la fin d'un traitement.")
        )
        content = QWidget()
        self.stack.addWidget(content)
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(12)

        self.meta = QLabel("")
        self.meta.setProperty("muted", True)
        content_layout.addWidget(self.meta)
        cards = QHBoxLayout()
        self.cards: dict[DocumentStatus, _Card] = {}
        for status in CARDS:
            card = _Card(STATUS_LABELS[status], STATUS_LEVELS.get(status, "neutral"))
            self.cards[status] = card
            cards.addWidget(card)
        content_layout.addLayout(cards)

        self.tabs = QTabWidget()
        self.files_table = _table(["Fichier", "Format", "Statut", "Entités", "Résidus", "À revoir"])
        self.files_table.setItemDelegateForColumn(2, BadgeDelegate(self.files_table))
        self.types_table = _table(["Type", "Code", "Nombre"])
        self.residues_table = _table(["Emplacement", "Zone", "Type", "Origine", "Détecteur"])
        self.failures_table = _table(["Fichier", "Statut", "Problèmes"])
        self.failures_table.setItemDelegateForColumn(1, BadgeDelegate(self.failures_table))
        self.failures_table.setWordWrap(True)
        self.tabs.addTab(self.files_table, "Fichiers")
        self.tabs.addTab(self.types_table, "Entités par type")
        self.tabs.addTab(self.residues_table, "Résidus")
        self.tabs.addTab(self.failures_table, "Échecs")
        content_layout.addWidget(self.tabs, 1)

        actions = QHBoxLayout()
        self.json_button = button("Exporter en JSON…", "export")
        self.md_button = button("Exporter en Markdown…", "export")
        self.html_button = button("Exporter en HTML…", "export")
        self.open_button = button("Ouvrir le dossier de sortie", "folder")
        self.json_button.clicked.connect(lambda: self.export("json"))
        self.md_button.clicked.connect(lambda: self.export("md"))
        self.html_button.clicked.connect(lambda: self.export("html"))
        self.open_button.clicked.connect(self.open_output)
        for b in (self.json_button, self.md_button, self.html_button):
            actions.addWidget(b)
        actions.addStretch(1)
        actions.addWidget(self.open_button)
        content_layout.addLayout(actions)

        controller.result_ready.connect(self.reload)
        controller.analysis_cleared.connect(self.reload)
        self.reload()

    @staticmethod
    def html_available() -> bool:
        return callable(getattr(BatchReport, "save_html", None))

    def reload(self) -> None:
        result = self.controller.result
        self.banner.clear()
        if result is None:
            self.report = None
            self.stack.setCurrentIndex(0)
            return
        try:
            report = BatchReport.from_result(result)
        except Exception as exc:
            log.warning("report failed: %s", type(exc).__name__)
            self.report = None
            self.stack.setCurrentIndex(0)
            self.banner.show_message(user_message(exc), "error")
            return
        self.report = report
        self.stack.setCurrentIndex(1)
        self.meta.setText(
            f"Lot {report.batch_id} · début {report.started_at.replace('T', ' ')} · fin "
            f"{report.finished_at.replace('T', ' ')} · sortie : {result.output_dir}"
        )
        for status, card in self.cards.items():
            card.set_value(report.status_counts.get(status, 0))
        _fill(
            self.files_table,
            [
                [
                    str(f["fichier"]),
                    str(f["format"]),
                    str(f["statut"]),
                    str(f["entites"]),
                    str(f["residus"]),
                    str(f["a_revoir"]),
                ]
                for f in report.files
            ],
            badge_col=2,
        )
        _fill(
            self.types_table,
            [
                [entity_type_label(t), t, str(n)]
                for t, n in sorted(report.entity_counts.items(), key=lambda kv: -kv[1])
            ],
        )
        _fill(
            self.residues_table,
            [
                [r["emplacement"], r["zone"], r["type"], r["origine"], r["detecteur"]]
                for r in report.residues
            ],
        )
        _fill(
            self.failures_table,
            [
                [str(f["fichier"]), str(f["statut"]), "\n".join(f["problemes"])]
                for f in report.failures
            ],
            badge_col=1,
        )
        self.failures_table.resizeRowsToContents()
        self.tabs.setTabText(2, f"Résidus ({len(report.residues)})")
        self.tabs.setTabText(3, f"Échecs ({len(report.failures)})")
        self.html_button.setVisible(self.html_available())
        quarantined = sum(
            1
            for r in result.results
            if r.output_path is not None and QUARANTINE_DIR in r.output_path.parts
        )
        if quarantined:
            self.banner.show_message(
                f"{quarantined} fichier(s) ont été placés en quarantaine dans "
                f"« {result.output_dir / QUARANTINE_DIR} ». Ils ne sont pas considérés comme "
                "anonymisés : ne les diffusez pas.",
                "warning",
            )

    def default_export_path(self, extension: str) -> Path:
        assert self.report is not None and self.controller.result is not None
        return self.controller.result.output_dir / f"rapport-{self.report.batch_id}.{extension}"

    def export(self, kind: str, target: Path | None = None) -> Path | None:
        if self.report is None or self.controller.result is None:
            return None
        if target is None:
            default = self.default_export_path(kind)
            target = dialogs.save_file(self, "Exporter le rapport", default.name, default.parent)
            if target is None:
                return None
        try:
            if kind == "json":
                self.report.save_json(target)
            elif kind == "md":
                target.write_text(self.report.to_markdown(), encoding="utf-8")
            elif kind == "html":
                save_html = getattr(self.report, "save_html", None)
                if not callable(save_html):
                    raise NotImplementedError
                save_html(target)
            else:
                return None
        except Exception as exc:
            log.warning("report export failed: %s", type(exc).__name__)
            self.banner.show_message(user_message(exc), "error")
            return None
        self.banner.show_message(f"Rapport exporté : {target}", "success")
        return target

    def open_output(self) -> None:
        result = self.controller.result
        if result is None:
            return
        if not result.output_dir.is_dir():
            dialogs.warning(self, "Le dossier de sortie n'existe plus.")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(result.output_dir)))
