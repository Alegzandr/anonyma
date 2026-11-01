"""LLM pass (profile FULL): indirect identifiers and names in prose.

The model reads paragraphs (:func:`passages`, e-mail quote markers removed), and only
those it can add something to (:meth:`LlmDetector.wanted`): prose rather than field
values, headers or identifiers, holding something shaped like an indirect identifier
(« la DRH de l'agence de Nantes », « le seul médecin », « sa voisine ») or a
capitalized word the lexicon does not explain and the other detectors did not cover
(it runs after them, ``uses_prior``). ``Config.llm_all_segments`` sends every
paragraph with letters.

A paragraph already analysed in the process (same normalized text: one mail in an
.eml, its JSON export and a quoted reply) is not sent again (:class:`Memo`).
Mentions are grounded: kept only where they occur in the segment, and filtered by the
plausibility rules shared with GLiNER. Skipped by the verifier (``verify = False``):
too slow and not deterministic enough for a second pass.
"""

from __future__ import annotations

import json
import threading
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import regex

from anonyma import llm
from anonyma.config import Config
from anonyma.data import lexicon
from anonyma.detectors._filters import (
    CALENDAR_WORDS,
    COMMON_WORD_SCORE,
    FUNCTION_WORDS,
    mid_sentence,
    plausible,
    skip_segment,
)
from anonyma.detectors.gliner import job_titles_near_people
from anonyma.logs import get_logger
from anonyma.model_catalog import LLM_PREFERENCE, ModelKind, ModelSpec, get_spec
from anonyma.models import Detector, Entity, EntityType, Profile, Segment, SegmentKind
from anonyma.registry import register_detector
from anonyma.textutils import normalize

log = get_logger("detectors.llm")

SCORE = 0.75
#: Below ``Config.review_threshold`` (0.6): masked and sent to review.
REVIEW_SCORE = 0.5
#: Segments with fewer letters carry nothing the model can add to the rule-based
#: detectors (dates, numbers, « Ok »), and only invite it to make something up.
MIN_LETTERS = 3
#: Segments up to this many tokens may share a model call.
SHORT_SEGMENT_TOKENS = 24
#: Words that make a description point at a person (roles, trades, family ties...),
#: normalized (no accents, lower case); feminine and plural forms are matched too.
_PERSON_WORDS = (
    "drh|rh|directeur|directrice|responsable|chef|cheffe|gerant|president|pdg|dg|maire|"
    "adjoint|medecin|docteur|infirmier|infirmiere|professeur|instituteur|institutrice|"
    "enseignant|avocat|notaire|juge|greffier|greffiere|huissier|employe|salarie|stagiaire|"
    "apprenti|collegue|voisin|epoux|epouse|mari|conjoint|compagnon|compagne|ami|amie|fils|"
    "fille|pere|mere|frere|soeur|oncle|tante|cousin|cousine|neveu|niece|grand-pere|"
    "grand-mere|petit-fils|petite-fille|beau-pere|belle-mere|beau-frere|belle-soeur|veuf|"
    "veuve|eleve|etudiant|patient|patiente|client|cliente|locataire|proprietaire|habitant|"
    "resident|temoin|victime|plaignant|comptable|secretaire|assistant|assistante|technicien|"
    "technicienne|ingenieur|chauffeur|livreur|pharmacien|pharmacienne|dentiste|veterinaire|"
    "psychologue|kinesitherapeute|sage-femme|aide-soignant|aide-soignante|pompier|gendarme|"
    "policier|policiere|militaire|officier|capitaine|commandant|cure|pretre|pasteur|imam|"
    "rabbin|elu|elue|conseiller|conseillere|depute|senateur|senatrice|ministre|prefet|"
    "delegue|representant|tresorier|tresoriere|benevole|fondateur|fondatrice|associe|"
    "heritier|heritiere|gardien|gardienne|concierge|facteur|factrice|boulanger|boulangere|"
    "boucher|bouchere|commercant|commercante|artisan|agriculteur|agricultrice|eleveur|"
    "vendeur|vendeuse|caissier|caissiere|serveur|serveuse|cuisinier|cuisiniere|nourrice|"
    "retraite|retraitee|ancien|ancienne|homme|femme|enfant|bebe|dame|garcon|jeune|personne|"
    "le seul|la seule|l'unique|seul|seule|unique"
)
PERSON_CUES = regex.compile(rf"(?<![a-z])(?:{_PERSON_WORDS})(?:e|s|es)?(?![a-z])")
#: An LLM date of birth needs a birth cue in its segment (other dates are left to
#: the date rules, which mask them as DATE).
BIRTH_CUES = regex.compile(r"(?<![a-z])(?:ne|nee|naissance|ddn|naitre|born|birth)(?![a-z])")
_TOKEN = regex.compile(r"[\p{L}\p{N}]+")

