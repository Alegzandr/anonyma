"""Business lists and always-mask terms: accent/case-insensitive multi-pattern search.

All terms are compiled once into an Aho-Corasick automaton, so the cost per segment
does not grow with the size of the lists. Overlapping terms are all reported (the
replacement plan merges overlaps).
"""

from __future__ import annotations

from collections.abc import Sequence

from anonyma.config import Config
from anonyma.models import Detector, Entity, Profile, Segment
from anonyma.registry import register_detector
from anonyma.textsearch import MultiMatcher


@register_detector
class TermListDetector(Detector):
    name = "listes_metier"
    min_profile = Profile.LIGHT

    def __init__(self, config: Config) -> None:
        super().__init__(config)
        self.terms = config.mask_terms()
        self._matcher: MultiMatcher[str] = MultiMatcher()
        for value, entity_type in self.terms:
            self._matcher.add(value, entity_type)

    def detect(self, segments: Sequence[Segment], document_id: str) -> list[Entity]:
        found: list[Entity] = []
        if not len(self._matcher):
            return found
        for segment in segments:
            for match in self._matcher.find(segment.text):
                for entity_type in dict.fromkeys(match.payloads):
                    found.append(
                        Entity(
                            entity_type=entity_type,
                            value=segment.text[match.start : match.end],
                            document_id=document_id,
                            segment_id=segment.id,
                            start=match.start,
                            end=match.end,
                            score=1.0,
                            detector=self.name,
                        )
                    )
        return found
