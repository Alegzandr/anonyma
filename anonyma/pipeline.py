"""Batch orchestration: extract -> detect -> resolve/propagate -> replace -> write -> verify.

Two-phase API so that the GUI can insert a review step:

    pipeline = Pipeline(config)
    analysis = pipeline.analyze(paths)            # extraction + detection + resolution
    result = pipeline.apply(analysis, out_dir, decisions)   # replace + write + verify
    pipeline.close(analysis)                      # delete the batch temp folder

``Pipeline.run`` chains the three. Per-file work runs in a ``ProcessPoolExecutor``
when more than one worker is configured; batch-wide steps (resolution,
propagation, substitute assignment) run in the calling process.
"""

from __future__ import annotations

import io
import os
import re
import shutil
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import CancelledError, Future, ProcessPoolExecutor, as_completed, wait
from concurrent.futures.process import BrokenProcessPool
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from anonyma import outputs as output_record
from anonyma import paths, registry
from anonyma.atomic import AtomicIndex
from anonyma.config import Config
from anonyma.errors import failure_reason
from anonyma.frenchtext import NBSP, grouped, plural
from anonyma.logs import get_logger
from anonyma.models import (
    FILENAME_SEGMENT_ID,
    FOLDER_FORMAT,
    WRITER_PLACEHOLDERS,
    ArchiveBombError,
    ChildOutput,
    Detector,
    Document,
    DocumentResult,
    DocumentStatus,
    Entity,
    EntityCluster,
    EntityType,
    ExpansionBudget,
    ExtractionContext,
    Issue,
    MappingStore,
    OcrEngine,
    Profile,
    Regime,
    ReplacementMode,
    ReviewDecisions,
    ReviewStatus,
    Segment,
    SegmentContext,
    SegmentKind,
    Severity,
    UserError,
    VerificationResult,
    WriteContext,
    Writer,
    apply_replacements,
    new_id,
)
from anonyma.policy import apply_policies
from anonyma.replacer import BLACKOUT_CHAR, Replacer, SubstitutionTable, build_plan
from anonyma.resolver import (
    DOCUMENT_LOCAL_TYPES,
    PROPAGATION_DETECTOR,
    TYPE_PRIORITY,
    cluster_forms,
    link_by_place,
    propagate,
    resolve,
)
from anonyma.structure import PROPERTY as STRUCTURE
from anonyma.textsearch import MultiMatcher, leftmost_longest
from anonyma.textutils import find_all, normalize, safe_filename, safe_suffix
from anonyma.verifier import Verifier, is_ocr_issue, spaced_name
from anonyma.writers.zip import write_zip

log = get_logger("pipeline")

#: Suffix of the quarantine folder, created next to the output folder (``sortie`` →
#: ``sortie_quarantaine``) and never inside it: the output folder is the deliverable
#: users zip or upload as a whole, so it must only hold verified files.
QUARANTINE_SUFFIX = "_quarantaine"
MANUAL_DETECTOR = "manuel"
CRASH_MESSAGE = (
    "Le traitement de ce fichier a fait planter le processus de travail deux fois : "
    "fichier non traité."
)
#: Issue codes of containers that could not keep every embedded item.
OMITTED_CODE = "child.omitted"
INCOMPLETE_CONTAINER_CODE = "container.incomplete"
EMPTY_CONTAINER_CODE = "container.empty"
#: Issue of a verified output in which residues were found (quarantined).
RESIDUE_ISSUE = "verify.residue"
#: Issue of a verified output holding uncertain entities, masked and to confirm.
REVIEW_ISSUE = "review.uncertain"
#: Zones (segment labels) named at most in a review issue, and their length.
REVIEW_ZONES, REVIEW_ZONE_LENGTH = 3, 60
EMPTY_CONTAINER = (
    "Aucun élément de ce fichier n'a pu être anonymisé (illisible, chiffré ou non pris en "
    "charge) : fichier non produit."
)
#: Format of the links skipped in input folders (never read, reported).
LINK_FORMAT = "lien"
#: RAM budget per worker process in the STANDARD profile (GLiNER loaded per process).
GLINER_WORKER_RAM = int(2.5 * 1024**3)


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    stage: str  # "analyse" | "ecriture"
    done: int
    total: int
    #: Index of the item just finished, in input order (``BatchAnalysis.documents``
    #: order); None for start/global events.
    index: int | None = None
    #: Progress inside the file being analysed (single-process runs only): segments
    #: done out of ``item_total`` and the detector at work (French label). Counts
    #: and labels only, never a value or a name.
    item_done: int = 0
    item_total: int = 0
    item_step: str = ""


ProgressCallback = Callable[[ProgressEvent], None]


class CancelToken:
    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def check(self) -> None:
        if self._event.is_set():
            raise CancelledError()


@dataclass(slots=True)
class BatchAnalysis:
    batch_id: str
    config: Config
    documents: list[Document]  # top-level documents, children nested
    entities: list[Entity]
    clusters: dict[str, EntityCluster]
    work_dir: Path
    inputs: list[Path] = field(default_factory=list)
    started_at: datetime = field(default_factory=datetime.now)

    def all_documents(self) -> Iterator[Document]:
        for doc in self.documents:
            yield from doc.walk()

    def document(self, document_id: str) -> Document:
        for doc in self.all_documents():
            if doc.id == document_id:
                return doc
        raise KeyError(document_id)

    def review_clusters(self) -> list[EntityCluster]:
        return [c for c in self.clusters.values() if c.needs_review]


@dataclass(slots=True)
class ResolvedDecisions:
    """Entities and clusters after applying the review decisions."""

    entities: list[Entity]
    #: Active (non-rejected) clusters.
    clusters: dict[str, EntityCluster]
    #: Values the verifier must tolerate (rejected clusters and mentions).
    allowed: list[str]


@dataclass(slots=True)
class BatchResult:
    batch_id: str
    output_dir: Path
    results: list[DocumentResult]
    started_at: datetime
    finished_at: datetime = field(default_factory=datetime.now)
    #: Outputs are pseudonymized (reports say « pseudonymisé ») or anonymized.
    regime: Regime = Regime.ANONYMIZATION

    def all_results(self) -> Iterator[DocumentResult]:
        def walk(r: DocumentResult) -> Iterator[DocumentResult]:
            yield r
            for c in r.children:
                yield from walk(c)

        for r in self.results:
            yield from walk(r)


# ---------------------------------------------------------------------------
# Input enumeration
# ---------------------------------------------------------------------------


def _is_link(path: Path) -> bool:
    """Symbolic link or Windows junction (``os.walk`` follows junctions)."""
    try:
        return path.is_symlink() or os.path.isjunction(path)
    except OSError:
        return True


def walk_inputs(inputs: Sequence[Path]) -> tuple[list[tuple[Path, str]], list[tuple[Path, str]]]:
    """Expand files and folders into ``(path, logical name)``; folders keep their
    own name as first component.

    Inside a folder, symbolic links and junctions (to files or folders) are never
    followed, nor is anything that resolves outside the folder: they are returned
    apart, as ``(path, logical name)`` too, so that the batch can report them. Files
    and folders given explicitly are taken as they are."""
    items: list[tuple[Path, str]] = []
    links: list[tuple[Path, str]] = []
    for raw in inputs:
        path = Path(raw)
        if path.is_dir():
            root = path.resolve()
            top = path.resolve().name or path.name
            for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
                folder = Path(dirpath)
                prefix = PurePosixPath(top, folder.relative_to(path).as_posix())
                kept = []
                for dirname in sorted(dirnames):
                    if _is_link(folder / dirname):
                        links.append((folder / dirname, (prefix / dirname).as_posix()))
                    else:
                        kept.append(dirname)
                dirnames[:] = kept
                for filename in sorted(filenames):
                    file = folder / filename
                    name = (prefix / filename).as_posix()
                    if _is_link(file) or not file.resolve().is_relative_to(root):
                        links.append((file, name))
                    elif file.is_file():
                        items.append((file, name))
        elif path.is_file():
            items.append((path, path.name))
    return items, links


def collect_inputs(inputs: Sequence[Path]) -> list[tuple[Path, str]]:
    """Expand files and folders into ``(path, logical name)`` (see
    :func:`walk_inputs`; links inside folders are left out)."""
    return walk_inputs(inputs)[0]


# ---------------------------------------------------------------------------
# Per-process caches (worker processes)
# ---------------------------------------------------------------------------

_detector_cache: dict[str, list[Detector]] = {}
_ocr_cache: dict[str, OcrEngine | None] = {}


class _StderrToLog(io.TextIOBase):
    """A worker's stderr: what multiprocessing prints there (the traceback of a worker
    dying with its pool) never reaches the terminal, where an agent or a user would
    read it as a failure of the batch. Only the exception type goes to the log: the
    message may quote a value."""

    _TYPE = re.compile(r"^([A-Za-z_][\w.]*(?:Error|Exception|Interrupt))(?::|$)")

    def writable(self) -> bool:
        return True

    def write(self, text: str) -> int:
        for line in text.splitlines():
            match = self._TYPE.match(line)
            if match:
                log.warning("worker process error: %s", match.group(1))
        return len(text)


def _worker_init() -> None:
    from anonyma import logs, netguard

    paths.configure_environment()
    netguard.install()
    # Like the main process: records go to data/logs, never to the user's terminal.
    logs.setup_logging(worker=True)
    sys.stderr = _StderrToLog()