_runners: dict[str, llm.LlmRunner] = {}
_lock = threading.Lock()


def _model_file(spec: ModelSpec) -> Path | None:
    from anonyma.model_manager import ModelManager

    folder = ModelManager().path(spec.id)
    name = spec.extra.get("gguf") or (spec.files[0].filename if spec.files else "")
    if folder is None or not name:
        return None
    path = folder / name
    return path if path.is_file() else None


def select_model(config: Config) -> tuple[ModelSpec, Path] | None:
    """Configured LLM, else the most capable installed one that fits in memory."""
    if config.llm_model:
        spec = get_spec(config.llm_model)
        if spec is None or spec.kind is not ModelKind.LLM:
            return None
        path = _model_file(spec)
        return (spec, path) if path else None
    from anonyma import hardware

    ram = hardware.detect_hardware().ram_bytes
    installed: list[tuple[ModelSpec, Path]] = []
    for model_id in LLM_PREFERENCE:
        spec = get_spec(model_id)
        path = _model_file(spec) if spec else None
        if spec is not None and path is not None:
            installed.append((spec, path))
    if not installed:
        return None
    fitting = [item for item in installed if not ram or item[0].min_ram <= ram]
    return fitting[0] if fitting else installed[-1]


@dataclass(frozen=True, slots=True)
class Passage:
    """A paragraph of a segment, as the model reads it."""

    segment: Segment
    #: Position of the segment in the document (progress).
    position: int
    start: int
    end: int
    #: Paragraph text without e-mail quote markers (``> ``).
    text: str
    #: Normalized text: identical passages of the batch are analysed once.
    key: str


#: Blank lines (quoted ones too: « > »), and the header lines of a quoted message
#: (« De : », « From: »...).
_PARAGRAPH = regex.compile(
    r"\n(?:[^\S\n]|>)*\n"
    r"|\r?\n(?=[ \t>]*(?:De|From|Envoyé|Sent|À|To|Cc|Objet|Subject|Date)[ \t\u00a0]*:)"
)
_QUOTE = regex.compile(r"^[ \t]*(?:>[ \t]?)+", regex.M)


def passage_key(text: str) -> str:
    """Letters and digits only (case, accents, punctuation and line wrapping ignored):
    the same paragraph from an .eml, its HTML version and a CSV cell."""
    return " ".join(_TOKEN.findall(normalize(text)))


def passages(segment: Segment, position: int = 0) -> list[Passage]:
    """The paragraphs of ``segment`` (blank-line separated), quote markers removed:
    a reply chain quotes earlier messages paragraph by paragraph."""
    out: list[Passage] = []
    text = segment.text
    start = 0
    for brk in [*_PARAGRAPH.finditer(text), None]:
        end = brk.start() if brk is not None else len(text)
        body = _QUOTE.sub("", text[start:end]).strip()
        if body:
            out.append(Passage(segment, position, start, end, body, passage_key(body)))
        if brk is not None:
            start = brk.end()
    return out


