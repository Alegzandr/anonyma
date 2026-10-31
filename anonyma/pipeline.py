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

import os
import shutil
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import CancelledError, Future, ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from anonyma import paths, registry
from anonyma.config import Config
from anonyma.logs import get_logger
from anonyma.models import (
    FILENAME_SEGMENT_ID,
    ArchiveBombError,
    ChildOutput,
    Detector,
    Document,
    DocumentResult,
    DocumentStatus,
    Entity,
    EntityCluster,
    ExpansionBudget,
    ExtractionContext,
    Issue,
    MappingStore,
    OcrEngine,
    Profile,
    ReviewDecisions,
    ReviewStatus,
    Segment,
    SegmentContext,
    SegmentKind,
    Severity,
    VerificationResult,
    WriteContext,
    new_id,
)
from anonyma.replacer import Replacer, SubstitutionTable, build_plan
from anonyma.resolver import propagate, resolve
from anonyma.textutils import find_all, normalize, safe_filename
from anonyma.verifier import Verifier

log = get_logger("pipeline")

QUARANTINE_DIR = "_quarantaine"
MANUAL_DETECTOR = "manuel"


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    stage: str  # "analyse" | "ecriture"
    done: int
    total: int
    message: str = ""  # French, never sensitive
    #: Index of the item just finished, in input order (``BatchAnalysis.documents``
    #: order); None for start/global events.
    index: int | None = None


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

    def entities_for(self, document_id: str) -> list[Entity]:
        return [e for e in self.entities if e.document_id == document_id]

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


def collect_inputs(inputs: Sequence[Path]) -> list[tuple[Path, str]]:
    """Expand files and folders into ``(path, logical name)``; folders keep their
    own name as first component. Symlinked folders are not followed."""
    items: list[tuple[Path, str]] = []
    for raw in inputs:
        path = Path(raw)
        if path.is_dir():
            base = path.resolve().parent
            for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
                dirnames.sort()
                for filename in sorted(filenames):
                    file = Path(dirpath) / filename
                    if file.is_file():
                        items.append((file, file.resolve().relative_to(base).as_posix()))
        elif path.is_file():
            items.append((path, path.name))
    return items


# ---------------------------------------------------------------------------
# Per-process caches (worker processes)
# ---------------------------------------------------------------------------

_detector_cache: dict[str, list[Detector]] = {}
_ocr_cache: dict[str, OcrEngine | None] = {}


def _worker_init() -> None:
    from anonyma import netguard

    paths.configure_environment()
    netguard.install()


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
    suffix = PurePosixPath(name.replace("\\", "/")).suffix.lower()
    return suffix if suffix.isascii() and len(suffix) <= 10 else ""


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
            f"Échec de la lecture du fichier ({type(exc).__name__}).",
        )
        return
    doc.status = DocumentStatus.FAILED if doc.has_errors else DocumentStatus.EXTRACTED


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def detect_tree(root: Document, config: Config) -> list[Entity]:
    """Run the detector chain on a document and all its descendants."""
    entities: list[Entity] = []
    detectors = detectors_for(config)
    for doc in root.walk():
        if doc.status is not DocumentStatus.EXTRACTED:
            continue
        texts = {s.id: s.text for s in doc.segments}
        for detector in detectors:
            try:
                found = detector.detect(doc.segments, doc.id)
            except Exception as exc:
                log.warning(
                    "detector %s failed on %s: %s", detector.name, doc.id, type(exc).__name__
                )
                doc.status = DocumentStatus.FAILED
                doc.add_issue(
                    Severity.ERROR,
                    "detect.error",
                    f"Le détecteur « {detector.name} » a échoué : détection incomplète.",
                )
                break
            for e in found:
                text = texts.get(e.segment_id)
                if text is None or not 0 <= e.start < e.end <= len(text):
                    continue
                if e.score < config.min_score:
                    continue
                e.value = text[e.start : e.end]
                e.document_id = doc.id
                entities.append(e)
    return entities


def _analyze_one(
    source: Path, name: str, config: Config, work_dir: Path
) -> tuple[Document, list[Entity]]:
    doc = extract_file(source, name, config, work_dir)
    return doc, detect_tree(doc, config)


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
    """Apply the plan to the FILENAME segment, sanitize, and set the extension."""
    plan = build_plan(doc, [e for e in entities if e.segment_id == FILENAME_SEGMENT_ID], table)
    raw = plan.apply(doc.segment(FILENAME_SEGMENT_ID)).replace("\\", "/")
    parts = [safe_filename(p) for p in raw.split("/") if p not in ("", ".", "..")] or ["_"]
    stem = PurePosixPath(parts[-1])
    parts[-1] = safe_filename((stem.stem if stem.suffix else stem.name) + extension)
    return "/".join(parts)


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


