"""LLM pass (profile FULL): indirect identifiers and ambiguous segments.

Only segments with indirect-identifier cues (roles, family ties, « le seul... »,
organisational units) or capitalized words the lexicon does not explain are sent to
the model, unless ``Config.llm_all_segments`` is set. Skipped by the verifier
(``verify = False``): too slow and not deterministic enough for a second pass.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from pathlib import Path

import regex

from anonyma import llm
from anonyma.config import Config
from anonyma.data import lexicon
from anonyma.logs import get_logger
from anonyma.model_catalog import LLM_PREFERENCE, ModelKind, ModelSpec, get_spec
from anonyma.models import Detector, Entity, Profile, Segment
from anonyma.registry import register_detector
from anonyma.textutils import find_all, normalize

log = get_logger("detectors.llm")

SCORE = 0.75
CUES = regex.compile(
    r"(?<![a-z])(?:drh|rh|directeur|directrice|responsable|chef|cheffe|gerant|gerante|"
    r"president|presidente|pdg|dg|maire|adjoint|adjointe|medecin|docteur|infirmier|"
    r"infirmiere|professeur|instituteur|institutrice|avocat|avocate|notaire|employe|employee|"
    r"salarie|salariee|stagiaire|collegue|voisin|voisine|epoux|epouse|mari|conjoint|"
    r"conjointe|fils|fille|pere|mere|frere|soeur|oncle|tante|cousin|cousine|eleve|patient|"
    r"patiente|locataire|proprietaire|le seul|la seule|l'unique|agence|service|filiale|"
    r"succursale|antenne|delegation|mairie|ecole|college|lycee|hopital|clinique|cabinet|"
    r"usine|magasin|boutique|equipe|departement)(?![a-z])"
)
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
        if not any(c.isalpha() for c in text):
            return False
        if self.config.llm_all_segments:
            return True
        if CUES.search(normalize(text)):
            return True
        return any(normalize(m.group()) not in self.lexicon for m in _CAPITALIZED.finditer(text))

    def detect(self, segments: Sequence[Segment], document_id: str) -> list[Entity]:
        todo = [s for s in segments if self.wanted(s.text)]
        if not todo:
            return []
        runner = self._runner()
        found: list[Entity] = []
        for segment in todo:
            seen: set[tuple[int, int]] = set()
            for offset, chunk in llm.chunks(segment.text):
                for mention in runner.extract(chunk):
                    for start, end in find_all(chunk, mention.text):
                        span = (offset + start, offset + end)
                        if span in seen:
                            continue
                        seen.add(span)
                        found.append(
                            Entity(
                                entity_type=mention.entity_type,
                                value=segment.text[span[0] : span[1]],
                                document_id=document_id,
                                segment_id=segment.id,
                                start=span[0],
                                end=span[1],
                                score=SCORE,
                                detector=self.name,
                            )
                        )
        return found


__all__ = ["LlmDetector", "select_model"]
