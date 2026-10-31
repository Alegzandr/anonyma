"""QR codes, Data Matrix and barcodes: always masked as a whole (every profile).

Extractors emit the decoded content of each code as a ``SegmentKind.BARCODE``
segment whose bbox (and every char box) is the code's region. This detector returns
one ``CODE_BARRES`` entity spanning the whole segment, so the writer blacks out the
code itself. The other detectors still read the decoded content: entities found
inside it (a name, an e-mail, a case reference) keep their own clusters and are
propagated to the rest of the batch; the replacement plan merges them into the
whole-code span.
"""

from __future__ import annotations

from collections.abc import Sequence

from anonyma.models import Detector, Entity, EntityType, Profile, Segment, SegmentKind
from anonyma.registry import register_detector

DETECTOR_NAME = "codes"


@register_detector
class CodeDetector(Detector):
    name = DETECTOR_NAME
    min_profile = Profile.LIGHT

    def detect(self, segments: Sequence[Segment], document_id: str) -> list[Entity]:
        found: list[Entity] = []
        for segment in segments:
            if segment.context.kind is not SegmentKind.BARCODE or not segment.text:
                continue
            found.append(
                Entity(
                    entity_type=EntityType.BARCODE,
                    value=segment.text,
                    document_id=document_id,
                    segment_id=segment.id,
                    start=0,
                    end=len(segment.text),
                    score=1.0,
                    detector=self.name,
                )
            )
        return found


__all__ = ["DETECTOR_NAME", "CodeDetector"]
