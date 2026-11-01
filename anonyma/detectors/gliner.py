"""GLiNER zero-shot NER on ONNX Runtime (profile STANDARD).

The model folder is located with ``ModelManager().path(spec.id)``; the detector
reports itself unavailable when the model (or onnxruntime/tokenizers) is missing.
The ONNX session is created lazily on the first call, once per process.
"""

from __future__ import annotations

import importlib.util
import re
import threading
from collections.abc import Sequence
from pathlib import Path

from anonyma.config import Config
from anonyma.data import first_name, is_common_word
from anonyma.detectors._custom import ACRONYMS
from anonyma.detectors._filters import mid_sentence, ml_text, plausible, skip_segment
from anonyma.logs import get_logger
from anonyma.model_catalog import DEFAULT_GLINER, ModelKind, ModelSpec, get_spec
from anonyma.models import Detector, Entity, EntityType, Profile, Segment, SegmentKind
from anonyma.names import civility_of, is_functional_name
from anonyma.registry import register_detector
from anonyma.textutils import normalize

log = get_logger("detectors.gliner")

#: Model label (English, as trained) -> entity type.
LABELS: dict[str, str] = {
    "person": EntityType.PERSON,
    "organization": EntityType.ORGANIZATION,
    "location": EntityType.LOCATION,
    "address": EntityType.ADDRESS,
    "job title": EntityType.JOB_TITLE,
    "phone number": EntityType.PHONE,
    "email address": EntityType.EMAIL,
    "date of birth": EntityType.DATE_OF_BIRTH,
    "username": EntityType.USERNAME,
    "iban": EntityType.IBAN,
    "social security number": EntityType.NIR,
    "credit card number": EntityType.CREDIT_CARD,
    "license plate number": EntityType.LICENSE_PLATE,
    "identity document number": EntityType.CASE_NUMBER,
    "ip address": EntityType.IP_ADDRESS,
}

FILENAME_TYPES = frozenset({EntityType.PERSON, EntityType.ORGANIZATION, EntityType.LOCATION})
#: File names are short and context-free: model spans there need more confidence.
FILENAME_MIN_SCORE = 0.5

_models: dict[tuple[str, str, int], object] = {}
_lock = threading.Lock()


def model_spec(config: Config) -> ModelSpec | None:
    spec = get_spec(config.gliner_model or DEFAULT_GLINER)
    return spec if spec is not None and spec.kind is ModelKind.GLINER else None


def model_dir(spec: ModelSpec) -> Path | None:
    from anonyma.model_manager import ModelManager

    path = ModelManager().path(spec.id)
    if path is None:
        return None
    onnx = spec.extra.get("onnx", "model.onnx")
    required = [onnx, "tokenizer.json"]
    return path if all((path / name).is_file() for name in required) else None


