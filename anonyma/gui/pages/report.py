"""« Rapport » page: end-of-batch summary, quarantine, residues and export."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QUrl, Signal
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
from anonyma.gui.labels import STATUS_LEVELS, entity_type_label, status_label
from anonyma.gui.tasks import user_message
from anonyma.gui.widgets import (
    BADGE_ROLE,
    BadgeDelegate,
    Banner,
    EmptyState,
    button,
    muted,
    page_header,
    primary_button,
)
from anonyma.logs import get_logger
from anonyma.models import DocumentResult, DocumentStatus, Residue, Severity
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

ORIGINS = {"original_value": "valeur d'origine", "detector": "détecteur"}


@dataclass(frozen=True, slots=True)
class ResidueRow:
    top_document_id: str
    file: str
    residue: Residue


def _table(headers: list[str]) -> QTableWidget:
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.verticalHeader().hide()
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
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


def is_quarantined(result: DocumentResult) -> bool:
    return result.output_path is not None and QUARANTINE_DIR in result.output_path.parts


def quarantine_reason(result: DocumentResult) -> str:
    """Plain-French explanation of why a file went to quarantine."""
    messages = [i.message for i in result.issues if i.severity is Severity.ERROR]
    if result.status is DocumentStatus.RESIDUE:
        head = (
            f"{result.residue_count} valeur(s) sensible(s) encore présente(s) après anonymisation."
        )
    else:
        head = "La vérification du fichier produit n'a pas pu aboutir."
    detail = " ".join(m for m in messages if "quarantaine" not in m)
    return f"{head} {detail}".strip()


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
    #: « Corriger et réappliquer »: back to the review with the same analysis.
    correct_requested = Signal()
    #: (top-level document id, residue location, residue value)
    residue_requested = Signal(str, str, str)

    def __init__(self, controller: BatchController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.controller = controller
        self.report: BatchReport | None = None
        self.residue_rows: list[ResidueRow] = []
        self.quarantined: list[DocumentResult] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)
        layout.addWidget(
            page_header(
                "Rapport",
                "Bilan du dernier lot. Les exports ne contiennent que des noms anonymisés, des "
                "nombres et des codes : aucune valeur sensible.",
            )
        )
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
        self.meta.setWordWrap(True)
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
        self.files_table.doubleClicked.connect(lambda _i: self.open_selected_output())
        self.types_table = _table(["Type", "Code", "Nombre"])

        quarantine_box = QWidget()
        q_layout = QVBoxLayout(quarantine_box)
        q_layout.setContentsMargins(0, 8, 0, 0)
        q_layout.addWidget(
            muted(
                "Un fichier est mis en quarantaine quand la vérification finale retrouve une "
                "valeur sensible ou ne peut pas relire le fichier produit. Il n'est pas "
                "considéré comme anonymisé et ne doit pas être diffusé. Corrigez les décisions "
                "dans « Revue » (valider l'entité oubliée, ajouter une entité…) puis réappliquez."
            )
        )
        self.quarantine_table = _table(["Fichier", "Statut", "Raison"])
        self.quarantine_table.setItemDelegateForColumn(1, BadgeDelegate(self.quarantine_table))
        self.quarantine_table.setWordWrap(True)
        q_layout.addWidget(self.quarantine_table, 1)
        q_actions = QHBoxLayout()
        self.correct_button = primary_button("Corriger et réappliquer", "review")
        self.correct_button.clicked.connect(self.correct_requested.emit)
        q_actions.addStretch(1)
        q_actions.addWidget(self.correct_button)
        q_layout.addLayout(q_actions)

        residues_box = QWidget()
        r_layout = QVBoxLayout(residues_box)
        r_layout.setContentsMargins(0, 8, 0, 0)
        self.residues_table = _table(["Fichier", "Emplacement", "Zone", "Type", "Origine"])
        self.residues_table.doubleClicked.connect(lambda _i: self.show_selected_residue())
        r_layout.addWidget(self.residues_table, 1)
        r_actions = QHBoxLayout()
        r_actions.addWidget(
            muted("Double-cliquez sur un résidu pour le voir dans le fichier produit.")
        )
        r_actions.addStretch(1)
        self.residue_button = button("Voir dans le fichier produit", "review")
        self.residue_button.clicked.connect(self.show_selected_residue)
        r_actions.addWidget(self.residue_button)
        r_layout.addLayout(r_actions)

        self.failures_table = _table(["Fichier", "Statut", "Problèmes"])
        self.failures_table.setItemDelegateForColumn(1, BadgeDelegate(self.failures_table))
        self.failures_table.setWordWrap(True)
        self.tabs.addTab(self.files_table, "Fichiers")
        self.tabs.addTab(self.types_table, "Entités par type")
        self.tabs.addTab(quarantine_box, "Quarantaine")
        self.tabs.addTab(residues_box, "Résidus")
        self.tabs.addTab(self.failures_table, "Échecs")
        content_layout.addWidget(self.tabs, 1)

        actions = QHBoxLayout()
        self.json_button = button("Exporter en JSON…", "export")
        self.md_button = button("Exporter en Markdown…", "export")
        self.html_button = button("Exporter en HTML…", "export")
        self.open_file_button = button("Afficher le fichier produit", "file")
        self.open_button = button("Ouvrir le dossier de sortie", "folder")
        self.json_button.clicked.connect(lambda: self.export("json"))
        self.md_button.clicked.connect(lambda: self.export("md"))
        self.html_button.clicked.connect(lambda: self.export("html"))
        self.open_file_button.clicked.connect(self.open_selected_output)
        self.open_button.clicked.connect(self.open_output)
        for b in (self.json_button, self.md_button, self.html_button):
            actions.addWidget(b)
        actions.addStretch(1)
        actions.addWidget(self.open_file_button)
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
            self.residue_rows = []
            self.quarantined = []
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
        self.quarantined = [r for r in result.results if is_quarantined(r)]
        _fill(
            self.quarantine_table,
            [
                [r.output_name, status_label(r.status), quarantine_reason(r)]
                for r in self.quarantined
            ],
            badge_col=1,
        )
        self.quarantine_table.resizeRowsToContents()
        self.residue_rows = [
            ResidueRow(r.document_id, r.output_name, res)
            for r in result.results
            if r.verification is not None
            for res in r.verification.residues
        ]
        _fill(
            self.residues_table,
            [
                [
                    row.file,
                    row.residue.location,
                    row.residue.segment_label,
                    entity_type_label(row.residue.entity_type),
                    ORIGINS.get(row.residue.reason, row.residue.reason),
                ]
                for row in self.residue_rows
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
        self.tabs.setTabText(2, f"Quarantaine ({len(self.quarantined)})")
        self.tabs.setTabText(3, f"Résidus ({len(self.residue_rows)})")
        self.tabs.setTabText(4, f"Échecs ({len(report.failures)})")
        self.correct_button.setEnabled(
            bool(self.quarantined) and self.controller.analysis is not None
        )
        self.residue_button.setEnabled(bool(self.residue_rows))
        self.html_button.setVisible(self.html_available())
        if self.quarantined:
            self.banner.show_message(
                f"{len(self.quarantined)} fichier(s) en quarantaine dans "
                f"« {result.output_dir / QUARANTINE_DIR} » : ils ne sont pas considérés comme "
                "anonymisés.",
                "warning",
                [
                    ("Voir pourquoi", lambda: self.tabs.setCurrentIndex(2)),
                    ("Corriger et réappliquer", self.correct_requested.emit),
                ],
            )

    def show_selected_residue(self) -> None:
        row = self.residues_table.currentRow()
        if not 0 <= row < len(self.residue_rows):
            return
        item = self.residue_rows[row]
        self.residue_requested.emit(item.top_document_id, item.residue.location, item.residue.value)

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

    def selected_output(self) -> Path | None:
        result = self.controller.result
        row = self.files_table.currentRow()
        if result is None or not 0 <= row < len(result.results):
            return None
        return result.results[row].output_path

    def open_selected_output(self) -> None:
        """Show the produced file in the file manager; a folder output is opened."""
        path = self.selected_output()
        if path is None or not path.exists():
            dialogs.warning(self, "Aucun fichier produit pour cette ligne.")
            return
        self._open(path if path.is_dir() else path.parent)

    def open_output(self) -> None:
        result = self.controller.result
        if result is None:
            return
        if not result.output_dir.is_dir():
            dialogs.warning(self, "Le dossier de sortie n'existe plus.")
            return
        self._open(result.output_dir)

    @staticmethod
    def _open(folder: Path) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))
