"""Shared contracts of the Anonyma pipeline.

Every pipeline stage communicates exclusively through the types defined here:

    Extractor -> Document/Segment -> Detector -> Entity -> resolver -> EntityCluster
    -> replacer -> ReplacementPlan -> Writer -> output file -> verifier -> VerificationResult

Rules that every implementation must respect:

* Sensitive values (segment text, entity values, substitutes) are declared with
  ``repr=False`` so they never leak into logs or tracebacks. Never log them.
* Offsets (``start``/``end``) are Python string indices into ``Segment.text``,
  ``end`` exclusive.
* All types are picklable: documents cross process boundaries.
"""

from __future__ import annotations

import enum
import uuid
from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Protocol, runtime_checkable

if TYPE_CHECKING:
    from PIL import Image

    from anonyma.config import Config


def new_id() -> str:
    return uuid.uuid4().hex


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class Profile(enum.StrEnum):
    """Detection profile, from cheapest to most thorough."""

    LIGHT = "leger"  # regex + Presidio
    STANDARD = "standard"  # + GLiNER
    FULL = "complet"  # + LLM

    @property
    def rank(self) -> int:
        return [Profile.LIGHT, Profile.STANDARD, Profile.FULL].index(self)


class ReplacementMode(enum.StrEnum):
    TAG = "balises"  # [PERSONNE_001]
    REALISTIC = "realiste"  # consistent Faker substitutes
    BLACKOUT = "masquage"  # black boxes (images, PDF); block chars in text formats


class Regime(enum.StrEnum):
    PSEUDONYMIZATION = "pseudonymisation"  # encrypted mapping table kept
    ANONYMIZATION = "anonymisation"  # nothing kept, irreversible


class EntityType(enum.StrEnum):
    """Built-in entity types. Values are the French tag names shown to users.

    ``Entity.entity_type`` is a plain ``str`` so that custom types declared in the
    configuration (e.g. ``PROJET``) are allowed as well.
    """

    PERSON = "PERSONNE"
    ORGANIZATION = "ORGANISATION"
    LOCATION = "LIEU"
    ADDRESS = "ADRESSE"
    JOB_TITLE = "FONCTION"
    EMAIL = "EMAIL"
    PHONE = "TELEPHONE"
    IBAN = "IBAN"
    BIC = "BIC"
    CREDIT_CARD = "CARTE_BANCAIRE"
    NIR = "NIR"
    SIREN = "SIREN"
    SIRET = "SIRET"
    VAT_NUMBER = "TVA"
    LICENSE_PLATE = "PLAQUE"
    POSTAL_CODE = "CODE_POSTAL"
    IP_ADDRESS = "IP"
    URL = "URL"
    DATE_OF_BIRTH = "DATE_NAISSANCE"
    DATE = "DATE"
    CASE_NUMBER = "NUMERO_DOSSIER"
    USERNAME = "IDENTIFIANT"
    MESSAGE_ID = "ID_MESSAGE"
    INDIRECT = "IDENTIFIANT_INDIRECT"
    PROPER_NOUN = "NOM_PROPRE"  # paranoid mode
    SUSPICIOUS_TOKEN = "SEQUENCE_SUSPECTE"  # paranoid mode
    BUSINESS_TERM = "TERME_METIER"  # business lists without explicit type
    OPAQUE_ID = "IDENTIFIANT_TECHNIQUE"  # high-entropy tokens: hashes, UUIDs, transaction ids
    BARCODE = "CODE_BARRES"  # QR code / Data Matrix / barcode: always masked as a whole


#: Direct identifiers: the benchmark requires >= 99 % recall on these.
DIRECT_IDENTIFIERS: frozenset[str] = frozenset(
    {
        EntityType.PERSON,
        EntityType.EMAIL,
        EntityType.PHONE,
        EntityType.IBAN,
        EntityType.CREDIT_CARD,
        EntityType.NIR,
        EntityType.ADDRESS,
        EntityType.LICENSE_PLATE,
        EntityType.IP_ADDRESS,
        EntityType.DATE_OF_BIRTH,
        EntityType.USERNAME,
    }
)