@register_detector
class GlinerDetector(Detector):
    name = "gliner"
    min_profile = Profile.STANDARD
    deterministic = False

    @classmethod
    def is_available(cls, config: Config) -> bool:
        if importlib.util.find_spec("onnxruntime") is None:
            return False
        if importlib.util.find_spec("tokenizers") is None:
            return False
        spec = model_spec(config)
        return spec is not None and model_dir(spec) is not None

    def __init__(self, config: Config) -> None:
        super().__init__(config)
        self.labels: dict[str, str] = dict(LABELS)
        for label, entity_type in config.custom_labels.items():
            if label.strip():
                self.labels[label.strip()] = entity_type.strip().upper() or EntityType.BUSINESS_TERM
        self.threshold = config.gliner_threshold
        #: File names only hold names, organisations and places worth a model pass;
        #: numbers, e-mails and identifiers there are left to the regex rules.
        self.filename_labels = [
            label
            for label, entity_type in self.labels.items()
            if entity_type in FILENAME_TYPES or label in config.custom_labels
        ]

    def _model(self) -> object:
        from anonyma.gliner_onnx import GlinerOnnx, default_threads

        spec = model_spec(self.config)
        if spec is None:
            raise RuntimeError("GLiNER model not configured")
        path = model_dir(spec)
        if path is None:
            raise RuntimeError("GLiNER model files missing")
        threads = self.config.gliner_threads or default_threads(self.config.workers)
        key = (str(path), spec.extra.get("onnx", "model.onnx"), threads)
        with _lock:
            if key not in _models:
                _models[key] = GlinerOnnx(path, key[1], threads=threads)
            return _models[key]

    def detect(self, segments: Sequence[Segment], document_id: str) -> list[Entity]:
        from anonyma.gliner_onnx import GlinerOnnx

        todo = [s for s in segments if has_words(s.text) and not skip_segment(s)]
        if not todo:
            return []
        model = self._model()
        assert isinstance(model, GlinerOnnx)
        names = [s for s in todo if s.context.kind is SegmentKind.FILENAME]
        others = [s for s in todo if s.context.kind is not SegmentKind.FILENAME]
        found: list[Entity] = []
        for group, labels in ((others, list(self.labels)), (names, self.filename_labels)):
            if not group or not labels:
                continue
            texts = [ml_text(s) for s in group]
            predictions = model.predict(texts, labels, threshold=self.threshold)
            for segment, text, spans in zip(group, texts, predictions, strict=True):
                filename = segment.context.kind is SegmentKind.FILENAME
                kept: list[Entity] = []
                for span in spans:
                    entity_type = self.labels.get(span.label)
                    if entity_type is None:
                        continue
                    start, end = _tight(text, span.start, span.end)
                    if entity_type == EntityType.PERSON:
                        start = _skip_leading_words(text, start, end)
                    if filename and entity_type in _NAME_TYPES:
                        # « RIB_chateau-lescure »: the document type stays readable.
                        start = _skip_document_words(text, start, end)
                    value = segment.text[start:end]
                    score = span.score
                    if filename and score < FILENAME_MIN_SCORE:
                        continue
                    ocr = segment.context.kind is SegmentKind.OCR
                    capitalized = not filename and value[:1].isupper() and mid_sentence(text, start)
                    if not plausible(
                        entity_type,
                        value,
                        score,
                        filename=filename,
                        ocr=ocr,
                        capitalized_mid_sentence=capitalized,
                    ) or not in_context(entity_type, text, start, end):
                        continue
                    kept.append(
                        Entity(
                            entity_type=entity_type,
                            value=value,
                            document_id=document_id,
                            segment_id=segment.id,
                            start=start,
                            end=end,
                            score=round(score, 3),
                            detector=self.name,
                            attributes={"label": span.label},
                        )
                    )
                found.extend(job_titles_near_people(text, kept))
        return found


#: A job title identifies someone only next to them: this close, across at most one
#: line break (a wrapped sentence), or on the line just before or after the
#: person's in a signature block.
JOB_TITLE_WINDOW = 120
#: An alphanumeric run (letters, digits).
_ALNUM_RUN = re.compile(r"[^\W_]+")


def has_words(text: str) -> bool:
    """Text worth a model pass: a run of at least two letters and no digit. GUIDs,
    hexadecimal ids, ISO dates and timestamps and numbers (JSON exports hold
    thousands of them) have none; the regex rules handle them."""
    return any(len(run) >= 2 and run.isalpha() for run in _ALNUM_RUN.findall(text))


_PARAGRAPH_BREAK = re.compile(r"\n[ \t ]*\n")


def job_titles_near_people(text: str, entities: list[Entity]) -> list[Entity]:
    """Drop job titles with no person nearby (« le boucher du marché », « comédien »).

    A job title is an indirect identifier only when it describes a given person
    (« Mme Durand, présidente »); alone it is ordinary text. A title that makes most
    of its segment (a table cell, a signature line) is kept as it is.
    """
    people = [e for e in entities if e.entity_type == EntityType.PERSON]
    letters = sum(1 for c in text if c.isalpha()) or 1
    out: list[Entity] = []
    for e in entities:
        if e.entity_type != EntityType.JOB_TITLE:
            out.append(e)
            continue
        if 2 * sum(1 for c in e.value if c.isalpha()) >= letters:
            out.append(e)
            continue
        for p in people:
            lo, hi = (p.end, e.start) if p.end <= e.start else (e.end, p.start)
            between = text[lo : max(lo, hi)]
            breaks = between.count("\n")
            if (breaks <= 1 and hi - lo <= JOB_TITLE_WINDOW) or (
                breaks == 1 and _line_neighbours(text, p, e)
            ):
                out.append(e)
                break
    return out


def _line_neighbours(text: str, a: Entity, b: Entity) -> bool:
    """``a`` and ``b`` on consecutive lines, each line short (a signature block:
    « Karim Ouchène » / « Délégué à la protection des données »)."""
    first, second = (a, b) if a.start <= b.start else (b, a)
    line_start = text.rfind("\n", 0, first.start) + 1
    line_end = text.find("\n", second.end)
    lines = text[line_start : line_end if line_end >= 0 else len(text)].split("\n")
    return len(lines) == 2 and all(len(line.strip()) <= SIGNATURE_LINE for line in lines)


