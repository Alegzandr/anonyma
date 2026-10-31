"""LLM pass (profile FULL): indirect identifiers and ambiguous segments.

Only segments with indirect-identifier cues (roles, family ties, « le seul... »,
organisational units) or capitalized words the lexicon does not explain are sent to
the model, unless ``Config.llm_all_segments`` is set; segments with fewer than
:data:`MIN_LETTERS` letters never are. Short consecutive segments share one call.
Every mention is grounded: kept only where it occurs in the segments sent, and
filtered by the same plausibility rules as GLiNER. Skipped by the verifier
(``verify = False``): too slow and not deterministic enough for a second pass.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator, Sequence
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
from anonyma.logs import get_logger
from anonyma.model_catalog import LLM_PREFERENCE, ModelKind, ModelSpec, get_spec
from anonyma.models import Detector, Entity, EntityType, Profile, Segment, SegmentKind
from anonyma.registry import register_detector
from anonyma.textutils import normalize

log = get_logger("detectors.llm")

SCORE = 0.75
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
CUES = regex.compile(
    rf"(?<![a-z])(?:{_PERSON_WORDS}|agence|service|filiale|succursale|antenne|delegation|"
    r"mairie|ecole|college|lycee|hopital|clinique|cabinet|usine|magasin|boutique|equipe|"
    r"departement)(?:e|s|es)?(?![a-z])"
)
#: An LLM date of birth needs a birth cue in its segment (other dates are left to
#: the date rules, which mask them as DATE).
BIRTH_CUES = regex.compile(r"(?<![a-z])(?:ne|nee|naissance|ddn|naitre|born|birth)(?![a-z])")
_TOKEN = regex.compile(r"[\p{L}\p{N}]+")
_CAPITALIZED = regex.compile(r"(?<=[\p{Ll},;] )\p{Lu}\p{Ll}+")

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


@register_detector
class LlmDetector(Detector):
    name = "llm"
    min_profile = Profile.FULL
    deterministic = False
    verify = False

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

    def wanted(self, text: str) -> bool:
        if sum(c.isalpha() for c in text) < MIN_LETTERS:
            return False
        if self.config.llm_all_segments:
            return True
        if CUES.search(normalize(text)):
            return True
        return any(normalize(m.group()) not in self.lexicon for m in _CAPITALIZED.finditer(text))

    def detect(self, segments: Sequence[Segment], document_id: str) -> list[Entity]:
        todo = [s for s in segments if not skip_segment(s) and self.wanted(s.text)]
        if not todo:
            return []
        runner = self._runner()
        found: list[Entity] = []
        seen: set[tuple[str, int, int]] = set()
        for group, text in batches(todo, runner.window_tokens, runner.count_tokens):
            for mention in runner.extract(text):
                for segment in group:
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
                                score=SCORE,
                                detector=self.name,
                            )
                        )
        return found


def batches(
    segments: Sequence[Segment],
    budget: int,
    count: Callable[[str], int],
    short: int = SHORT_SEGMENT_TOKENS,
) -> Iterator[tuple[list[Segment], str]]:
    """Model calls for ``segments``, each at most ``budget`` tokens of text.

    Consecutive short segments (cells, header fields, signature lines: at most
    ``short`` tokens) share one call, separated by blank lines: a lone « Merci » is
    read in its context, and 100 cells do not mean 100 calls. A longer segment goes
    alone (the model lists fewer entities when sentences are pooled), cut into
    overlapping windows when it exceeds ``budget``.

    Each item is ``(segments, text)``: mentions are searched in every segment of the
    group (a window of a long segment is searched in the whole segment).
    """
    group: list[Segment] = []
    used = 0
    separator = count(llm.SEGMENT_SEPARATOR)

    def flush() -> Iterator[tuple[list[Segment], str]]:
        if group:
            yield list(group), llm.SEGMENT_SEPARATOR.join(s.text for s in group)

    for segment in segments:
        size = count(segment.text)
        if size > short:
            yield from flush()
            group, used = [], 0
            for _, window in llm.windows(segment.text, budget, count, llm.WINDOW_OVERLAP):
                yield [segment], window
            continue
        if group and used + separator + size > budget:
            yield from flush()
            group, used = [], 0
        used += size + (separator if group else 0)
        group.append(segment)
    yield from flush()


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


def _born(text: str) -> bool:
    return BIRTH_CUES.search(normalize(text)) is not None


__all__ = ["LlmDetector", "batches", "select_model"]