class SegmentKind(enum.StrEnum):
    BODY = "body"  # main running text
    HEADER = "header"  # mail header field, document header/footer
    CELL = "cell"  # spreadsheet / table cell
    METADATA = "metadata"  # document properties, EXIF, XMP...
    OCR = "ocr"  # text recognised in an image region
    ATTRIBUTE = "attribute"  # markup attribute (href, alt, title...)
    COMMENT = "comment"  # comments, annotations, tracked changes
    NOTE = "note"  # speaker notes, footnotes
    FILENAME = "filename"  # file / entry / attachment name (added by the pipeline)
    HIDDEN = "hidden"  # hidden sheets/text, form fields, alt text not otherwise covered
    #: Decoded content of a QR code / barcode. ``context.bbox`` and every char box
    #: are the code's region: the "codes" detector masks the whole segment.
    BARCODE = "barcode"
    #: Text of an embedded digital signature (signer, certificate, timestamps),
    #: exposed for detection only: the writer deletes the signature itself.
    SIGNATURE = "signature"


class DocumentStatus(enum.StrEnum):
    PENDING = "en_attente"
    EXTRACTED = "extrait"
    ANONYMIZED = "anonymise"  # written AND verified with no residue
    NEEDS_REVIEW = "a_revoir"  # verified, but low-confidence entities await review
    RESIDUE = "residu"  # verification found sensitive leftovers
    FAILED = "echec"
    UNSUPPORTED = "non_supporte"
    CANCELLED = "annule"


class Severity(enum.StrEnum):
    INFO = "info"
    WARNING = "avertissement"
    ERROR = "erreur"  # the document cannot be considered anonymized


class ReviewStatus(enum.StrEnum):
    AUTO = "auto"
    CONFIRMED = "confirme"
    REJECTED = "rejete"  # false positive: will not be replaced
    MANUAL = "manuel"  # added by the user


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BBox:
    """Rectangle in page points (PDF) or pixels (images); origin top-left."""

    x0: float
    y0: float
    x1: float
    y1: float

    def union(self, other: BBox) -> BBox:
        return BBox(
            min(self.x0, other.x0),
            min(self.y0, other.y0),
            max(self.x1, other.x1),
            max(self.y1, other.y1),
        )


@dataclass(frozen=True, slots=True)
class SegmentContext:
    """Where a segment comes from. Used by detectors (context) and the GUI."""

    kind: SegmentKind
    #: Human-readable location, e.g. ``"From"``, ``"Feuil1!B3"``, ``"dc:creator"``,
    #: ``"href"``, ``"page 2"``. Must not contain sensitive values.
    label: str = ""
    page: int | None = None  # 0-based page / slide / sheet index when relevant
    bbox: BBox | None = None  # region of the whole segment (OCR, PDF)


@dataclass(frozen=True, slots=True)
class Segment:
    """A unit of text to analyse, with an exact address in the source file."""

    #: Unique within its document. The pipeline reserves ``FILENAME_SEGMENT_ID``.
    id: str
    text: str = field(repr=False)
    #: Opaque address meaningful only to the matching Writer (paragraph index,
    #: XML path, cell coordinate, character offset...). Must be picklable.
    locator: tuple[str | int, ...]
    context: SegmentContext
    #: Optional per-character boxes (``len(char_boxes) == len(text)``) for formats
    #: whose writer redacts visually (PDF, images).
    char_boxes: tuple[BBox, ...] | None = field(default=None, repr=False)

    def span_bbox(self, start: int, end: int) -> BBox | None:
        """Union of the character boxes of ``text[start:end]``, or the segment bbox."""
        if self.char_boxes:
            boxes = self.char_boxes[start:end]
            if boxes:
                result = boxes[0]
                for box in boxes[1:]:
                    result = result.union(box)
                return result
        return self.context.bbox


FILENAME_SEGMENT_ID = "__filename__"
#: Neutral values writers put in place of purged authors/initials (comments,
#: revisions...). Verification tolerates them in outputs.
ANONYMOUS_AUTHOR = "Anonyme"
WRITER_PLACEHOLDERS: frozenset[str] = frozenset({ANONYMOUS_AUTHOR, "A", "Unknown"})
#: Format id of a folder output re-read by the pipeline for verification.
FOLDER_FORMAT = "dossier"


@dataclass(frozen=True, slots=True)
class Issue:
    """A problem worth reporting. ``message`` is French, user-facing and must
    never contain a sensitive value."""

    severity: Severity
    code: str  # stable machine code, e.g. "extract.encrypted", "archive.bomb"
    message: str