_WORDS = regex.compile(r"\p{L}[\p{L}'’\-]+")
#: Segment kinds that hold no prose: names, markup attributes, codes, signatures.
_NO_PROSE = frozenset(
    {SegmentKind.FILENAME, SegmentKind.ATTRIBUTE, SegmentKind.BARCODE, SegmentKind.SIGNATURE}
)
#: A sentence: enough words, mostly lower-case ones, few symbols (not a field value,
#: an identifier, a URL list or a JSON fragment).
PROSE_WORDS = 6
PROSE_LOWER = 0.4
PROSE_PLAIN = 0.75
_PLAIN = frozenset(" '’,.;:!?()-\n\u00a0")


_NOISE = regex.compile(r"\S+@\S+|\w+://\S+|www\.\S+")


def _sentence_start(text: str, start: int) -> bool:
    """Capitalized because a sentence starts there (« (Berlin office » does not)."""
    before = text[:start].rstrip(" \t\u00a0\u202f")
    return not before or before[-1] in ".!?…:\n\r•-–—"


def is_prose(text: str) -> bool:
    text = _NOISE.sub(" ", text)  # addresses and links are no words
    words = _WORDS.findall(text)
    lower = sum(1 for w in words if w[0].islower())
    if len(words) < PROSE_WORDS or lower < 3 or lower < PROSE_LOWER * len(words):
        return False
    # Numbers are part of prose (« le 12 mars », « au +49 30 901820 »).
    plain = sum(1 for c in text if c.isalnum() or c in _PLAIN)
    return plain >= PROSE_PLAIN * len(text)


_FAMILY = (
    "fils|fille|pere|mere|frere|soeur|oncle|tante|cousin|cousine|neveu|niece|epoux|epouse|"
    "mari|femme|conjoint|conjointe|compagnon|compagne|voisin|voisine|collegue|ami|amie|"
    "veuf|veuve|beau-pere|belle-mere|beau-frere|belle-soeur|grand-pere|grand-mere|"
    "petit-fils|petite-fille|enfant|bebe"
)
_POSSESSIVE = "mon|ma|mes|ton|ta|tes|son|sa|ses|notre|nos|votre|vos|leur|leurs"
#: What an indirect identifier looks like (normalized text): a role or tie qualified
#: by an organisation, a place or a person (« la DRH de l'agence », « le fils du
#: maire »), uniqueness (« le seul médecin »), or a tie to someone (« sa voisine »).
SINGLING_OUT = regex.compile(
    rf"(?<![a-z])(?:(?:{_PERSON_WORDS})(?:e|s|es)?\s+(?:de|du|des|d'|au|aux|chez)(?![a-z])"
    r"|(?:le|la|l')\s*(?:seul|seule|unique)(?![a-z])"
    rf"|(?:{_POSSESSIVE})\s+(?:{_FAMILY})s?(?![a-z]))"
)
_NAME = regex.compile(r"\b\p{Lu}[\p{L}'’\-]+")


