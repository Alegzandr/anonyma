"""« Traitement » page: inputs, output folder, queue, progress and actions."""

from __future__ import annotations

import time
from pathlib import Path
from typing import override

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QDragEnterEvent, QDragLeaveEvent, QDropEvent, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QProgressBar,
    QStackedWidget,
    QTableView,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from anonyma import registry
from anonyma.frenchtext import NBSP, item_progress, plural, typo
from anonyma.gui import dialogs
from anonyma.gui.context import OUTPUT_DIR_KEY
from anonyma.gui.controller import REVIEW_POLICIES, REVIEW_POLICY_GLOSSES, BatchController, Phase
from anonyma.gui.itemmodels import QueueModel, QueueRow
from anonyma.gui.labels import STATUS_LEVELS, status_label
from anonyma.gui.widgets import (
    BadgeDelegate,
    Banner,
    DropArea,
    button,
    left_header,
    muted,
    page_header,
    primary_button,
)
from anonyma.models import DocumentResult, DocumentStatus, Regime
from anonyma.pipeline import ProgressEvent, is_quarantined, quarantine_dir

STAGE_LABELS = {
    "analyse": "Analyse des fichiers",
    "langue": "Modèle de langue",
    "ecriture": "Écriture et vérification",
}

#: Summary of the supported families (the full extension list is in the tooltip).
FORMATS_SUMMARY = "Office, PDF, courriels (eml, msg, pst), images, archives, texte et données."

RUN_LABEL = "Anonymiser"
APPLY_LABEL = "Appliquer les décisions"


def _count_tree(result: DocumentResult) -> int:
    return sum(result.entity_counts.values()) + sum(_count_tree(c) for c in result.children)


def duration(seconds: float) -> str:
    """« 8 s », « 2 min 05 s », « 1 h 03 min » (unbreakable spaces)."""
    s = max(0, round(seconds))
    if s < 60:
        return f"{s}{NBSP}s"
    if s < 3600:
        return f"{s // 60}{NBSP}min{NBSP}{s % 60:02d}{NBSP}s"
    return f"{s // 3600}{NBSP}h{NBSP}{s % 3600 // 60:02d}{NBSP}min"


def pending_review_question(count: int) -> str:
    """Confirmation asked before applying with uncertain entities left unreviewed."""
    entities = plural(count, "entité incertaine n'a", "entités incertaines n'ont")
    reviewed = "revue" if abs(count) < 2 else "revues"
    return (
        f"{entities} pas été {reviewed}. Les fichiers concernés seront marqués « À revoir ». "
        "Appliquer quand même ?"
    )