@dataclass(slots=True)
class Document:
    """A file (or an embedded item of a container) turned into segments."""

    #: Readable file holding the original bytes. For embedded items, a copy in the
    #: batch temporary directory. Never modified.
    source: Path
    #: Name of the item: path relative to the input root for top-level files
    #: (``"Clients/Dupont/cv.pdf"``), entry/attachment name for children.
    #: Always uses ``/`` as separator. Sensitive: it is anonymized like any text.
    name: str = field(repr=False)
    format: str  # format id, e.g. "docx", "eml", "zip"
    id: str = field(default_factory=new_id)
    segments: list[Segment] = field(default_factory=list, repr=False)
    children: list[Document] = field(default_factory=list, repr=False)
    depth: int = 0
    parent_id: str | None = None
    extractor: str = ""
    status: DocumentStatus = DocumentStatus.PENDING
    issues: list[Issue] = field(default_factory=list)
    #: Extractor-private data the matching Writer needs (picklable). Never logged.
    properties: dict[str, Any] = field(default_factory=dict, repr=False)

    def walk(self) -> Iterator[Document]:
        """This document then all descendants, depth-first."""
        yield self
        for child in self.children:
            yield from child.walk()

    def segment(self, segment_id: str) -> Segment:
        for seg in self.segments:
            if seg.id == segment_id:
                return seg
        raise KeyError(segment_id)

    def add_issue(self, severity: Severity, code: str, message: str) -> None:
        self.issues.append(Issue(severity, code, message))

    @property
    def has_errors(self) -> bool:
        return any(i.severity is Severity.ERROR for i in self.issues)


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Entity:
    """A sensitive span found by a detector."""

    entity_type: str
    value: str = field(repr=False)  # == segment.text[start:end]
    document_id: str
    segment_id: str
    start: int
    end: int
    score: float  # 0..1
    detector: str
    cluster_id: str | None = None  # set by the resolver
    review: ReviewStatus = ReviewStatus.AUTO
    #: Optional hints for the replacer (e.g. {"gender": "f", "role": "given_name"}).
    attributes: dict[str, str] = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, str, int, int]:
        return (self.document_id, self.segment_id, self.start, self.end)


@dataclass(slots=True)
class EntityCluster:
    """All mentions of the same real-world entity across the whole batch."""

    id: str
    entity_type: str
    canonical: str = field(repr=False)  # most complete surface form
    variants: set[str] = field(default_factory=set, repr=False)  # every surface form
    max_score: float = 0.0
    detectors: set[str] = field(default_factory=set)
    attributes: dict[str, str] = field(default_factory=dict)
    needs_review: bool = False
    review: ReviewStatus = ReviewStatus.AUTO


@dataclass(frozen=True, slots=True)
class ManualEntity:
    """An entity added by the user during review; propagated to the whole batch."""

    value: str = field(repr=False)
    entity_type: str


@dataclass(slots=True)
class ReviewDecisions:
    """User corrections applied between ``analyze`` and ``apply``."""

    rejected_clusters: set[str] = field(default_factory=set)
    confirmed_clusters: set[str] = field(default_factory=set)
    retyped_clusters: dict[str, str] = field(default_factory=dict)
    #: Individual mentions to drop (``Entity.key``) without rejecting the cluster.
    rejected_mentions: set[tuple[str, str, int, int]] = field(default_factory=set)
    manual: list[ManualEntity] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Replacement
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Replacement:
    start: int
    end: int
    original: str = field(repr=False)
    substitute: str = field(repr=False)
    entity_type: str
    cluster_id: str


@dataclass(slots=True)
class ReplacementPlan:
    """Replacements for one document, per segment, sorted and non-overlapping."""

    document_id: str
    by_segment: dict[str, list[Replacement]] = field(default_factory=dict, repr=False)

    def for_segment(self, segment_id: str) -> list[Replacement]:
        return self.by_segment.get(segment_id, [])

    def apply(self, segment: Segment) -> str:
        return apply_replacements(segment.text, self.for_segment(segment.id))

    def __len__(self) -> int:
        return sum(len(v) for v in self.by_segment.values())


def apply_replacements(text: str, replacements: Sequence[Replacement]) -> str:
    """Return ``text`` with the given sorted, non-overlapping replacements applied."""
    out: list[str] = []
    cursor = 0
    for rep in replacements:
        if rep.start < cursor or rep.end > len(text) or rep.start > rep.end:
            raise ValueError("replacements must be sorted, in range and non-overlapping")
        out.append(text[cursor : rep.start])
        out.append(rep.substitute)
        cursor = rep.end
    out.append(text[cursor:])
    return "".join(out)


