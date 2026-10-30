"""Verification: rescan an output with every detector and against the batch table.

Reference implementation (Phase 0). Public signatures are part of the contract.
"""

from __future__ import annotations

from collections.abc import Sequence

from anonyma.config import Config
from anonyma.models import (
    Detector,
    Document,
    DocumentStatus,
    Issue,
    Residue,
    Severity,
    VerificationResult,
)
from anonyma.textutils import find_all, normalize

ORIGINAL_VALUE = "original_value"
DETECTOR_HIT = "detector"
MIN_VALUE_LENGTH = 3


class Verifier:
    def __init__(self, config: Config, detectors: Sequence[Detector]) -> None:
        self.config = config
        self.detectors = list(detectors)

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
        values = [
            (v, t)
            for v, t in sensitive
            if len(normalize(v)) >= MIN_VALUE_LENGTH and normalize(v) not in allowed_keys
        ]
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
            for segment in doc.segments:
                for value, entity_type in values:
                    for start, end in find_all(segment.text, value):
                        result.residues.append(
                            Residue(
                                location=doc.name,
                                segment_label=segment.context.label,
                                entity_type=entity_type,
                                reason=ORIGINAL_VALUE,
                                detector="table",
                                value=segment.text[start:end],
                            )
                        )
            for detector in self.detectors:
                for entity in detector.detect(doc.segments, doc.id):
                    if entity.score < self.config.min_score:
                        continue
                    if normalize(entity.value) in allowed_keys:
                        continue
                    text = doc.segment(entity.segment_id).text
                    if _inside_substitute(text, entity.start, entity.end, substitutes):
                        continue
                    result.residues.append(
                        Residue(
                            location=doc.name,
                            segment_label=doc.segment(entity.segment_id).context.label,
                            entity_type=entity.entity_type,
                            reason=DETECTOR_HIT,
                            detector=entity.detector,
                            value=entity.value,
                        )
                    )
        return result


def _inside_substitute(text: str, start: int, end: int, substitutes: set[str]) -> bool:
    for sub in substitutes:
        if not sub:
            continue
        pos = text.find(sub)
        while pos != -1:
            if pos <= start and end <= pos + len(sub):
                return True
            pos = text.find(sub, pos + 1)
    return False