@register_detector
class LlmDetector(Detector):
    name = "llm"
    min_profile = Profile.FULL
    deterministic = False
    verify = False
    uses_prior = True

    @classmethod
    def is_available(cls, config: Config) -> bool:
        return llm.available() and select_model(config) is not None

    def __init__(self, config: Config) -> None:
        super().__init__(config)
        self.lexicon = lexicon()

    def _runner(self) -> llm.LlmRunner:
        selected = select_model(self.config)
        if selected is None:
            raise RuntimeError("LLM model missing")
        key = str(selected[1])
        with _lock:
            if key not in _runners:
                _runners[key] = llm.LlmRunner(selected[1])
            return _runners[key]

    def wanted(self, passage: Passage, covered: Sequence[tuple[int, int]] = ()) -> bool:
        """Whether the model can add anything to ``passage``: prose (not a field value,
        a header or an identifier) with a possible indirect identifier, or a name the
        other detectors (``covered`` spans of the segment) left out.
        ``Config.llm_all_segments`` sends every passage with letters."""
        text = passage.text
        if sum(c.isalpha() for c in text) < MIN_LETTERS:
            return False
        if self.config.llm_all_segments:
            return True
        if passage.segment.context.kind in _NO_PROSE or not is_prose(text):
            return False
        if SINGLING_OUT.search(normalize(text)):
            return True
        return any(self._uncovered(passage, m, covered) for m in _NAME.finditer(text))

    def _uncovered(
        self, passage: Passage, match: regex.Match[str], covered: Sequence[tuple[int, int]]
    ) -> bool:
        """A capitalized word the lexicon does not explain, inside a sentence (or in
        capitals), that no other detector covers (« avec Kervella hier »)."""
        word = match.group()
        key = normalize(word)
        if key in self.lexicon or key in FUNCTION_WORDS:
            return False
        shouted = word.isupper() and len(word) > 2
        if not shouted and _sentence_start(passage.text, match.start()):
            return False
        spans = list(llm.locate(passage.segment.text, word))
        return not spans or any(
            not any(cs <= s and e <= ce for cs, ce in covered) for s, e in spans
        )

    def detect(self, segments: Sequence[Segment], document_id: str) -> list[Entity]:
        return self.detect_after(segments, document_id, ())

    def _todo(self, segments: Sequence[Segment], prior: Sequence[Entity]) -> list[Passage]:
        covered: dict[str, list[tuple[int, int]]] = {}
        for e in prior:
            covered.setdefault(e.segment_id, []).append((e.start, e.end))
        return [
            p
            for i, s in enumerate(segments)
            if not skip_segment(s)
            for p in passages(s, i)
            if self.wanted(p, covered.get(s.id, ()))
        ]

    def pending(
        self, segments: Sequence[Segment], prior: Sequence[Entity], seen: set[str]
    ) -> list[Passage]:
        """Passages :meth:`detect_after` will send to the model for ``segments``: gated
        with the other detectors' entities (``prior``), minus those already analysed
        in this process or in ``seen`` (keys, updated). The exact workload, without
        loading the model."""
        selected = select_model(self.config)
        memo = _memo(memo_key(selected[1])) if selected is not None else Memo()
        out: list[Passage] = []
        for p in self._todo(segments, prior):
            if p.key not in seen and p.key not in memo:
                seen.add(p.key)
                out.append(p)
        return out

    def detect_after(
        self, segments: Sequence[Segment], document_id: str, prior: Sequence[Entity]
    ) -> list[Entity]:
        todo = self._todo(segments, prior)
        if not todo:
            return []
        runner = self._runner()
        memo = _memo(runner.key)
        pending: dict[str, Passage] = {}
        for p in todo:
            if p.key not in memo and p.key not in pending:
                pending[p.key] = p
        calls = plan(list(pending.values()), runner.window_tokens, runner.count_tokens)
        for group, text in calls:
            mentions = runner.extract(text)
            for p in group:
                memo.add(p.key, [m for m in mentions if next(llm.locate(p.text, m.text), None)])
            self.report(group[-1].position + 1, len(segments))
        if calls:
            record_throughput(runner)
        return _entities(todo, memo, document_id, self.name)

    def workload(self, segments: Sequence[Segment], seen: set[str]) -> list[Passage]:
        """Passages of ``segments`` the model would read, not counting those in ``seen``
        (keys, updated): an upper bound, the other detectors' coverage is unknown."""
        out: list[Passage] = []
        for i, s in enumerate(segments):
            if skip_segment(s):
                continue
            for p in passages(s, i):
                if p.key not in seen and self.wanted(p):
                    seen.add(p.key)
                    out.append(p)
        return out