# ---------------------------------------------------------------------------
# Verification and results
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Residue:
    """Sensitive content still present in an output file."""

    #: Logical location inside the output (anonymized names only), e.g.
    #: ``"archive.zip/[PERSONNE_001].pdf"`` + segment label.
    location: str
    segment_label: str
    entity_type: str
    reason: str  # "original_value" (matched the batch table) or "detector"
    detector: str
    value: str = field(repr=False)
    #: True: original value found again, or a deterministic detector hit (file goes
    #: to quarantine). False: statistical detector hit only (file sent to review).
    certain: bool = True


@dataclass(slots=True)
class VerificationResult:
    #: False when verification could not run (e.g. output not re-extractable):
    #: the document must then never be marked as anonymized.
    verified: bool
    residues: list[Residue] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    #: Images/pages of the output that could not be read back (OCR disabled,
    #: unavailable or failed): their content was never checked. A result with
    #: ``unread`` is never marked anonymized (review + quarantine).
    unread: int = 0

    @property
    def complete(self) -> bool:
        """Every image and page of the output was read back."""
        return self.unread == 0

    @property
    def certain_residues(self) -> list[Residue]:
        return [r for r in self.residues if r.certain]

    @property
    def possible_residues(self) -> list[Residue]:
        return [r for r in self.residues if not r.certain]

    @property
    def clean(self) -> bool:
        """Verified and no certain residue (possible ones only require review)."""
        return self.verified and not self.certain_residues


@dataclass(slots=True)
class DocumentResult:
    document_id: str
    #: Anonymized display name (safe for reports).
    output_name: str
    format: str
    output_format: str
    status: DocumentStatus
    output_path: Path | None = None
    entity_counts: dict[str, int] = field(default_factory=dict)
    residue_count: int = 0  # certain residues
    possible_residue_count: int = 0
    review_count: int = 0
    issues: list[Issue] = field(default_factory=list)
    children: list[DocumentResult] = field(default_factory=list)
    verification: VerificationResult | None = None


# ---------------------------------------------------------------------------
# Service protocols (implemented outside models.py)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OcrLine:
    text: str = field(repr=False)
    bbox: BBox
    score: float
    char_boxes: tuple[BBox, ...] | None = field(default=None, repr=False)


@runtime_checkable
class OcrEngine(Protocol):
    def recognize(self, image: Image.Image) -> list[OcrLine]:
        """Recognise text lines in a PIL image; coordinates in image pixels."""
        ...


@runtime_checkable
class MappingStore(Protocol):
    """Persistent substitute table (pseudonymization regime only).

    ``key`` is the normalized value computed by the resolver; implementations must
    encrypt originals at rest.
    """

    def lookup(self, entity_type: str, key: str) -> str | None: ...

    def save(self, entity_type: str, key: str, original: str, substitute: str) -> None: ...

    def reverse(self, substitute: str) -> str | None: ...

    def used_substitutes(self) -> set[str]: ...

    def save_filename(self, batch_id: str, output_name: str, original: str) -> None:
        """Keep the original path behind a neutral output path."""
        ...


class UserError(ValueError):
    """An error whose message is French, value-free and meant to be shown as is."""


class ArchiveBombError(Exception):
    """Raised when a container exceeds the expansion budget."""


@dataclass(slots=True)
class ExpansionBudget:
    """Shared expansion budget for one top-level file and all its descendants.

    Containers must call :meth:`consume` *before* materialising each child, with the
    declared uncompressed size, and again with the actual size if it differs.
    """

    max_total_bytes: int
    max_files: int
    max_ratio: float
    max_depth: int
    used_bytes: int = 0
    used_files: int = 0

    def consume(self, nbytes: int, *, compressed: int | None = None, depth: int = 0) -> None:
        if depth > self.max_depth:
            raise ArchiveBombError("profondeur d'imbrication excessive")
        if compressed is not None and compressed > 0 and nbytes / compressed > self.max_ratio:
            raise ArchiveBombError("taux de compression suspect")
        self.used_bytes += max(nbytes, 0)
        self.used_files += 1
        if self.used_bytes > self.max_total_bytes:
            raise ArchiveBombError("volume décompressé excessif")
        if self.used_files > self.max_files:
            raise ArchiveBombError("nombre de fichiers excessif")


