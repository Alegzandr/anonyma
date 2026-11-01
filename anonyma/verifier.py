"""Verification: rescan an output with every detector and against the batch table.

Two independent checks on every segment of the re-extracted output tree:

1. **Original values**: every sensitive value of the batch, plus its derived forms
   (``Dupont Jean``, ``jdupont``, phone numbers in other formats...), searched in one
   Aho-Corasick pass, accent/case-insensitively, with separator-aware boundaries.
   Residue reason ``original_value``, detector ``table`` (exact value) or
   ``table_variante`` (derived form).
2. **Detectors**: every detector of the profile is run again. A hit is ignored when
   it is an allowed value, scores below ``min_score``, or is covered by substitutes
   (what remains outside them is only punctuation, civilities or stop words).
   Residue reason ``detector``, detector = its name.
3. **OCR forms**: in text read by OCR, the same values are also searched in a
   "skeleton" form where characters OCR confuses are merged (``0``/``O``,
   ``1``/``l``/``I``, ``5``/``S``...) and hyphens, underscores and dots count as
   spaces (detector ``table_ocr``).
4. **Original names**: the names of the input file and of every embedded item (each
   path component and its stem) must not survive anywhere in the output (detector
   ``nom_origine``, certain), unless they are still part of the output names (then
   nothing was hidden in them). Their distinctive words (not common words, not in
   the output names) are residues too: certain when capitalized like a proper name
   (« Quintavalle »), possible otherwise.

5. **Raw bytes** (:meth:`Verifier.verify_raw`, :mod:`anonyma.rawscan`): the written
   file itself, decoded every way that matters (encodings, character references,
   MIME transfer encodings, archive members, compound-file and PDF streams), is
   searched for the identifying values of the batch: a backstop for anything an
   extractor would not expose (detector ``octets_bruts``, certain).

Every value and detector hit kept by the organization policy (``policy.keeps``:
whitelisted publishers, domains, public URLs) is ignored.

Incomplete verification: a re-extracted item carrying an OCR issue (OCR disabled,
unavailable or failed on an image or page) is still scanned, but counted in
``VerificationResult.unread``: the pipeline never marks such an output anonymized.

Short-value policy: values shorter than 3 characters (after normalisation) and
numbers shorter than 4 digits are not searched; single words that are also common
words (``Petit``, ``Rose``) only count when capitalized.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import PurePosixPath

from anonyma import checksums
from anonyma.atomic import AtomicIndex
from anonyma.config import Config
from anonyma.data import is_common_word
from anonyma.detectors._custom import STOP_WORDS
from anonyma.detectors.opaque import IdWhitelist
from anonyma.detectors.regex_fr import kept_reference
from anonyma.models import (
    Detector,
    Document,
    DocumentStatus,
    EntityCluster,
    EntityType,
    Issue,
    Residue,
    Segment,
    SegmentKind,
    Severity,
    VerificationResult,
)
from anonyma.names import PARTICLES, ROLE_WORDS, civility_of, needs_capital
from anonyma.policy import keeps
from anonyma.rawscan import RawScanner, raw_residues
from anonyma.replacer import BLACKOUT_CHAR
from anonyma.resolver import NAME_TYPES, cluster_forms
from anonyma.textsearch import MultiMatcher, leftmost_longest
from anonyma.textutils import normalize, normalize_with_map, strip_accents

ORIGINAL_VALUE = "original_value"
DETECTOR_HIT = "detector"
TABLE_DETECTOR = "table"
TABLE_VARIANT_DETECTOR = "table_variante"
TABLE_OCR_DETECTOR = "table_ocr"
NAME_DETECTOR = "nom_origine"
#: Residue type of an original file name found in an output.
FILENAME_TYPE = "NOM_FICHIER"
MIN_VALUE_LENGTH = 3
MIN_DIGITS = 4
#: Skeleton (OCR) forms shorter than this are not searched; longer than
#: ``CERTAIN_OCR_LENGTH`` they are certain residues.
MIN_OCR_LENGTH = 5
CERTAIN_OCR_LENGTH = 8
#: Original names (and stems) shorter than this are not searched.
MIN_NAME_LENGTH = 6
#: Words of original names shorter than this are not searched.
MIN_NAME_WORD = 4
#: Blackout substitutes (any length) count as substitutes.
_BLACKOUT = re.compile(re.escape(BLACKOUT_CHAR) + "+")
#: File name / path separators read as spaces when names are compared.
_NAME_SEPARATORS = str.maketrans({c: " " for c in "_-.+~"})
#: Characters OCR confuses, merged in skeleton forms (applied after normalisation).
_SKELETON = str.maketrans({"0": "o", "1": "l", "i": "l", "|": "l", "!": "l", "5": "s", "8": "b"})
_NAME_WORD = re.compile(r"[^\W\d_]+|\d+")


def spaced_name(text: str) -> str:
    """``text`` with file name separators (``_ - . + ~``) as spaces (same length)."""
    return text.translate(_NAME_SEPARATORS)


def skeleton(text: str) -> str:
    """OCR-tolerant form of ``text``, same length: accents dropped, lower case,
    confusable characters merged, separators as spaces."""
    out = []
    for ch in text:
        base = strip_accents(ch).casefold()
        out.append(base if len(base) == 1 else ch)
    return spaced_name("".join(out)).translate(_SKELETON)


def is_ocr_issue(code: str) -> bool:
    """True for issue codes telling that OCR could not read an image or a page
    (``ocr.unavailable``, ``ocr.failed``, ``pdf.ocr_unavailable``...)."""
    return any(part.startswith("ocr") for part in code.split("."))


#: Metadata labels of the save time, which a writer stamps on the output itself.
_SAVE_STAMPS = frozenset({"modified"})


def stamped_today(segment: Segment) -> bool:
    """A save time of today: stamped by the tool that produced the output (LibreOffice
    dates the legacy files it saves), not left over from the original, even when the
    original was also saved today."""
    context = segment.context
    if context.kind is not SegmentKind.METADATA or context.label not in _SAVE_STAMPS:
        return False
    today = {datetime.now().date().isoformat(), datetime.now(UTC).date().isoformat()}
    return segment.text.strip()[:10] in today


def worth_checking(value: str) -> bool:
    """Short-value policy (see module docstring)."""
    key = normalize(value)
    if len(key) < MIN_VALUE_LENGTH:
        return False
    compact = checksums.compact(value)
    return not (compact.isdigit() and len(compact) < MIN_DIGITS)


class Verifier:
    def __init__(self, config: Config, detectors: Sequence[Detector]) -> None:
        self.config = config
        #: Detectors flagged ``verify = False`` (slow, non-deterministic LLM) are skipped.
        self.detectors = [d for d in detectors if d.verify]
        #: References kept on purpose are no residues (« --conserver-identifiants »).
        self.ids = IdWhitelist(config.opaque_id_whitelist or [])
        self.keeping = bool(self.ids) or not config.mask_opaque_ids

    def verify(
        self,
        output: Document,
        sensitive: Sequence[tuple[str, str]],
        substitutes: set[str],
        allowed: Sequence[str] = (),
        *,
        names: Sequence[str] = (),
    ) -> VerificationResult:
        """Check a re-extracted output tree.

        ``sensitive``: every original ``(value, entity_type)`` of the batch.
        ``substitutes``: values introduced by the replacer (ignored by detectors).
        ``allowed``: values the user chose to keep (whitelist, rejected clusters).
        ``names``: original names of the input file and of its embedded items.
        """
        result = VerificationResult(verified=True)
        allowed_keys = {normalize(a) for a in allowed} | self.config.whitelist_keys()
        table = _cached(
            ("table", tuple(sensitive), frozenset(allowed_keys)),
            lambda: self._table(sensitive, allowed_keys),
        )
        ocr_table = _cached(
            ("ocr", tuple(sensitive), frozenset(allowed_keys)),
            lambda: self._ocr_table(sensitive, allowed_keys),
        )
        subs = _cached(("subs", frozenset(substitutes)), lambda: _substitutes(substitutes))
        # Values the user kept may contain masked values (a whitelisted e-mail holding a
        # masked surname): text inside them is not a residue.
        kept = frozenset([*allowed, *self.config.whitelist])
        kept_matcher = _cached(("kept", kept), lambda: _substitutes(set(kept)))
        name_table = _name_table(
            names,
            [d.name for d in output.walk()],
            allowed_keys,
            certain_words=not self.config.neutral_filenames,
        )
        kinds: list[SegmentKind] = []  # of each residue, for neutral labels
        atomic = AtomicIndex()

        for doc in output.walk():
            gaps = [
                i for i in doc.issues if is_ocr_issue(i.code) and i.severity is not Severity.INFO
            ]
            result.unread += len(gaps)
            errors = [i for i in doc.issues if i.severity is Severity.ERROR]
            ocr_only = bool(errors) and all(is_ocr_issue(i.code) for i in errors)
            readable = doc.status is DocumentStatus.EXTRACTED and not errors
            if not readable and not (doc.status is DocumentStatus.FAILED and ocr_only):
                result.verified = False
                result.issues.append(
                    errors[0]
                    if errors
                    else Issue(
                        Severity.ERROR,
                        "verify.unreadable",
                        "Un élément du fichier produit n'a pas pu être relu.",
                    )
                )
                continue
            covered: dict[str, list[tuple[int, int]]] = {}
            for segment in doc.segments:
                if stamped_today(segment):
                    covered[segment.id] = [(0, len(segment.text))]
                    continue
                prepared = normalize_with_map(segment.text)
                spans = [(m.start, m.end) for m in subs.find(segment.text, prepared=prepared)]
                spans += [(m.start(), m.end()) for m in _BLACKOUT.finditer(segment.text)]
                spans += [(m.start(), m.end()) for m in _TAG_LIKE.finditer(segment.text)]
                spans += [
                    (m.start, m.end) for m in kept_matcher.find(segment.text, prepared=prepared)
                ]
                covered[segment.id] = spans
                hits: list[tuple[int, int, str, str, bool]] = []
                for m in leftmost_longest(table.find(segment.text, prepared=prepared)):
                    if not _inside(m.start, m.end, spans):
                        entity_type, source = m.payloads[0]
                        hits.append((m.start, m.end, entity_type, source, True))
                if segment.context.kind is SegmentKind.OCR and len(ocr_table):
                    for om in leftmost_longest(ocr_table.find(skeleton(segment.text))):
                        if not _inside(om.start, om.end, spans):
                            ocr_type, length = om.payloads[0]
                            sure = length >= CERTAIN_OCR_LENGTH
                            hits.append((om.start, om.end, ocr_type, TABLE_OCR_DETECTOR, sure))
                if len(name_table):
                    for nm in leftmost_longest(name_table.find(spaced_name(segment.text))):
                        if not _inside(nm.start, nm.end, spans):
                            hits.append(
                                (nm.start, nm.end, FILENAME_TYPE, NAME_DETECTOR, nm.payloads[0])
                            )
                flagged: list[tuple[int, int]] = []
                for hit_start, hit_end, entity_type, source, certain in hits:
                    # A fragment of a GUID or a token is not that value (atomic tokens).
                    span = atomic.fit(segment.text, hit_start, hit_end)
                    if span is None:
                        continue
                    start, end = span
                    value = segment.text[start:end]
                    if _overlaps(start, end, flagged):
                        continue
                    if keeps(entity_type, value, self.config):
                        continue
                    flagged.append((start, end))
                    kinds.append(segment.context.kind)
                    result.residues.append(
                        Residue(
                            location=doc.name,
                            segment_label=place(segment, start),
                            entity_type=entity_type,
                            reason=ORIGINAL_VALUE,
                            detector=source,
                            value=value,
                            certain=certain,
                        )
                    )
            segments = {s.id: s for s in doc.segments}
            for detector in self.detectors:
                for entity in detector.detect(doc.segments, doc.id):
                    found = segments.get(entity.segment_id)
                    if found is None or entity.score < self.config.min_score:
                        continue
                    segment = found
                    span = atomic.fit_typed(
                        segment.text, entity.start, entity.end, entity.entity_type
                    )
                    if span is None:
                        continue
                    if self.keeping and kept_reference(
                        self.config, self.ids, segment.text, span[0], span[1]
                    ):
                        continue  # « Facture_FAC-2026-743117.pdf »: kept on purpose
                    entity.start, entity.end = span
                    value = segment.text[entity.start : entity.end]
                    if normalize(value) in allowed_keys:
                        continue
                    if stamped_today(segment) or _neutral_outside(
                        segment, entity.start, entity.end, covered[segment.id]
                    ):
                        continue
                    if keeps(entity.entity_type, value, self.config):
                        continue
                    kinds.append(segment.context.kind)
                    result.residues.append(
                        Residue(
                            location=doc.name,
                            segment_label=place(segment, entity.start),
                            entity_type=entity.entity_type,
                            reason=DETECTOR_HIT,
                            detector=entity.detector,
                            value=value,
                            certain=detector.deterministic
                            and entity.score >= CERTAIN_DETECTOR_SCORE,
                        )
                    )
        result.residues = neutral_places(result.residues, kinds)
        return result

    def verify_raw(
        self,
        output: Document,
        sensitive: Sequence[tuple[str, str]],
        substitutes: set[str],
        allowed: Sequence[str] = (),
        structure: frozenset[str] = frozenset(),
    ) -> list[Residue]:
        """Original values found in the raw bytes of a re-extracted output tree (the
        written file and the items extracted from it, still on disk), and embedded
        files none of these items accounts for. Same arguments as :meth:`verify`;
        ``structure``: the batch's structural names (:mod:`anonyma.structure`), never
        searched."""
        allowed_keys = {normalize(a) for a in allowed} | self.config.whitelist_keys()
        kept = (*allowed, *self.config.whitelist)
        scanner = _cached(
            ("raw", tuple(sensitive), frozenset(substitutes), frozenset(kept), structure),
            lambda: RawScanner(sensitive, substitutes, allowed_keys, kept, self.config, structure),
        )
        return neutral_places(raw_residues(scanner.scan(output)))

    @staticmethod
    def _values(
        sensitive: Sequence[tuple[str, str]], allowed_keys: set[str]
    ) -> dict[str, set[str]]:
        by_type: dict[str, set[str]] = {}
        for value, entity_type in sensitive:
            if normalize(value) in allowed_keys or not worth_checking(value):
                continue
            by_type.setdefault(entity_type, set()).add(value)
        return by_type

    @classmethod
    def _table(
        cls, sensitive: Sequence[tuple[str, str]], allowed_keys: set[str]
    ) -> MultiMatcher[tuple[str, str]]:
        table: MultiMatcher[tuple[str, str]] = MultiMatcher()
        for entity_type, values in cls._values(sensitive, allowed_keys).items():
            for value in values:
                table.add(
                    value,
                    (entity_type, TABLE_DETECTOR),
                    require_capital=_needs_capital(entity_type, value),
                )
            for form, cap in _derived(entity_type, values):
                if normalize(form) not in allowed_keys and worth_checking(form):
                    table.add(form, (entity_type, TABLE_VARIANT_DETECTOR), require_capital=cap)
        return table

    @classmethod
    def _ocr_table(
        cls, sensitive: Sequence[tuple[str, str]], allowed_keys: set[str]
    ) -> MultiMatcher[tuple[str, int]]:
        """Skeleton forms of every value, searched in OCR text only."""
        table: MultiMatcher[tuple[str, int]] = MultiMatcher()
        for entity_type, values in cls._values(sensitive, allowed_keys).items():
            forms = set(values) | {f for f, _ in _derived(entity_type, values)}
            for form in forms:
                key = skeleton(form)
                length = len(re.sub(r"\W+", "", key))
                if length >= MIN_OCR_LENGTH and normalize(form) not in allowed_keys:
                    table.add(key, (entity_type, length))
        return table


#: Per-process cache: every document of a batch shares the same table and substitutes.
_CACHE: dict[object, object] = {}

#: Deterministic detector hits below this score are only possible residues.
CERTAIN_DETECTOR_SCORE = 0.8
#: Tags as altered by formats or OCR: brackets dropped or replaced (sheet names,
#: defined names), digits misread as letters (``PERSONNE_O01``). Both numberings:
#: sequential (``PERSONNE_001``) and random ids (``PERSONNE_K7Q2``: 4-5 characters
#: holding at least one digit, so that plain words such as ``TODO_LIST`` never count).
_TAG_LIKE = re.compile(
    r"[\[(<{]?\b[A-Z][A-Z_]{1,40}_(?:[0-9OoIl]{3,}|(?=[A-Za-z0-9]{0,4}[0-9])[A-Za-z0-9]{4,5})\b"
    r"[\])>}]?"
)
_CACHE_SIZE = 8


def _cached[T](key: object, build: Callable[[], T]) -> T:
    if key not in _CACHE:
        while len(_CACHE) >= _CACHE_SIZE:
            _CACHE.pop(next(iter(_CACHE)))
        _CACHE[key] = build()
    return _CACHE[key]  # type: ignore[return-value]


def _substitutes(substitutes: set[str]) -> MultiMatcher[str]:
    subs: MultiMatcher[str] = MultiMatcher()
    for sub in substitutes:
        if sub.strip():
            subs.add(sub, sub)
            # OCR glues the words of a line (« Votre interlocuteur:AydenDupuis »): the
            # glued substitute is still a substitute, not a name left over.
            words = sub.split()
            if len(words) > 1:
                subs.add("".join(words), sub)
    return subs


def _needs_capital(entity_type: str, value: str) -> bool:
    return entity_type in NAME_TYPES and needs_capital(value)


def _derived(entity_type: str, values: set[str]) -> Iterator[tuple[str, bool]]:
    """Derived forms of the batch values (same rules as propagation), plus, for names
    and addresses, every form with its hyphens as spaces or glued (OCR and file names
    drop or add hyphens: ``QUINTAVALLE BREZAC``, ``QuintavalleBrezac``)."""
    forms: dict[str, bool] = {}
    if entity_type in (
        EntityType.PERSON,
        EntityType.PHONE,
        EntityType.IBAN,
        EntityType.CREDIT_CARD,
        EntityType.SIREN,
        EntityType.SIRET,
        EntityType.NIR,
    ):
        for value in values:
            cluster = EntityCluster(
                id="", entity_type=entity_type, canonical=value, variants={value}
            )
            for form, cap in cluster_forms(cluster):
                if form != value:
                    forms.setdefault(form, cap)
    if entity_type in (*NAME_TYPES, EntityType.ADDRESS):
        for form in [*values, *forms]:
            if "-" in form:
                spaced = " ".join(form.replace("-", " ").split())
                forms.setdefault(spaced, _needs_capital(entity_type, spaced))
                forms.setdefault("".join(spaced.split()), False)
    for form, cap in forms.items():
        if form not in values:
            yield form, cap


#: Zone shown instead of a segment label that holds a residue (a key, a sheet or
#: attribute name, a path...).
NEUTRAL_LABELS: dict[SegmentKind, str] = {
    SegmentKind.BODY: "texte",
    SegmentKind.HEADER: "en-tête",
    SegmentKind.CELL: "cellule",
    SegmentKind.METADATA: "métadonnée",
    SegmentKind.OCR: "texte reconnu (OCR)",
    SegmentKind.ATTRIBUTE: "attribut ou clé",
    SegmentKind.COMMENT: "commentaire",
    SegmentKind.NOTE: "note",
    SegmentKind.FILENAME: "nom du fichier",
    SegmentKind.HIDDEN: "contenu masqué",
    SegmentKind.BARCODE: "code-barres",
    SegmentKind.SIGNATURE: "signature",
}
#: Path component shown instead of an output name that holds a residue.
MASKED_NAME = "[nom masqué]"


def neutral_places(residues: Sequence[Residue], kinds: Sequence[SegmentKind] = ()) -> list[Residue]:
    """``residues`` whose location and zone never echo one of their values: a name
    or label holding a residue (a file or folder name, a JSON key, an attribute or
    sheet name) is replaced by a neutral one (``kinds``: segment kind of each)."""
    keys = {k for k in (normalize(spaced_name(r.value)) for r in residues) if len(k) >= 3}
    if not keys:
        return list(residues)

    def leaks(text: str) -> bool:
        flat = normalize(spaced_name(text))
        return any(key in flat for key in keys)

    out: list[Residue] = []
    for i, residue in enumerate(residues):
        label, location = residue.segment_label, residue.location
        if leaks(label):
            kind = kinds[i] if i < len(kinds) else None
            label = NEUTRAL_LABELS.get(kind, "zone") if kind is not None else "zone"
        if leaks(location):
            location = "/".join(MASKED_NAME if leaks(p) else p for p in location.split("/"))
        out.append(replace(residue, segment_label=label, location=location))
    return out


def place(segment: Segment, start: int) -> str:
    """Where a residue at ``start`` in ``segment`` lies, for reports: the precise
    location (``where``, else the label) and, for text formats, the file line."""
    context = segment.context
    base = context.where or context.label
    if context.line is None:
        return base
    line = context.line + segment.text.count("\n", 0, start)
    return f"{base} (ligne {line} du fichier)"


def _inside(start: int, end: int, spans: Sequence[tuple[int, int]]) -> bool:
    return any(s <= start and end <= e for s, e in spans)


def _overlaps(start: int, end: int, spans: Sequence[tuple[int, int]]) -> bool:
    return any(s < end and start < e for s, e in spans)


def _name_words(text: str) -> list[str]:
    return _NAME_WORD.findall(text)


def _distinctive_word(word: str) -> bool:
    """A word of a file name that may identify someone (not common, not a number)."""
    key = normalize(word)
    return (
        len(key) >= MIN_NAME_WORD
        and not key.isdigit()
        and key not in STOP_WORDS
        and key not in PARTICLES
        and civility_of(word) is None
        and not is_common_word(word)
    )


def _name_table(
    names: Iterable[str],
    output_names: Sequence[str],
    allowed_keys: set[str],
    *,
    certain_words: bool = True,
) -> MultiMatcher[bool]:
    """Original names to search in an output (payload: certain residue).

    Each path component and its stem (certain) when distinctive and absent from the
    output names; their distinctive words absent from the output names, i.e. masked
    in them: certain when they read as proper names, possible otherwise. With neutral
    output names (``certain_words`` False) every word is absent from them, masked or
    not: words are only possible residues. Everything is compared with separators as
    spaces.
    """
    table: MultiMatcher[bool] = MultiMatcher()
    in_output = normalize(spaced_name(" / ".join(output_names)))
    output_words = {normalize(w) for n in output_names for w in _name_words(n)}
    for name in names:
        for part in name.replace("\\", "/").split("/"):
            stem = PurePosixPath(part).stem if "." in part.strip(".") else part
            words = [w for w in _name_words(stem) if _distinctive_word(w)]
            if not words:
                continue
            for form in {part, stem}:
                key = normalize(spaced_name(form))
                if len(key) >= MIN_NAME_LENGTH and key not in in_output and key not in allowed_keys:
                    table.add(spaced_name(form), True)
            for word in words:
                key = normalize(word)
                if key not in output_words and key not in allowed_keys:
                    table.add(word, certain_words and _name_like_word(word))
    return table


def _name_like_word(word: str) -> bool:
    """A distinctive word of an original name that reads as a proper name
    (« Quintavalle » of « CV Zéphyrin Quintavalle.txt »): found verbatim in the
    output, it is a certain residue. Lower-case words and short acronyms
    (« rgpd », « ACME ») stay possible residues."""
    letters = [c for c in word if c.isalpha()]
    if len(letters) < MIN_NAME_WORD or not word[:1].isupper():
        return False
    return not (word.isupper() and len(letters) <= 4)


def _neutral_outside(
    segment: Segment, start: int, end: int, spans: Sequence[tuple[int, int]]
) -> bool:
    """True if every part of ``[start, end)`` outside substitutes is insignificant."""
    if not spans:
        return False
    text = segment.text
    mask = [False] * (end - start)
    hit = False
    for s, e in spans:
        lo, hi = max(s, start), min(e, end)
        if lo < hi:
            hit = True
            for i in range(lo, hi):
                mask[i - start] = True
    if not hit:
        return False
    rest = "".join(" " if covered else text[start + i] for i, covered in enumerate(mask))
    for token in rest.replace("_", " ").split():
        word = token.strip(".,;:!?()[]{}<>\"'«»-–—/\\|")
        if not word or not any(c.isalnum() for c in word):
            continue
        key = normalize(word)
        if civility_of(word) is not None or key in PARTICLES or key in STOP_WORDS:
            continue
        if key in ROLE_WORDS or key in _LINK_SCHEMES:
            continue  # « Service Client » around a substitute, ``mailto:`` before one
        return False
    return True


#: URL schemes glued to a substitute by a model span (``mailto:EMAIL_001@…``).
_LINK_SCHEMES = frozenset({"mailto", "tel", "callto", "sip", "http", "https", "www"})


__all__ = [
    "DETECTOR_HIT",
    "FILENAME_TYPE",
    "NAME_DETECTOR",
    "ORIGINAL_VALUE",
    "TABLE_OCR_DETECTOR",
    "Verifier",
    "is_ocr_issue",
    "skeleton",
    "spaced_name",
    "worth_checking",
]