#: Seconds given to idle worker processes to exit once a pool is shut down, before
#: they are terminated.
POOL_EXIT_TIMEOUT = 10.0


@contextmanager
def worker_pool(workers: int) -> Iterator[ProcessPoolExecutor]:
    """A process pool whose shutdown is bounded.

    ``ProcessPoolExecutor.__exit__`` waits without limit for its management thread,
    which waits for every worker to take its stop signal from the call queue: one
    worker that never takes it (blocked on the queue's lock, or stuck in native
    code) hangs the batch after its last result. Here, pending jobs are cancelled,
    workers get :data:`POOL_EXIT_TIMEOUT` seconds to exit, and the ones left are
    terminated (every result has been collected by then)."""
    pool = ProcessPoolExecutor(workers, initializer=_worker_init)
    try:
        yield pool
    finally:
        shutdown_pool(pool)


def shutdown_pool(pool: ProcessPoolExecutor, timeout: float = POOL_EXIT_TIMEOUT) -> None:
    """Shut ``pool`` down without waiting more than ``timeout`` for its workers."""
    # Taken before shutdown(), which drops its references.
    processes = list((getattr(pool, "_processes", None) or {}).values())
    manager = getattr(pool, "_executor_manager_thread", None)
    call_queue = getattr(pool, "_call_queue", None)
    pool.shutdown(wait=False, cancel_futures=True)
    deadline = time.monotonic() + timeout
    for process in processes:
        process.join(max(0.0, deadline - time.monotonic()))
    stuck = [p for p in processes if p.is_alive()]
    if stuck:
        log.warning("%d worker process(es) did not exit; terminating them", len(stuck))
        # The management thread must not then wait for the queue's feeder thread,
        # which may be blocked writing to workers that are gone.
        cancel_join = getattr(call_queue, "cancel_join_thread", None)
        if callable(cancel_join):
            cancel_join()
        for process in stuck:
            process.terminate()
        for process in stuck:
            process.join(POOL_EXIT_TIMEOUT)
    if isinstance(manager, threading.Thread):
        manager.join(timeout)
        if manager.is_alive():
            log.warning("worker pool management thread still running after shutdown")


def detectors_for(config: Config) -> list[Detector]:
    key = config.fingerprint()
    if key not in _detector_cache:
        _detector_cache[key] = registry.build_detectors(config, config.effective_profile())
    return _detector_cache[key]


def ocr_for(config: Config) -> OcrEngine | None:
    if not config.ocr_enabled:
        return None
    key = config.fingerprint()
    if key not in _ocr_cache:
        from anonyma import ocr

        try:
            _ocr_cache[key] = ocr.create_engine(config)
        except Exception as exc:
            log.warning("OCR engine unavailable: %s", type(exc).__name__)
            _ocr_cache[key] = None
    return _ocr_cache[key]


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


class _ExtractionContext(ExtractionContext):
    def __init__(
        self,
        config: Config,
        depth: int,
        budget: ExpansionBudget,
        work_dir: Path,
        cancel: CancelToken | None,
    ) -> None:
        self.config = config
        self.depth = depth
        self.budget = budget
        self.work_dir = work_dir
        self.cancel = cancel
        self._ocr_loaded = False
        self._ocr: OcrEngine | None = None

    @property
    def ocr(self) -> OcrEngine | None:
        if not self._ocr_loaded:
            self._ocr = ocr_for(self.config)
            self._ocr_loaded = True
        return self._ocr

    def extract_child(self, data: bytes | Path, name: str, parent: Document) -> Document:
        if self.depth + 1 > self.budget.max_depth:
            raise ArchiveBombError("profondeur d'imbrication excessive")
        if isinstance(data, bytes):
            source = self.temp_path(_suffix_of(name))
            source.write_bytes(data)
        else:
            source = data
        child = extract_file(
            source,
            name,
            self.config,
            self.work_dir,
            budget=self.budget,
            depth=self.depth + 1,
            parent=parent,
            cancel=self.cancel,
        )
        parent.children.append(child)
        return child

    def temp_path(self, suffix: str = "") -> Path:
        self.work_dir.mkdir(parents=True, exist_ok=True)
        return self.work_dir / f"{new_id()}{suffix}"

    def check_cancelled(self) -> None:
        if self.cancel is not None:
            self.cancel.check()


def _suffix_of(name: str) -> str:
    return safe_suffix(name)


def extract_file(
    source: Path,
    name: str,
    config: Config,
    work_dir: Path,
    *,
    budget: ExpansionBudget | None = None,
    depth: int = 0,
    parent: Document | None = None,
    cancel: CancelToken | None = None,
) -> Document:
    """Extract one file (recursively for containers). Never raises except for
    cancellation and, below the top level, archive bombs."""
    budget = budget or config.limits.budget()
    if source.is_dir():
        return _extract_folder(source, name, config, work_dir, budget, depth, parent, cancel)
    fmt = registry.detect_format(source) if source.is_file() else None
    doc = Document(
        source=source,
        name=name,
        format=fmt or "inconnu",
        depth=depth,
        parent_id=parent.id if parent else None,
    )
    try:
        _extract_into(doc, fmt, config, work_dir, budget, cancel)
    except ArchiveBombError as exc:
        if depth > 0:
            raise
        doc.status = DocumentStatus.FAILED
        doc.add_issue(Severity.ERROR, "archive.bomb", f"Archive refusée : {exc}.")
    doc.segments.append(
        Segment(
            id=FILENAME_SEGMENT_ID,
            text=name,
            locator=(),
            context=SegmentContext(SegmentKind.FILENAME, label="nom de fichier"),
        )
    )
    return doc


def _extract_folder(
    source: Path,
    name: str,
    config: Config,
    work_dir: Path,
    budget: ExpansionBudget,
    depth: int,
    parent: Document | None,
    cancel: CancelToken | None,
) -> Document:
    """A folder produced by a ``directory_output`` writer, read back for verification:
    every file becomes a child named by its path relative to the folder."""
    doc = Document(
        source=source,
        name=name,
        format=FOLDER_FORMAT,
        depth=depth,
        parent_id=parent.id if parent else None,
        status=DocumentStatus.EXTRACTED,
        extractor="dossier",
    )
    for path, _ in collect_inputs([source]):
        relative = path.relative_to(source).as_posix()
        doc.children.append(
            extract_file(
                path,
                relative,
                config,
                work_dir,
                budget=budget,
                depth=depth + 1,
                parent=doc,
                cancel=cancel,
            )
        )
    doc.segments.append(
        Segment(
            id=FILENAME_SEGMENT_ID,
            text=name,
            locator=(),
            context=SegmentContext(SegmentKind.FILENAME, label="nom de dossier"),
        )
    )
    return doc


def _zip_folder(folder: Path, destination: Path) -> None:
    """Pack ``folder`` (files and empty dirs) into ``destination`` without metadata."""
    items = sorted(folder.rglob("*"))
    write_zip(
        [(p.relative_to(folder).as_posix(), p) for p in items if p.is_file()],
        [p.relative_to(folder).as_posix() for p in items if p.is_dir() and not any(p.iterdir())],
        destination,
    )


def _remove_output(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)


def _extract_into(
    doc: Document,
    fmt: str | None,
    config: Config,
    work_dir: Path,
    budget: ExpansionBudget,
    cancel: CancelToken | None,
) -> None:
    if fmt is None:
        doc.status = DocumentStatus.UNSUPPORTED
        doc.add_issue(Severity.ERROR, "format.unknown", "Format de fichier non reconnu.")
        return
    extractor = registry.extractor_for(fmt)
    writer = registry.writer_for(fmt)
    if extractor is None or writer is None:
        doc.status = DocumentStatus.UNSUPPORTED
        missing = "lecture" if extractor is None else "écriture"
        doc.add_issue(
            Severity.ERROR,
            "format.unsupported",
            f"Format « {fmt} » non pris en charge ({missing} indisponible).",
        )
        return
    doc.extractor = extractor.name
    ctx = _ExtractionContext(config, doc.depth, budget, work_dir, cancel)
    try:
        extractor.extract(doc.source, doc, ctx)
    except (CancelledError, ArchiveBombError):
        raise
    except Exception as exc:
        log.warning("extraction failed for %s: %s", doc.id, type(exc).__name__)
        doc.status = DocumentStatus.FAILED
        doc.add_issue(
            Severity.ERROR,
            "extract.error",
            f"Échec de la lecture du fichier : {failure_reason(exc, doc.source)}.",
        )
        return
    doc.status = DocumentStatus.FAILED if doc.has_errors else DocumentStatus.EXTRACTED


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


#: ``(done, total, step)`` progress inside the file being analysed, set by
#: :meth:`Pipeline._map` around each item of a single-process run.
ItemProgress = Callable[[int, int, str], None]
_item_progress: ItemProgress | None = None


#: :func:`detect_tree` phases: every detector, those without ``uses_prior`` (run in
#: worker processes), or only those with it (the LLM, run once per batch in the main
#: process after the others).
PHASE_ALL, PHASE_EARLY, PHASE_LATE = "all", "early", "late"


