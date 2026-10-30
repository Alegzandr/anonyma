"""Business lists and always-mask terms: exact, accent/case-insensitive matching."""

from __future__ import annotations

from collections.abc import Sequence

from anonyma.config import Config
from anonyma.models import Detector, Entity, Profile, Segment
from anonyma.registry import register_detector
from anonyma.textutils import find_all


@register_detector
class TermListDetector(Detector):
    name = "listes_metier"
    min_profile = Profile.LIGHT

    def __init__(self, config: Config) -> None:
        super().__init__(config)
        self.terms = config.mask_terms()

    def detect(self, segments: Sequence[Segment], document_id: str) -> list[Entity]:
        found: list[Entity] = []
        for segment in segments:
            for value, entity_type in self.terms:
                for start, end in find_all(segment.text, value):
                    found.append(
                        Entity(
                            entity_type=entity_type,
                            value=segment.text[start:end],
                            document_id=document_id,
                            segment_id=segment.id,
                            start=start,
                            end=end,
                            score=1.0,
                            detector=self.name,
                        )
                    )
        return found
