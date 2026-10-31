"""Batch controller: inputs, analysis, review decisions, application, results.

Owns the ``Pipeline`` objects and runs them in worker threads (``tasks.Task``);
pages only talk to this object and listen to its signals.
"""

from __future__ import annotations

import contextlib
import copy
import enum
from collections.abc import Callable, Iterable
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from anonyma import paths, registry
from anonyma.config import Config
from anonyma.gui.context import AppContext, detection_fingerprint
from anonyma.gui.preview import (
    EffectiveState,
    MentionKey,
    compute_effective,
    pending_review,
    same_value,
)
from anonyma.gui.tasks import Report, Task, TaskRunner
from anonyma.logs import get_logger
from anonyma.models import (
    Document,
    DocumentResult,
    ManualEntity,
    MappingStore,
    Regime,
    ReviewDecisions,
)
from anonyma.pipeline import (
    BatchAnalysis,
    BatchResult,
    CancelToken,
    Pipeline,
    ProgressEvent,
    collect_inputs,
    extract_file,
)

log = get_logger("gui.controller")

#: Same wording as ``Pipeline._check_output_dir``.
OUTPUT_CONFLICT = "Le dossier de sortie doit être distinct des fichiers et dossiers d'origine."
DEFAULT_STORE_NAME = "correspondances.db"


class Phase(enum.StrEnum):
    IDLE = "idle"
    ANALYZING = "analyse"
    APPLYING = "ecriture"


def output_conflict(inputs: Iterable[Path], output: Path) -> bool:
    """True when ``output`` is, contains, or lies inside one of the inputs."""
    output = output.resolve()
    for raw in inputs:
        root = Path(raw).resolve()
        folder = root if root.is_dir() else root.parent
        if root.is_dir() and (output == folder or folder in output.parents):
            return True
        if output == root or output in root.parents:
            return True
    return False


def default_store_path(config: Config) -> Path:
    return Path(config.store_path) if config.store_path else paths.data_dir() / DEFAULT_STORE_NAME


def open_store(path: Path, passphrase: str, create: bool) -> MappingStore:
    """Open the encrypted mapping store (raises ``StoreError``/``NotImplementedError``)."""
    from anonyma.store import EncryptedMappingStore

    path.parent.mkdir(parents=True, exist_ok=True)
    return EncryptedMappingStore.open(path, passphrase, create=create)


def close_store(store: object) -> None:
    close = getattr(store, "close", None)
    if callable(close):
        with contextlib.suppress(Exception):
            close()


