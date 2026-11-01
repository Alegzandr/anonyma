"""« Modèles » page: hardware, recommendations, catalogue, download and import."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import override

from PySide6.QtCore import Qt
from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressBar,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from anonyma import hardware
from anonyma.frenchtext import typo
from anonyma.gui import dialogs
from anonyma.gui.context import AppContext
from anonyma.gui.labels import PROFILE_SHORT, format_bytes
from anonyma.gui.tasks import Report, Task, TaskRunner
from anonyma.gui.widgets import (
    BADGE_ROLE,
    BadgeDelegate,
    Banner,
    EmptyState,
    button,
    left_header,
    muted,
    page_header,
    primary_button,
)
from anonyma.model_catalog import ModelKind, ModelSpec
from anonyma.model_manager import ModelStatus

KIND_LABELS = {ModelKind.GLINER: "GLiNER", ModelKind.LLM: "Modèle de langue", ModelKind.OCR: "OCR"}
COLUMNS = ("Modèle", "Type", "Taille", "Licence", "État")

#: ONNX Runtime execution providers -> French label.
PROVIDER_LABELS = {
    "DmlExecutionProvider": "DirectML (GPU)",
    "CUDAExecutionProvider": "CUDA (GPU)",
    "TensorrtExecutionProvider": "TensorRT (GPU)",
    "ROCMExecutionProvider": "ROCm (GPU)",
    "CoreMLExecutionProvider": "Core ML",
    "OpenVINOExecutionProvider": "OpenVINO",
    "CPUExecutionProvider": "processeur",
}


def providers_label(providers: tuple[str, ...] | list[str]) -> str:
    labels = [PROVIDER_LABELS.get(p, p.replace("ExecutionProvider", "")) for p in providers]
    text = ", ".join(labels) or "processeur"
    return text[0].upper() + text[1:]


def spec_size(spec: ModelSpec) -> int:
    return sum(f.size for f in spec.files)


def status_text(status: ModelStatus | None) -> tuple[str, str]:
    if status is None:
        return "Inconnu", "neutral"
    if status.installed and status.verified:
        return "Installé · vérifié", "success"
    if status.installed:
        return "Installé · non vérifié", "warning"
    # The common case: plain muted text, no badge.
    return "Non installé", ""


class ModelsPage(QWidget):
    def __init__(self, context: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.context = context
        self.runner = TaskRunner(self)
        self.specs: list[ModelSpec] = []
        self.statuses: dict[str, ModelStatus] = {}
        self._download: Task | None = None
        self._cancel_download = False
        self._busy_model = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)
        layout.addWidget(page_header("Modèles"))
        self.banner = Banner()
        layout.addWidget(self.banner)

        top = QHBoxLayout()
        hw_box = QGroupBox("Matériel")
        self.hw_form = QFormLayout(hw_box)
        self.hw_labels: dict[str, QLabel] = {}
        for key, label in (
            ("ram", "Mémoire vive"),
            ("cpu", "Processeurs"),
            ("gpu", "Carte graphique"),
            ("vram", "Mémoire graphique"),
            ("providers", "Accélération"),
            ("notes", "Remarques"),
        ):
            value = QLabel("…")
            value.setWordWrap(True)
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self.hw_labels[key] = value
            self.hw_form.addRow(label, value)
        top.addWidget(hw_box, 1)
        rec_box = QGroupBox("Recommandation")
        rec_layout = QVBoxLayout(rec_box)
        self.profile_label = QLabel("…")
        self.profile_label.setWordWrap(True)
        self.llm_label = QLabel("…")
        self.llm_label.setWordWrap(True)
        rec_layout.addWidget(self.profile_label)
        rec_layout.addWidget(self.llm_label)
        rec_layout.addStretch(1)
        self.refresh_hw_button = button("Détecter à nouveau")
        self.refresh_hw_button.clicked.connect(self.refresh_hardware)
        rec_layout.addWidget(self.refresh_hw_button, 0, Qt.AlignmentFlag.AlignRight)
        top.addWidget(rec_box, 1)
        layout.addLayout(top)

        catalogue = QGroupBox("Catalogue")
        cat_layout = QVBoxLayout(catalogue)
        cat_layout.addWidget(
            muted(
                "Le téléchargement n'a lieu qu'à votre demande ; avec la recherche de mises "
                "à jour, c'est la seule connexion d'Anonyma (par le proxy du système ou "
                "celui des Réglages). Sans réseau, importez les fichiers du modèle depuis "
                "un support amovible."
            )
        )
        self.cat_stack = QStackedWidget()
        self.empty = EmptyState(
            "Catalogue vide",
            "Aucun modèle n'est encore référencé dans cette version. Le profil « Léger » "
            "fonctionne sans aucun modèle.",
        )
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setObjectName("ModelTable")
        self.table.setHorizontalHeaderLabels(list(COLUMNS))
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        left_header(self.table)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col, width in ((1, 140), (2, 90), (3, 130), (4, 170)):
            self.table.setColumnWidth(col, width)
        self.table.setItemDelegateForColumn(4, BadgeDelegate(self.table))
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self.cat_stack.addWidget(self.empty)
        self.cat_stack.addWidget(self.table)
        cat_layout.addWidget(self.cat_stack, 1)
        self.description = muted("")
        cat_layout.addWidget(self.description)

        progress_row = QHBoxLayout()
        self.progress_label = muted("")
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.progress.hide()
        progress_row.addWidget(self.progress_label, 1)
        progress_row.addWidget(self.progress, 2)
        cat_layout.addLayout(progress_row)

        self.actions_box = QWidget()
        actions = QHBoxLayout(self.actions_box)
        actions.setContentsMargins(0, 0, 0, 0)
        self.download_button = primary_button("Télécharger…", "download")
        self.cancel_button = button("Annuler le téléchargement")
        self.import_files_button = button("Importer des fichiers…")
        self.import_folder_button = button("Importer un dossier…")
        self.remove_button = button("Supprimer")
        self.remove_button.setProperty("danger", True)
        self.download_button.clicked.connect(self.download_selected)
        self.cancel_button.clicked.connect(self.cancel_download)
        self.import_files_button.clicked.connect(self.import_files)
        self.import_folder_button.clicked.connect(self.import_folder)
        self.remove_button.clicked.connect(self.remove_selected)
        actions.addWidget(self.download_button)
        actions.addWidget(self.cancel_button)
        actions.addStretch(1)
        actions.addWidget(self.import_files_button)
        actions.addWidget(self.import_folder_button)
        actions.addWidget(self.remove_button)
        cat_layout.addWidget(self.actions_box)
        layout.addWidget(catalogue, 1)

        self._loaded = False
        self._recommended_llm = ""
        self._update_buttons()

    # -- lifecycle -------------------------------------------------------------------

    @override
    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        if not self._loaded:
            self._loaded = True
            self.refresh_hardware()
            self.refresh_catalog()

    def busy(self) -> bool:
        return self.runner.running()

    def _run(
        self,
        fn: Callable[[Report], object],
        ok: Callable[[object], None],
        ko: Callable[[str, str], None] | None = None,
        progress: Callable[[object], None] | None = None,
        cancelled: Callable[[], None] | None = None,
    ) -> Task:
        task = self.runner.create(fn)
        task.succeeded.connect(ok)
        task.failed.connect(ko or self._failed)
        if progress is not None:
            task.progress.connect(progress)
        if cancelled is not None:
            task.cancelled.connect(cancelled)
        task.start()
        return task

    def _failed(self, message: str, _type: str) -> None:
        self.banner.show_message(message, "error")
        self._busy_model = ""
        self.progress.hide()
        self.progress_label.setText("")
        self._update_buttons()

    # -- hardware ----------------------------------------------------------------------

    def refresh_hardware(self) -> None:
        self.refresh_hw_button.setEnabled(False)

        def work(_report: Report) -> tuple[hardware.HardwareInfo, object, str | None]:
            info = hardware.detect_hardware()
            return info, hardware.recommend_profile(info), hardware.recommend_llm(info)

        def ko(message: str, type_name: str) -> None:
            self.refresh_hw_button.setEnabled(True)
            for label in self.hw_labels.values():
                label.setText("—")
            self.profile_label.setText(typo("Détection du matériel impossible : " + message))

        self._run(work, self._show_hardware, ko)

    def _show_hardware(self, value: object) -> None:
        self.refresh_hw_button.setEnabled(True)
        assert isinstance(value, tuple)
        info, profile, llm = value
        assert isinstance(info, hardware.HardwareInfo)
        self.hw_labels["ram"].setText(
            format_bytes(info.ram_bytes) if info.ram_bytes else "Inconnue"
        )
        self.hw_labels["cpu"].setText(str(info.cpu_count))
        self.hw_labels["gpu"].setText(info.gpu_name or "Aucune détectée")
        self.hw_labels["vram"].setText(format_bytes(info.vram_bytes) if info.vram_bytes else "—")
        self.hw_labels["providers"].setText(providers_label(info.providers))
        self.hw_labels["notes"].setText(" ".join(info.notes) or "—")
        label = PROFILE_SHORT.get(profile, str(profile))
        self.profile_label.setText(typo(f"Profil conseillé : <b>{label}</b>"))
        self._recommended_llm = str(llm or "")
        if self.specs:
            self._show_catalog((self.specs, self.statuses))
        if llm:
            spec = next((s for s in self.specs if s.id == llm), None)
            name = spec.title if spec else str(llm)
            self.llm_label.setText(typo(f"Modèle de langue conseillé : <b>{name}</b>"))
        else:
            self.llm_label.setText(typo("Modèle de langue conseillé : aucun pour cette machine."))

    # -- catalogue ------------------------------------------------------------------------

    def refresh_catalog(self) -> None:
        manager = self.context.model_manager

        def work(_report: Report) -> tuple[list[ModelSpec], dict[str, ModelStatus]]:
            specs = manager.catalog()
            statuses: dict[str, ModelStatus] = {}
            for spec in specs:
                try:
                    statuses[spec.id] = manager.status(spec.id)
                except Exception:  # one broken entry must not hide the others
                    continue
            return specs, statuses

        self._run(work, self._show_catalog)

    def _show_catalog(self, value: object) -> None:
        assert isinstance(value, tuple)
        self.specs, self.statuses = value
        selected = self.selected_spec()
        self.table.setRowCount(len(self.specs))
        for row, spec in enumerate(self.specs):
            status = self.statuses.get(spec.id)
            text, level = status_text(status)
            size = status.size_on_disk if status and status.size_on_disk else spec_size(spec)
            name = spec.title or spec.id
            if spec.id == self._recommended_llm:
                name += " (conseillé)"
            cells = [
                name,
                KIND_LABELS.get(spec.kind, str(spec.kind)),
                format_bytes(size),
                spec.license or "—",
                text,
            ]
            for col, cell in enumerate(cells):
                item = QTableWidgetItem(cell)
                if col == 0:
                    item.setData(Qt.ItemDataRole.UserRole, spec.id)
                    item.setToolTip(spec.description or spec.repo_id)
                if col == 4 and level:
                    item.setData(BADGE_ROLE, level)
                self.table.setItem(row, col, item)
            if selected is not None and spec.id == selected.id:
                self.table.selectRow(row)
        self.cat_stack.setCurrentWidget(self.table if self.specs else self.empty)
        self.actions_box.setVisible(bool(self.specs))
        self._update_buttons()

    def selected_spec(self) -> ModelSpec | None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        item = self.table.item(rows[0].row(), 0)
        model_id = item.data(Qt.ItemDataRole.UserRole) if item else None
        return next((s for s in self.specs if s.id == model_id), None)

    def _update_buttons(self) -> None:
        spec = self.selected_spec()
        status = self.statuses.get(spec.id) if spec else None
        working = bool(self._busy_model)
        installed = bool(status and status.installed)
        self.download_button.setEnabled(spec is not None and not working and not installed)
        if spec is None:
            tip = "Sélectionnez d'abord un modèle dans le catalogue."
        elif installed:
            tip = "Ce modèle est déjà installé."
        elif working:
            tip = "Une opération est en cours."
        else:
            tip = "Télécharger ce modèle depuis Hugging Face, à votre demande."
        self.download_button.setToolTip(tip)
        self.import_files_button.setEnabled(spec is not None and not working)
        self.import_folder_button.setEnabled(spec is not None and not working)
        self.remove_button.setEnabled(bool(status and status.installed) and not working)
        self.cancel_button.setVisible(self._download is not None)
        self.description.setText((spec.description or spec.repo_id) if spec is not None else "")

    # -- actions ----------------------------------------------------------------------------

    def download_selected(self) -> None:
        spec = self.selected_spec()
        if spec is None or self._busy_model:
            return
        size = format_bytes(spec_size(spec))
        if not dialogs.confirm(
            self,
            f"Télécharger « {spec.title or spec.id} » ({size}) depuis Hugging Face ?\n\n"
            "Anonyma va se connecter au réseau uniquement pour ce téléchargement. La licence "
            f"déclarée ({spec.license or 'inconnue'}) est vérifiée avant le transfert, puis "
            "l'intégrité de chaque fichier est contrôlée.",
            title="Téléchargement d'un modèle",
            ok_label="Télécharger",
        ):
            return
        manager = self.context.model_manager
        self._cancel_download = False
        self._busy_model = spec.id

        def work(report: Report) -> Path:
            return manager.download(
                spec.id,
                progress=lambda done, total: report((done, total)),
                cancel=lambda: self._cancel_download,
            )

        def ok(_value: object) -> None:
            self._download = None
            self._busy_model = ""
            self.progress.hide()
            self.progress_label.setText("")
            self.banner.show_message(f"Modèle « {spec.title or spec.id} » installé.", "success")
            self.refresh_catalog()

        def ko(message: str, type_name: str) -> None:
            self._download = None
            if self._cancel_download or type_name == "CancelledError":
                self._busy_model = ""
                self.progress.hide()
                self.progress_label.setText("")
                self.banner.show_message("Téléchargement annulé.", "info")
                self._update_buttons()
            else:
                self._failed(message, type_name)

        self.progress.setRange(0, 0)
        self.progress.show()
        self.progress_label.setText("Connexion…")
        self._download = self._run(
            work, ok, ko, self._download_progress, lambda: ko("", "CancelledError")
        )
        self._update_buttons()

    def _download_progress(self, payload: object) -> None:
        if not isinstance(payload, tuple):
            return
        done, total = payload
        if total > 0:
            self.progress.setRange(0, 1000)
            self.progress.setValue(int(done / total * 1000))
            self.progress_label.setText(
                typo(f"Téléchargement : {format_bytes(done)} sur {format_bytes(total)}")
            )
        else:
            self.progress.setRange(0, 0)
            self.progress_label.setText(typo(f"Téléchargement : {format_bytes(done)}"))

    def cancel_download(self) -> None:
        if self._download is not None:
            self._cancel_download = True
            self.progress_label.setText("Annulation…")

    def _import(self, files: list[Path]) -> None:
        spec = self.selected_spec()
        if spec is None or not files:
            return
        manager = self.context.model_manager
        self._busy_model = spec.id
        self.progress.setRange(0, 0)
        self.progress.show()
        self.progress_label.setText("Import et vérification des fichiers…")

        def ok(_value: object) -> None:
            self._busy_model = ""
            self.progress.hide()
            self.progress_label.setText("")
            self.banner.show_message(f"Modèle « {spec.title or spec.id} » importé.", "success")
            self.refresh_catalog()

        self._run(lambda _r: manager.import_files(spec.id, files), ok)
        self._update_buttons()

    def import_files(self) -> None:
        if self.selected_spec() is None:
            return
        files = dialogs.open_files(
            self, "Importer les fichiers du modèle", start=self.context.last_dir("modeles")
        )
        self.context.remember_selection("modeles", files)
        self._import(files)

    def import_folder(self) -> None:
        if self.selected_spec() is None:
            return
        folder = dialogs.open_folder(
            self, "Importer le dossier du modèle", self.context.last_dir("modeles")
        )
        self.context.remember_selection("modeles", folder)
        if folder is not None:
            self._import([folder])

    def remove_selected(self) -> None:
        spec = self.selected_spec()
        if spec is None or self._busy_model:
            return
        if not dialogs.confirm(
            self,
            f"Supprimer le modèle « {spec.title or spec.id} » de ce poste ?",
            ok_label="Supprimer",
        ):
            return
        manager = self.context.model_manager

        def ok(_value: object) -> None:
            self.banner.show_message("Modèle supprimé.", "success")
            self.refresh_catalog()

        self._run(lambda _r: manager.remove(spec.id), ok)