def detect_tree(
    root: Document, config: Config, phase: str = PHASE_ALL, prior: Sequence[Entity] = ()
) -> list[Entity]:
    """Run the detector chain on a document and all its descendants. Detectors with
    ``uses_prior`` run last and receive what the others found (``prior`` holds the
    early phase's entities when ``phase`` is :data:`PHASE_LATE`)."""
    entities: list[Entity] = []
    chain = detectors_for(config)
    early = [d for d in chain if not d.uses_prior] if phase != PHASE_LATE else []
    late = [d for d in chain if d.uses_prior] if phase != PHASE_EARLY else []
    detectors = early + late
    hook = _item_progress
    atomic = AtomicIndex()
    for doc in root.walk():
        # Unreadable items still get their name analysed: it shows up in reports.
        segments = (
            doc.segments
            if doc.status is DocumentStatus.EXTRACTED
            else [s for s in doc.segments if s.id == FILENAME_SEGMENT_ID]
        )
        texts = {s.id: s.text for s in segments}
        # Detectors read file names with their separators as spaces
        # (``Contrat_jean_DUPONT.pdf`` -> ``Contrat jean DUPONT pdf``); offsets and
        # values still refer to the real name.
        view = [_name_view(s) if s.id == FILENAME_SEGMENT_ID else s for s in segments]
        first = len(entities)
        known = [e for e in prior if e.document_id == doc.id]
        for detector in detectors:
            if hook is not None:
                step = _detector_label(detector.name)
                detector.on_progress = lambda d, t, step=step: hook(d, t, step)
            try:
                if detector.uses_prior:
                    found = detector.detect_after(view, doc.id, [*known, *entities[first:]])
                else:
                    found = detector.detect(view, doc.id)
            except Exception as exc:
                log.warning(
                    "detector %s failed on %s: %s", detector.name, doc.id, type(exc).__name__
                )
                doc.status = DocumentStatus.FAILED
                doc.add_issue(
                    Severity.ERROR,
                    "detect.error",
                    f"Le détecteur « {_detector_label(detector.name)} » a échoué : détection "
                    "incomplète.",
                )
                break
            for e in found:
                text = texts.get(e.segment_id)
                if text is None or not 0 <= e.start < e.end <= len(text):
                    continue
                if e.score < config.min_score:
                    continue
                # No detector may cut a GUID, a hash or a token (:mod:`anonyma.atomic`).
                span = atomic.fit(text, e.start, e.end)
                if span is None:
                    continue
                e.start, e.end = span
                e.value = text[e.start : e.end]
                e.document_id = doc.id
                entities.append(e)
            detector.on_progress = None
    return entities


def _detector_label(name: str) -> str:
    from anonyma.gui.labels import detector_label

    return detector_label(name)


#: Why an input is not read (``_analyze_one(..., skipped=...)``).
SKIP_LINK = "link"
SKIP_CRASH = "crash"


def _analyze_one(
    source: Path,
    name: str,
    config: Config,
    work_dir: Path,
    skipped: str = "",
    cancel: CancelToken | None = None,
    phase: str = PHASE_ALL,
) -> tuple[Document, list[Entity]]:
    """Extract and detect one input; a ``skipped`` input is not read, only its name
    is analysed (so that it shows up anonymized in reports). ``cancel``: inline runs
    only (a token does not cross processes), checked by containers between items.
    ``phase``: :data:`PHASE_EARLY` leaves the LLM to :meth:`Pipeline.analyze`."""
    doc = skipped_document(source, name, skipped) if skipped else None
    doc = doc or extract_file(source, name, config, work_dir, cancel=cancel)
    return doc, detect_tree(doc, config, phase)


def skipped_document(source: Path, name: str, reason: str) -> Document:
    """An input never read: a link of an input folder (``SKIP_LINK``) or a file that
    crashed its worker process twice (``SKIP_CRASH``)."""
    if reason == SKIP_LINK:
        doc = Document(source, name, LINK_FORMAT, status=DocumentStatus.UNSUPPORTED)
        doc.add_issue(
            Severity.ERROR,
            "input.link",
            "Lien symbolique ou jonction ignoré : sa cible n'est pas traitée "
            "(copiez les fichiers eux-mêmes dans le dossier).",
        )
    else:
        doc = Document(source, name, "inconnu", status=DocumentStatus.FAILED)
        doc.add_issue(Severity.ERROR, "worker.crash", CRASH_MESSAGE)
    doc.segments.append(
        Segment(
            id=FILENAME_SEGMENT_ID,
            text=name,
            locator=(),
            context=SegmentContext(SegmentKind.FILENAME, label="nom de fichier"),
        )
    )
    return doc


# ---------------------------------------------------------------------------
# File names
# ---------------------------------------------------------------------------

#: Segments that often hold file names or paths (properties, attributes, headers):
#: batch values are also searched there with separators read as spaces.
NAME_LIKE_KINDS = frozenset(
    {
        SegmentKind.FILENAME,
        SegmentKind.METADATA,
        SegmentKind.ATTRIBUTE,
        SegmentKind.HEADER,
        SegmentKind.HIDDEN,
    }
)
NEUTRAL_DOCUMENT = "document"
NEUTRAL_CHILD = "piece_jointe"
NEUTRAL_FOLDER = "dossier"
#: Entity type under which neutral file names are kept in the encrypted store.
FILENAME_STORE_TYPE = "NOM_FICHIER"


def _name_view(segment: Segment) -> Segment:
    """``segment`` (a file name) as detectors should read it: ``_``, ``+`` and the
    extension dot become spaces. Same length, so offsets stay valid."""
    text = segment.text.replace("_", " ").replace("+", " ")
    stem, dot, _ = text.rpartition(".")
    if dot and stem and "/" not in text[len(stem) :]:
        text = f"{stem} {text[len(stem) + 1 :]}"
    return replace(segment, text=text)


def propagate_names(
    documents: Sequence[Document],
    clusters: Mapping[str, EntityCluster],
    existing: Sequence[Entity],
) -> list[Entity]:
    """Batch values in file names, paths and properties, separators ignored.

    ``propagate`` already finds ``DUPONT`` in ``Contrat_jean_DUPONT.pdf``; this pass
    also finds the full forms whatever the separators (``jean_DUPONT``,
    ``DUPONT-Jean``, ``Quintavalle_Brezac``) so that no given name or compound part
    is left behind. Only new spans (or longer ones) are returned.
    """
    matcher: MultiMatcher[tuple[str, str, float]] = MultiMatcher()
    for cluster in clusters.values():
        if cluster.review is ReviewStatus.REJECTED or cluster.entity_type in DOCUMENT_LOCAL_TYPES:
            continue
        for form, cap in cluster_forms(cluster):
            needle = spaced_name(form)
            if len(normalize(needle)) >= 3:
                target = (cluster.id, cluster.entity_type, cluster.max_score)
                matcher.add(needle, target, require_capital=cap)
    if not len(matcher):
        return []
    taken: dict[tuple[str, str], list[tuple[int, int]]] = {}
    for e in existing:
        taken.setdefault((e.document_id, e.segment_id), []).append((e.start, e.end))
    found: list[Entity] = []
    for root in documents:
        for doc in root.walk():
            readable = doc.status is DocumentStatus.EXTRACTED
            for segment in doc.segments:
                if segment.context.kind not in NAME_LIKE_KINDS:
                    continue
                if not readable and segment.id != FILENAME_SEGMENT_ID:
                    continue
                spans = taken.get((doc.id, segment.id), [])
                for match in leftmost_longest(matcher.find(spaced_name(segment.text))):
                    if any(s <= match.start and match.end <= e for s, e in spans):
                        continue
                    cluster_id, entity_type, score = max(
                        match.payloads,
                        key=lambda t: (t[1] == EntityType.PERSON, TYPE_PRIORITY.get(t[1], 30)),
                    )
                    found.append(
                        Entity(
                            entity_type=entity_type,
                            value=segment.text[match.start : match.end],
                            document_id=doc.id,
                            segment_id=segment.id,
                            start=match.start,
                            end=match.end,
                            score=score,
                            detector=PROPAGATION_DETECTOR,
                            cluster_id=cluster_id,
                            review=(
                                ReviewStatus.MANUAL
                                if clusters[cluster_id].review is ReviewStatus.MANUAL
                                else ReviewStatus.AUTO
                            ),
                        )
                    )
    return found


def _output_shape(doc: Document) -> tuple[Writer | None, str, bool]:
    """(writer, output extension, packed folder) of a document."""
    writer = registry.writer_for(doc.format) if doc.status is DocumentStatus.EXTRACTED else None
    extension = writer.output_extension(doc) if writer else PurePosixPath(doc.name).suffix
    # Embedded folder outputs are packed into a zip (containers need one file).
    pack_folder = writer is not None and writer.directory_output and doc.depth > 0
    if pack_folder:
        extension += ".zip"
    return writer, extension, pack_folder


