"""Batch controller: inputs, analysis, review decisions, application, results.

Owns the ``Pipeline`` objects and runs them in worker threads (``tasks.Task``);
pages only talk to this object and listen to its signals. Nothing slow runs on
the UI thread: folder inventory, analysis, preview computation, writing and
re-extraction of produced files are all background tasks.
"""

from __future__ import annotations

import contextlib
import copy
import enum
import shutil
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from anonyma import paths, registry
from anonyma.config import Config
from anonyma.gui.context import AppContext, detection_fingerprint
from anonyma.gui.preview import (
    EffectiveState,
    MentionKey,
    ReviewIndex,
    build_index,
    compute_effective,
    is_decided,
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
    OUTPUT_CONFLICT_MESSAGE,
    BatchAnalysis,
    BatchResult,
    CancelToken,
    Pipeline,
    ProgressEvent,
    collect_inputs,
    extract_file,
    validate_output_dir,
)
from anonyma.textutils import normalize

log = get_logger("gui.controller")

OUTPUT_CONFLICT = OUTPUT_CONFLICT_MESSAGE
DEFAULT_STORE_NAME = "correspondances.db"

#: Config.review_policy values -> French labels.
REVIEW_POLICIES: dict[str, str] = {
    "aucune": "Aucune — écrire directement",
    "incertaines": "Entités incertaines seulement",
    "systematique": "Systématique — toujours revoir avant d'écrire",
}
DEFAULT_REVIEW_POLICY = "incertaines"


def review_policy(config: Config) -> str:
    policy = getattr(config, "review_policy", DEFAULT_REVIEW_POLICY)
    return policy if policy in REVIEW_POLICIES else DEFAULT_REVIEW_POLICY


class Phase(enum.StrEnum):
    IDLE = "idle"
    ANALYZING = "analyse"
    APPLYING = "ecriture"


def output_conflict(inputs: Iterable[Path], output: Path) -> bool:
    """True when ``output`` is, contains, or lies inside one of the inputs."""
    try:
        validate_output_dir(list(inputs), output)
    except ValueError:
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


def remove_outputs(result: BatchResult) -> None:
    """Delete the files/folders a previous application produced (and only those)."""
    root = result.output_dir.resolve()
    for r in result.results:
        path = r.output_path
        if path is None:
            continue
        try:
            resolved = path.resolve()
            if root not in resolved.parents:
                continue
            if resolved.is_dir():
                shutil.rmtree(resolved, ignore_errors=True)
            else:
                resolved.unlink(missing_ok=True)
        except OSError:
            continue


@dataclass(frozen=True, slots=True)
class QueuedFile:
    path: Path
    name: str  # logical name (sensitive, shown locally)
    root: Path
    format: str | None


@dataclass(slots=True)
class ConfigUndo:
    """What a « learn from decisions » action changed, to revert it."""

    label: str
    whitelist: list[str]
    always_mask: list[dict[str, str]]