class ExtractionContext(ABC):
    """Services offered by the pipeline to extractors."""

    config: Config
    depth: int
    budget: ExpansionBudget

    @property
    @abstractmethod
    def ocr(self) -> OcrEngine | None:
        """Shared OCR engine (loaded lazily), or None when disabled/unavailable."""

    @abstractmethod
    def extract_child(self, data: bytes | Path, name: str, parent: Document) -> Document:
        """Extract an embedded item recursively.

        Never raises for child problems: returns a Document whose status is FAILED or
        UNSUPPORTED with explanatory issues. Raises :class:`ArchiveBombError` when the
        budget is exhausted (the container must then fail as a whole).
        """

    @abstractmethod
    def temp_path(self, suffix: str = "") -> Path:
        """A fresh path inside the batch temporary directory (file not created)."""

    @abstractmethod
    def check_cancelled(self) -> None:
        """Raise ``CancelledError`` if the user cancelled the batch."""


@dataclass(frozen=True, slots=True)
class ChildOutput:
    """Result of writing a child, handed to the container writer."""

    document_id: str
    #: Anonymized output file, or None if the child could not be anonymized. The
    #: container writer must then OMIT the child (never embed original bytes).
    path: Path | None
    name: str = field(repr=False)  # anonymized entry/attachment name


class WriteContext(ABC):
    config: Config
    mode: ReplacementMode
    child_outputs: Mapping[str, ChildOutput]

    @abstractmethod
    def temp_path(self, suffix: str = "") -> Path: ...


# ---------------------------------------------------------------------------
# Plugin interfaces
# ---------------------------------------------------------------------------


class Extractor(ABC):
    """Turns a file into a :class:`Document`. One subclass per format family.

    Register with ``@register_extractor`` (``anonyma.registry``); any module placed
    in ``anonyma/extractors/`` is imported automatically.
    """

    name: ClassVar[str]
    #: Lower-case extension (with dot) -> format id.
    extensions: ClassVar[Mapping[str, str]]

    @classmethod
    def sniff(cls, head: bytes) -> str | None:
        """Return a format id if ``head`` (first 8 KiB) identifies a handled format.

        Only implement for unambiguous magic bytes; content sniffing wins over the
        extension.
        """
        return None

    @abstractmethod
    def extract(self, source: Path, document: Document, ctx: ExtractionContext) -> None:
        """Fill ``document.segments``/``children``/``properties`` from ``source``.

        ``document`` is pre-built by the pipeline (id, name, format, depth). Raise on
        fatal errors; record non-fatal ones with ``document.add_issue``. Segments
        must cover *all* text of the file, metadata included.
        """


class Writer(ABC):
    """Re-injects substitutes into a copy of the original file and purges metadata.

    Register with ``@register_writer``.
    """

    name: ClassVar[str]
    formats: ClassVar[frozenset[str]]
    #: When True, ``write`` receives a folder path (not yet created) that it creates
    #: and fills (e.g. a PST restored as a tree of eml files). Below the top level the
    #: pipeline packs that folder into a ``.zip`` so that containers get one file.
    #: ``output_format`` should then return ``FOLDER_FORMAT``.
    directory_output: ClassVar[bool] = False

    def output_extension(self, document: Document) -> str:
        """Extension (with dot) of the produced file. Override when the format is
        converted (e.g. ``doc`` -> ``.docx``)."""
        return Path(document.name).suffix

    def output_format(self, document: Document) -> str:
        """Format id of the produced file, used to re-extract it for verification."""
        return document.format

    @abstractmethod
    def write(
        self,
        document: Document,
        plan: ReplacementPlan,
        destination: Path,
        ctx: WriteContext,
    ) -> list[Issue]:
        """Write the anonymized file to ``destination`` (never touch ``source``).

        Must purge metadata (author, properties, EXIF, comments, revisions, history).
        Raise on failure.
        """


class Detector(ABC):
    """Finds entities in segments. Register with ``@register_detector``."""

    name: ClassVar[str]
    #: Lowest profile in which this detector runs.
    min_profile: ClassVar[Profile] = Profile.LIGHT
    #: False for statistical detectors (ML models): their hits on outputs are
    #: reported as possible residues (review) rather than certain ones (quarantine).
    deterministic: ClassVar[bool] = True
    #: False when the verifier must not run it again on outputs (slow LLM).
    verify: ClassVar[bool] = True

    def __init__(self, config: Config) -> None:
        self.config = config

    @classmethod
    def is_available(cls, config: Config) -> bool:
        """False when a dependency or model is missing (detector skipped, reported)."""
        return True

    @abstractmethod
    def detect(self, segments: Sequence[Segment], document_id: str) -> list[Entity]:
        """Return entities found in ``segments``; offsets relative to each segment."""
