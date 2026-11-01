"""« Modèles d'IA » section of the « Réglages » page: what the application installed for
this machine, and how far it got.

Nothing to choose here: ``model_setup`` decides which models this computer needs from
its hardware, downloads them at startup (when automatic connections are allowed),
replaces those an update of Anonyma made obsolete and removes the ones no longer
used. The section shows that state; its only actions are retrying, installing on
demand when automatic connections are off, and an offline import from a folder.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import override

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressBar,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from anonyma import hardware, model_setup
from anonyma.frenchtext import plural, typo
from anonyma.gui import dialogs
from anonyma.gui.context import AppContext
from anonyma.gui.labels import PROFILE_SHORT, format_bytes
from anonyma.gui.tasks import Report, Task, TaskRunner
from anonyma.gui.widgets import (
    BADGE_ROLE,
    BadgeDelegate,
    Banner,
    button,
    left_header,
    muted,
    primary_button,
    section_title,
)
from anonyma.model_catalog import ModelKind, ModelSpec
from anonyma.models import Profile

MODEL_KIND_LABELS = {
    ModelKind.GLINER: "Reconnaissance des noms",
    ModelKind.LLM: "Modèle de langue",
    ModelKind.OCR: "Lecture des images",
}
COLUMNS = ("Modèle", "Rôle", "État")

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
    return ", ".join(labels) or "processeur"


def machine_label(info: hardware.HardwareInfo) -> str:
    """One French sentence on what the choice of models is based on."""
    parts = [f"{format_bytes(info.ram_bytes)} de mémoire vive"] if info.ram_bytes else []
    parts.append(plural(info.cpu_count, "processeur"))
    if info.gpu_name:
        gpu = f"carte graphique {info.gpu_name}"
        if info.vram_bytes:
            gpu += f" ({format_bytes(info.vram_bytes)})"
        parts.append(gpu)
    parts.append(f"calcul sur {providers_label(info.providers)}")
    return "Choisie selon ce poste : " + ", ".join(parts) + "."


def spec_size(spec: ModelSpec) -> int:
    return sum(f.size for f in spec.files)


class ModelsSection(QWidget):
    #: Emitted when the installed models changed (the next batch detects more).
    models_changed = Signal()

    def __init__(self, context: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.context = context
        self.runner = TaskRunner(self)
        self.setup: model_setup.SetupPlan | None = None
        self.info: hardware.HardwareInfo | None = None
        self.profile: Profile | None = None
        self._task: Task | None = None
        self._cancel = False
        #: Model id -> French state shown while the setup runs.
        self._states: dict[str, tuple[str, str]] = {}
        self._loaded = False
        #: French messages of the last failed attempt (network down, folder in use...).
        self._last_errors: list[str] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(section_title("Modèles d'IA"))
        self.banner = Banner()
        layout.addWidget(self.banner)
        self.profile_label = QLabel("Détection : …")
        self.profile_label.setObjectName("DetectionProfile")
        layout.addWidget(self.profile_label)
        self.machine = muted("")
        self.machine.setObjectName("MachineInfo")
        layout.addWidget(self.machine)

        self.headline = QLabel("Vérification des modèles…")
        self.headline.setObjectName("ModelsHeadline")
        self.headline.setWordWrap(True)
        layout.addSpacing(6)
        layout.addWidget(self.headline)
        self.detail = muted("")
        layout.addWidget(self.detail)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setTextVisible(False)
        self.progress.hide()
        layout.addWidget(self.progress)
        actions = QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        self.install_button = primary_button("Installer maintenant", "download")
        self.install_button.clicked.connect(lambda: self.start(download=True))
        self.cancel_button = button("Interrompre")
        self.cancel_button.clicked.connect(self.cancel_download)
        self.import_button = button("Importer depuis un dossier…")
        self.import_button.setToolTip(
            "Sans réseau : choisissez le dossier models d'un autre poste où Anonyma est "
            "installé, ou une clé USB contenant les fichiers des modèles."
        )
        self.import_button.clicked.connect(self.import_folder)
        actions.addWidget(self.install_button)
        actions.addWidget(self.cancel_button)
        actions.addWidget(self.import_button)
        actions.addStretch(1)
        layout.addLayout(actions)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setObjectName("ModelTable")
        self.table.setHorizontalHeaderLabels(list(COLUMNS))
        self.table.verticalHeader().hide()
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        left_header(self.table)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col, width in ((1, 200), (2, 190)):
            self.table.setColumnWidth(col, width)
        self.table.setItemDelegateForColumn(2, BadgeDelegate(self.table))
        self.table.hide()
        layout.addWidget(self.table)
        layout.addWidget(
            muted(
                "Anonyma les télécharge au démarrage depuis Hugging Face (licence et "
                "intégrité vérifiées), remplace ceux qu'une mise à jour rend périmés et "
                "supprime ceux qui ne servent plus. Les documents, eux, ne quittent jamais "
                "le poste."
            )
        )
        # Turning automatic connections on or off changes what the section says.
        context.config_changed.connect(self._update_actions)
        self._update_actions()

    # -- lifecycle -------------------------------------------------------------------

    @override
    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        if not self._loaded and not self.busy():
            self.refresh()

    def busy(self) -> bool:
        return self.runner.running()

    def downloading(self) -> bool:
        return self._task is not None

    def _run(
        self,
        fn: Callable[[Report], object],
        ok: Callable[[object], None],
        ko: Callable[[str, str], None] | None = None,
        progress: Callable[[object], None] | None = None,
    ) -> Task:
        task = self.runner.create(fn)
        task.succeeded.connect(ok)
        task.failed.connect(ko or self._failed)
        if progress is not None:
            task.progress.connect(progress)
        task.start()
        return task

    def _failed(self, message: str, _type: str) -> None:
        self._task = None
        self.progress.hide()
        self.banner.show_message(message, "error")
        self.refresh()

    # -- state -----------------------------------------------------------------------

    def _compute(self) -> tuple[hardware.HardwareInfo, model_setup.SetupPlan, Profile]:
        """Worker thread: hardware, what ``models/`` still needs, the profile served."""
        info = hardware.detect_hardware()
        setup = model_setup.plan(self.context.model_manager, info)
        return info, setup, self.context.config_copy().effective_profile()

    def refresh(self) -> None:
        """Re-read the hardware and ``models/`` and show what is (still) needed."""
        self._loaded = True
        self._run(lambda _r: self._compute(), self._show)

    def _show(self, value: object) -> None:
        assert isinstance(value, tuple)
        self.info, self.setup, self.profile = value
        self._show_machine()
        self._show_models()
        self._update_actions()

    def _show_machine(self) -> None:
        if self.profile is not None:
            profile = PROFILE_SHORT.get(self.profile, str(self.profile))
            self.profile_label.setText(typo(f"Détection : {profile}"))
        if self.info is not None:
            self.machine.setText(typo(machine_label(self.info)))

    def _show_models(self) -> None:
        setup = self.setup
        if setup is None:
            return
        todo = {spec.id for spec in setup.install}
        rows: list[tuple[str, str, str, str]] = []
        for spec in setup.wanted:
            text, level = self._states.get(spec.id) or (
                ("À installer", "warning") if spec.id in todo else ("Installé", "success")
            )
            rows.append(
                (
                    spec.title or spec.id,
                    MODEL_KIND_LABELS.get(spec.kind, str(spec.kind)),
                    text,
                    level,
                )
            )
        for name in setup.obsolete:
            rows.append((name, "Ne sert plus", "Sera supprimé", "neutral"))
        self.table.setRowCount(len(rows))
        for row, cells in enumerate(rows):
            for col, cell in enumerate(cells[:3]):
                item = QTableWidgetItem(cell)
                if col == 2 and cells[3]:
                    item.setData(BADGE_ROLE, cells[3])
                self.table.setItem(row, col, item)
        self.table.resizeRowsToContents()
        height = self.table.horizontalHeader().height() + 2 * self.table.frameWidth()
        height += sum(self.table.rowHeight(r) for r in range(self.table.rowCount()))
        self.table.setFixedHeight(height)
        self.table.setVisible(bool(rows))

    def _update_actions(self) -> None:
        setup = self.setup
        working = self._task is not None
        self.cancel_button.setVisible(working)
        needs = setup is not None and bool(setup.install)
        self.install_button.setVisible(needs and not working)
        self.install_button.setText("Réessayer" if self._last_errors else "Installer maintenant")
        self.import_button.setVisible(needs and not working)
        if working:
            return
        self.progress.hide()
        if setup is None:
            self.headline.setText("Vérification des modèles…")
            self.detail.setText("")
        elif not setup.install:
            self.headline.setText(typo("Les modèles de ce poste sont installés et à jour."))
            self.detail.setText("")
        else:
            size = format_bytes(setup.download_bytes)
            self.headline.setText(typo(f"Modèles à installer : {size} à télécharger."))
            if self._last_errors:
                self.detail.setText(typo(" ".join(self._last_errors)))
            elif not self.context.config.check_updates:
                self.detail.setText(
                    typo(
                        "Les mises à jour au démarrage sont désactivées : installez-les "
                        "maintenant, ou importez-les depuis un dossier sans réseau. En "
                        "attendant, la détection fonctionne avec ce qui est installé."
                    )
                )
            else:
                self.detail.setText(
                    typo(
                        "L'installation reprendra au prochain démarrage. En attendant, la "
                        "détection fonctionne avec ce qui est installé."
                    )
                )

    # -- automatic setup ---------------------------------------------------------------

    def start_automatic(self) -> None:
        """Startup: install what this machine needs (network only when automatic
        connections are allowed) and remove what no longer serves."""
        self.start(download=self.context.config.check_updates)

    def start(self, *, download: bool) -> None:
        if self._task is not None:
            return
        self._loaded = True
        self._cancel = False
        self._last_errors = []
        self._states = {}
        manager = self.context.model_manager

        def work(report: Report) -> model_setup.SetupResult:
            info, setup, profile = self._compute()
            report(("plan", info, setup, profile))
            if setup.ready:
                return model_setup.SetupResult()
            result = model_setup.run(
                manager,
                setup,
                download=download,
                progress=lambda spec, done, total: report(("progress", spec, done, total)),
                cancel=lambda: self._cancel,
            )
            return result

        self._task = self._run(work, self._finished, progress=self._on_progress)
        self._update_actions()

    def _on_progress(self, payload: object) -> None:
        if not isinstance(payload, tuple):
            return
        if payload[0] == "plan":
            _kind, self.info, self.setup, self.profile = payload
            setup = self.setup
            assert isinstance(setup, model_setup.SetupPlan)
            self._show_machine()
            if setup.install and self._task is not None:
                self.headline.setText(typo("Installation des modèles de ce poste…"))
                self.detail.setText(
                    typo(
                        "La détection fonctionne déjà avec ce qui est installé ; elle "
                        "s'enrichit dès qu'un modèle est prêt."
                    )
                )
                self.progress.setRange(0, 0)
                self.progress.show()
            self._show_models()
            return
        _kind, spec, done, total = payload
        assert isinstance(spec, ModelSpec)
        setup = self.setup
        if setup is None:
            return
        for other in setup.install:
            if other.id == spec.id:
                break
            self._states[other.id] = ("Installé", "success")
        before = 0
        for other in setup.install:
            if other.id == spec.id:
                break
            before += spec_size(other)
        overall = setup.download_bytes
        if total and done >= total:
            self._states[spec.id] = ("Vérification…", "info")
        else:
            percent = int(done * 100 / total) if total else 0
            self._states[spec.id] = (f"Téléchargement : {percent} %", "info")
        if overall:
            self.progress.setRange(0, 1000)
            self.progress.setValue(min(1000, int((before + done) * 1000 / overall)))
        self.detail.setText(
            typo(
                f"{spec.title} : {format_bytes(done)} sur {format_bytes(total or spec_size(spec))}"
            )
        )
        self._show_models()

    def _finished(self, value: object) -> None:
        self._task = None
        result = value
        assert isinstance(result, model_setup.SetupResult)
        self._states = {}
        self._last_errors = list(result.errors)
        if result.installed or result.removed:
            self.models_changed.emit()
        if result.cancelled:
            self.banner.show_message("Installation interrompue : elle reprendra plus tard.", "info")
        elif result.installed and result.ok:
            self.banner.show_message("Modèles installés : la détection est complète.", "success")
        self.refresh()

    def cancel_download(self) -> None:
        if self._task is not None:
            self._cancel = True
            self.detail.setText("Interruption…")

    # -- offline import ------------------------------------------------------------------

    def import_folder(self) -> None:
        if self._task is not None or self.setup is None:
            return
        folder = dialogs.open_folder(
            self, "Dossier contenant les modèles", self.context.last_dir("modeles")
        )
        self.context.remember_selection("modeles", folder)
        if folder is None:
            return
        self._import([folder])

    def _import(self, sources: list[Path]) -> None:
        setup = self.setup
        if setup is None:
            return
        manager = self.context.model_manager
        self.progress.setRange(0, 0)
        self.progress.show()
        self.headline.setText("Import et vérification des fichiers…")

        def ok(value: object) -> None:
            self._task = None
            assert isinstance(value, model_setup.SetupResult)
            self._last_errors = []
            if value.installed or value.removed:
                self.models_changed.emit()
            if value.errors:
                self.banner.show_message(typo(" ".join(value.errors)), "warning")
            else:
                self.banner.show_message("Modèles importés et vérifiés.", "success")
            self.refresh()

        self._task = self._run(lambda _r: model_setup.import_from(manager, setup, sources), ok)
        self._update_actions()
