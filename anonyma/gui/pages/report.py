"""« Rapport » page: end-of-batch verdict, quarantine, residues and export."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from anonyma.frenchtext import NBSP, plural, typo
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
    fit_badge_column,
    left_header,
    muted,
    page_header,
    primary_button,
)
from anonyma.logs import get_logger
from anonyma.models import DocumentResult, DocumentStatus, Residue, Severity
from anonyma.pipeline import is_quarantined, quarantine_dir
from anonyma.report import STATUS_LABELS, BatchReport

log = get_logger("gui.report")

#: Breakdown under the verdict: status -> (singular, plural) after the number.
COUNT_WORDS: dict[DocumentStatus, tuple[str, str]] = {
    DocumentStatus.ANONYMIZED: ("anonymisé", "anonymisés"),
    DocumentStatus.NEEDS_REVIEW: ("à revoir", "à revoir"),
    DocumentStatus.RESIDUE: ("avec résidus", "avec résidus"),
    DocumentStatus.FAILED: ("en échec", "en échec"),
    DocumentStatus.UNSUPPORTED: ("non pris en charge", "non pris en charge"),
    DocumentStatus.CANCELLED: ("annulé", "annulés"),
}

_LABEL_TO_LEVEL = {STATUS_LABELS[s]: STATUS_LEVELS.get(s, "neutral") for s in STATUS_LABELS}

ORIGINS = {"original_value": "valeur d'origine", "detector": "détecteur"}

EXPORTS = (("json", "JSON…"), ("md", "Markdown…"), ("html", "HTML…"))


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
    left_header(table)
    table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
    return table


_RIGHT = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter


def _fill(
    table: QTableWidget,
    rows: list[list[str]],
    badge_col: int = -1,
    numeric: tuple[int, ...] = (),
) -> None:
    """Fill ``table``; ``numeric`` columns (cells and titles) are right-aligned."""
    for c in numeric:
        header_item = table.horizontalHeaderItem(c)
        if header_item is not None:
            header_item.setTextAlignment(_RIGHT)
    table.setRowCount(len(rows))
    for r, values in enumerate(rows):
        for c, value in enumerate(values):
            item = QTableWidgetItem(value)
            item.setToolTip(value)
            if c == badge_col:
                item.setData(BADGE_ROLE, _LABEL_TO_LEVEL.get(value, "neutral"))
            if c in numeric:
                item.setTextAlignment(_RIGHT)
            table.setItem(r, c, item)


def _nonzero(value: object) -> str:
    """Empty cell for a zero count: only the files with something to see stand out."""
    return "" if str(value) in ("0", "") else str(value)


def quarantine_reason(result: DocumentResult) -> str:
    """Plain-French explanation of why a file went to quarantine."""
    messages = [i.message for i in result.issues if i.severity is Severity.ERROR]
    if result.status is DocumentStatus.RESIDUE:
        values = plural(
            result.residue_count,
            "valeur sensible encore présente",
            "valeurs sensibles encore présentes",
        )
        head = f"{values} après anonymisation."
    else:
        head = "La vérification du fichier produit n'a pas pu aboutir."
    detail = " ".join(m for m in messages if "quarantaine" not in m)
    return typo(f"{head} {detail}".strip())


def verdict(counts: dict[DocumentStatus, int]) -> tuple[str, str]:
    """(sentence, level) answering « can I hand this folder over? »."""
    total = sum(counts.values())
    ready = counts.get(DocumentStatus.ANONYMIZED, 0)
    if total and ready == total:
        if total == 1:
            return "Le fichier est anonymisé et vérifié.", "success"
        return f"Les {total} fichiers sont anonymisés et vérifiés.", "success"
    blocked = sum(
        counts.get(s, 0)
        for s in (DocumentStatus.RESIDUE, DocumentStatus.FAILED, DocumentStatus.UNSUPPORTED)
    )
    level = "danger" if counts.get(DocumentStatus.RESIDUE, 0) else "warning"
    if not blocked and not counts.get(DocumentStatus.NEEDS_REVIEW, 0):
        level = "neutral"
    if ready == 0:
        return "Aucun fichier n'est prêt à être diffusé.", level
    if ready == 1:
        return f"1 fichier sur {total} est prêt à être diffusé.", level
    return f"{ready} fichiers sur {total} sont prêts à être diffusés.", level


def breakdown(counts: dict[DocumentStatus, int]) -> str:
    """« 8 fichiers : 6 anonymisés · 2 à revoir » (zeros left out)."""
    parts = [
        f"{n}{NBSP}{words[0] if n < 2 else words[1]}"
        for status, words in COUNT_WORDS.items()
        if (n := counts.get(status, 0))
    ]
    total = sum(counts.values())
    if len(parts) < 2:  # a single status: the verdict already says it
        return ""
    return typo(f"{plural(total, 'fichier')} : " + " · ".join(parts))


def batch_period(started: str, finished: str) -> str:
    """« Traité le 31/10/2026 de 14:08 à 14:09 » from ISO timestamps."""
    try:
        start = datetime.fromisoformat(started)
        end = datetime.fromisoformat(finished)
    except ValueError:
        return ""
    day = start.strftime("%d/%m/%Y")
    if end.date() != start.date():
        return f"Traité du {day} {start:%H:%M} au {end:%d/%m/%Y} {end:%H:%M}"
    return f"Traité le {day} de {start:%H:%M} à {end:%H:%M}"


class _Tab(QStackedWidget):
    """A report tab: its content, or an empty state when there is nothing to show."""

    def __init__(self, content: QWidget, empty_title: str, empty_text: str) -> None:
        super().__init__()
        self.empty = EmptyState(empty_title, empty_text)
        self.addWidget(self.empty)
        self.addWidget(content)

    def set_empty(self, empty: bool) -> None:
        self.setCurrentIndex(0 if empty else 1)


class ReportPage(QWidget):
    #: « Corriger et réappliquer »: back to the review with the same analysis.
    correct_requested = Signal()
    #: (top-level document id, residue location, residue value)
    residue_requested = Signal(str, str, str)
    #: (top-level document id) — a file of the report was double-clicked.
    document_requested = Signal(str)

    def __init__(self, controller: BatchController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.controller = controller
        self.report: BatchReport | None = None
        self.status_counts: dict[DocumentStatus, int] = {}
        self.residue_rows: list[ResidueRow] = []
        self.quarantined: list[DocumentResult] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)
        layout.addWidget(page_header("Rapport"))
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
        content_layout.setSpacing(4)

        verdict_row = QHBoxLayout()
        verdict_row.setSpacing(8)
        self.marker = QLabel("●")
        verdict_row.addWidget(self.marker, 0, Qt.AlignmentFlag.AlignVCenter)
        self.verdict = QLabel("")
        self.verdict.setObjectName("Verdict")
        self.verdict.setWordWrap(True)
        verdict_row.addWidget(self.verdict, 1)
        content_layout.addLayout(verdict_row)
        self.counts_label = muted("")
        content_layout.addWidget(self.counts_label)
        self.meta = muted("")
        self.meta.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        content_layout.addWidget(self.meta)
        content_layout.addSpacing(8)

        self.tabs = QTabWidget()
        self.files_table = _table(
            ["Fichier", "Format", "Statut", "Occurrences", "Résidus", "Résidus possibles"]
        )
        self.files_table.setItemDelegateForColumn(2, BadgeDelegate(self.files_table))
        fit_badge_column(self.files_table, 2, STATUS_LABELS.values())
        self.files_table.doubleClicked.connect(lambda _i: self.show_selected_document())
        self.types_table = _table(["Type", "Code", "Occurrences"])

        quarantine_box = QWidget()
        q_layout = QVBoxLayout(quarantine_box)
        q_layout.setContentsMargins(0, 8, 0, 0)
        self.quarantine_hint = muted("")
        q_layout.addWidget(self.quarantine_hint)
        self.quarantine_table = _table(["Fichier", "Statut", "Raison"])
        self.quarantine_table.setItemDelegateForColumn(1, BadgeDelegate(self.quarantine_table))
        fit_badge_column(self.quarantine_table, 1, STATUS_LABELS.values())
        self.quarantine_table.setWordWrap(True)
        q_layout.addWidget(self.quarantine_table, 1)
        q_actions = QHBoxLayout()
        self.correct_button = primary_button("Corriger et réappliquer")
        self.correct_button.setToolTip(
            "Ouvrir l'aperçu (masquer l'entité oubliée, ajouter une entité…), puis "
            "remplacer les fichiers produits."
        )
        self.correct_button.clicked.connect(self.correct_requested.emit)
        q_actions.addStretch(1)
        q_actions.addWidget(self.correct_button)
        q_layout.addLayout(q_actions)
        self.quarantine_tab = _Tab(
            quarantine_box,
            "Aucun fichier en quarantaine",
            "La vérification n'a retrouvé aucune valeur d'origine dans les fichiers produits.",
        )

        residues_box = QWidget()
        r_layout = QVBoxLayout(residues_box)
        r_layout.setContentsMargins(0, 8, 0, 0)
        self.residues_table = _table(["Fichier", "Emplacement", "Zone", "Type", "Origine"])
        self.residues_table.doubleClicked.connect(lambda _i: self.show_selected_residue())
        r_layout.addWidget(self.residues_table, 1)
        r_actions = QHBoxLayout()
        r_actions.addWidget(muted("Double-cliquez sur un résidu pour le voir en place."))
        r_actions.addStretch(1)
        self.residue_button = button("Voir dans le fichier produit")
        self.residue_button.clicked.connect(self.show_selected_residue)
        r_actions.addWidget(self.residue_button)
        r_layout.addLayout(r_actions)
        self.residues_tab = _Tab(
            residues_box, "Aucun résidu", "Aucune valeur sensible n'a été retrouvée."
        )

        self.failures_table = _table(["Fichier", "Statut", "Problèmes"])
        self.failures_table.setItemDelegateForColumn(1, BadgeDelegate(self.failures_table))
        fit_badge_column(self.failures_table, 1, STATUS_LABELS.values())
        self.failures_table.setWordWrap(True)
        self.failures_tab = _Tab(
            self.failures_table, "Aucun échec", "Tous les fichiers ont pu être traités."
        )
        self.tabs.addTab(self.files_table, "Fichiers")
        self.tabs.addTab(self.types_table, "Occurrences par type")
        self.tabs.addTab(self.quarantine_tab, "Quarantaine")
        self.tabs.addTab(self.residues_tab, "Résidus")
        self.tabs.addTab(self.failures_tab, "Échecs")
        content_layout.addWidget(self.tabs, 1)
        content_layout.addSpacing(8)

        actions = QHBoxLayout()
        self.export_button = QToolButton()
        self.export_button.setText("Exporter")
        self.export_button.setToolTip(
            "Les exports ne contiennent que des noms anonymisés, des nombres et des codes : "
            "aucune valeur sensible."
        )
        self.export_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        export_menu = QMenu(self.export_button)
        for kind, label in EXPORTS:
            export_menu.addAction(label, self._export_action(kind))
        self.export_button.setMenu(export_menu)
        self.open_file_button = button("Afficher le fichier produit")
        self.open_button = button("Ouvrir le dossier de sortie")
        self.open_file_button.clicked.connect(self.open_selected_output)
        self.open_button.clicked.connect(self.open_output)
        actions.addWidget(self.export_button)
        actions.addStretch(1)
        actions.addWidget(self.open_file_button)
        actions.addWidget(self.open_button)
        content_layout.addLayout(actions)

        controller.result_ready.connect(self.reload)
        controller.analysis_cleared.connect(self.reload)
        theme.notifier().changed.connect(self._paint_marker)
        self._level = "neutral"
        self.reload()

    def _paint_marker(self) -> None:
        colour = theme.badge_color(self._level).name()
        self.marker.setStyleSheet(f"color: {colour}; font-size: 12pt;")

    def reload(self) -> None:
        result = self.controller.result
        self.banner.clear()
        if result is None:
            self.report = None
            self.status_counts = {}
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
        self.status_counts = {s: report.status_counts.get(s, 0) for s in COUNT_WORDS}
        sentence, self._level = verdict(self.status_counts)
        self.verdict.setText(sentence)
        self._paint_marker()
        self.counts_label.setText(breakdown(self.status_counts))
        period = batch_period(report.started_at, report.finished_at)
        self.meta.setText(" · ".join(p for p in (period, f"lot {report.batch_id}") if p))
        self.open_button.setToolTip(str(result.output_dir))
        _fill(
            self.files_table,
            [
                [
                    str(f["fichier"]),
                    str(f["format"]),
                    str(f["statut"]),
                    str(f["entites"]),
                    _nonzero(f["residus"]),
                    _nonzero(f["residus_possibles"]),
                ]
                for f in report.files
            ],
            badge_col=2,
            numeric=(3, 4, 5),
        )
        _fill(
            self.types_table,
            [
                [entity_type_label(t), t, str(n)]
                for t, n in sorted(report.entity_counts.items(), key=lambda kv: -kv[1])
            ],
            numeric=(2,),
        )
        self.quarantined = [r for r in result.results if is_quarantined(r, result.output_dir)]
        folder = quarantine_dir(result.output_dir).name
        self.quarantine_hint.setText(
            typo(
                f"Ces fichiers sont dans le dossier « {folder} », à côté du dossier de sortie. "
                "La vérification finale y a retrouvé une valeur sensible, ou n'a pas pu les "
                "relire : ils ne sont pas anonymisés et ne doivent pas être diffusés."
            )
        )
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
        self.quarantine_tab.set_empty(not self.quarantined)
        self.residues_tab.set_empty(not self.residue_rows)
        self.failures_tab.set_empty(not report.failures)
        self.tabs.setTabText(2, f"Quarantaine ({len(self.quarantined)})")
        self.tabs.setTabText(3, f"Résidus ({len(self.residue_rows)})")
        self.tabs.setTabText(4, f"Échecs ({len(report.failures)})")
        self.correct_button.setEnabled(
            bool(self.quarantined) and self.controller.analysis is not None
        )
        self.residue_button.setEnabled(bool(self.residue_rows))
        if self.quarantined:
            self.banner.show_message(
                f"{plural(len(self.quarantined), 'fichier')} en quarantaine dans le dossier "
                f"« {folder} » : à ne pas diffuser.",
                "warning",
                [
                    ("Voir pourquoi", lambda: self.tabs.setCurrentIndex(2)),
                    ("Corriger et réappliquer", self.correct_requested.emit),
                ],
            )

    def _export_action(self, kind: str) -> Callable[[], None]:
        def run() -> None:
            self.export(kind)

        return run

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
        savers = {
            "json": self.report.save_json,
            "md": self.report.save_markdown,
            "html": self.report.save_html,
        }
        if kind not in savers:
            return None
        try:
            savers[kind](target)
        except OSError as exc:
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

    def show_selected_document(self) -> None:
        """Open the preview of the selected file (its produced file once written)."""
        result = self.controller.result
        row = self.files_table.currentRow()
        if result is not None and 0 <= row < len(result.results):
            self.document_requested.emit(result.results[row].document_id)

    def open_selected_output(self) -> None:
        """Show the produced file in the file manager; a folder output is opened."""
        path = self.selected_output()
        if path is None or not path.exists():
            dialogs.warning(self, "Aucun fichier produit pour cette ligne.", title="Rapport")
            return
        self._open(path if path.is_dir() else path.parent)

    def open_output(self) -> None:
        result = self.controller.result
        if result is None:
            return
        if not result.output_dir.is_dir():
            dialogs.warning(self, "Le dossier de sortie n'existe plus.", title="Rapport")
            return
        self._open(result.output_dir)

    @staticmethod
    def _open(folder: Path) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))