def neutral_names(documents: Sequence[Document]) -> dict[str, str]:
    """``document id -> neutral output name`` for the whole batch.

    Top-level files: ``document_0001.pdf``... in input order; their folders
    ``dossier_0001``... (the tree is kept); embedded items (attachments, archive
    members, mails of a mailbox): ``piece_jointe_0001.pdf``... numbered per
    container in their order of appearance (flat: inner folders are dropped).
    Extensions are those of the produced files.
    """
    names: dict[str, str] = {}
    folders: dict[tuple[str, ...], str] = {}

    def child_names(parent: Document) -> None:
        for number, child in enumerate(parent.children, start=1):
            names[child.id] = f"{NEUTRAL_CHILD}_{number:04d}{_output_shape(child)[1]}"
            child_names(child)

    for number, doc in enumerate(documents, start=1):
        parts = [p for p in doc.name.replace("\\", "/").split("/") if p not in ("", ".", "..")]
        neutral_dirs = []
        for depth in range(1, len(parts)):
            key = tuple(normalize(p) for p in parts[:depth])
            if key not in folders:
                folders[key] = f"{NEUTRAL_FOLDER}_{len(folders) + 1:04d}"
            neutral_dirs.append(folders[key])
        extension = _output_shape(doc)[1]
        names[doc.id] = "/".join([*neutral_dirs, f"{NEUTRAL_DOCUMENT}_{number:04d}{extension}"])
        child_names(doc)
    return names


def _logical_path(doc: Document, parents: Mapping[str, Document], names: Mapping[str, str]) -> str:
    """Full path of ``doc`` inside the batch (``archive.zip/dossier/x.pdf``)."""
    parts = [names.get(doc.id, doc.name)]
    current = doc
    while current.parent_id is not None and current.parent_id in parents:
        current = parents[current.parent_id]
        parts.append(names.get(current.id, current.name))
    return "/".join(reversed(parts))


def store_neutral_names(
    store: MappingStore, batch_id: str, documents: Sequence[Document], names: Mapping[str, str]
) -> None:
    """Keep ``neutral path -> original path`` in the encrypted store (pseudonymization)."""
    parents = {d.id: d for root in documents for d in root.walk()}
    for root in documents:
        for doc in root.walk():
            if doc.id in names:
                store.save_filename(
                    batch_id, _logical_path(doc, parents, names), _logical_path(doc, parents, {})
                )


def store_surfaces(
    store: MappingStore, entities: Sequence[Entity], table: SubstitutionTable
) -> None:
    """Keep, for each substitute written in the outputs, the exact text it replaces
    (``[PERSONNE_001]`` for « DUPONT » only), so that re-identification restores the
    text as it was; a substitute standing for several texts gets an empty original
    (the canonical value is restored), which also clears an older exact form."""
    from anonyma.store import SURFACE_TYPE

    if table.mode is ReplacementMode.BLACKOUT:
        return
    texts: dict[str, set[str]] = {}
    for e in entities:
        if e.cluster_id in table.by_cluster:
            texts.setdefault(table.substitute_for(e), set()).add(e.value)
    for substitute, values in sorted(texts.items()):
        original = next(iter(values)) if len(values) == 1 else ""
        store.save(SURFACE_TYPE, substitute, original, substitute)


def original_names(root: Document) -> list[str]:
    """Every original name of a top-level document's tree (sensitive)."""
    return sorted({d.name for d in root.walk() if d.name})


def ocr_gaps(root: Document) -> int:
    """Images/pages of a document tree that OCR could not read at analysis."""
    return sum(
        1
        for d in root.walk()
        for i in d.issues
        if is_ocr_issue(i.code) and i.severity is not Severity.INFO
    )


# ---------------------------------------------------------------------------
# Writing and verification
# ---------------------------------------------------------------------------


class _WriteContext(WriteContext):
    def __init__(
        self, config: Config, child_outputs: Mapping[str, ChildOutput], work_dir: Path
    ) -> None:
        self.config = config
        self.mode = config.mode
        self.child_outputs = child_outputs
        self.work_dir = work_dir

    def temp_path(self, suffix: str = "") -> Path:
        self.work_dir.mkdir(parents=True, exist_ok=True)
        return self.work_dir / f"{new_id()}{suffix}"


def anonymized_name(
    doc: Document, entities: Sequence[Entity], table: SubstitutionTable, extension: str
) -> str:
    """Apply the plan to the FILENAME segment, sanitize, and set the extension.

    Each path component is anonymized on its own: a replacement spanning a separator
    (an entity ``corpus/Exports`` read across two folder names) is cut at the
    separator and each piece gets the substitute, so the output tree always has the
    depth of the input tree (never two folders merged into one)."""
    plan = build_plan(doc, [e for e in entities if e.segment_id == FILENAME_SEGMENT_ID], table)
    text = doc.segment(FILENAME_SEGMENT_ID).text
    replacements = plan.for_segment(FILENAME_SEGMENT_ID)
    blackout = table.mode is ReplacementMode.BLACKOUT
    parts: list[str] = []
    for start, end in _path_components(text):
        component = text[start:end]
        if component in ("", ".", ".."):
            continue
        pieces = []
        for rep in replacements:
            lo, hi = max(rep.start, start), min(rep.end, end)
            if lo >= hi:
                continue
            # Separators a substitute carries at its edges belong to the path.
            substitute = (
                BLACKOUT_CHAR * (hi - lo) if blackout else rep.substitute.strip("/\\") or "_"
            )
            piece = replace(rep, start=lo - start, end=hi - start, original=text[lo:hi])
            pieces.append(replace(piece, substitute=substitute))
        parts.append(safe_filename(apply_replacements(component, pieces)))
    parts = parts or ["_"]
    stem = PurePosixPath(parts[-1])
    parts[-1] = safe_filename((stem.stem if stem.suffix else stem.name) + extension)
    return "/".join(parts)


def top_level_names(
    documents: Sequence[Document], entities: Sequence[Entity], table: SubstitutionTable
) -> dict[str, str]:
    """``document id -> output name`` of the top-level documents, folders named once
    for the whole batch.

    Each document's name is anonymized on its own (:func:`anonymized_name`), so a
    folder could come out masked for one file and not for another (an entity read
    in one name only). An input folder gets a single output name: the masked one
    when any of its files masked it (recall over precision), else the first."""
    by_doc: dict[str, list[Entity]] = {}
    for e in entities:
        if e.segment_id == FILENAME_SEGMENT_ID:
            by_doc.setdefault(e.document_id, []).append(e)
    own: dict[str, tuple[list[str], list[str]]] = {}
    folders: dict[tuple[str, ...], str] = {}
    for doc in documents:
        name = anonymized_name(doc, by_doc.get(doc.id, []), table, _output_shape(doc)[1])
        original = [doc.name[s:e] for s, e in _path_components(doc.name)]
        original = [p for p in original if p not in ("", ".", "..")]
        parts = name.split("/")
        if len(parts) != len(original):
            continue
        own[doc.id] = (original, parts)
        for depth in range(1, len(parts)):
            key = tuple(original[:depth])
            current = folders.get(key)
            unmasked = safe_filename(original[depth - 1])
            if current is None or (current == unmasked and parts[depth - 1] != unmasked):
                folders[key] = parts[depth - 1]
    return {
        doc_id: "/".join(
            [*(folders[tuple(original[:depth])] for depth in range(1, len(parts))), parts[-1]]
        )
        for doc_id, (original, parts) in own.items()
    }


def _path_components(name: str) -> list[tuple[int, int]]:
    """``(start, end)`` of each component of ``name`` (``/`` and ``\\`` separators)."""
    bounds: list[tuple[int, int]] = []
    start = 0
    for index, char in enumerate(name):
        if char in "/\\":
            bounds.append((start, index))
            start = index + 1
    bounds.append((start, len(name)))
    return bounds


@dataclass(slots=True)
class _WriteJob:
    document: Document
    entities: list[Entity]
    table: SubstitutionTable
    sensitive: list[tuple[str, str]]
    allowed: list[str]
    review_clusters: set[str]
    config: Config
    output_dir: Path
    work_dir: Path
    #: Original names of the tree (searched in the output by the verifier).
    names: list[str] = field(default_factory=list)
    #: Structural names of the batch (JSON keys, CSV headers, XML names), never
    #: searched in raw bytes (:mod:`anonyma.structure`).
    structure: frozenset[str] = frozenset()
    #: ``document id -> output name`` decided batch-wide: neutral names
    #: (``config.neutral_filenames``), else the top-level names, whose folders are
    #: named consistently for the whole batch (:func:`top_level_names`).
    output_names: dict[str, str] = field(default_factory=dict)


