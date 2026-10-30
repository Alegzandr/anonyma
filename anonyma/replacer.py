"""Substitute assignment (batch-stable) and per-document replacement plans.

Reference implementation (Phase 0): typed tags, block characters for blackout,
and tags as a fallback for the realistic mode. Public signatures are part of the
contract.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from anonyma.config import Config
from anonyma.models import (
    Document,
    Entity,
    EntityCluster,
    MappingStore,
    Regime,
    Replacement,
    ReplacementMode,
    ReplacementPlan,
)
from anonyma.textutils import normalize

BLACKOUT_CHAR = "█"


@dataclass(slots=True)
class SubstitutionTable:
    """Picklable substitute table shared by every worker of a batch."""

    mode: ReplacementMode
    by_cluster: dict[str, str] = field(default_factory=dict, repr=False)
    #: Variant-specific substitutes: (cluster id, normalized variant) -> substitute.
    by_variant: dict[tuple[str, str], str] = field(default_factory=dict, repr=False)

    def substitute_for(self, entity: Entity) -> str:
        if self.mode is ReplacementMode.BLACKOUT:
            return BLACKOUT_CHAR * max(len(entity.value), 1)
        assert entity.cluster_id is not None
        variant = self.by_variant.get((entity.cluster_id, normalize(entity.value)))
        return variant if variant is not None else self.by_cluster[entity.cluster_id]

    def all_substitutes(self) -> set[str]:
        return set(self.by_cluster.values()) | set(self.by_variant.values())


class Replacer:
    def __init__(self, config: Config, store: MappingStore | None = None) -> None:
        if config.regime is Regime.PSEUDONYMIZATION and store is None:
            raise ValueError(
                "Le régime de pseudonymisation nécessite une table de correspondance chiffrée."
            )
        self.config = config
        self.store = store if config.regime is Regime.PSEUDONYMIZATION else None

    def assign(
        self, clusters: Mapping[str, EntityCluster], entities: Sequence[Entity]
    ) -> SubstitutionTable:
        """Give every cluster a substitute, stable for the whole batch (and across
        batches through the store in pseudonymization)."""
        table = SubstitutionTable(mode=self.config.mode)
        used = self.store.used_substitutes() if self.store else set()
        counters: dict[str, int] = {}
        first_seen = {e.cluster_id: i for i, e in reversed(list(enumerate(entities)))}
        for cluster in sorted(clusters.values(), key=lambda c: (first_seen.get(c.id, 0), c.id)):
            key = normalize(cluster.canonical)
            if self.store and (known := self.store.lookup(cluster.entity_type, key)):
                table.by_cluster[cluster.id] = known
                continue
            while True:
                counters[cluster.entity_type] = counters.get(cluster.entity_type, 0) + 1
                tag = f"[{cluster.entity_type}_{counters[cluster.entity_type]:03d}]"
                if tag not in used:
                    break
            used.add(tag)
            table.by_cluster[cluster.id] = tag
            if self.store:
                self.store.save(cluster.entity_type, key, cluster.canonical, tag)
        return table


def build_plan(
    document: Document, entities: Sequence[Entity], table: SubstitutionTable
) -> ReplacementPlan:
    """Plan for one document (not its children). Overlapping spans are merged into
    their union so that nothing detected is left visible."""
    plan = ReplacementPlan(document_id=document.id)
    by_segment: dict[str, list[Entity]] = {}
    for e in entities:
        if e.document_id == document.id and e.cluster_id in table.by_cluster:
            by_segment.setdefault(e.segment_id, []).append(e)
    texts = {s.id: s.text for s in document.segments}
    for segment_id, items in by_segment.items():
        text = texts.get(segment_id)
        if text is None:
            continue
        items.sort(key=lambda e: (e.start, -(e.end - e.start)))
        merged: list[tuple[int, int, Entity]] = []
        for e in items:
            if merged and e.start < merged[-1][1]:
                start, end, head = merged[-1]
                merged[-1] = (start, max(end, e.end), head)
            else:
                merged.append((e.start, e.end, e))
        reps = []
        for start, end, head in merged:
            substitute = table.substitute_for(head)
            if table.mode is ReplacementMode.BLACKOUT:
                substitute = BLACKOUT_CHAR * max(end - start, 1)
            reps.append(
                Replacement(
                    start=start,
                    end=end,
                    original=text[start:end],
                    substitute=substitute,
                    entity_type=head.entity_type,
                    cluster_id=head.cluster_id or "",
                )
            )
        plan.by_segment[segment_id] = reps
    return plan