def _counts(entities: Sequence[Entity]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for e in entities:
        counts[e.entity_type] = counts.get(e.entity_type, 0) + 1
    return counts


def _write_tree(
    doc: Document, job: _WriteJob, by_doc: Mapping[str, list[Entity]]
) -> tuple[DocumentResult, Path | None]:
    """Write ``doc`` (children first). Top-level goes to the output folder, children
    to the work folder. Returns the result and the written path."""
    entities = by_doc.get(doc.id, [])
    writer = registry.writer_for(doc.format) if doc.status is DocumentStatus.EXTRACTED else None
    extension = writer.output_extension(doc) if writer else PurePosixPath(doc.name).suffix
    name = anonymized_name(doc, entities, job.table, extension)
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
                    "child.omitted",
                    f"Élément « {child_result.output_name} » omis : il n'a pas pu être anonymisé.",
                )
            )

    if doc.depth == 0:
        destination = _unique_path(job.output_dir / name)
    else:
        destination = job.work_dir / new_id() / PurePosixPath(name).name
    destination.parent.mkdir(parents=True, exist_ok=True)
    plan = build_plan(doc, entities, job.table)
    try:
        result.issues.extend(
            writer.write(
                doc, plan, destination, _WriteContext(job.config, child_outputs, job.work_dir)
            )
        )
    except Exception as exc:
        log.warning("write failed for %s: %s", doc.id, type(exc).__name__)
        destination.unlink(missing_ok=True)
        result.status = DocumentStatus.FAILED
        result.issues.append(
            Issue(
                Severity.ERROR,
                "write.error",
                f"Échec de l'écriture du fichier anonymisé ({type(exc).__name__}).",
            )
        )
        return result, None
    if any(i.severity is Severity.ERROR for i in result.issues):
        destination.unlink(missing_ok=True)
        result.status = DocumentStatus.FAILED
        return result, None
    result.output_path = destination
    return result, destination


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for i in range(2, 100_000):
        candidate = path.with_name(f"{path.stem} ({i}){path.suffix}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(path)


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

    verification = _verify_output(written, result.output_name, job)
    result.verification = verification
    result.residue_count = len(verification.residues)
    result.issues.extend(verification.issues)
    tree_ids = {d.id for d in job.document.walk()}
    result.review_count = len(
        {e.cluster_id for e in job.entities if e.document_id in tree_ids} & job.review_clusters
    )
    if verification.clean:
        _set_status(
            result,
            DocumentStatus.NEEDS_REVIEW if result.review_count else DocumentStatus.ANONYMIZED,
        )
        return result

    quarantined = _unique_path(job.output_dir / QUARANTINE_DIR / result.output_name)
    quarantined.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(written, quarantined)
    result.output_path = quarantined
    if verification.verified:
        _set_status(result, DocumentStatus.RESIDUE)
        result.status = DocumentStatus.RESIDUE
        result.issues.append(
            Issue(
                Severity.ERROR,
                "verify.residue",
                f"{len(verification.residues)} résidu(s) détecté(s) : fichier placé en "
                "quarantaine.",
            )
        )
    else:
        _set_status(result, DocumentStatus.FAILED)
        result.status = DocumentStatus.FAILED
        result.issues.append(
            Issue(
                Severity.ERROR,
                "verify.failed",
                "Vérification impossible : fichier placé en quarantaine, non anonymisé.",
            )
        )
    return result


def _verify_output(path: Path, name: str, job: _WriteJob) -> VerificationResult:
    verify_dir = job.work_dir / f"verify-{new_id()}"
    try:
        output_doc = extract_file(path, name, job.config, verify_dir)
        verifier = Verifier(job.config, detectors_for(job.config))
        return verifier.verify(output_doc, job.sensitive, job.table.all_substitutes(), job.allowed)
    except Exception as exc:
        log.warning("verification failed for %s: %s", job.document.id, type(exc).__name__)
        return VerificationResult(
            verified=False,
            issues=[
                Issue(
                    Severity.ERROR,
                    "verify.error",
                    f"La vérification a échoué ({type(exc).__name__}).",
                )
            ],
        )
    finally:
        shutil.rmtree(verify_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


class Pipeline:
    def __init__(self, config: Config, *, store: MappingStore | None = None) -> None:
        profile: Profile = config.effective_profile()
        # Freeze the profile so that worker processes never re-detect hardware.
        self.config = replace(config, profile=profile)
        self.store = store
        registry.load_plugins()

    # -- execution helpers --------------------------------------------------

    def _workers(self, jobs: int) -> int:
        if self.config.workers:
            wanted = self.config.workers
        elif self.config.profile is Profile.FULL:
            wanted = 1  # one LLM instance at a time
        else:
            wanted = max(1, min(os.cpu_count() or 1, 8) - 1)
        return max(1, min(wanted, jobs))

    def _map(
        self,
        fn: Callable[..., Any],
        jobs: list[tuple[Any, ...]],
        stage: str,
        progress: ProgressCallback | None,
        cancel: CancelToken | None,
    ) -> list[Any | None]:
        """Run ``fn(*job)`` for each job; ``None`` for jobs skipped by cancellation."""
        results: list[Any | None] = [None] * len(jobs)
        total = len(jobs)
        if progress:
            progress(ProgressEvent(stage, 0, total))
        if self._workers(total) == 1:
            for i, job in enumerate(jobs):
                if cancel and cancel.cancelled:
                    break
                results[i] = fn(*job)
                if progress:
                    progress(ProgressEvent(stage, i + 1, total, index=i))
            return results
        with ProcessPoolExecutor(self._workers(total), initializer=_worker_init) as pool:
            futures: dict[Future[Any], int] = {
                pool.submit(fn, *job): i for i, job in enumerate(jobs)
            }
            for done, future in enumerate(as_completed(futures), start=1):
                if cancel and cancel.cancelled:
                    for f in futures:
                        f.cancel()
                    break
                results[futures[future]] = future.result()
                if progress:
                    progress(ProgressEvent(stage, done, total, index=futures[future]))
        return results

    # -- public API ----------------------------------------------------------

    def analyze(
        self,
        inputs: Sequence[Path],
        progress: ProgressCallback | None = None,
        cancel: CancelToken | None = None,
    ) -> BatchAnalysis:
        batch_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + new_id()[:6]
        work_dir = paths.tmp_dir() / batch_id
        work_dir.mkdir(parents=True, exist_ok=True)
        items = collect_inputs(inputs)
        jobs = [
            (path, name, self.config, work_dir / f"f{i}") for i, (path, name) in enumerate(items)
        ]
        outputs = self._map(_analyze_one, jobs, "analyse", progress, cancel)

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
        entities, clusters = resolve([*entities, *extra], self.config, clusters)
        return BatchAnalysis(
            batch_id,
            self.config,
            documents,
            entities,
            clusters,
            work_dir,
            inputs=[Path(p).resolve() for p in inputs],
        )

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
        self.check_output_dir(analysis, output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        resolved = self.resolve_decisions(analysis, decisions)
        entities, active = resolved.entities, resolved.clusters
        table = Replacer(self.config, self.store).assign(active, entities)
        sensitive = sorted({(v, c.entity_type) for c in active.values() for v in c.variants})
        allowed = resolved.allowed
        review = {cid for cid, c in active.items() if c.needs_review}

        jobs: list[tuple[Any, ...]] = []
        for doc in analysis.documents:
            tree = {d.id for d in doc.walk()}
            jobs.append(
                (
                    _WriteJob(
                        document=doc,
                        entities=[e for e in entities if e.document_id in tree],
                        table=table,
                        sensitive=sensitive,
                        allowed=allowed,
                        review_clusters=review,
                        config=self.config,
                        output_dir=output_dir,
                        work_dir=analysis.work_dir / f"w-{new_id()[:8]}",
                    ),
                )
            )
        outputs = self._map(_write_and_verify, jobs, "ecriture", progress, cancel)
        results: list[DocumentResult] = []
        for job, out in zip(jobs, outputs, strict=True):
            doc = job[0].document
            results.append(
                out
                if out is not None
                else DocumentResult(doc.id, "", doc.format, doc.format, DocumentStatus.CANCELLED)
            )
        return BatchResult(analysis.batch_id, output_dir, results, analysis.started_at)

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
        shutil.rmtree(analysis.work_dir, ignore_errors=True)

    def resolve_decisions(
        self, analysis: BatchAnalysis, decisions: ReviewDecisions
    ) -> ResolvedDecisions:
        return resolve_decisions(analysis, decisions)

    def manual_entities(self, analysis: BatchAnalysis, decisions: ReviewDecisions) -> list[Entity]:
        return manual_entities(analysis, decisions)

    @staticmethod
    def check_output_dir(analysis: BatchAnalysis, output_dir: Path) -> None:
        validate_output_dir([*analysis.inputs, *(d.source for d in analysis.documents)], output_dir)


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
    return ResolvedDecisions(entities, active, sorted(allowed))


def manual_entities(analysis: BatchAnalysis, decisions: ReviewDecisions) -> list[Entity]:
    """Every occurrence in the batch of the user-added values."""
    found: list[Entity] = []
    for manual in decisions.manual:
        for doc in analysis.all_documents():
            if doc.status is not DocumentStatus.EXTRACTED:
                continue
            for seg in doc.segments:
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


OUTPUT_CONFLICT_MESSAGE = (
    "Le dossier de sortie doit être distinct des fichiers et dossiers d'origine."
)


def validate_output_dir(inputs: Sequence[Path], output_dir: Path) -> None:
    """Refuse an output folder that is, contains, or lies inside one of the inputs.

    Raises ``ValueError`` with a French message (usable before any analysis)."""
    output_dir = Path(output_dir).resolve()
    for raw in inputs:
        root = Path(raw).resolve()
        inside_input = root.is_dir() and (output_dir == root or root in output_dir.parents)
        contains_input = output_dir == root or output_dir in root.parents
        if inside_input or contains_input:
            raise ValueError(OUTPUT_CONFLICT_MESSAGE)


__all__ = [
    "OUTPUT_CONFLICT_MESSAGE",
    "QUARANTINE_DIR",
    "BatchAnalysis",
    "BatchResult",
    "CancelToken",
    "Pipeline",
    "ProgressCallback",
    "ProgressEvent",
    "ResolvedDecisions",
    "collect_inputs",
    "detect_tree",
    "extract_file",
    "manual_entities",
    "resolve_decisions",
    "validate_output_dir",
]
