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
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from anonyma import paths, registry
from anonyma.config import Config
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
    new_id,
)
from anonyma.policy import apply_policies
from anonyma.replacer import Replacer, SubstitutionTable, build_plan
from anonyma.resolver import (
    DOCUMENT_LOCAL_TYPES,
    PROPAGATION_DETECTOR,
    TYPE_PRIORITY,
    cluster_forms,
    propagate,
    resolve,
)
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
        for detector in detectors:
            try:
                found = detector.detect(view, doc.id)
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
) -> tuple[Document, list[Entity]]:
    """Extract and detect one input; a ``skipped`` input is not read, only its name
    is analysed (so that it shows up anonymized in reports). ``cancel``: inline runs
    only (a token does not cross processes), checked by containers between items."""
    doc = skipped_document(source, name, skipped) if skipped else None
    doc = doc or extract_file(source, name, config, work_dir, cancel=cancel)
    return doc, detect_tree(doc, config)


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
    #: Original names of the tree (searched in the output by the verifier).
    names: list[str] = field(default_factory=list)
    #: ``document id -> neutral name`` when ``config.neutral_filenames``.
    neutral_names: dict[str, str] = field(default_factory=dict)


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
    name = job.neutral_names.get(doc.id) or anonymized_name(doc, entities, job.table, extension)
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

    # Top-level outputs too: they only reach the output folder once verified.
    destination = job.work_dir / new_id() / PurePosixPath(name).name
    target = destination.with_name(new_id()) if pack_folder else destination
    plan = build_plan(doc, entities, job.table)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        result.issues.extend(
            writer.write(doc, plan, target, _WriteContext(job.config, child_outputs, job.work_dir))
        )
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
                f"Échec de l'écriture du fichier anonymisé ({type(exc).__name__}).",
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
                f"La vérification a échoué ({type(exc).__name__}).",
            )
        )
        return result
    _set_status(result, status)
    return result


def _write_crashed(job: _WriteJob) -> DocumentResult:
    """A document whose writing crashed its worker twice: failed, nothing written."""
    doc = job.document
    _, extension, _ = _output_shape(doc)
    entities = [e for e in job.entities if e.document_id == doc.id]
    name = job.neutral_names.get(doc.id) or anonymized_name(doc, entities, job.table, extension)
    return DocumentResult(
        document_id=doc.id,
        output_name=name,
        format=doc.format,
        output_format=doc.format,
        status=DocumentStatus.FAILED,
        entity_counts=_counts(entities),
        issues=[Issue(Severity.ERROR, "worker.crash", CRASH_MESSAGE)],
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
    quarantine = quarantine_dir(job.output_dir)
    if not verification.clean:
        if verification.verified:
            result.issues.append(
                Issue(
                    Severity.ERROR,
                    "verify.residue",
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
                verifier.verify_raw(output_doc, job.sensitive, substitutes, job.allowed)
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
            if self.config.profile is Profile.STANDARD:
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
        item that crashes that pool too gets ``crashed(*job)`` as its result."""
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
        if self._workers(total) == 1:
            for i, job in enumerate(jobs):
                if cancel and cancel.cancelled:
                    break
                try:
                    done(i, fn(*job))
                except CancelledError:  # raised inside the item (inline extraction)
                    break
            return results
        try:
            self._pool(fn, jobs, list(range(total)), self._workers(total), cancel, done)
        except BrokenProcessPool:
            log.warning("worker pool broken during %s; retrying the remaining items", stage)
            while not (cancel and cancel.cancelled):
                pending = [i for i in range(total) if i not in finished]
                culprit = self._serial(fn, jobs, pending, cancel, done) if pending else None
                if culprit is None:
                    break
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
        with ProcessPoolExecutor(1, initializer=_worker_init) as pool:
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
        with ProcessPoolExecutor(workers, initializer=_worker_init) as pool:
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

    def _analysis_crashed(
        self, source: Path, name: str, config: Config, work_dir: Path, *rest: Any
    ) -> tuple[Document, list[Entity]]:
        """An input that crashed its worker twice: only its name is analysed (in a
        worker as well), so that it is reported anonymized."""
        job = (source, name, config, work_dir, SKIP_CRASH)
        try:
            with ProcessPoolExecutor(1, initializer=_worker_init) as pool:
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
        work_dir.mkdir(parents=True, exist_ok=True)
        files, links = walk_inputs(inputs)
        if links:
            log.warning("%d link(s) skipped in input folders", len(links))
        items = [*files, *links]
        inline = cancel if self._workers(len(items)) == 1 else None
        jobs = [
            (
                path,
                name,
                self.config,
                work_dir / f"f{i}",
                SKIP_LINK if i >= len(files) else "",
                inline,
            )
            for i, (path, name) in enumerate(items)
        ]
        outputs = self._map(_analyze_one, jobs, "analyse", progress, cancel, self._analysis_crashed)

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
        neutral: dict[str, str] = {}
        if self.config.neutral_filenames:
            neutral = neutral_names(analysis.documents)
            if self.store is not None and self.config.regime is Regime.PSEUDONYMIZATION:
                store_neutral_names(self.store, analysis.batch_id, analysis.documents, neutral)

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
                        neutral_names={d.id: neutral[d.id] for d in doc.walk() if d.id in neutral},
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
    "drop_inside_whitelist",
    "extract_file",
    "is_quarantined",
    "manual_entities",
    "neutral_names",
    "propagate_names",
    "quarantine_dir",
    "resolve_decisions",
    "validate_output_dir",
    "walk_inputs",
]
