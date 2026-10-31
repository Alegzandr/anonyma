"""Outlook thread indexes (``Thread-Index``, ``conversationindex``...) wherever they
appear, in every profile and whatever ``Config.mask_opaque_ids`` says: an index left
as is gives away the real start date of its thread (see :mod:`anonyma.threadindex`).
The replacer rebuilds each one, so a kept identifier stays a valid index."""

from __future__ import annotations

from collections.abc import Sequence

from anonyma import threadindex
from anonyma.models import Detector, Entity, EntityType, Profile, Segment
from anonyma.registry import register_detector

DETECTOR_NAME = "index_conversation"


@register_detector
class ThreadIndexDetector(Detector):
    name = DETECTOR_NAME
    min_profile = Profile.LIGHT

    def detect(self, segments: Sequence[Segment], document_id: str) -> list[Entity]:
        found: list[Entity] = []
        for segment in segments:
            text = segment.text
            if len(text) < 30 or not ("01" in text or "AQ" in text):
                continue
            for start, end in threadindex.find(text):
                found.append(
                    Entity(
                        entity_type=EntityType.THREAD_INDEX,
                        value=text[start:end],
                        document_id=document_id,
                        segment_id=segment.id,
                        start=start,
                        end=end,
                        score=1.0,
                        detector=self.name,
                    )
                )
        return found