#: Longest line of a signature block (name, title, organisation).
SIGNATURE_LINE = 90


#: Words naming a product, an offer or a programme: the capitalized words after them
#: (« Borne Hexa Charge 22 kW », « offre Hexa Flex 24 », « programme Advenir ») are
#: a catalogue name, not a person, an organisation or a place.
PRODUCT_CUES = frozenset(
    {
        "offre", "offres", "borne", "bornes", "pack", "forfait", "formule", "gamme", "produit",
        "modele", "option", "abonnement", "programme", "dispositif", "application", "logiciel",
        "plateforme", "solution", "tarif", "garantie",
    }
)  # fmt: skip
_UNIT = re.compile(r"\d\s*(?:k?w|kva|kwh|mwh|wh|go|mo|ghz|mhz|cv|km|%)\b", re.I)
_PREVIOUS_WORD = re.compile(r"([^\W\d_]+)[\s(«\"']*$")
#: Name types a model may confuse with products and identifiers.
_NAME_TYPES = frozenset({EntityType.PERSON, EntityType.ORGANIZATION, EntityType.LOCATION})


def in_context(entity_type: str, text: str, start: int, end: int) -> bool:
    """Whether a model name span survives its surroundings: not a catalogue name
    after (or starting with) a product cue, not a product with a power or a size, not
    the prefix of an identifier (``CRM:0004104``, ``DEV-2026-6304``), not a team."""
    if entity_type not in _NAME_TYPES:
        return True
    value = text[start:end]
    words = value.split()
    if not words:
        return False
    if normalize(words[0]) in PRODUCT_CUES and len(words) > 1:
        return False
    previous = _PREVIOUS_WORD.search(text, max(0, start - 30), start)
    if previous is not None and normalize(previous.group(1)) in PRODUCT_CUES:
        return False
    digits = any(c.isdigit() for c in value)
    if _UNIT.search(value) or (entity_type == EntityType.PERSON and digits):
        return False
    after = text[end : end + 2]
    if len(words) == 1 and after[:1] in ":-_/#" and after[1:2].isdigit():
        return False
    return not is_functional_name(value)


#: Words a model span may start with that never belong to the name.
_LEADING = frozenset(
    {"bonjour", "bonsoir", "salut", "hello", "hi", "dear", "cher", "chere", "chers", "cheres",
     "merci", "cordialement", "coucou"}
)  # fmt: skip


def _skip_leading_words(text: str, start: int, end: int) -> int:
    """``Bonjour Rémi`` -> ``Rémi``; ``Rappeler M. Joubert`` -> ``M. Joubert``."""
    words = list(_WORD.finditer(text, start, end))
    while len(words) > 1 and normalize(words[0].group()) in _LEADING:
        words.pop(0)
    begin = words[0].start() if words else start
    for i, w in enumerate(words[1:], start=1):
        if civility_of(w.group()) is not None:
            leading = words[:i]
            if all(is_common_word(x.group()) and first_name(x.group()) is None for x in leading):
                return w.start()
            break
    return begin


_WORD = re.compile(r"[^\W\d_]+(?:['’\-][^\W\d_]+)*\.?")


def _skip_document_words(text: str, start: int, end: int) -> int:
    """``RIB chateau-lescure`` -> ``chateau-lescure`` in a file name: leading
    ordinary words and document acronyms (RIB, CV, PV) are the type of the file. A
    span of ordinary words only is kept whole (« riviere » may be a surname)."""
    for w in _WORD.finditer(text, start, end):
        word = w.group().rstrip(".")
        if word.upper() in ACRONYMS or (is_common_word(word) and first_name(word) is None):
            continue
        return w.start()
    return start


def _tight(text: str, start: int, end: int) -> tuple[int, int]:
    """Strip surrounding spaces and punctuation (``# Projet`` -> ``Projet``)."""
    while start < end and not (text[start].isalnum() or text[start] in "@+"):
        start += 1
    while end > start and not (text[end - 1].isalnum() or text[end - 1] in ".)"):
        end -= 1
    return start, end


__all__ = [
    "LABELS",
    "PRODUCT_CUES",
    "GlinerDetector",
    "in_context",
    "job_titles_near_people",
    "model_dir",
    "model_spec",
]