class BatchController(QObject):
    inputs_changed = Signal()
    #: Emitted when the background folder inventory finished (queue rows ready).
    queue_ready = Signal()
    phase_changed = Signal(str)
    progress = Signal(object)  # ProgressEvent
    analysis_ready = Signal()
    analysis_cleared = Signal()
    decisions_changed = Signal()
    #: The preview (EffectiveState) for the current decisions is available.
    effective_ready = Signal()
    result_ready = Signal()
    #: (French message) — the batch task failed.
    failed = Signal(str)
    #: Batch task cancelled by the user.
    cancelled = Signal()
    #: The one-click flow stopped at review: (number of entities to look at).
    review_required = Signal(int)
    #: (document id, Document | None, French error) — produced file re-extracted.
    output_extracted = Signal(str, object, str)

    def __init__(self, context: AppContext, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.context = context
        self.inputs: list[Path] = []
        self.queue: list[QueuedFile] = []
        self.queue_loading = False
        self.output_dir: Path | None = None
        self.analysis: BatchAnalysis | None = None
        self.index = ReviewIndex()
        self.decisions = ReviewDecisions()
        self.result: BatchResult | None = None
        self.phase = Phase.IDLE
        #: Per-document progress of the running stage (input order index -> done).
        self.done_items: set[int] = set()
        self._pipeline: Pipeline | None = None
        self._analysis_fingerprint = ""
        self._cancel: CancelToken | None = None
        self._runner = TaskRunner(self)
        self._task: Task | None = None
        self._effective: EffectiveState | None = None
        self._effective_generation = 0
        self._effective_for = -1
        self._effective_running = False
        self._queue_generation = 0
        self._outputs: dict[str, Document | None] = {}
        self._applied_decisions: ReviewDecisions | None = None

    # -- inputs ----------------------------------------------------------------

    def add_inputs(self, items: Iterable[Path]) -> int:
        """Add files/folders (deduplicated). Returns how many roots were added; the
        folder inventory runs in the background (``queue_ready``)."""
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
            self._inputs_updated()
        return added

    def remove_inputs(self, items: Iterable[Path]) -> None:
        if self.busy:
            return
        drop = {Path(p).resolve() for p in items}
        kept = [p for p in self.inputs if p.resolve() not in drop]
        if len(kept) != len(self.inputs):
            self.inputs = kept
            self._inputs_updated()

    def clear_inputs(self) -> None:
        if self.busy or not self.inputs:
            return
        self.inputs = []
        self._inputs_updated()

    def _inputs_updated(self) -> None:
        self._invalidate_analysis()
        self._refresh_queue()
        self.inputs_changed.emit()

    def _refresh_queue(self) -> None:
        self._queue_generation += 1
        generation = self._queue_generation
        roots = list(self.inputs)
        if not roots:
            self.queue = []
            self.queue_loading = False
            self.queue_ready.emit()
            return
        self.queue_loading = True

        def work(_report: Report) -> list[QueuedFile]:
            extensions = registry.supported_extensions()
            rows: list[QueuedFile] = []
            for root in roots:
                for path, name in collect_inputs([root]):
                    rows.append(QueuedFile(path, name, root, extensions.get(path.suffix.lower())))
            return rows

        task = self._runner.create(work)

        def done(value: object) -> None:
            if generation != self._queue_generation:
                return
            assert isinstance(value, list)
            self.queue = value
            self.queue_loading = False
            self.queue_ready.emit()

        def ko(message: str, _type: str) -> None:
            if generation == self._queue_generation:
                self.queue = []
                self.queue_loading = False
                self.queue_ready.emit()
                self.failed.emit(message)

        task.succeeded.connect(done)
        task.failed.connect(ko)
        task.start()

    def queued_files(self) -> list[tuple[Path, str, Path]]:
        """``(file, logical name, input root)`` of the inventoried files."""
        return [(q.path, q.name, q.root) for q in self.queue]

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
        self.done_items = set()
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

    def review_policy(self) -> str:
        return review_policy(self.context.config)

    def pending_review_count(self) -> int:
        if self.analysis is None:
            return 0
        return len(pending_review(self.analysis, self.decisions))

    def cluster_ids(self) -> list[str]:
        """Every entity of the review queue: detected clusters, then manual ones."""
        ids = list(self.index.order)
        effective = self._effective
        if effective is not None and self.analysis is not None:
            known = set(ids)
            ids += sorted(
                cid
                for cid in effective.active
                if cid not in known and cid not in self.analysis.clusters
            )
        return ids

    def decided_counts(self) -> tuple[int, int]:
        """(decided, total) entities of the review queue."""
        if self.analysis is None:
            return 0, 0
        ids = self.cluster_ids()
        decided = sum(1 for cid in ids if is_decided(cid, self.analysis, self.decisions))
        return decided, len(ids)

    def is_decided(self, cluster_id: str) -> bool:
        return self.analysis is not None and is_decided(cluster_id, self.analysis, self.decisions)

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
        self.index = ReviewIndex()
        self.result = None
        self.decisions = ReviewDecisions()
        self._effective = None
        self._effective_generation += 1
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
            if payload.index is not None:
                self.done_items.add(payload.index)
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
        """Start the analysis. ``then_apply``: one-click flow, which honours the
        review policy (« aucune », « incertaines », « systematique »)."""
        if self.busy or not self.inputs:
            return False
        self._invalidate_analysis()
        config = self.context.config_copy()
        inputs = list(self.inputs)
        token = CancelToken()
        self._cancel = token

        def work(report: Report) -> tuple[Pipeline, BatchAnalysis, ReviewIndex]:
            pipeline = Pipeline(config)
            analysis = pipeline.analyze(inputs, progress=report, cancel=token)
            return pipeline, analysis, build_index(analysis)

        task = self._start(work, Phase.ANALYZING)
        fingerprint = detection_fingerprint(config)

        def done(value: object) -> None:
            assert isinstance(value, tuple)
            self._pipeline, self.analysis, self.index = value
            self._analysis_fingerprint = fingerprint
            self.decisions = ReviewDecisions()
            self._effective = None
            self._task = None
            self._set_phase(Phase.IDLE)
            self.analysis_ready.emit()
            self.request_effective()
            if token.cancelled:
                self.cancelled.emit()
                return
            if then_apply:
                self._after_one_click_analysis(passphrase)

        task.succeeded.connect(done)
        task.start()
        return True

    def _after_one_click_analysis(self, passphrase: str | None) -> None:
        policy = self.review_policy()
        pending = self.pending_review_count()
        if policy == "systematique":
            self.review_required.emit(len(self.index.order))
        elif policy == "incertaines" and pending:
            self.review_required.emit(pending)
        else:
            self.apply(passphrase=passphrase)

    def apply(
        self,
        *,
        passphrase: str | None = None,
        create_store: bool | None = None,
        replace_previous: bool = False,
    ) -> bool:
        """Write the anonymized files with the current decisions.

        ``replace_previous``: first delete what the previous application of this
        analysis produced (« Corriger et réappliquer »)."""
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
        previous = self.result if replace_previous else None
        token = CancelToken()
        self._cancel = token

        def work(report: Report) -> BatchResult:
            store: MappingStore | None = None
            if pseudonymize:
                assert passphrase is not None
                store = open_store(store_path, passphrase, create)
            try:
                if previous is not None:
                    remove_outputs(previous)
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
        """One-click flow: analyze, then apply according to the review policy."""
        return self.analyze(then_apply=True, passphrase=passphrase)

    # -- preview (background) ---------------------------------------------------------

    def effective(self) -> EffectiveState | None:
        """Latest computed preview (may lag one decision behind while recomputing)."""
        return self._effective

    def effective_is_current(self) -> bool:
        return self._effective is not None and self._effective_for == self._effective_generation

    def effective_now(self) -> EffectiveState | None:
        """Synchronous, always current preview (small batches, tests)."""
        if self.analysis is None:
            return None
        if not self.effective_is_current():
            self._effective = compute_effective(
                self.analysis, self.decisions, self.context.config.mode
            )
            self._effective_for = self._effective_generation
        return self._effective

    def request_effective(self) -> None:
        """Recompute the preview in the background (coalesced)."""
        if self.analysis is None or self._effective_running:
            return
        generation = self._effective_generation
        analysis = self.analysis
        decisions = copy.deepcopy(self.decisions)
        mode = self.context.config.mode
        self._effective_running = True
        task = self._runner.create(lambda _r: compute_effective(analysis, decisions, mode))

        def done(value: object) -> None:
            self._effective_running = False
            if analysis is not self.analysis:
                return
            assert isinstance(value, EffectiveState)
            self._effective = value
            self._effective_for = generation
            if generation != self._effective_generation:
                self.request_effective()  # decisions changed meanwhile
            self.effective_ready.emit()

        def ko(message: str, _type: str) -> None:
            self._effective_running = False
            self.failed.emit(message)

        task.succeeded.connect(done)
        task.failed.connect(ko)
        task.start()

    # -- review decisions ----------------------------------------------------------

    def _decisions_changed(self) -> None:
        self._effective_generation += 1
        self.decisions_changed.emit()
        self.request_effective()

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

    # -- learning from decisions ---------------------------------------------------------

    def _save_learned(self, config: Config, label: str, before: Config) -> ConfigUndo:
        undo = ConfigUndo(label, list(before.whitelist), copy.deepcopy(before.always_mask))
        refresh = self._analysis_fingerprint == detection_fingerprint(before)
        self.context.update_config(config)
        # The current analysis already reflects this decision: no « stale » warning.
        if refresh:
            self._analysis_fingerprint = detection_fingerprint(config)
        return undo

    def add_to_whitelist(self, values: Iterable[str]) -> ConfigUndo | None:
        """Persist values in the whitelist (never masked in future batches)."""
        before = self.context.config
        config = self.context.config_copy()
        known = {normalize(v) for v in config.whitelist}
        added = False
        for value in values:
            key = normalize(value)
            if key and key not in known:
                config.whitelist.append(value.strip())
                known.add(key)
                added = True
        if not added:
            return None
        return self._save_learned(config, "liste blanche", before)

    def add_always_mask(self, value: str, entity_type: str) -> ConfigUndo | None:
        """Persist a term in ``always_mask`` (masked in every future batch)."""
        before = self.context.config
        config = self.context.config_copy()
        if any(
            normalize(t.get("value", "")) == normalize(value) and t.get("type") == entity_type
            for t in config.always_mask
        ):
            return None
        config.always_mask.append({"value": value.strip(), "type": entity_type})
        return self._save_learned(config, "termes toujours masqués", before)

    def undo_learning(self, undo: ConfigUndo) -> None:
        before = self.context.config
        config = self.context.config_copy()
        config.whitelist = list(undo.whitelist)
        config.always_mask = copy.deepcopy(undo.always_mask)
        self._save_learned(config, undo.label, before)

    # -- produced files ---------------------------------------------------------------

    def cached_output(self, top_document_id: str) -> tuple[bool, Document | None]:
        if top_document_id in self._outputs:
            return True, self._outputs[top_document_id]
        return False, None

    def extract_output(self, top_document_id: str) -> None:
        """Re-extract the produced file (or folder) of a top-level document."""
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

    def wait_idle(self, msecs: int = 30_000) -> bool:
        """Wait for background tasks (tests)."""
        return self._runner.wait_all(msecs)

    def shutdown(self, wait_ms: int = 30_000) -> None:
        """Cancel running work, wait for it, and delete the batch temporary folder."""
        self.cancel()
        self._runner.wait_all(wait_ms)
        if self.analysis is not None and self._pipeline is not None:
            self._pipeline.close(self.analysis)