class BatchController(QObject):
    inputs_changed = Signal()
    phase_changed = Signal(str)
    progress = Signal(object)  # ProgressEvent
    analysis_ready = Signal()
    analysis_cleared = Signal()
    decisions_changed = Signal()
    result_ready = Signal()
    #: (French message) — the batch task failed.
    failed = Signal(str)
    #: Batch task cancelled by the user.
    cancelled = Signal()
    #: Emitted after the one-click flow stopped because clusters need review.
    review_required = Signal(int)
    #: (document id, Document | None, French error) — produced file re-extracted.
    output_extracted = Signal(str, object, str)

    def __init__(self, context: AppContext, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.context = context
        self.inputs: list[Path] = []
        self.output_dir: Path | None = None
        self.analysis: BatchAnalysis | None = None
        self.decisions = ReviewDecisions()
        self.result: BatchResult | None = None
        self.phase = Phase.IDLE
        self._pipeline: Pipeline | None = None
        self._analysis_fingerprint = ""
        self._cancel: CancelToken | None = None
        self._runner = TaskRunner(self)
        self._task: Task | None = None
        self._effective: EffectiveState | None = None
        self._outputs: dict[str, Document | None] = {}
        self._applied_decisions: ReviewDecisions | None = None

    # -- inputs ----------------------------------------------------------------

    def add_inputs(self, items: Iterable[Path]) -> int:
        """Add files/folders (deduplicated). Returns how many roots were added."""
        if self.busy:
            return 0
        added = 0
        current = [p.resolve() for p in self.inputs]
        for raw in items:
            path = Path(raw)
            if not path.exists():
                continue
            path = path.resolve()
            if any(path == c or c in path.parents for c in current if c.is_dir() or c == path):
                continue
            # A folder replaces the files/folders it contains.
            current = [c for c in current if path not in c.parents]
            current.append(path)
            added += 1
        if added:
            self.inputs = current
            self._invalidate_analysis()
            self.inputs_changed.emit()
        return added

    def remove_inputs(self, items: Iterable[Path]) -> None:
        if self.busy:
            return
        drop = {Path(p).resolve() for p in items}
        kept = [p for p in self.inputs if p.resolve() not in drop]
        if len(kept) != len(self.inputs):
            self.inputs = kept
            self._invalidate_analysis()
            self.inputs_changed.emit()

    def clear_inputs(self) -> None:
        if self.busy or not self.inputs:
            return
        self.inputs = []
        self._invalidate_analysis()
        self.inputs_changed.emit()

    def queued_files(self) -> list[tuple[Path, str, Path]]:
        """``(file, logical name, input root)`` of every file that will be processed."""
        rows: list[tuple[Path, str, Path]] = []
        for root in self.inputs:
            for path, name in collect_inputs([root]):
                rows.append((path, name, root))
        return rows

    @staticmethod
    def format_for(path: Path) -> str | None:
        """Format id by extension (cheap, for display before analysis)."""
        return registry.supported_extensions().get(path.suffix.lower())

    # -- output folder -----------------------------------------------------------

    def check_output_dir(self, path: Path) -> str:
        """French error message, or "" when ``path`` is acceptable."""
        if output_conflict(self.inputs, path):
            return OUTPUT_CONFLICT
        if path.exists() and not path.is_dir():
            return "Le chemin de sortie désigne un fichier, pas un dossier."
        return ""

    def set_output_dir(self, path: Path | None) -> str:
        error = self.check_output_dir(path) if path is not None else ""
        if not error:
            self.output_dir = path.resolve() if path is not None else None
        return error

    # -- state -------------------------------------------------------------------

    @property
    def busy(self) -> bool:
        return self.phase is not Phase.IDLE

    def _set_phase(self, phase: Phase) -> None:
        self.phase = phase
        self.phase_changed.emit(phase.value)

    def analysis_is_stale(self) -> bool:
        """True when detection settings changed since the analysis."""
        return self.analysis is not None and self._analysis_fingerprint != detection_fingerprint(
            self.context.config
        )

    def store_path(self) -> Path:
        return default_store_path(self.context.config)

    def needs_passphrase(self) -> bool:
        return self.context.config.regime is Regime.PSEUDONYMIZATION

    def pending_review_count(self) -> int:
        if self.analysis is None:
            return 0
        return len(pending_review(self.analysis, self.decisions))

    def decisions_since_apply(self) -> bool:
        """True when decisions changed after the last application."""
        return self.result is not None and self._applied_decisions != self.decisions

    def result_for(self, document_id: str) -> DocumentResult | None:
        if self.result is None:
            return None
        for r in self.result.all_results():
            if r.document_id == document_id:
                return r
        return None

    def _invalidate_analysis(self) -> None:
        if self.analysis is not None and self._pipeline is not None:
            self._pipeline.close(self.analysis)
        had = self.analysis is not None or self.result is not None
        self.analysis = None
        self.result = None
        self.decisions = ReviewDecisions()
        self._effective = None
        self._outputs.clear()
        self._applied_decisions = None
        if had:
            self.analysis_cleared.emit()

    # -- running -----------------------------------------------------------------

    def _start(self, fn: Callable[[Report], object], phase: Phase) -> Task:
        task = self._runner.create(fn)
        task.progress.connect(self._on_progress)
        task.failed.connect(self._on_failed)
        task.cancelled.connect(self._on_cancelled)
        self._task = task
        self._set_phase(phase)
        return task

    def _on_progress(self, payload: object) -> None:
        if isinstance(payload, ProgressEvent):
            self.progress.emit(payload)

    def _on_failed(self, message: str, _type_name: str) -> None:
        self._task = None
        self._set_phase(Phase.IDLE)
        self.failed.emit(message)

    def _on_cancelled(self) -> None:
        self._task = None
        self._set_phase(Phase.IDLE)
        self.cancelled.emit()

    def cancel(self) -> None:
        if self._cancel is not None:
            self._cancel.cancel()

    def analyze(self, *, then_apply: bool = False, passphrase: str | None = None) -> bool:
        """Start the analysis (``then_apply``: apply right away if nothing needs review)."""
        if self.busy or not self.inputs:
            return False
        self._invalidate_analysis()
        config = self.context.config_copy()
        inputs = list(self.inputs)
        token = CancelToken()
        self._cancel = token

        def work(report: Report) -> tuple[Pipeline, BatchAnalysis]:
            pipeline = Pipeline(config)
            return pipeline, pipeline.analyze(inputs, progress=report, cancel=token)

        task = self._start(work, Phase.ANALYZING)
        fingerprint = detection_fingerprint(config)

        def done(value: object) -> None:
            assert isinstance(value, tuple)
            self._pipeline, self.analysis = value
            self._analysis_fingerprint = fingerprint
            self.decisions = ReviewDecisions()
            self._effective = None
            self._task = None
            self._set_phase(Phase.IDLE)
            self.analysis_ready.emit()
            if token.cancelled:
                self.cancelled.emit()
                return
            if then_apply:
                pending = self.pending_review_count()
                if pending:
                    self.review_required.emit(pending)
                else:
                    self.apply(passphrase=passphrase)

        task.succeeded.connect(done)
        task.start()
        return True

    def apply(self, *, passphrase: str | None = None, create_store: bool | None = None) -> bool:
        """Write the anonymized files with the current decisions."""
        if self.busy or self.analysis is None or self.output_dir is None:
            return False
        analysis = self.analysis
        output_dir = self.output_dir
        live = self.context.config
        # Detection settings stay those of the analysis; output settings are live.
        config = replace(
            analysis.config, mode=live.mode, regime=live.regime, store_path=live.store_path
        )
        decisions = copy.deepcopy(self.decisions)
        store_path = default_store_path(config)
        create = (not store_path.exists()) if create_store is None else create_store
        pseudonymize = config.regime is Regime.PSEUDONYMIZATION
        if pseudonymize and not passphrase:
            self.failed.emit("Une phrase secrète est nécessaire pour la pseudonymisation.")
            return False
        token = CancelToken()
        self._cancel = token

        def work(report: Report) -> BatchResult:
            store: MappingStore | None = None
            if pseudonymize:
                assert passphrase is not None
                store = open_store(store_path, passphrase, create)
            try:
                return Pipeline(config, store=store).apply(
                    analysis, output_dir, decisions, progress=report, cancel=token
                )
            finally:
                if store is not None:
                    close_store(store)

        task = self._start(work, Phase.APPLYING)

        def done(value: object) -> None:
            assert isinstance(value, BatchResult)
            self.result = value
            self._applied_decisions = decisions
            self._outputs.clear()
            self._task = None
            self._set_phase(Phase.IDLE)
            self.result_ready.emit()
            if token.cancelled:
                self.cancelled.emit()

        task.succeeded.connect(done)
        task.start()
        return True

    def run_all(self, passphrase: str | None = None) -> bool:
        """One-click flow: analyze, then apply unless clusters need review."""
        return self.analyze(then_apply=True, passphrase=passphrase)

    # -- review decisions ----------------------------------------------------------

    def effective(self) -> EffectiveState | None:
        if self.analysis is None:
            return None
        if self._effective is None:
            self._effective = compute_effective(
                self.analysis, self.decisions, self.context.config.mode
            )
        return self._effective

    def _decisions_changed(self) -> None:
        self._effective = None
        self.decisions_changed.emit()

    def refresh_preview(self) -> None:
        """Recompute the preview (e.g. after the replacement mode changed)."""
        self._decisions_changed()

    def confirm(self, cluster_ids: Iterable[str]) -> None:
        ids = {c for c in cluster_ids if c}
        if not ids:
            return
        self.decisions.rejected_clusters -= ids
        self.decisions.confirmed_clusters |= ids
        self._decisions_changed()

    def reject(self, cluster_ids: Iterable[str]) -> None:
        ids = {c for c in cluster_ids if c}
        if not ids:
            return
        self.decisions.confirmed_clusters -= ids
        self.decisions.rejected_clusters |= ids
        self._decisions_changed()

    def retype(self, cluster_ids: Iterable[str], entity_type: str) -> None:
        """Change the type of clusters (which also counts as validating them)."""
        ids = {c for c in cluster_ids if c}
        if not ids or not entity_type:
            return
        for cid in ids:
            original = self.analysis.clusters.get(cid) if self.analysis else None
            if original is not None and original.entity_type == entity_type:
                self.decisions.retyped_clusters.pop(cid, None)
            else:
                self.decisions.retyped_clusters[cid] = entity_type
        self.decisions.rejected_clusters -= ids
        self.decisions.confirmed_clusters |= ids
        self._decisions_changed()

    def reject_mentions(self, keys: Iterable[MentionKey]) -> None:
        new = set(keys) - self.decisions.rejected_mentions
        if new:
            self.decisions.rejected_mentions |= new
            self._decisions_changed()

    def reset(self, cluster_ids: Iterable[str] = (), keys: Iterable[MentionKey] = ()) -> None:
        """Forget every decision taken on these clusters / mentions."""
        ids = set(cluster_ids)
        self.decisions.confirmed_clusters -= ids
        self.decisions.rejected_clusters -= ids
        for cid in ids:
            self.decisions.retyped_clusters.pop(cid, None)
        self.decisions.rejected_mentions -= set(keys)
        self._decisions_changed()

    def add_manual(self, value: str, entity_type: str) -> bool:
        value = value.strip()
        if not value or not entity_type:
            return False
        for existing in self.decisions.manual:
            if same_value(existing.value, value) and existing.entity_type == entity_type:
                return False
        self.decisions.manual.append(ManualEntity(value, entity_type))
        # A rejected cluster of the same value and type would swallow the addition.
        if self.analysis is not None:
            for cid in list(self.decisions.rejected_clusters):
                cluster = self.analysis.clusters.get(cid)
                if (
                    cluster is not None
                    and cluster.entity_type == entity_type
                    and any(same_value(v, value) for v in cluster.variants)
                ):
                    self.decisions.rejected_clusters.discard(cid)
        self._decisions_changed()
        return True

    def remove_manual(self, values: Iterable[str]) -> None:
        drop = list(values)
        kept = [m for m in self.decisions.manual if not any(same_value(m.value, v) for v in drop)]
        if len(kept) != len(self.decisions.manual):
            self.decisions.manual = kept
            self._decisions_changed()

    def retype_manual(self, values: Iterable[str], entity_type: str) -> None:
        targets = list(values)
        changed = False
        for i, m in enumerate(self.decisions.manual):
            if any(same_value(m.value, v) for v in targets) and m.entity_type != entity_type:
                self.decisions.manual[i] = ManualEntity(m.value, entity_type)
                changed = True
        if changed:
            self._decisions_changed()

    # -- produced files ---------------------------------------------------------------

    def cached_output(self, top_document_id: str) -> tuple[bool, Document | None]:
        if top_document_id in self._outputs:
            return True, self._outputs[top_document_id]
        return False, None

    def extract_output(self, top_document_id: str) -> None:
        """Re-extract the produced file of a top-level document (in a thread)."""
        result = self.result_for(top_document_id)
        analysis = self.analysis
        if result is None or analysis is None:
            return
        path = result.output_path
        if path is None or not path.exists():
            self._outputs[top_document_id] = None
            self.output_extracted.emit(
                top_document_id, None, "Aucun fichier n'a été produit pour ce document."
            )
            return
        config = analysis.config
        work_dir = analysis.work_dir / "gui-sortie"
        name = result.output_name

        def work(_report: Report) -> Document:
            return extract_file(path, name, config, work_dir)

        task = self._runner.create(work)

        def ok(value: object) -> None:
            doc = value if isinstance(value, Document) else None
            self._outputs[top_document_id] = doc
            self.output_extracted.emit(top_document_id, doc, "")

        def ko(message: str, _type: str) -> None:
            self.output_extracted.emit(top_document_id, None, message)

        task.succeeded.connect(ok)
        task.failed.connect(ko)
        task.start()

    # -- shutdown -------------------------------------------------------------------------

    def shutdown(self, wait_ms: int = 30_000) -> None:
        """Cancel running work, wait for it, and delete the batch temporary folder."""
        self.cancel()
        self._runner.wait_all(wait_ms)
        if self.analysis is not None and self._pipeline is not None:
            self._pipeline.close(self.analysis)
