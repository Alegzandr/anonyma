"""« Traitement » page: inputs, output folder, queue, progress and actions."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QProgressBar,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from anonyma import registry
from anonyma.gui import dialogs
from anonyma.gui.controller import REVIEW_POLICIES, BatchController, Phase
from anonyma.gui.itemmodels import QueueModel, QueueRow
from anonyma.gui.labels import STATUS_LEVELS, status_label
from anonyma.gui.widgets import (
    BadgeDelegate,
    Banner,
    DropArea,
    ProgressDelegate,
    button,
    muted,
    page_header,
    primary_button,
)
from anonyma.models import DocumentResult, DocumentStatus, Regime
from anonyma.pipeline import QUARANTINE_DIR, ProgressEvent

STAGE_LABELS = {"analyse": "Analyse des fichiers", "ecriture": "Écriture et vérification"}

POLICY_HINTS = {
    "aucune": "« Anonymiser » écrit directement les fichiers, sans s'arrêter à la revue.",
    "incertaines": "« Anonymiser » s'arrête à la revue seulement si des entités sont incertaines.",
    "systematique": "« Anonymiser » s'arrête toujours à la revue avant d'écrire les fichiers.",
}


def _count_tree(result: DocumentResult) -> int:
    return sum(result.entity_counts.values()) + sum(_count_tree(c) for c in result.children)


class ProcessingPage(QWidget):
    review_requested = Signal()
    report_requested = Signal()

    def __init__(self, controller: BatchController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.controller = controller

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)
        layout.addWidget(
            page_header(
                "Traitement",
                "Ajoutez des fichiers ou des dossiers, choisissez un dossier de sortie, puis "
                "anonymisez. Les originaux ne sont jamais modifiés.",
            )
        )

        self.drop_area = DropArea()
        self.add_files_button = button("Ajouter des fichiers…", "file")
        self.add_folder_button = button("Ajouter un dossier…", "folder")
        self.add_files_button.clicked.connect(self._pick_files)
        self.add_folder_button.clicked.connect(self._pick_folder)
        self.drop_area.add_button(self.add_files_button)
        self.drop_area.add_button(self.add_folder_button)
        self.drop_area.finish_buttons()
        self.drop_area.dropped.connect(self.add_paths)
        self.drop_area.hint.setText(self._formats_hint())
        layout.addWidget(self.drop_area)

        self.banner = Banner()
        layout.addWidget(self.banner)

        self.model = QueueModel()
        self.table = QTableView()
        self.table.setObjectName("QueueTable")
        self.table.setModel(self.model)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(28)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col, width in ((1, 90), (2, 150), (3, 80), (4, 140)):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.Interactive)
            self.table.setColumnWidth(col, width)
        self.table.setItemDelegateForColumn(2, BadgeDelegate(self.table))
        self.table.setItemDelegateForColumn(4, ProgressDelegate(self.table))
        self.table.doubleClicked.connect(lambda _i: self.review_requested.emit())
        layout.addWidget(self.table, 1)

        queue_bar = QHBoxLayout()
        self.summary = muted("")
        queue_bar.addWidget(self.summary, 1)
        self.remove_button = button("Retirer la sélection", "trash")
        self.remove_button.clicked.connect(self._remove_selected)
        self.clear_button = button("Vider la liste")
        self.clear_button.clicked.connect(self.controller.clear_inputs)
        queue_bar.addWidget(self.remove_button)
        queue_bar.addWidget(self.clear_button)
        layout.addLayout(queue_bar)

        out_row = QHBoxLayout()
        out_row.addWidget(QLabel("Dossier de sortie"))
        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText("Choisissez un dossier distinct des originaux")
        self.output_edit.editingFinished.connect(self._output_edited)
        out_row.addWidget(self.output_edit, 1)
        self.output_button = button("Choisir…", "folder")
        self.output_button.clicked.connect(self._pick_output)
        out_row.addWidget(self.output_button)
        layout.addLayout(out_row)
        self.output_error = QLabel("")
        self.output_error.setWordWrap(True)
        self.output_error.hide()
        layout.addWidget(self.output_error)

        policy_row = QHBoxLayout()
        policy_row.addWidget(QLabel("Relecture"))
        self.policy_combo = QComboBox()
        self.policy_combo.setObjectName("ReviewPolicy")
        for key, label in REVIEW_POLICIES.items():
            self.policy_combo.addItem(label, key)
        self.policy_combo.currentIndexChanged.connect(self._policy_changed)
        policy_row.addWidget(self.policy_combo)
        self.policy_hint = muted("")
        self.policy_hint.setWordWrap(False)
        policy_row.addWidget(self.policy_hint, 1)
        layout.addLayout(policy_row)

        progress_row = QHBoxLayout()
        self.stage_label = muted("Prêt.")
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        progress_row.addWidget(self.stage_label, 1)
        progress_row.addWidget(self.progress_bar, 2)
        layout.addLayout(progress_row)

        actions = QHBoxLayout()
        self.analyze_button = button("Analyser", "process")
        self.analyze_button.setToolTip("Détecter les entités sans rien écrire.")
        self.review_button = button("Revoir", "review")
        self.review_button.setToolTip("Contrôler et corriger les entités détectées.")
        self.apply_button = button("Appliquer", "check")
        self.apply_button.setToolTip("Écrire les fichiers anonymisés avec les décisions de revue.")
        self.report_button = button("Voir le rapport", "report")
        self.cancel_button = button("Annuler", "cancel")
        self.cancel_button.setProperty("danger", True)
        self.run_button = primary_button("Anonymiser", "play")
        self.analyze_button.clicked.connect(self.start_analyze)
        self.review_button.clicked.connect(self.review_requested.emit)
        self.apply_button.clicked.connect(self.start_apply)
        self.report_button.clicked.connect(self.report_requested.emit)
        self.cancel_button.clicked.connect(self._cancel)
        self.run_button.clicked.connect(self.start_one_click)
        for b in (self.analyze_button, self.review_button, self.apply_button, self.report_button):
            actions.addWidget(b)
        actions.addStretch(1)
        actions.addWidget(self.cancel_button)
        actions.addWidget(self.run_button)
        layout.addLayout(actions)

        c = self.controller
        c.inputs_changed.connect(self.refresh)
        c.queue_ready.connect(self.refresh)
        c.phase_changed.connect(lambda _p: self.refresh())
        c.progress.connect(self._on_progress)
        c.analysis_ready.connect(self._on_analysis)
        c.analysis_cleared.connect(self.refresh)
        c.result_ready.connect(self._on_result)
        c.failed.connect(self._on_failed)
        c.cancelled.connect(self._on_cancelled)
        c.review_required.connect(self._on_review_required)
        c.effective_ready.connect(self.refresh)
        c.context.config_changed.connect(self._load_policy)
        self._load_policy()
        self.refresh()

    # -- helpers ------------------------------------------------------------------

    @staticmethod
    def _formats_hint() -> str:
        formats = registry.supported_formats()
        extensions = sorted(
            ext
            for ext, fmt in registry.supported_extensions().items()
            if all(formats.get(fmt, (False, False)))
        )
        if not extensions:
            return "Aucun format n'est disponible dans cette installation."
        if len(extensions) > 18:
            return (
                f"{len(extensions)} extensions prises en charge : "
                + ", ".join(extensions[:18])
                + "…"
            )
        return "Formats pris en charge : " + ", ".join(extensions)

    def _load_policy(self) -> None:
        policy = self.controller.review_policy()
        self.policy_combo.blockSignals(True)
        self.policy_combo.setCurrentIndex(max(self.policy_combo.findData(policy), 0))
        self.policy_combo.blockSignals(False)
        self.policy_hint.setText(POLICY_HINTS[policy])
        self.run_button.setToolTip(POLICY_HINTS[policy])

    def _policy_changed(self) -> None:
        policy = str(self.policy_combo.currentData())
        context = self.controller.context
        if context.config.review_policy == policy:
            return
        config = context.config_copy()
        config.review_policy = policy
        try:
            context.update_config(config)
        except (OSError, ValueError):
            self.banner.show_message("Impossible d'enregistrer ce réglage.", "error")
        self._load_policy()

    def set_review_policy(self, policy: str) -> None:
        self.policy_combo.setCurrentIndex(self.policy_combo.findData(policy))

    def add_paths(self, items: list[Path]) -> None:
        if self.controller.busy:
            self.banner.show_message(
                "Un traitement est en cours : patientez ou annulez-le.", "warning"
            )
            return
        added = self.controller.add_inputs(items)
        if not added:
            self.banner.show_message("Ces éléments figurent déjà dans la liste.", "info")
        elif self.output_edit.text().strip():
            self._output_edited()

    def _pick_files(self) -> None:
        files = dialogs.open_files(self, "Ajouter des fichiers")
        if files:
            self.add_paths(files)

    def _pick_folder(self) -> None:
        folder = dialogs.open_folder(self, "Ajouter un dossier")
        if folder is not None:
            self.add_paths([folder])

    def _pick_output(self) -> None:
        folder = dialogs.open_folder(self, "Dossier de sortie", self.controller.output_dir)
        if folder is not None:
            self.output_edit.setText(str(folder))
            self._output_edited()

    def set_output_dir(self, path: Path) -> bool:
        self.output_edit.setText(str(path))
        return self._output_edited()

    def _output_edited(self) -> bool:
        text = self.output_edit.text().strip()
        error = self.controller.set_output_dir(Path(text) if text else None)
        if error:
            self.controller.output_dir = None
            self.output_error.setText(error)
            self.output_error.setStyleSheet("color: palette(bright-text);")
            self.output_error.show()
        else:
            self.output_error.hide()
        self._update_buttons()
        return not error

    def _remove_selected(self) -> None:
        roots = {
            Path(str(self.model.data(i, Qt.ItemDataRole.UserRole + 1)))
            for i in self.table.selectionModel().selectedRows()
            if self.model.data(i, Qt.ItemDataRole.UserRole + 1)
        }
        if roots:
            if any(r.is_dir() for r in roots) and not dialogs.confirm(
                self,
                "Retirer les éléments sélectionnés ? Un fichier issu d'un dossier ajouté retire "
                "le dossier entier.",
                ok_label="Retirer",
            ):
                return
            self.controller.remove_inputs(roots)

    def _cancel(self) -> None:
        self.controller.cancel()
        self.stage_label.setText("Annulation en cours…")
        self.cancel_button.setEnabled(False)

    # -- flows ----------------------------------------------------------------------

    def _ask_passphrase(self) -> tuple[bool, str | None]:
        if self.controller.context.config.regime is not Regime.PSEUDONYMIZATION:
            return True, None
        path = self.controller.store_path()
        passphrase = dialogs.ask_passphrase(self, not path.exists(), path)
        return (passphrase is not None), passphrase

    def _check_output(self, *, replacing: bool = False) -> bool:
        if not self._output_edited() or self.controller.output_dir is None:
            dialogs.warning(
                self,
                self.output_error.text()
                if self.output_error.isVisible()
                else "Choisissez d'abord un dossier de sortie.",
            )
            return False
        out = self.controller.output_dir
        if not replacing and out.is_dir() and any(out.iterdir()):
            return dialogs.confirm(
                self,
                "Le dossier de sortie n'est pas vide. Les fichiers existants ne seront pas "
                "écrasés : les nouveaux fichiers recevront un suffixe numéroté.",
                ok_label="Continuer",
            )
        return True

    def start_analyze(self) -> None:
        if not self.controller.inputs:
            self.banner.show_message("Ajoutez d'abord des fichiers ou des dossiers.", "warning")
            return
        self.banner.clear()
        self.controller.analyze()

    def start_apply(self) -> None:
        c = self.controller
        if c.busy:
            return
        if c.analysis is None:
            self.banner.show_message("Lancez d'abord l'analyse.", "warning")
            return
        if c.analysis_is_stale() and not dialogs.confirm(
            self,
            "Les réglages de détection ont changé depuis l'analyse. Appliquer quand même avec "
            "l'analyse existante ?",
            ok_label="Appliquer",
        ):
            return
        pending = c.pending_review_count()
        if pending and not dialogs.confirm(
            self,
            f"{pending} entité(s) incertaine(s) n'ont pas été revues. Les fichiers concernés "
            "seront marqués « À revoir ». Appliquer quand même ?",
            ok_label="Appliquer",
        ):
            return
        replacing = False
        if c.result is not None and c.result.output_dir == c.output_dir:
            if not dialogs.confirm(
                self,
                "Les fichiers produits lors de l'application précédente vont être remplacés "
                "par la nouvelle version.",
                title="Réappliquer",
                ok_label="Remplacer",
            ):
                return
            replacing = True
        if not self._check_output(replacing=replacing):
            return
        ok, passphrase = self._ask_passphrase()
        if not ok:
            return
        self.banner.clear()
        c.apply(passphrase=passphrase, replace_previous=replacing)

    def start_one_click(self) -> None:
        c = self.controller
        if c.busy:
            return
        if not c.inputs:
            self.banner.show_message("Ajoutez d'abord des fichiers ou des dossiers.", "warning")
            return
        if not self._check_output():
            return
        ok, passphrase = self._ask_passphrase()
        if not ok:
            return
        self.banner.clear()
        c.run_all(passphrase=passphrase)

    # -- controller events -------------------------------------------------------------

    def _on_progress(self, event: ProgressEvent) -> None:
        label = STAGE_LABELS.get(event.stage, event.stage)
        text = f"{label} : {event.done} / {event.total}"
        if event.message:
            text += f" — {event.message}"
        self.stage_label.setText(text)
        self.progress_bar.setRange(0, max(event.total, 1))
        self.progress_bar.setValue(event.done)
        if event.index is not None:
            if event.stage == "analyse":
                self.model.update_row(event.index, status="Analysé", level="info", progress=50)
            else:
                self.model.update_row(event.index, status="Écrit", level="info", progress=100)

    def _on_analysis(self) -> None:
        self.refresh()
        analysis = self.controller.analysis
        if analysis is None:
            return
        pending = self.controller.pending_review_count()
        files = len(analysis.documents)
        text = (
            f"Analyse terminée : {files} fichier(s), {len(analysis.clusters)} entité(s) "
            f"distincte(s), {len(analysis.entities)} occurrence(s)."
        )
        if pending:
            text += f" {pending} incertaine(s) à revoir."
        self.stage_label.setText(text)

    def _on_result(self) -> None:
        self.refresh()
        result = self.controller.result
        if result is None:
            return
        statuses = [r.status for r in result.results]
        ok = statuses.count(DocumentStatus.ANONYMIZED)
        review = statuses.count(DocumentStatus.NEEDS_REVIEW)
        bad = len(statuses) - ok - review
        quarantined = sum(
            1 for r in result.results if r.output_path and QUARANTINE_DIR in r.output_path.parts
        )
        text = f"Terminé : {ok} fichier(s) anonymisé(s)"
        if review:
            text += f", {review} à revoir"
        if bad:
            text += f", {bad} en échec ou non pris en charge"
        text += "."
        if quarantined:
            text += f" {quarantined} fichier(s) placé(s) en quarantaine ({QUARANTINE_DIR})."
        level = "success" if not bad and not review else "warning"
        self.banner.show_message(text, level, [("Voir le rapport", self.report_requested.emit)])
        self.stage_label.setText("Traitement terminé.")

    def _on_failed(self, message: str) -> None:
        self.stage_label.setText("Échec du traitement.")
        self.banner.show_message(message, "error")
        dialogs.error(self, message)

    def _on_cancelled(self) -> None:
        self.stage_label.setText("Traitement annulé.")
        self.banner.show_message(
            "Traitement annulé : les fichiers non traités sont marqués « Annulé ».", "warning"
        )

    def _on_review_required(self, count: int) -> None:
        if self.controller.review_policy() == "systematique":
            text = (
                f"Relecture systématique : {count} entité(s) à contrôler dans « Revue », puis "
                "cliquez sur « Appliquer »."
            )
        else:
            text = (
                f"{count} entité(s) incertaine(s) nécessitent votre revue avant l'écriture. "
                "Vérifiez-les dans « Revue », puis cliquez sur « Appliquer »."
            )
        self.banner.show_message(text, "warning")
        self.review_requested.emit()

    # -- table ---------------------------------------------------------------------------

    def _root_for(self, source: Path) -> str:
        resolved = source.resolve()
        for root in self.controller.inputs:
            if resolved == root or root in resolved.parents:
                return str(root)
        return ""

    def refresh(self) -> None:
        c = self.controller
        phase = c.phase
        running = {Phase.ANALYZING: "Analyse…", Phase.APPLYING: "Écriture…"}.get(phase)
        rows: list[QueueRow] = []
        if c.analysis is None:
            for i, q in enumerate(c.queue):
                if running and i in c.done_items:
                    status, level, progress = "Analysé", "info", 50
                elif running:
                    status, level, progress = running, "info", 0
                elif q.format is None:
                    status, level, progress = "Non pris en charge", "neutral", 0
                else:
                    status, level, progress = "En attente", "neutral", 0
                rows.append(
                    QueueRow(q.name, q.format or "—", status, level, "—", progress, str(q.root))
                )
        else:
            effective = c.effective()
            for i, doc in enumerate(c.analysis.documents):
                ids = [d.id for d in doc.walk()]
                if effective is not None:
                    count = sum(len(effective.for_document(d)) for d in ids)
                else:
                    count = sum(len(c.index.by_document.get(d, [])) for d in ids)
                result = c.result_for(doc.id)
                status_enum = result.status if result is not None else doc.status
                progress = 100 if result is not None else 50
                if result is not None:
                    count = _count_tree(result)
                if status_enum is DocumentStatus.CANCELLED:
                    progress = 0
                if phase is Phase.APPLYING:
                    done = i in c.done_items
                    status = "Écrit" if done else "Écriture…"
                    level = "info"
                    progress = 100 if done else 50
                else:
                    status = status_label(status_enum)
                    level = STATUS_LEVELS.get(status_enum, "neutral")
                rows.append(
                    QueueRow(
                        doc.name,
                        doc.format,
                        status,
                        level,
                        str(count),
                        progress,
                        self._root_for(doc.source),
                        doc.id,
                    )
                )
        self.model.set_rows(rows)
        if not c.busy and self.progress_bar.maximum() <= 1 and c.analysis is None:
            self.progress_bar.setValue(0)
        self._update_summary()
        self._update_buttons()

    def _update_summary(self) -> None:
        c = self.controller
        if c.analysis is None:
            if c.queue_loading:
                self.summary.setText("Inventaire des fichiers en cours…")
                return
            n = len(c.queue)
            self.summary.setText(f"{n} fichier(s) dans la file." if n else "La file est vide.")
            return
        pending = c.pending_review_count()
        decided, total = c.decided_counts()
        text = (
            f"{len(c.analysis.documents)} fichier(s) · {total} entité(s) · "
            f"{len(c.analysis.entities)} occurrence(s) · {decided} / {total} décidées"
        )
        if pending:
            text += f" · {pending} à revoir"
        self.summary.setText(text)

    def _update_buttons(self) -> None:
        c = self.controller
        busy = c.busy
        has_inputs = bool(c.inputs)
        for w in (
            self.drop_area,
            self.add_files_button,
            self.add_folder_button,
            self.output_edit,
            self.output_button,
            self.remove_button,
            self.policy_combo,
        ):
            w.setEnabled(not busy)
        self.clear_button.setEnabled(not busy and has_inputs)
        self.analyze_button.setEnabled(not busy and has_inputs)
        self.run_button.setEnabled(not busy and has_inputs)
        self.review_button.setEnabled(c.analysis is not None)
        self.apply_button.setEnabled(not busy and c.analysis is not None)
        self.report_button.setEnabled(c.result is not None and not busy)
        self.cancel_button.setEnabled(busy)
