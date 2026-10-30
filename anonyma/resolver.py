"""Entity resolution and batch-wide propagation.

Reference implementation (Phase 0): exact clustering on (type, normalized value)
and exact-variant propagation. Public signatures are part of the contract.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence

from anonyma.config import Config
from anonyma.models import (
    Document,
    DocumentStatus,
    Entity,
    EntityCluster,
    ReviewStatus,
)
from anonyma.textutils import find_all, normalize

PROPAGATION_DETECTOR = "propagation"
MIN_PROPAGATION_LENGTH = 3


def cluster_key(entity_type: str, value: str) -> str:
    return f"{entity_type}:{normalize(value)}"


def _cluster_id(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def resolve(
    entities: Sequence[Entity],
    config: Config,
    clusters: dict[str, EntityCluster] | None = None,
) -> tuple[list[Entity], dict[str, EntityCluster]]:
    """Deduplicate entities, drop whitelisted ones, and group them into clusters.

    ``clusters`` (mutated and returned) lets later calls extend an existing
    resolution while keeping cluster ids stable; entities whose ``cluster_id`` is
    already in ``clusters`` keep it.
    """
    clusters = clusters if clusters is not None else {}
    whitelist = config.whitelist_keys()
    by_key: dict[tuple[str, str, int, int], Entity] = {}
    for entity in entities:
        if normalize(entity.value) in whitelist and entity.review is not ReviewStatus.MANUAL:
            continue
        previous = by_key.get(entity.key)
        if previous is None or entity.score > previous.score:
            by_key[entity.key] = entity

    index = {cluster_key(c.entity_type, v): c for c in clusters.values() for v in c.variants}
    kept = sorted(by_key.values(), key=lambda e: e.key)
    for entity in kept:
        if entity.cluster_id in clusters:
            cluster = clusters[entity.cluster_id]
        else:
            key = cluster_key(entity.entity_type, entity.value)
            cluster = index.get(key)  # type: ignore[assignment]
            if cluster is None:
                cluster = EntityCluster(
                    id=_cluster_id(key), entity_type=entity.entity_type, canonical=entity.value
                )
                clusters[cluster.id] = cluster
                index[key] = cluster
            entity.cluster_id = cluster.id
        cluster.variants.add(entity.value)
        cluster.detectors.add(entity.detector)
        cluster.max_score = max(cluster.max_score, entity.score)
        if len(entity.value) > len(cluster.canonical):
            cluster.canonical = entity.value
        if entity.review is ReviewStatus.MANUAL:
            cluster.review = ReviewStatus.MANUAL
    for cluster in clusters.values():
        cluster.needs_review = (
            cluster.max_score < config.review_threshold and cluster.review is ReviewStatus.AUTO
        )
    return kept, clusters


def propagate(
    documents: Sequence[Document],
    clusters: Mapping[str, EntityCluster],
    existing: Sequence[Entity],
    config: Config,
) -> list[Entity]:
    """Find every other occurrence of each cluster variant in the whole batch.

    Returns only *new* entities (cluster id set), never overlapping ``existing``.
    """
    taken: dict[tuple[str, str], list[tuple[int, int]]] = {}
    for e in existing:
        taken.setdefault((e.document_id, e.segment_id), []).append((e.start, e.end))
    variants = [
        (variant, cluster)
        for cluster in clusters.values()
        for variant in cluster.variants
        if len(normalize(variant)) >= MIN_PROPAGATION_LENGTH
    ]
    found: list[Entity] = []
    for root in documents:
        for doc in root.walk():
            if doc.status is not DocumentStatus.EXTRACTED:
                continue
            for segment in doc.segments:
                spans = taken.setdefault((doc.id, segment.id), [])
                for variant, cluster in variants:
                    for start, end in find_all(segment.text, variant):
                        if any(s < end and start < e for s, e in spans):
                            continue
                        spans.append((start, end))
                        found.append(
                            Entity(
                                entity_type=cluster.entity_type,
                                value=segment.text[start:end],
                                document_id=doc.id,
                                segment_id=segment.id,
                                start=start,
                                end=end,
                                score=cluster.max_score,
                                detector=PROPAGATION_DETECTOR,
                                cluster_id=cluster.id,
                                review=(
                                    ReviewStatus.MANUAL
                                    if cluster.review is ReviewStatus.MANUAL
                                    else ReviewStatus.AUTO
                                ),
                            )
                        )
    return found