def _counts(entities: Sequence[Entity]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for e in entities:
        counts[e.entity_type] = counts.get(e.entity_type, 0) + 1
    return counts


def _write_tree(
    doc: Document, job: _WriteJob, by_doc: Mapping[str, list[Entity]]
) -> tuple[DocumentResult, Path | None]:
    """Write ``doc`` (children first) into the work folder. Returns the result and
    the written path (None when the document is omitted)."""
    entities = by_doc.get(doc.id, [])
    writer, extension, pack_folder = _output_shape(doc)
    name = job.output_names.get(doc.id) or anonymized_name(doc, entities, job.table, extension)
    result = DocumentResult(
        document_id=doc.id,
        output_name=name,
        format=doc.format,
        output_format=writer.output_format(doc) if writer else doc.format,
        status=doc.status,
        entity_counts=_counts(entities),
        issues=list(doc.issues),
    )
    if writer is None:
        if result.status is DocumentStatus.EXTRACTED:
            result.status = DocumentStatus.UNSUPPORTED
        return result, None

    child_outputs: dict[str, ChildOutput] = {}
    for child in doc.children:
        child_result, child_path = _write_tree(child, job, by_doc)
        result.children.append(child_result)
        child_outputs[child.id] = ChildOutput(child.id, child_path, child_result.output_name)
        if child_path is None:
            result.issues.append(
                Issue(
                    Severity.WARNING,
                    OMITTED_CODE,
                    f"Élément « {child_result.output_name} » omis : il n'a pas pu être anonymisé.",
                )
            )

    nothing_kept = doc.children and all(o.path is None for o in child_outputs.values())
    if nothing_kept and not _has_own_text(doc):
        # Nothing of the container could be kept: no empty shell is produced.
        result.status = DocumentStatus.FAILED
        result.issues.append(Issue(Severity.ERROR, EMPTY_CONTAINER_CODE, EMPTY_CONTAINER))
        return result, None

    # Top-level outputs too: they only reach the output folder once verified.
    destination = job.work_dir / new_id() / PurePosixPath(name).name
    target = destination.with_name(new_id()) if pack_folder else destination
    plan = build_plan(doc, entities, job.table)
    shown = doc
    if job.config.neutral_filenames:
        # Embedded items are flattened under neutral names: the (empty) folders of an
        # archive go too, their original names would otherwise stay.
        shown = replace(doc, segments=[s for s in doc.segments if s.locator[:1] != ("dir",)])
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        context = _WriteContext(job.config, child_outputs, job.work_dir)
        result.issues.extend(writer.write(shown, plan, target, context))
        if pack_folder:
            _zip_folder(target, destination)
            shutil.rmtree(target, ignore_errors=True)
            result.output_format = "zip"
    except Exception as exc:
        log.warning("write failed for %s: %s", doc.id, type(exc).__name__)
        _remove_output(target)
        _remove_output(destination)
        result.status = DocumentStatus.FAILED
        result.issues.append(
            Issue(
                Severity.ERROR,
                "write.error",
                f"Échec de l'écriture du fichier produit : {failure_reason(exc)}.",
            )
        )
        return result, None
    if any(i.severity is Severity.ERROR for i in result.issues):
        _remove_output(destination)
        result.status = DocumentStatus.FAILED
        return result, None
    result.output_path = destination
    return result, destination


def _place(source: Path, folder: Path, name: str) -> Path:
    """Move a written output to ``folder/name`` (``x (2).pdf``... when taken).

    The name is reserved by exclusive creation: worker processes place their outputs
    concurrently and must never overwrite each other."""
    path = folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    is_dir = source.is_dir()
    for i in range(1, 100_000):
        candidate = path if i == 1 else path.with_name(f"{path.stem} ({i}){path.suffix}")
        try:
            if is_dir:
                candidate.mkdir()
            else:
                candidate.touch(exist_ok=False)
        except FileExistsError:
            continue
        try:
            try:
                os.replace(source, candidate)  # same volume (directories: POSIX only)
            except OSError:
                if is_dir:
                    shutil.copytree(source, candidate, dirs_exist_ok=True)
                    shutil.rmtree(source, ignore_errors=True)
                else:
                    shutil.copyfile(source, candidate)
                    source.unlink()
        except BaseException:
            _remove_output(candidate)
            raise
        return candidate
    raise FileExistsError("nom de fichier de sortie indisponible")


def _set_status(result: DocumentResult, status: DocumentStatus) -> None:
    for child in result.children:
        if child.status is DocumentStatus.EXTRACTED:
            _set_status(child, status)
    if result.status is DocumentStatus.EXTRACTED:
        result.status = status


def _write_and_verify(job: _WriteJob) -> DocumentResult:
    by_doc: dict[str, list[Entity]] = {}
    for e in job.entities:
        by_doc.setdefault(e.document_id, []).append(e)
    result, written = _write_tree(job.document, job, by_doc)
    if written is None:
        return result
    try:
        status, folder = _assess(result, written, job)
        result.output_path = _place(written, folder, result.output_name)
    except Exception as exc:
        # Whatever failed, an output whose verification and placement did not
        # complete never stays anywhere a user could pick it up.
        log.warning("verification failed for %s: %s", job.document.id, type(exc).__name__)
        _remove_output(written)  # ``_place`` removes its own partial copy
        result.output_path = None
        _set_status(result, DocumentStatus.FAILED)
        result.issues.append(
            Issue(
                Severity.ERROR,
                "verify.error",
                f"La vérification a échoué : {failure_reason(exc)}.",
            )
        )
        return result
    _set_status(result, status)
    omitted = _flag_omissions(result)
    if omitted and result.status is DocumentStatus.NEEDS_REVIEW:
        result.issues.append(
            Issue(
                Severity.WARNING,
                INCOMPLETE_CONTAINER_CODE,
                f"{plural(omitted, 'élément inclus omis', 'éléments inclus omis')} (illisible, "
                "chiffré ou non pris en charge) : le fichier produit est incomplet, à revoir.",
            )
        )
    return result


def _has_own_text(doc: Document) -> bool:
    """Whether a container holds text of its own (mail headers and body...), besides
    its name and the names of its folders."""
    return any(s.text.strip() for s in doc.segments if s.context.kind is not SegmentKind.FILENAME)


def _flag_omissions(result: DocumentResult) -> int:
    """Embedded items left out of ``result``'s tree (``child.omitted``). A verified
    container missing some of them is never « Anonymisé »: it becomes « À revoir »."""
    omitted = sum(1 for i in result.issues if i.code == OMITTED_CODE)
    omitted += sum(_flag_omissions(child) for child in result.children)
    if omitted and result.status is DocumentStatus.ANONYMIZED:
        result.status = DocumentStatus.NEEDS_REVIEW
    return omitted


def _write_crashed(job: _WriteJob) -> DocumentResult:
    """A document whose writing crashed its worker twice: failed, nothing written."""
    doc = job.document
    _, extension, _ = _output_shape(doc)
    entities = [e for e in job.entities if e.document_id == doc.id]
    name = job.output_names.get(doc.id) or anonymized_name(doc, entities, job.table, extension)
    return DocumentResult(
        document_id=doc.id,
        output_name=name,
        format=doc.format,
        output_format=doc.format,
        status=DocumentStatus.FAILED,
        entity_counts=_counts(entities),
        issues=[Issue(Severity.ERROR, "worker.crash", CRASH_MESSAGE)],
    )


def review_issue(job: _WriteJob, review_ids: set[str | None], tree_ids: set[str]) -> Issue:
    """Why a file is « À revoir »: how many uncertain entities, of which types, in which
    zones (segment labels, as in the residue reports): never a value."""
    from anonyma.gui.labels import entity_type_label

    segments = {(d.id, s.id): s for d in job.document.walk() for s in d.segments}
    types: dict[str, set[str | None]] = {}
    zones: list[str] = []
    for e in job.entities:
        if e.document_id not in tree_ids or e.cluster_id not in review_ids:
            continue
        types.setdefault(e.entity_type, set()).add(e.cluster_id)
        segment = segments.get((e.document_id, e.segment_id))
        label = " ".join((segment.context.label if segment else "").split())[:REVIEW_ZONE_LENGTH]
        if label and label not in zones:
            zones.append(label)
    count = len(review_ids)
    kinds = ", ".join(
        f"{entity_type_label(t)} ×{len(c)}" if len(c) > 1 else entity_type_label(t)
        for t, c in sorted(types.items(), key=lambda item: (-len(item[1]), item[0]))
    )
    what = plural(count, "entité incertaine", "entités incertaines")
    masked = "masquée" if count < 2 else "masquées"
    where = ""
    if zones:
        shown = ", ".join(f"« {z} »" for z in zones[:REVIEW_ZONES])
        more = f" et {len(zones) - REVIEW_ZONES} autre(s)" if len(zones) > REVIEW_ZONES else ""
        where = f" ; zones : {shown}{more}"
    return Issue(
        Severity.WARNING,
        REVIEW_ISSUE,
        f"{what} ({kinds}) {masked} par précaution : à confirmer ou rejeter dans la revue "
        f"avant diffusion{where}.",
    )


def _assess(result: DocumentResult, written: Path, job: _WriteJob) -> tuple[DocumentStatus, Path]:
    """Verify ``written`` and fill ``result`` (counters, issues). Returns the status of
    the tree and the folder the output belongs in: the output folder, or the
    quarantine (residue, failed or incomplete verification)."""
    verification = _verify_output(written, result.output_name, job)
    result.verification = verification
    result.residue_count = len(verification.certain_residues)
    result.possible_residue_count = len(verification.possible_residues)
    result.issues.extend(verification.issues)
    tree_ids = {d.id for d in job.document.walk()}
    review_ids = {
        e.cluster_id for e in job.entities if e.document_id in tree_ids
    } & job.review_clusters
    result.review_count = len(review_ids)
    review_types = {e.cluster_id: e.entity_type for e in job.entities if e.cluster_id in review_ids}
    blackout = job.table.mode is ReplacementMode.BLACKOUT
    result.review_items = sorted(
        (review_types[c], "" if blackout else job.table.by_cluster.get(c, ""))
        for c in review_ids
        if c is not None
    )
    if review_ids:
        result.issues.append(review_issue(job, review_ids, tree_ids))
    quarantine = quarantine_dir(job.output_dir)
    if not verification.clean:
        if verification.verified:
            result.issues.append(
                Issue(
                    Severity.ERROR,
                    RESIDUE_ISSUE,
                    f"{result.residue_count} résidu(s) détecté(s) : fichier placé en quarantaine.",
                )
            )
            return DocumentStatus.RESIDUE, quarantine
        result.issues.append(
            Issue(
                Severity.ERROR,
                "verify.failed",
                "Vérification impossible : fichier placé en quarantaine, non anonymisé.",
            )
        )
        return DocumentStatus.FAILED, quarantine

    if result.possible_residue_count:
        result.issues.append(
            Issue(
                Severity.WARNING,
                "verify.possible_residue",
                f"{result.possible_residue_count} résidu(s) possible(s) signalé(s) : à vérifier.",
            )
        )
    gaps = ocr_gaps(job.document)
    if gaps:
        result.issues.append(
            Issue(
                Severity.WARNING,
                "analyse.ocr_incomplete",
                f"Analyse incomplète : {gaps} image(s) ou page(s) n'ont pas pu être lues "
                "par l'OCR. Fichier à revoir.",
            )
        )
    if not verification.complete:
        # Some image or page of the output could not be read back: what it shows was
        # never checked. Never anonymized: to review, kept out of the clean set.
        result.issues.append(
            Issue(
                Severity.WARNING,
                "verify.incomplete",
                f"Vérification incomplète : {verification.unread} image(s) ou page(s) du "
                "fichier produit n'ont pas pu être lues par l'OCR. Fichier placé en "
                "quarantaine, à contrôler visuellement.",
            )
        )
        return DocumentStatus.NEEDS_REVIEW, quarantine
    needs_review = result.review_count or result.possible_residue_count or gaps
    status = DocumentStatus.NEEDS_REVIEW if needs_review else DocumentStatus.ANONYMIZED
    return status, job.output_dir


def _verify_output(path: Path, name: str, job: _WriteJob) -> VerificationResult:
    verify_dir = job.work_dir / f"verify-{new_id()}"
    try:
        output_doc = extract_file(path, name, job.config, verify_dir)
        verifier = Verifier(job.config, detectors_for(job.config))
        substitutes = job.table.all_substitutes()
        result = verifier.verify(
            output_doc, job.sensitive, substitutes, job.allowed, names=job.names
        )
        if result.clean:
            # Backstop: what no extractor exposed is still in the bytes.
            result.residues.extend(
                verifier.verify_raw(
                    output_doc, job.sensitive, substitutes, job.allowed, job.structure
                )
            )
        return result
    except Exception as exc:
        log.warning("verification failed for %s: %s", job.document.id, type(exc).__name__)
        return VerificationResult(
            verified=False,
            issues=[
                Issue(
                    Severity.ERROR,
                    "verify.error",
                    f"La vérification a échoué : {failure_reason(exc)}.",
                )
            ],
        )
    finally:
        shutil.rmtree(verify_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

#: Progress stage of the language model, run after the other detectors (FULL).
LLM_STAGE = "langue"
#: Beyond this, a FULL run is announced as long (CLI warning, GUI notice).
LLM_WARNING_MINUTES = 15.0
#: Files read (at most) and seconds of extraction spent to estimate a FULL run.
ESTIMATE_SAMPLE_FILES = 12
ESTIMATE_SAMPLE_SECONDS = 20.0


@dataclass(frozen=True, slots=True)
class LlmEstimate:
    """Expected language-model work of a FULL run (:meth:`Pipeline.estimate_llm`)."""

    minutes: float
    #: Model calls expected (paragraphs pooled into windows), extrapolated.
    calls: int
    sampled_files: int
    total_files: int

    @property
    def long(self) -> bool:
        return self.minutes > LLM_WARNING_MINUTES

    def warning(self, switch: str) -> str:
        """The estimate in French, with ``switch`` (how to choose Standard here)."""
        total = max(1, round(self.minutes))
        hours, rest = divmod(total, 60)
        duration = f"{hours} h {rest:02d} min" if hours else f"{rest} min"
        sample = (
            ""
            if self.sampled_files >= self.total_files
            else f", d'après {self.sampled_files} fichiers sur {self.total_files}"
        )
        noun = "passage" if self.calls < 2 else "passages"
        return (
            f"profil Complet : environ {duration} estimées pour le modèle de langue local "
            f"({grouped(self.calls)}{NBSP}{noun} à analyser{sample}). Le profil Standard "
            f"traite ce lot en quelques minutes : {switch}."
        )


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


class Pipeline:
    def __init__(self, config: Config, *, store: MappingStore | None = None) -> None:
        profile: Profile = config.effective_profile()
        # Freeze the profile so that worker processes never re-detect hardware.
        self.config = replace(config, profile=profile)
        self.store = store
        registry.load_plugins()
        #: Profile the installed models actually serve (``complet`` without its
        #: language model runs as ``standard``): it sizes the worker pool.
        self.detection_profile = registry.usable_profile(self.config, profile)

    def estimate_llm(
        self, inputs: Sequence[Path], *, sample_seconds: float = ESTIMATE_SAMPLE_SECONDS
    ) -> LlmEstimate | None:
        """Language-model workload of a FULL run on ``inputs``, before it starts: a
        sample of the files (spread over the batch, at most ``sample_seconds`` of
        extraction) is read, its paragraphs the model would analyse counted (repeats
        once) and extrapolated by size, times the measured seconds per call. None
        outside FULL. An upper bound: the other detectors' coverage is not known yet."""
        if self.detection_profile is not Profile.FULL:
            return None
        llm_detector = next((d for d in detectors_for(self.config) if d.uses_prior), None)
        if llm_detector is None or not hasattr(llm_detector, "workload"):
            return None
        from anonyma.detectors import llm as llm_module

        files, _ = walk_inputs(inputs)
        if not files:
            return None
        sizes = [_size(path) for path, _ in files]
        total = sum(sizes) or 1
        step = max(1, len(files) // ESTIMATE_SAMPLE_FILES)
        order = [*range(0, len(files), step), *(i for i in range(len(files)) if i % step)]
        seen: set[str] = set()
        todo: list[Any] = []
        sampled = sampled_bytes = 0
        started = time.monotonic()
        work_dir = paths.tmp_dir() / f"estimation-{new_id()[:8]}"
        try:
            for i in order:
                if sampled and time.monotonic() - started > sample_seconds:
                    break
                path, name = files[i]
                doc = extract_file(path, name, self.config, work_dir / f"f{i}")
                for item in doc.walk():
                    todo += llm_detector.workload(item.segments, seen)
                sampled += 1
                sampled_bytes += sizes[i]
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)
        scale = total / max(1, sampled_bytes) if sampled < len(files) else 1.0
        calls = round(llm_module.estimated_calls(todo) * scale)
        minutes = calls * llm_module.call_seconds(self.config) / 60
        return LlmEstimate(minutes, calls, sampled, len(files))

    # -- execution helpers --------------------------------------------------

    def _workers(self, jobs: int, stage: str = "analyse") -> int:
        """Worker processes for ``stage``. FULL runs its language model in one process
        (one model instance, a memo shared by the whole batch: :data:`LLM_STAGE`);
        everything else (extraction, the other detectors, writing and verification,
        where the LLM never runs) is sized like STANDARD."""
        full = self.detection_profile is Profile.FULL
        if self.config.workers:
            wanted = self.config.workers
        elif full and stage == LLM_STAGE:
            wanted = 1  # one LLM instance at a time
        else:
            wanted = max(1, min(os.cpu_count() or 1, 8) - 1)
            if full or self.detection_profile is Profile.STANDARD:
                # Each worker holds its own GLiNER instance (~2 GiB).
                from anonyma import hardware

                info = hardware.detect_hardware()
                free = info.available_ram_bytes or info.ram_bytes
                if free:
                    wanted = min(wanted, max(1, int(free // GLINER_WORKER_RAM)))
        return max(1, min(wanted, jobs))

    def _map(
        self,
        fn: Callable[..., Any],
        jobs: list[tuple[Any, ...]],
        stage: str,
        progress: ProgressCallback | None,
        cancel: CancelToken | None,
        crashed: Callable[..., Any],
    ) -> list[Any | None]:
        """Run ``fn(*job)`` for each job; ``None`` for jobs skipped by cancellation.

        Worker processes may die (native crash, memory): the items left are then run
        one at a time in a fresh single-worker pool, never in this process, and an
        item that crashes such a pool twice gets ``crashed(*job)`` as its result."""
        results: list[Any | None] = [None] * len(jobs)
        total = len(jobs)
        finished: set[int] = set()

        def done(index: int, value: Any) -> None:
            results[index] = value
            finished.add(index)
            if progress:
                progress(ProgressEvent(stage, len(finished), total, index=index))

        if progress:
            progress(ProgressEvent(stage, 0, total))
        if self._workers(total, stage) == 1:
            global _item_progress

            def item(done_: int, total_: int, step: str) -> None:
                if progress is not None:
                    progress(ProgressEvent(stage, len(finished), total, None, done_, total_, step))

            for i, job in enumerate(jobs):
                if cancel and cancel.cancelled:
                    break
                _item_progress = item if progress is not None else None
                try:
                    done(i, fn(*job))
                except CancelledError:  # raised inside the item (inline extraction)
                    break
                finally:
                    _item_progress = None
            return results
        try:
            self._pool(fn, jobs, list(range(total)), self._workers(total, stage), cancel, done)
        except BrokenProcessPool:
            log.warning("worker pool broken during %s; retrying the remaining items", stage)
            # A worker can also die for reasons outside the item (memory pressure from
            # other programs): an item fails only after crashing a worker of its own
            # twice, not on the first crash where it happened to be running.
            isolated_crashes: dict[int, int] = {}
            while not (cancel and cancel.cancelled):
                pending = [i for i in range(total) if i not in finished]
                culprit = self._serial(fn, jobs, pending, cancel, done) if pending else None
                if culprit is None:
                    break
                isolated_crashes[culprit] = isolated_crashes.get(culprit, 0) + 1
                if isolated_crashes[culprit] < 2:
                    continue
                log.warning("item %d of %s crashed its worker twice", culprit, stage)
                done(culprit, crashed(*jobs[culprit]))
        return results

    @staticmethod
    def _serial(
        fn: Callable[..., Any],
        jobs: list[tuple[Any, ...]],
        indices: list[int],
        cancel: CancelToken | None,
        done: Callable[[int, Any], None],
    ) -> int | None:
        """Run ``jobs[indices]`` one at a time in a fresh single-worker pool; returns
        the index of the item that broke it (None when all ran or cancelled)."""
        with worker_pool(1) as pool:
            for index in indices:
                if cancel and cancel.cancelled:
                    return None
                try:
                    value = pool.submit(fn, *jobs[index]).result()
                except BrokenProcessPool:
                    return index
                done(index, value)
        return None

    @staticmethod
    def _pool(
        fn: Callable[..., Any],
        jobs: list[tuple[Any, ...]],
        indices: list[int],
        workers: int,
        cancel: CancelToken | None,
        done: Callable[[int, Any], None],
    ) -> None:
        """Run ``jobs[indices]`` in a fresh pool, reporting each result to ``done``.
        Raises ``BrokenProcessPool`` after reporting every result completed before
        the break."""
        reported: set[int] = set()
        with worker_pool(workers) as pool:
            futures: dict[Future[Any], int] = {pool.submit(fn, *jobs[i]): i for i in indices}
            try:
                for future in as_completed(futures):
                    if cancel and cancel.cancelled:
                        # Pending jobs are dropped; running ones still finish and are
                        # kept, so that results match what was written.
                        for f in futures:
                            f.cancel()
                    if future.cancelled():
                        continue
                    value = future.result()
                    reported.add(futures[future])
                    done(futures[future], value)
            except BrokenProcessPool:
                for future, index in futures.items():
                    if index in reported or not future.done() or future.cancelled():
                        continue
                    if future.exception() is None:
                        done(index, future.result())
                raise
            finally:
                # Leaving early (interrupted): pending jobs never start, running ones
                # finish (they may be placing an output), before the pool shuts down.
                for f in futures:
                    f.cancel()
                wait(list(futures))

    def _analysis_crashed(
        self, source: Path, name: str, config: Config, work_dir: Path, *rest: Any
    ) -> tuple[Document, list[Entity]]:
        """An input that crashed its worker twice: only its name is analysed (in a
        worker as well), so that it is reported anonymized."""
        job = (source, name, config, work_dir, SKIP_CRASH)
        try:
            with worker_pool(1) as pool:
                result: tuple[Document, list[Entity]] = pool.submit(_analyze_one, *job).result()
                return result
        except BrokenProcessPool:
            return skipped_document(source, name, SKIP_CRASH), []

    # -- public API ----------------------------------------------------------

    def analyze(
        self,
        inputs: Sequence[Path],
        progress: ProgressCallback | None = None,
        cancel: CancelToken | None = None,
    ) -> BatchAnalysis:
        batch_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + new_id()[:6]
        work_dir = paths.tmp_dir() / batch_id
        paths.claim(work_dir)  # kept by clean_tmp() of other instances
        files, links = walk_inputs(inputs)
        if links:
            log.warning("%d link(s) skipped in input folders", len(links))
        items = [*files, *links]
        inline = cancel if self._workers(len(items)) == 1 else None
        # The language model runs after the pool, here, once for the whole batch: one
        # instance and one memo, the other detectors in parallel worker processes.
        late = (
            any(d.uses_prior for d in detectors_for(self.config))
            and self._workers(len(items), LLM_STAGE) == 1
        )
        phase = PHASE_EARLY if late else PHASE_ALL
        jobs = [
            (
                path,
                name,
                self.config,
                work_dir / f"f{i}",
                SKIP_LINK if i >= len(files) else "",
                inline,
                phase,
            )
            for i, (path, name) in enumerate(items)
        ]
        outputs = self._map(_analyze_one, jobs, "analyse", progress, cancel, self._analysis_crashed)
        if late:
            self._late_detection(outputs, progress, cancel)

        documents: list[Document] = []
        entities: list[Entity] = []
        for (path, name), out in zip(items, outputs, strict=True):
            if out is None:
                doc = Document(
                    source=path, name=name, format="inconnu", status=DocumentStatus.CANCELLED
                )
                doc.segments.append(
                    Segment(FILENAME_SEGMENT_ID, name, (), SegmentContext(SegmentKind.FILENAME))
                )
                documents.append(doc)
                continue
            documents.append(out[0])
            entities.extend(out[1])

        entities, clusters = resolve(entities, self.config)
        extra = propagate(documents, clusters, entities, self.config)
        extra += propagate_names(documents, clusters, [*entities, *extra])
        entities, clusters = resolve([*entities, *extra], self.config, clusters)
        link_by_place(documents, entities, clusters)
        entities = drop_inside_whitelist(documents, entities, self.config)
        entities, clusters = apply_policies(documents, entities, clusters, self.config)
        return BatchAnalysis(
            batch_id,
            self.config,
            documents,
            entities,
            clusters,
            work_dir,
            inputs=[Path(p).resolve() for p in inputs],
        )

    def _late_detection(
        self,
        outputs: list[Any | None],
        progress: ProgressCallback | None,
        cancel: CancelToken | None,
    ) -> None:
        """Run the ``uses_prior`` detectors (the LLM) on every analysed document, in
        this process, with the entities the other detectors found; progress as the
        :data:`LLM_STAGE` stage (files, and segments inside the current one)."""
        global _item_progress
        total = len(outputs)
        done = 0

        def item(done_: int, total_: int, step: str) -> None:
            if progress is not None:
                progress(ProgressEvent(LLM_STAGE, done, total, None, done_, total_, step))

        if progress:
            progress(ProgressEvent(LLM_STAGE, 0, total))
        for index, out in enumerate(outputs):
            if cancel and cancel.cancelled:
                break
            if out is not None:
                doc, found = out
                _item_progress = item if progress is not None else None
                try:
                    found.extend(detect_tree(doc, self.config, PHASE_LATE, found))
                finally:
                    _item_progress = None
            done += 1
            if progress:
                progress(ProgressEvent(LLM_STAGE, done, total, index=index))

    def apply(
        self,
        analysis: BatchAnalysis,
        output_dir: Path,
        decisions: ReviewDecisions | None = None,
        progress: ProgressCallback | None = None,
        cancel: CancelToken | None = None,
    ) -> BatchResult:
        decisions = decisions or ReviewDecisions()
        output_dir = Path(output_dir).resolve()
        sources = [d.source for d in analysis.documents if d.format != LINK_FORMAT]
        validate_output_dir([*analysis.inputs, *sources], output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        resolved = resolve_decisions(analysis, decisions)
        entities, active = resolved.entities, resolved.clusters
        table = Replacer(self.config, self.store).assign(active, entities, analysis.documents)
        if self.store is not None and self.config.regime is Regime.PSEUDONYMIZATION:
            store_surfaces(self.store, entities, table)
        local = {cid for cid, c in active.items() if c.entity_type in DOCUMENT_LOCAL_TYPES}
        batch_wide = sorted(
            {
                (v, c.entity_type)
                for cid, c in active.items()
                if cid not in local
                for v in c.variants
            }
        )
        allowed = sorted(set(resolved.allowed) | WRITER_PLACEHOLDERS)
        review = {cid for cid, c in active.items() if c.needs_review}
        structure = frozenset(
            name
            for root in analysis.documents
            for d in root.walk()
            for name in d.properties.get(STRUCTURE, ())
        )
        if self.config.neutral_filenames:
            names = neutral_names(analysis.documents)
            if self.store is not None and self.config.regime is Regime.PSEUDONYMIZATION:
                store_neutral_names(self.store, analysis.batch_id, analysis.documents, names)
        else:
            names = top_level_names(analysis.documents, entities, table)

        jobs: list[tuple[Any, ...]] = []
        for doc in analysis.documents:
            tree = {d.id for d in doc.walk()}
            doc_entities = [e for e in entities if e.document_id in tree]
            # Document-local values (job titles) are only searched in their own file.
            here = {e.cluster_id for e in doc_entities if e.cluster_id in local}
            sensitive = batch_wide + sorted(
                {(v, active[cid].entity_type) for cid in here for v in active[cid].variants}
            )
            jobs.append(
                (
                    _WriteJob(
                        document=doc,
                        entities=doc_entities,
                        table=table,
                        sensitive=sensitive,
                        allowed=allowed,
                        review_clusters=review,
                        config=self.config,
                        output_dir=output_dir,
                        work_dir=analysis.work_dir / f"w-{new_id()[:8]}",
                        names=original_names(doc),
                        structure=structure,
                        output_names={d.id: names[d.id] for d in doc.walk() if d.id in names},
                    ),
                )
            )
        outputs = self._map(_write_and_verify, jobs, "ecriture", progress, cancel, _write_crashed)
        results: list[DocumentResult] = []
        for job, out in zip(jobs, outputs, strict=True):
            doc = job[0].document
            results.append(
                out
                if out is not None
                else DocumentResult(doc.id, "", doc.format, doc.format, DocumentStatus.CANCELLED)
            )
        # What this batch wrote, so that a later run can replace it (``--remplacer``).
        output_record.record(output_dir, [r.output_path for r in results if r.output_path])
        return BatchResult(
            analysis.batch_id,
            output_dir,
            results,
            analysis.started_at,
            regime=self.config.regime,
        )

    def run(
        self,
        inputs: Sequence[Path],
        output_dir: Path,
        progress: ProgressCallback | None = None,
        cancel: CancelToken | None = None,
    ) -> BatchResult:
        analysis = self.analyze(inputs, progress, cancel)
        try:
            return self.apply(analysis, output_dir, None, progress, cancel)
        finally:
            self.close(analysis)

    def close(self, analysis: BatchAnalysis) -> None:
        paths.release(analysis.work_dir)
        shutil.rmtree(analysis.work_dir, ignore_errors=True)


def display_names(analysis: BatchAnalysis, separator: str = " › ") -> dict[str, str]:
    """``document id -> anonymized path`` of every item of an analysis, as a run
    with tags would name it (« [PERSONNE_001]/cv.pdf › piece.txt »): what reports
    and messages may show before anything is written. Neutral names when the
    configuration asks for them."""
    if analysis.config.neutral_filenames:
        names = neutral_names(analysis.documents)
    else:
        config = replace(analysis.config, mode=ReplacementMode.TAG, regime=Regime.ANONYMIZATION)
        clusters = {
            cid: c for cid, c in analysis.clusters.items() if c.review is not ReviewStatus.REJECTED
        }
        entities = [e for e in analysis.entities if e.cluster_id in clusters]
        table = Replacer(config).assign(clusters, entities, analysis.documents)
        names = top_level_names(analysis.documents, entities, table)
        by_doc: dict[str, list[Entity]] = {}
        for e in entities:
            if e.segment_id == FILENAME_SEGMENT_ID:
                by_doc.setdefault(e.document_id, []).append(e)
        for doc in analysis.all_documents():
            if doc.id not in names:
                extension = _output_shape(doc)[1]
                names[doc.id] = anonymized_name(doc, by_doc.get(doc.id, []), table, extension)
    paths: dict[str, str] = {}

    def visit(doc: Document, prefix: str) -> None:
        name = names.get(doc.id, "_")
        path = f"{prefix}{separator}{name}" if prefix else name
        paths[doc.id] = path
        for child in doc.children:
            visit(child, path)

    for doc in analysis.documents:
        visit(doc, "")
    return paths


def resolve_decisions(analysis: BatchAnalysis, decisions: ReviewDecisions) -> ResolvedDecisions:
    """Apply review decisions to an analysis without touching it.

    Pure (no store, no I/O): the GUI uses it for previews, ``apply`` for output.
    """
    config = analysis.config
    clusters = {
        cid: replace(c, variants=set(c.variants), detectors=set(c.detectors))
        for cid, c in analysis.clusters.items()
    }
    for cid, new_type in decisions.retyped_clusters.items():
        if cid in clusters:
            clusters[cid].entity_type = new_type
    for cid in decisions.confirmed_clusters:
        if cid in clusters:
            clusters[cid].review = ReviewStatus.CONFIRMED
    rejected = set(decisions.rejected_clusters)
    for cid in rejected:
        if cid in clusters:
            clusters[cid].review = ReviewStatus.REJECTED

    entities = [
        replace(e, entity_type=clusters[e.cluster_id].entity_type)
        if e.cluster_id in clusters
        else replace(e)
        for e in analysis.entities
        if e.cluster_id not in rejected
    ]
    if decisions.manual:
        manual = manual_entities(analysis, decisions)
        entities, clusters = resolve([*entities, *manual], config, clusters)
        extra = propagate(analysis.documents, clusters, entities, config)
        extra += propagate_names(analysis.documents, clusters, [*entities, *extra])
        entities, clusters = resolve([*entities, *extra], config, clusters)
    for cid in decisions.confirmed_clusters:
        if cid in clusters:
            clusters[cid].needs_review = False

    active = {cid: c for cid, c in clusters.items() if cid not in rejected}
    # Filter single-mention rejections last so propagation cannot bring them back.
    dropped = [e for e in entities if e.key in decisions.rejected_mentions]
    entities = [
        e for e in entities if e.cluster_id in active and e.key not in decisions.rejected_mentions
    ]
    used = {e.cluster_id for e in entities}
    active = {cid: c for cid, c in active.items() if cid in used}
    kept_values = {normalize(e.value) for e in entities}
    allowed = {v for cid in rejected if cid in clusters for v in clusters[cid].variants}
    # A rejected mention stays visible; it is only allowed by the verifier when the
    # same value is not masked elsewhere (otherwise it is a genuine residue).
    allowed |= {e.value for e in dropped if normalize(e.value) not in kept_values}
    entities = drop_inside_whitelist(analysis.documents, entities, config)
    entities, active = apply_policies(analysis.documents, entities, active, config, discover=False)
    return ResolvedDecisions(entities, active, sorted(allowed))


def manual_entities(analysis: BatchAnalysis, decisions: ReviewDecisions) -> list[Entity]:
    """Every occurrence in the batch of the user-added values."""
    found: list[Entity] = []
    for manual in decisions.manual:
        for doc in analysis.all_documents():
            segments = (
                doc.segments
                if doc.status is DocumentStatus.EXTRACTED
                else [s for s in doc.segments if s.id == FILENAME_SEGMENT_ID]
            )
            for seg in segments:
                for start, end in find_all(seg.text, manual.value):
                    found.append(
                        Entity(
                            entity_type=manual.entity_type,
                            value=seg.text[start:end],
                            document_id=doc.id,
                            segment_id=seg.id,
                            start=start,
                            end=end,
                            score=1.0,
                            detector=MANUAL_DETECTOR,
                            review=ReviewStatus.MANUAL,
                        )
                    )
    return found


def drop_inside_whitelist(
    documents: Sequence[Document], entities: list[Entity], config: Config
) -> list[Entity]:
    """Drop entities lying inside an occurrence of a whitelisted value.

    A kept value must stay intact: e.g. a whitelisted e-mail whose local part is
    also a masked person login. Manual entities are kept (explicit user choice)."""
    if not config.whitelist or not entities:
        return entities
    keep_values = [v for v in config.whitelist if v.strip()]
    texts = {(d.id, s.id): s.text for root in documents for d in root.walk() for s in d.segments}
    protected: dict[tuple[str, str], list[tuple[int, int]]] = {}
    for key in {(e.document_id, e.segment_id) for e in entities}:
        text = texts.get(key)
        if text:
            protected[key] = [span for v in keep_values for span in find_all(text, v)]
    return [
        e
        for e in entities
        if e.review is ReviewStatus.MANUAL
        or not any(
            s <= e.start and e.end <= end
            for s, end in protected.get((e.document_id, e.segment_id), [])
        )
    ]


OUTPUT_CONFLICT_MESSAGE = (
    "Le dossier de sortie doit être distinct des fichiers et dossiers d'origine."
)


class OutputDirError(UserError):
    """The output folder overlaps the inputs."""


def quarantine_dir(output_dir: Path) -> Path:
    """Folder receiving the outputs that failed verification: a sibling of the output
    folder (``sortie_quarantaine``), so that sharing the output folder never shares them.
    An output folder at a drive root has no sibling: the quarantine then sits inside."""
    output_dir = Path(output_dir)
    if not output_dir.name:
        return output_dir / QUARANTINE_SUFFIX.lstrip("_")
    return output_dir.with_name(output_dir.name + QUARANTINE_SUFFIX)


def is_quarantined(result: DocumentResult, output_dir: Path) -> bool:
    """Whether ``result``'s output was placed in the quarantine of ``output_dir``."""
    if result.output_path is None:
        return False
    quarantine = quarantine_dir(output_dir)
    return result.output_path == quarantine or quarantine in result.output_path.parents


def validate_output_dir(inputs: Sequence[Path], output_dir: Path) -> None:
    """Refuse an output folder (or its quarantine) that is, contains, or lies inside one
    of the inputs.

    Raises ``OutputDirError`` (usable before any analysis)."""
    output_dir = Path(output_dir).resolve()
    for target in (output_dir, quarantine_dir(output_dir)):
        for raw in inputs:
            root = Path(raw).resolve()
            inside_input = root.is_dir() and (target == root or root in target.parents)
            contains_input = target == root or target in root.parents
            if inside_input or contains_input:
                raise OutputDirError(OUTPUT_CONFLICT_MESSAGE)


__all__ = [
    "OUTPUT_CONFLICT_MESSAGE",
    "QUARANTINE_SUFFIX",
    "BatchAnalysis",
    "BatchResult",
    "CancelToken",
    "OutputDirError",
    "Pipeline",
    "ProgressCallback",
    "ProgressEvent",
    "ResolvedDecisions",
    "collect_inputs",
    "detect_tree",
    "display_names",
    "drop_inside_whitelist",
    "extract_file",
    "is_quarantined",
    "manual_entities",
    "neutral_names",
    "propagate_names",
    "quarantine_dir",
    "resolve_decisions",
    "top_level_names",
    "validate_output_dir",
    "walk_inputs",
]