class ProcessingPage(QWidget):
    review_requested = Signal()
    report_requested = Signal()

    def __init__(self, controller: BatchController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.controller = controller
        self._started = 0.0
        self._last_event: ProgressEvent | None = None
        self.setAcceptDrops(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)
        layout.addWidget(page_header("Traitement"))

        self.banner = Banner()
        layout.addWidget(self.banner)

        # Queue: the drop area is the empty state of the table.
        self.queue_stack = QStackedWidget()
        self.drop_area = DropArea()
        self.add_files_button = button("Ajouter des fichiers…")
        self.add_folder_button = button("Ajouter un dossier…")
        self.add_files_button.clicked.connect(self._pick_files)
        self.add_folder_button.clicked.connect(self._pick_folder)
        self.drop_area.add_button(self.add_files_button)
        self.drop_area.add_button(self.add_folder_button)
        self.drop_area.finish_buttons()
        self.drop_area.dropped.connect(self.add_paths)
        self.drop_area.hint.setText(self._formats_hint())
        self.drop_area.hint.setToolTip(self._formats_tooltip())
        self.queue_stack.addWidget(self.drop_area)

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
        left_header(self.table)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col, width in ((1, 90), (2, 150), (3, 110)):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.Interactive)
            self.table.setColumnWidth(col, width)
        self.table.setItemDelegateForColumn(2, BadgeDelegate(self.table))
        self.table.doubleClicked.connect(lambda _i: self.review_requested.emit())
        self.queue_stack.addWidget(self.table)
        layout.addWidget(self.queue_stack, 1)

        queue_bar = QHBoxLayout()
        self.summary = muted("")
        queue_bar.addWidget(self.summary, 1)
        self.add_button = QToolButton()
        self.add_button.setText("Ajouter")
        self.add_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        add_menu = QMenu(self.add_button)
        add_menu.addAction("Des fichiers…", self.add_files_button.click)
        add_menu.addAction("Un dossier…", self.add_folder_button.click)
        self.add_button.setMenu(add_menu)
        self.remove_button = button("Retirer la sélection")
        self.remove_button.clicked.connect(self._remove_selected)
        self.clear_button = button("Vider la liste")
        self.clear_button.clicked.connect(self._clear)
        queue_bar.addWidget(self.add_button)
        queue_bar.addWidget(self.remove_button)
        queue_bar.addWidget(self.clear_button)
        self.queue_bar = QWidget()
        self.queue_bar.setLayout(queue_bar)
        queue_bar.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.queue_bar)

        out_row = QHBoxLayout()
        output_label = QLabel("Dossier de &sortie")
        out_row.addWidget(output_label)
        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText("Un dossier distinct des originaux")
        self.output_edit.editingFinished.connect(self._output_edited)
        output_label.setBuddy(self.output_edit)
        out_row.addWidget(self.output_edit, 1)
        self.output_button = button("Choisir…")
        self.output_button.clicked.connect(self._pick_output)
        out_row.addWidget(self.output_button)
        layout.addLayout(out_row)
        self.output_error = QLabel("")
        self.output_error.setWordWrap(True)
        self.output_error.hide()
        layout.addWidget(self.output_error)

        policy_row = QHBoxLayout()
        policy_label = QLabel("&Relecture")
        policy_row.addWidget(policy_label)
        self.policy_combo = QComboBox()
        self.policy_combo.setObjectName("ReviewPolicy")
        for key, label in REVIEW_POLICIES.items():
            self.policy_combo.addItem(label, key)
            self.policy_combo.setItemData(
                self.policy_combo.count() - 1,
                typo(REVIEW_POLICY_GLOSSES[key]),
                Qt.ItemDataRole.ToolTipRole,
            )
        policy_label.setBuddy(self.policy_combo)
        self.policy_combo.currentIndexChanged.connect(self._policy_changed)
        policy_row.addWidget(self.policy_combo)
        self.policy_hint = muted("")
        self.policy_hint.setWordWrap(False)
        policy_row.addWidget(self.policy_hint, 1)
        layout.addLayout(policy_row)

        # Global progress: visible while running, or to say why a run stopped.
        self.progress_box = QWidget()
        progress_row = QHBoxLayout(self.progress_box)
        progress_row.setContentsMargins(0, 0, 0, 0)
        self.stage_label = muted("")
        self.stage_label.setWordWrap(False)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        # The stage label already reads « n / total »: no percentage drawn on the bar.
        self.progress_bar.setTextVisible(False)
        self.time_label = muted("")
        self.time_label.setWordWrap(False)
        progress_row.addWidget(self.stage_label)
        progress_row.addWidget(self.progress_bar, 1)
        progress_row.addWidget(self.time_label)
        layout.addWidget(self.progress_box)
        #: A FULL analysis announced as long, and the faster alternative.
        self.notice_label = muted("")
        self.notice_label.setWordWrap(True)
        self.notice_label.hide()
        layout.addWidget(self.notice_label)
        self._clock = QTimer(self)
        self._clock.setInterval(1000)
        self._clock.timeout.connect(self._update_times)

        actions = QHBoxLayout()
        self.analyze_button = button("Analyser")
        self.analyze_button.setToolTip("Détecter les entités sans rien écrire.")
        self.review_button = button("Revoir")
        self.review_button.setToolTip("Contrôler et corriger les entités détectées.")
        self.report_button = button("Voir le rapport")
        self.cancel_button = button("Annuler")
        self.cancel_button.setProperty("danger", True)
        self.run_button = primary_button(RUN_LABEL, "play")
        self.analyze_button.clicked.connect(self.start_analyze)
        self.review_button.clicked.connect(self.review_requested.emit)
        self.report_button.clicked.connect(self.report_requested.emit)
        self.cancel_button.clicked.connect(self._cancel)
        self.run_button.clicked.connect(self.run_primary)
        for b in (self.analyze_button, self.review_button, self.report_button):
            actions.addWidget(b)
        actions.addStretch(1)
        actions.addWidget(self.cancel_button)
        actions.addWidget(self.run_button)
        layout.addLayout(actions)
        shortcut = QShortcut(QKeySequence("Ctrl+Return"), self)
        shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        shortcut.activated.connect(self._primary_shortcut)

        c = self.controller
        c.inputs_changed.connect(self.refresh)
        c.queue_ready.connect(self.refresh)
        c.phase_changed.connect(self._phase_changed)
        c.progress.connect(self._on_progress)
        c.long_run.connect(self._on_long_run)
        c.analysis_ready.connect(self._on_analysis)
        c.analysis_cleared.connect(self.refresh)
        c.decisions_changed.connect(self._update_buttons)
        c.result_ready.connect(self._on_result)
        c.failed.connect(self._on_failed)
        c.cancelled.connect(self._on_cancelled)
        c.review_required.connect(self._on_review_required)
        c.effective_ready.connect(self.refresh)
        c.context.config_changed.connect(self._load_policy)
        self._load_policy()
        self._restore_output()
        self.refresh()

    # -- helpers ------------------------------------------------------------------

    @staticmethod
    def _extensions() -> list[str]:
        formats = registry.supported_formats()
        return sorted(
            ext
            for ext, fmt in registry.supported_extensions().items()
            if all(formats.get(fmt, (False, False)))
        )

    @classmethod
    def _formats_hint(cls) -> str:
        if not cls._extensions():
            return "Aucun format n'est disponible dans cette installation."
        return FORMATS_SUMMARY

    @classmethod
    def _formats_tooltip(cls) -> str:
        extensions = cls._extensions()
        if not extensions:
            return ""
        return typo(
            f"{plural(len(extensions), 'extension prise', 'extensions prises')} en "
            f"charge : " + ", ".join(extensions)
        )

    def _load_policy(self) -> None:
        policy = self.controller.review_policy()
        self.policy_combo.blockSignals(True)
        self.policy_combo.setCurrentIndex(max(self.policy_combo.findData(policy), 0))
        self.policy_combo.blockSignals(False)
        gloss = typo(REVIEW_POLICY_GLOSSES[policy])
        self.policy_hint.setText(gloss)
        self._update_buttons()

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

    def _keep_decisions(self, action: str, ok_label: str) -> bool:
        """True when the user prefers to keep the review decisions that ``action``
        would discard (nothing to ask when there are none)."""
        count = self.controller.decision_count()
        if not count:
            return False
        return not dialogs.confirm(
            self,
            f"{action} efface {plural(count, 'décision', 'décisions')} de revue.",
            title=ok_label,
            ok_label=ok_label,
            cancel_label="Garder mes décisions",
            warn=True,
        )

    def add_paths(self, items: list[Path]) -> None:
        if self.controller.busy:
            self.banner.show_message(
                "Un traitement est en cours : patientez ou annulez-le.", "warning"
            )
            return
        if self._keep_decisions("Modifier la liste des fichiers", "Modifier la liste"):
            return
        added = self.controller.add_inputs(items)
        if not added:
            self.banner.show_message("Ces éléments figurent déjà dans la liste.", "info")
        elif self.output_edit.text().strip():
            self._output_edited()

    def _pick_files(self) -> None:
        context = self.controller.context
        files = dialogs.open_files(self, "Ajouter des fichiers", start=context.last_dir("entrees"))
        context.remember_selection("entrees", files)
        if files:
            self.add_paths(files)

    def _pick_folder(self) -> None:
        context = self.controller.context
        folder = dialogs.open_folder(self, "Ajouter un dossier", context.last_dir("entrees"))
        context.remember_selection("entrees", folder)
        if folder is not None:
            self.add_paths([folder])

    def _pick_output(self) -> bool:
        start = self.controller.output_dir or self.controller.context.last_dir("sortie")
        folder = dialogs.open_folder(self, "Dossier de sortie", start)
        if folder is None:
            return False
        self.output_edit.setText(str(folder))
        return self._output_edited()

    def _restore_output(self) -> None:
        """Pre-fill the output folder chosen last time (when it still exists)."""
        saved = self.controller.context.ui_pref(OUTPUT_DIR_KEY)
        if not isinstance(saved, str) or not saved:
            return
        try:
            exists = Path(saved).is_dir()
        except OSError:
            exists = False
        if exists:
            self.output_edit.setText(saved)
            self._output_edited()

    def set_output_dir(self, path: Path) -> bool:
        self.output_edit.setText(str(path))
        return self._output_edited()

    def _output_edited(self) -> bool:
        text = self.output_edit.text().strip()
        error = self.controller.set_output_dir(Path(text) if text else None)
        if error:
            self.controller.output_dir = None
            self.output_error.setText(typo(error))
            self.output_error.setStyleSheet("color: palette(bright-text);")
            self.output_error.show()
        else:
            self.output_error.hide()
            self._remember_output()
        self._update_buttons()
        return not error

    def _remember_output(self) -> None:
        out = self.controller.output_dir
        context = self.controller.context
        context.set_ui_pref(OUTPUT_DIR_KEY, str(out) if out is not None else "")
        if out is not None:
            context.remember_dir("sortie", out)

    def _remove_selected(self) -> None:
        roots = {
            Path(str(self.model.data(i, Qt.ItemDataRole.UserRole + 1)))
            for i in self.table.selectionModel().selectedRows()
            if self.model.data(i, Qt.ItemDataRole.UserRole + 1)
        }
        if not roots:
            return
        if any(r.is_dir() for r in roots) and not dialogs.confirm(
            self,
            "Retirer les éléments sélectionnés ? Un fichier issu d'un dossier ajouté retire "
            "le dossier entier.",
            title="Retirer de la liste",
            ok_label="Retirer",
        ):
            return
        if self._keep_decisions("Modifier la liste des fichiers", "Modifier la liste"):
            return
        self.controller.remove_inputs(roots)

    def _clear(self) -> None:
        if self._keep_decisions("Vider la liste", "Vider la liste"):
            return
        self.controller.clear_inputs()

    def _cancel(self) -> None:
        self.controller.cancel()
        self.stage_label.setText("Annulation en cours…")
        self.cancel_button.setEnabled(False)

    # -- drops anywhere on the page --------------------------------------------------

    @override
    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if DropArea.paths(event) and not self.controller.busy:
            event.acceptProposedAction()
            self.drop_area.set_active(True)
        else:
            event.ignore()

    @override
    def dragLeaveEvent(self, event: QDragLeaveEvent) -> None:
        self.drop_area.set_active(False)
        super().dragLeaveEvent(event)

    @override
    def dropEvent(self, event: QDropEvent) -> None:
        self.drop_area.set_active(False)
        found = DropArea.paths(event)
        if found:
            event.acceptProposedAction()
            self.add_paths(found)

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
                title="Dossier de sortie",
            )
            return False
        out = self.controller.output_dir
        if not replacing and out.is_dir() and any(out.iterdir()):
            if dialogs.confirm(
                self,
                "Le dossier de sortie n'est pas vide. Les fichiers existants ne seront pas "
                "écrasés : les nouveaux fichiers recevront un suffixe numéroté.",
                title="Dossier de sortie",
                ok_label="Écrire dans ce dossier",
                cancel_label="Choisir un autre dossier",
                warn=True,
            ):
                return True
            return self._pick_output() and self._check_output()
        return True

    def _primary_shortcut(self) -> None:
        if self.run_button.isEnabled():
            self.run_primary()

    def applies_decisions(self) -> bool:
        """The primary button applies the review decisions instead of starting over."""
        c = self.controller
        return c.analysis is not None and c.decision_count() > 0

    def run_primary(self) -> None:
        if self.applies_decisions():
            self.start_apply()
        else:
            self.start_one_click()

    def start_analyze(self) -> None:
        if not self.controller.inputs:
            self.banner.show_message("Ajoutez d'abord des fichiers ou des dossiers.", "warning")
            return
        if self._keep_decisions("Relancer l'analyse", "Relancer l'analyse"):
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
            title=APPLY_LABEL,
            ok_label="Appliquer",
        ):
            return
        pending = c.pending_review_count()
        if pending and not dialogs.confirm(
            self,
            pending_review_question(pending),
            title=APPLY_LABEL,
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
                warn=True,
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
        if self._keep_decisions("Relancer l'analyse", "Relancer l'analyse"):
            return
        if not self._check_output():
            return
        ok, passphrase = self._ask_passphrase()
        if not ok:
            return
        self.banner.clear()
        c.run_all(passphrase=passphrase)

    # -- controller events -------------------------------------------------------------

    def _phase_changed(self, phase: str) -> None:
        if phase != Phase.IDLE.value:
            self._started = time.monotonic()
            self._last_event = None
            if phase == Phase.ANALYZING.value:
                self.notice_label.hide()  # a new estimate comes with each analysis
            self.stage_label.setText(STAGE_LABELS.get(phase, "") + "…")
            self.time_label.setText("")
            self.progress_bar.setRange(0, 0)
            self._clock.start()
        else:
            self._clock.stop()
            self.stage_label.setText("")
            self.time_label.setText("")
        self.refresh()

    def _on_long_run(self, message: str) -> None:
        self.notice_label.setText(typo(message[:1].upper() + message[1:]))
        self.notice_label.show()

    def _on_progress(self, event: ProgressEvent) -> None:
        if event.note:  # the language model's exact workload, once known
            self.notice_label.setText(typo(event.note))
            self.notice_label.show()
        self._last_event = event
        label = STAGE_LABELS.get(event.stage, event.stage)
        text = f"{label} : {event.done} / {event.total}"
        if event.item_total and event.done < event.total:
            text += " · " + item_progress(event.item_done, event.item_total, event.item_step)
        self.stage_label.setText(typo(text))
        self.progress_bar.setRange(0, max(event.total, 1))
        self.progress_bar.setValue(event.done)
        self._update_times()
        if event.index is not None:
            status = "Écrit" if event.stage == "ecriture" else "Analysé"
            self.model.update_row(event.index, status=status, level="info")

    def _update_times(self) -> None:
        if not self.controller.busy:
            return
        elapsed = time.monotonic() - self._started
        text = f"{duration(elapsed)} écoulées"
        event = self._last_event
        if event is not None and 0 < event.done < event.total:
            remaining = elapsed / event.done * (event.total - event.done)
            text += f" · environ {duration(remaining)} restantes"
        self.time_label.setText(text)

    def _on_analysis(self) -> None:
        self.refresh()

    def _on_result(self) -> None:
        self.refresh()
        result = self.controller.result
        if result is None:
            return
        statuses = [r.status for r in result.results]
        ok = statuses.count(DocumentStatus.ANONYMIZED)
        review = statuses.count(DocumentStatus.NEEDS_REVIEW)
        bad = len(statuses) - ok - review
        quarantined = sum(1 for r in result.results if is_quarantined(r, result.output_dir))
        parts = [plural(ok, "fichier anonymisé", "fichiers anonymisés")]
        if review:
            parts.append(f"{review} à revoir")
        if bad:
            parts.append(f"{bad} en échec ou non pris en charge")
        text = "Terminé : " + ", ".join(parts) + "."
        if quarantined:
            folder = quarantine_dir(result.output_dir)
            placed = "placé" if quarantined < 2 else "placés"
            text += (
                f" {plural(quarantined, 'fichier')} {placed} en quarantaine dans le dossier "
                f"voisin « {folder.name} »."
            )
        level = "success" if not bad and not review else "warning"
        self.banner.show_message(text, level, [("Voir le rapport", self.report_requested.emit)])

    def _on_failed(self, message: str) -> None:
        self.stage_label.setText("Le traitement a échoué.")
        dialogs.error(self, message, title="Traitement")
        self._update_progress_visibility()

    def _on_cancelled(self) -> None:
        self.stage_label.setText(
            typo("Traitement annulé : les fichiers non traités sont marqués « Annulé ».")
        )
        self._update_progress_visibility()

    def _on_review_required(self, count: int) -> None:
        entities = plural(count, "entité")
        if self.controller.review_policy() == "systematique":
            text = (
                f"Relecture systématique : {entities} à contrôler dans « Revue », puis "
                f"cliquez sur « {APPLY_LABEL} »."
            )
        else:
            uncertain = plural(count, "entité incertaine", "entités incertaines")
            text = (
                f"{uncertain} à vérifier avant l'écriture. Vérifiez-les dans « Revue », "
                f"puis cliquez sur « {APPLY_LABEL} »."
            )
        self.banner.show_message(text, "warning")
        self.review_requested.emit()

    # -- table ---------------------------------------------------------------------------

    def _root_for(self, source: Path) -> str:
        # Memoised per input list: refresh() runs after every review decision, and
        # resolving thousands of paths (on a network share) each time froze the GUI.
        inputs = tuple(self.controller.inputs)
        cache: dict[Path, str] | None = getattr(self, "_roots", None)
        if cache is None or getattr(self, "_roots_inputs", None) != inputs:
            cache = {}
            self._roots: dict[Path, str] = cache
            self._roots_inputs = inputs
        if source not in cache:
            resolved = source.resolve()
            cache[source] = next(
                (str(root) for root in inputs if resolved == root or root in resolved.parents),
                "",
            )
        return cache[source]

    def refresh(self) -> None:
        c = self.controller
        phase = c.phase
        running = {Phase.ANALYZING: "Analyse…", Phase.APPLYING: "Écriture…"}.get(phase)
        rows: list[QueueRow] = []
        if c.analysis is None:
            for i, q in enumerate(c.queue):
                if running and i in c.done_items:
                    status, level = "Analysé", "info"
                elif running:
                    status, level = running, "info"
                elif q.format is None:
                    status, level = "Non pris en charge", "neutral"
                else:
                    status, level = "En attente", "neutral"
                rows.append(QueueRow(q.name, q.format or "—", status, level, "—", str(q.root)))
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
                if result is not None:
                    count = _count_tree(result)
                if phase is Phase.APPLYING:
                    status = "Écrit" if i in c.done_items else "Écriture…"
                    level = "info"
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
                        self._root_for(doc.source),
                        doc.id,
                    )
                )
        self.model.set_rows(rows)
        empty = not c.inputs and not c.queue_loading
        self.queue_stack.setCurrentWidget(self.drop_area if empty else self.table)
        self.queue_bar.setVisible(not empty)
        self._update_summary()
        self._update_buttons()
        self._update_progress_visibility()

    def _update_progress_visibility(self) -> None:
        busy = self.controller.busy
        self.progress_bar.setVisible(busy)
        self.time_label.setVisible(busy)
        self.progress_box.setVisible(busy or bool(self.stage_label.text()))

    def _update_summary(self) -> None:
        c = self.controller
        if c.analysis is None:
            if c.queue_loading:
                self.summary.setText("Inventaire des fichiers en cours…")
                return
            n = len(c.queue)
            self.summary.setText(f"{plural(n, 'fichier')} dans la liste." if n else "")
            return
        pending = c.pending_review_count()
        decided, total = c.decided_counts()
        parts = [
            plural(len(c.analysis.documents), "fichier"),
            plural(total, "entité"),
            plural(len(c.analysis.entities), "occurrence"),
            f"{decided}{NBSP}/{NBSP}{total} décidées",
        ]
        if pending:
            parts.append(f"{pending}{NBSP}à revoir")
        self.summary.setText(" · ".join(parts))

    def _update_buttons(self) -> None:
        c = self.controller
        busy = c.busy
        has_inputs = bool(c.inputs)
        for w in (
            self.drop_area,
            self.add_files_button,
            self.add_folder_button,
            self.add_button,
            self.output_edit,
            self.output_button,
            self.remove_button,
            self.policy_combo,
        ):
            w.setEnabled(not busy)
        self.clear_button.setEnabled(not busy and has_inputs)
        self.analyze_button.setEnabled(not busy and has_inputs)
        self.review_button.setEnabled(c.analysis is not None)
        self.report_button.setEnabled(c.result is not None and not busy)
        self.cancel_button.setVisible(busy)
        self.cancel_button.setEnabled(busy)
        applying = self.applies_decisions()
        self.run_button.setText(APPLY_LABEL if applying else RUN_LABEL)
        self.run_button.setEnabled(not busy and (has_inputs or applying))
        if applying:
            self.run_button.setToolTip(
                "Écrire les fichiers avec les décisions de revue, sans relancer l'analyse "
                "(Ctrl+Entrée)."
            )
        else:
            gloss = REVIEW_POLICY_GLOSSES[c.review_policy()]
            self.run_button.setToolTip(typo(f"{gloss} (Ctrl+Entrée)"))