def _entities(todo: Sequence[Passage], memo: Memo, document_id: str, detector: str) -> list[Entity]:
    """Every mention of every passage, located (grounded) in the passage's segment."""
    found: list[Entity] = []
    seen: set[tuple[str, int, int]] = set()
    for p in todo:
        segment = p.segment
        for mention in memo.get(p.key):
            for start, end in llm.locate(segment.text, mention.text):
                key = (segment.id, start, end)
                value = segment.text[start:end]
                if key in seen or not _keep(segment, mention.entity_type, value, start):
                    continue
                seen.add(key)
                found.append(
                    Entity(
                        entity_type=mention.entity_type,
                        value=value,
                        document_id=document_id,
                        segment_id=segment.id,
                        start=start,
                        end=end,
                        score=_score(mention.entity_type, value),
                        detector=detector,
                    )
                )
    # Same rule as GLiNER: a job title alone (« le boucher du marché couvert »
    # as FONCTION) is ordinary text; next to a person it identifies them.
    by_segment: dict[str, list[Entity]] = {}
    for e in found:
        by_segment.setdefault(e.segment_id, []).append(e)
    texts = {p.segment.id: p.segment.text for p in todo}
    return [
        e for sid, group in by_segment.items() for e in job_titles_near_people(texts[sid], group)
    ]


class Memo:
    """Mentions of the passages already analysed in this process, for one model and
    prompt version (least recently used dropped beyond :data:`MEMO_SIZE`). FULL runs
    in one process, so a passage repeated in the batch (the same mail in an .eml, a
    JSON export and a CSV, quoted replies) costs one model call; with several worker
    processes (``-j``) each keeps its own."""

    def __init__(self) -> None:
        self._items: OrderedDict[str, tuple[llm.LlmMention, ...]] = OrderedDict()
        self._lock = threading.Lock()

    def __contains__(self, key: object) -> bool:
        with self._lock:
            return key in self._items

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def get(self, key: str) -> tuple[llm.LlmMention, ...]:
        with self._lock:
            if key not in self._items:
                return ()
            self._items.move_to_end(key)
            return self._items[key]

    def add(self, key: str, mentions: Sequence[llm.LlmMention]) -> None:
        with self._lock:
            merged = dict.fromkeys([*self._items.get(key, ()), *mentions])
            self._items[key] = tuple(merged)
            self._items.move_to_end(key)
            while len(self._items) > MEMO_SIZE:
                self._items.popitem(last=False)


#: Seconds per model call assumed before any was measured on this machine (Qwen3
#: 4B Q4 on an 8-core desktop CPU: 1 s for an empty answer, 3 to 5 s with a few
#: entities; a laptop is slower).
DEFAULT_CALL_SECONDS = 4.0
#: Measured throughput, per model file: ``{"<model>": {"appels": n, "secondes": s}}``.
THROUGHPUT_FILE = "debit_modele_langue.json"


def _throughput_path() -> Path:
    from anonyma import paths

    return paths.data_dir() / THROUGHPUT_FILE


#: Calls the saved throughput is averaged over (older runs fade out beyond).
THROUGHPUT_WINDOW = 2000
#: Calls measured before the saved throughput replaces :data:`DEFAULT_CALL_SECONDS`.
THROUGHPUT_MIN_CALLS = 50
_recorded: dict[int, tuple[int, float]] = {}


def record_throughput(runner: llm.LlmRunner) -> None:
    """Add this process's new measured calls to the saved average seconds per call,
    over the last :data:`THROUGHPUT_WINDOW` calls of every run (best effort: counts
    only). One small or unusual run does not set the next estimate on its own."""
    done_calls, done_seconds = _recorded.get(id(runner), (0, 0.0))
    calls = runner.measured_calls - done_calls
    seconds = runner.measured_seconds - done_seconds
    if calls <= 0:
        return
    _recorded[id(runner)] = (runner.measured_calls, runner.measured_seconds)
    path = _throughput_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        if not isinstance(data, dict):
            data = {}
        old = data.get(runner.key)
        if isinstance(old, dict):
            calls += int(old.get("appels", 0))
            seconds += float(old.get("secondes", 0.0))
        if calls > THROUGHPUT_WINDOW:
            seconds *= THROUGHPUT_WINDOW / calls
            calls = THROUGHPUT_WINDOW
        data[runner.key] = {"appels": calls, "secondes": round(seconds, 3)}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=1), encoding="utf-8")
    except (OSError, ValueError):
        log.info("llm throughput not saved")


