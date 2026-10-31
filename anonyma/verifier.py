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

Short-value policy: values shorter than 3 characters (after normalisation) and
numbers shorter than 4 digits are not searched; single words that are also common
words (``Petit``, ``Rose``) only count when capitalized.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence

from anonyma import checksums
from anonyma.config import Config
from anonyma.detectors._custom import STOP_WORDS
from anonyma.models import (
    Detector,
    Document,
    DocumentStatus,
    EntityCluster,
    EntityType,
    Issue,
    Residue,
    Segment,
    Severity,
    VerificationResult,
)
from anonyma.names import PARTICLES, civility_of
from anonyma.resolver import NAME_TYPES, cluster_forms
from anonyma.textsearch import MultiMatcher
from anonyma.textutils import normalize, normalize_with_map

ORIGINAL_VALUE = "original_value"
DETECTOR_HIT = "detector"
TABLE_DETECTOR = "table"
TABLE_VARIANT_DETECTOR = "table_variante"
MIN_VALUE_LENGTH = 3
MIN_DIGITS = 4


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
        self.detectors = [d for d in detectors if getattr(d, "verify", True)]

    def verify(
        self,
        output: Document,
        sensitive: Sequence[tuple[str, str]],
        substitutes: set[str],
        allowed: Sequence[str] = (),
    ) -> VerificationResult:
        """Check a re-extracted output tree.

        ``sensitive``: every original ``(value, entity_type)`` of the batch.
        ``substitutes``: values introduced by the replacer (ignored by detectors).
        ``allowed``: values the user chose to keep (whitelist, rejected clusters).
        """
        result = VerificationResult(verified=True)
        allowed_keys = {normalize(a) for a in allowed} | self.config.whitelist_keys()
        table = self._table(sensitive, allowed_keys)
        subs: MultiMatcher[str] = MultiMatcher()
        for sub in substitutes:
            if sub.strip():
                subs.add(sub, sub)

        for doc in output.walk():
            if doc.status is not DocumentStatus.EXTRACTED or doc.has_errors:
                result.verified = False
                errors = [i for i in doc.issues if i.severity is Severity.ERROR]
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
                prepared = normalize_with_map(segment.text)
                spans = [(m.start, m.end) for m in subs.find(segment.text, prepared=prepared)]
                covered[segment.id] = spans
                for m in table.find(segment.text, prepared=prepared):
                    if _inside(m.start, m.end, spans):
                        continue
                    entity_type, source = m.payloads[0]
                    result.residues.append(
                        Residue(
                            location=doc.name,
                            segment_label=segment.context.label,
                            entity_type=entity_type,
                            reason=ORIGINAL_VALUE,
                            detector=source,
                            value=segment.text[m.start : m.end],
                        )
                    )
            segments = {s.id: s for s in doc.segments}
            for detector in self.detectors:
                for entity in detector.detect(doc.segments, doc.id):
                    found = segments.get(entity.segment_id)
                    if found is None or entity.score < self.config.min_score:
                        continue
                    segment = found
                    value = segment.text[entity.start : entity.end]
                    if normalize(value) in allowed_keys:
                        continue
                    if _neutral_outside(segment, entity.start, entity.end, covered[segment.id]):
                        continue
                    result.residues.append(
                        Residue(
                            location=doc.name,
                            segment_label=segment.context.label,
                            entity_type=entity.entity_type,
                            reason=DETECTOR_HIT,
                            detector=entity.detector,
                            value=value,
                        )
                    )
        return result

    @staticmethod
    def _table(
        sensitive: Sequence[tuple[str, str]], allowed_keys: set[str]
    ) -> MultiMatcher[tuple[str, str]]:
        table: MultiMatcher[tuple[str, str]] = MultiMatcher()
        by_type: dict[str, set[str]] = {}
        for value, entity_type in sensitive:
            if normalize(value) in allowed_keys or not worth_checking(value):
                continue
            by_type.setdefault(entity_type, set()).add(value)
        for entity_type, values in by_type.items():
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


def _needs_capital(entity_type: str, value: str) -> bool:
    from anonyma.names import needs_capital

    return entity_type in NAME_TYPES and needs_capital(value)


def _derived(entity_type: str, values: set[str]) -> Iterator[tuple[str, bool]]:
    """Derived forms of the batch values (same rules as propagation)."""
    if entity_type not in (
        EntityType.PERSON,
        EntityType.PHONE,
        EntityType.IBAN,
        EntityType.CREDIT_CARD,
        EntityType.SIREN,
        EntityType.SIRET,
        EntityType.NIR,
    ):
        return
    for value in values:
        cluster = EntityCluster(id="", entity_type=entity_type, canonical=value, variants={value})
        for form, cap in cluster_forms(cluster):
            if form != value:
                yield form, cap


def _inside(start: int, end: int, spans: Sequence[tuple[int, int]]) -> bool:
    return any(s <= start and end <= e for s, e in spans)


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
        return False
    return True


__all__ = ["DETECTOR_HIT", "ORIGINAL_VALUE", "Verifier", "worth_checking"]