def call_seconds(config: Config) -> float:
    """Seconds per model call: measured on this machine for the selected model if a
    run did so, :data:`DEFAULT_CALL_SECONDS` otherwise."""
    selected = select_model(config)
    if selected is None:
        return DEFAULT_CALL_SECONDS
    key = memo_key(selected[1])
    try:
        data = json.loads(_throughput_path().read_text(encoding="utf-8"))
        item = data[key]
        calls, seconds = int(item["appels"]), float(item["secondes"])
    except (OSError, ValueError, KeyError, TypeError):
        return DEFAULT_CALL_SECONDS
    return seconds / calls if calls >= THROUGHPUT_MIN_CALLS else DEFAULT_CALL_SECONDS


def memo_key(model: Path) -> str:
    """:attr:`anonyma.llm.LlmRunner.key` of a model file, without loading it."""
    return f"{model.name}:v{llm.PROMPT_VERSION}"


def estimated_calls(todo: Sequence[Passage]) -> int:
    """Model calls for ``todo`` without loading the model (about 3 characters per
    token of French text)."""
    return len(plan(todo, llm.WINDOW_TOKENS, lambda text: max(1, len(text) // 3)))


MEMO_SIZE = 50_000
_memos: dict[str, Memo] = {}


def _memo(key: str) -> Memo:
    with _lock:
        return _memos.setdefault(key, Memo())


def forget() -> None:
    """Drop every memo (tests; a new model or prompt gets its own memo anyway)."""
    with _lock:
        _memos.clear()


def plan(
    todo: Sequence[Passage],
    budget: int,
    count: Callable[[str], int],
    short: int = SHORT_SEGMENT_TOKENS,
) -> list[tuple[list[Passage], str]]:
    """Model calls for ``todo``, each at most ``budget`` tokens of text.

    Consecutive paragraphs of one segment share a call (a window over its text);
    paragraphs of different segments only when short (cells, fields, signature lines:
    at most ``short`` tokens), since sentences pooled from unrelated places make the
    model list fewer entities. A paragraph longer than ``budget`` is cut into
    overlapping windows.
    """
    calls: list[tuple[list[Passage], str]] = []
    group: list[Passage] = []
    sizes: list[int] = []
    separator = count(llm.SEGMENT_SEPARATOR)

    def flush() -> None:
        if group:
            calls.append((list(group), llm.SEGMENT_SEPARATOR.join(p.text for p in group)))
            group.clear()
            sizes.clear()

    for p in todo:
        size = count(p.text)
        if size > budget:
            flush()
            windows = llm.windows(p.text, budget, count, llm.WINDOW_OVERLAP)
            calls.extend(([p], window) for _, window in windows)
            continue
        if group:
            same = group[-1].segment is p.segment
            pooled = size <= short and max(sizes) <= short
            fits = sum(sizes) + separator * len(group) + size <= budget
            if not (same or pooled) or not fits:
                flush()
        group.append(p)
        sizes.append(size)
    flush()
    return calls


def _keep(segment: Segment, entity_type: str, value: str, start: int) -> bool:
    """Type sanity on top of the plausibility rules shared with GLiNER: « Merci » is no
    name, « Lundi » no place, « Re: Réunion » no description of a person."""
    words = _TOKEN.findall(normalize(value))
    if words and all(w in CALENDAR_WORDS or w.isdigit() for w in words):
        return entity_type == EntityType.DATE_OF_BIRTH and _born(segment.text)
    if entity_type == EntityType.DATE_OF_BIRTH:
        return any(c.isdigit() for c in value) and _born(segment.text)
    if entity_type == EntityType.INDIRECT and not PERSON_CUES.search(normalize(value)):
        return False
    if entity_type in _PROPER_TYPES and not _proper(value, segment.text, start):
        return False  # « À l'attention de » as ADRESSE, « Pièce jointe » as LIEU
    text = segment.text
    filename = segment.context.kind is SegmentKind.FILENAME
    capitalized = not filename and value[:1].isupper() and mid_sentence(text, start)
    # A mention is no probability. Several words copied together (« mairie de
    # Pont-Aven », « 3 impasse des Mouettes ») are as sure as GLiNER's surest spans; a
    # lone common word (« Cordialement », « Bises ») or a job title is not.
    sure = len(words) > 1 and entity_type != EntityType.JOB_TITLE
    score = COMMON_WORD_SCORE if sure else SCORE
    return plausible(
        entity_type,
        value,
        score,
        filename=filename,
        ocr=segment.context.kind is SegmentKind.OCR,
        capitalized_mid_sentence=capitalized,
    )


_PROPER_TYPES = frozenset({EntityType.ORGANIZATION, EntityType.LOCATION, EntityType.ADDRESS})
_WORD = regex.compile(r"[\p{L}]+(?:['’][\p{L}]+)*")


def _proper(value: str, text: str, start: int) -> bool:
    """A digit, or a capitalized word that is not a grammatical one (``l'Aven`` counts)
    and owes its capital to more than its place: a single word (« Quimper »), a word
    after the first, or a sentence-case phrase in the middle of a sentence
    (« Pièce jointe » or « Envoyé de mon iPhone » alone are no place)."""
    if any(c.isdigit() for c in value):
        return True
    words = [
        (w.start(), regex.split(r"['’]", w.group())[-1])
        for w in _WORD.finditer(value)
        if normalize(w.group()) not in FUNCTION_WORDS
    ]
    if not words:
        return False
    if any(core[:1].isupper() for _, core in words[1:]):
        return True
    offset, first = words[0]
    return first[:1].isupper() and (len(words) == 1 or mid_sentence(text, start + offset))


#: Uniqueness and ties to someone: what turns a description into an identifier.
_SINGLING_OUT = regex.compile(
    r"(?<![a-z])(?:seul|seule|unique|fils|fille|pere|mere|frere|soeur|epoux|epouse|mari|"
    r"femme|conjoint|conjointe|compagnon|compagne|voisin|voisine|collegue|ami|amie|oncle|"
    r"tante|cousin|cousine|neveu|niece|veuf|veuve|beau-pere|belle-mere|beau-frere|"
    r"belle-soeur|grand-pere|grand-mere|petit-fils|petite-fille)s?(?![a-z])"
)


def _score(entity_type: str, value: str) -> float:
    """:data:`SCORE`; an indirect identifier with nothing that singles a person out
    (« le boucher du marché couvert ») is masked but sent to review."""
    if entity_type != EntityType.INDIRECT:
        return SCORE
    if any(c.isdigit() for c in value) or _SINGLING_OUT.search(normalize(value)):
        return SCORE
    words = [w for w in _WORD.findall(value) if normalize(w) not in FUNCTION_WORDS]
    if any(regex.split(r"['’]", w)[-1][:1].isupper() for w in words[1:]):
        return SCORE  # « DRH de l'agence de Nantes »
    return REVIEW_SCORE


def _born(text: str) -> bool:
    return BIRTH_CUES.search(normalize(text)) is not None


__all__ = [
    "LlmDetector",
    "Memo",
    "Passage",
    "call_seconds",
    "estimated_calls",
    "forget",
    "is_prose",
    "memo_key",
    "passages",
    "plan",
    "record_throughput",
    "select_model",
]
