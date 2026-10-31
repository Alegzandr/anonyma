"""Review state computed locally from a ``BatchAnalysis`` and ``ReviewDecisions``.

Mirrors what ``Pipeline.apply`` does with the decisions (retype, confirm, reject,
rejected mentions, manual entities propagated batch-wide) so that the review
screen can show an anonymized *preview* without writing anything. Substitutes
are assigned with a throw-away ``Replacer`` in the anonymization regime: no
mapping store is ever touched, so pseudonymization tags shown here are only
indicative.

Qt-free: unit-testable on its own.
"""

from __future__ import annotations

import enum
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace

from anonyma.models import (
    Document,
    DocumentStatus,
    Entity,
    EntityCluster,
    ManualEntity,
    Regime,
    Replacement,
    ReplacementMode,
    ReviewDecisions,
    ReviewStatus,
)
from anonyma.pipeline import MANUAL_DETECTOR, BatchAnalysis
from anonyma.replacer import BLACKOUT_CHAR, Replacer, SubstitutionTable, build_plan
from anonyma.resolver import propagate, resolve
from anonyma.textutils import find_all, normalize

MentionKey = tuple[str, str, int, int]


class RowState(enum.StrEnum):
    AUTO = "auto"
    REVIEW = "a_revoir"
    CONFIRMED = "valide"
    REJECTED = "rejete"
    MENTION_REJECTED = "occurrence_rejetee"
    MANUAL = "manuel"


STATE_LABELS: dict[RowState, str] = {
    RowState.AUTO: "Détecté",
    RowState.REVIEW: "À revoir",
    RowState.CONFIRMED: "Validé",
    RowState.REJECTED: "Rejeté",
    RowState.MENTION_REJECTED: "Occurrence rejetée",
    RowState.MANUAL: "Ajout manuel",
}

STATE_LEVELS: dict[RowState, str] = {
    RowState.AUTO: "info",
    RowState.REVIEW: "warning",
    RowState.CONFIRMED: "success",
    RowState.REJECTED: "neutral",
    RowState.MENTION_REJECTED: "neutral",
    RowState.MANUAL: "success",
}


@dataclass(slots=True)
class EffectiveState:
    """Entities and substitutes ``apply`` would use with the current decisions."""

    entities: list[Entity]
    clusters: dict[str, EntityCluster]  # every cluster, rejected ones included
    active: dict[str, EntityCluster]
    table: SubstitutionTable
    by_document: dict[str, list[Entity]] = field(default_factory=dict)

    def for_document(self, document_id: str) -> list[Entity]:
        return self.by_document.get(document_id, [])


def _manual_entities(analysis: BatchAnalysis, manual: Sequence[ManualEntity]) -> list[Entity]:
    found: list[Entity] = []
    for item in manual:
        for doc in analysis.all_documents():
            if doc.status is not DocumentStatus.EXTRACTED:
                continue
            for seg in doc.segments:
                for start, end in find_all(seg.text, item.value):
                    found.append(
                        Entity(
                            entity_type=item.entity_type,
                            value=seg.text[start:end],
                            document_id=doc.id,
                            segment_id=seg.id,
                            start=start,
                            end=end,
                            score=1.0,
                            detector=MANUAL_DETECTOR,
                            review=ReviewStatus.MANUAL,
                        )
                    )
    return found


def compute_effective(
    analysis: BatchAnalysis, decisions: ReviewDecisions, mode: ReplacementMode | None = None
) -> EffectiveState:
    """Replay ``Pipeline.apply``'s decision handling without writing anything."""
    config = analysis.config
    clusters = {
        cid: replace(c, variants=set(c.variants), detectors=set(c.detectors))
        for cid, c in analysis.clusters.items()
    }
    for cid, new_type in decisions.retyped_clusters.items():
        if cid in clusters:
            clusters[cid].entity_type = new_type
    for cid in decisions.confirmed_clusters:
        if cid in clusters:
            clusters[cid].review = ReviewStatus.CONFIRMED
            clusters[cid].needs_review = False
    rejected = set(decisions.rejected_clusters)
    for cid in rejected:
        if cid in clusters:
            clusters[cid].review = ReviewStatus.REJECTED

    entities = [
        replace(e, entity_type=clusters[e.cluster_id].entity_type)
        if e.cluster_id in clusters
        else replace(e)
        for e in analysis.entities
        if e.cluster_id not in rejected and e.key not in decisions.rejected_mentions
    ]
    if decisions.manual:
        manual = _manual_entities(analysis, decisions.manual)
        entities, clusters = resolve([*entities, *manual], config, clusters)
        extra = propagate(analysis.documents, clusters, entities, config)
        entities, clusters = resolve([*entities, *extra], config, clusters)
    for cid in decisions.confirmed_clusters:
        if cid in clusters:
            clusters[cid].needs_review = False

    active = {cid: c for cid, c in clusters.items() if cid not in rejected}
    entities = [e for e in entities if e.cluster_id in active]
    preview_config = replace(
        config, regime=Regime.ANONYMIZATION, mode=mode if mode is not None else config.mode
    )
    table = Replacer(preview_config, None).assign(active, entities)
    state = EffectiveState(entities, clusters, active, table)
    for e in entities:
        state.by_document.setdefault(e.document_id, []).append(e)
    return state


# ---------------------------------------------------------------------------
# Rows of the entity list
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EntityRow:
    entity: Entity
    entity_type: str  # after retyping
    state: RowState
    cluster_id: str
    #: Cluster flagged for review by the analysis and not decided yet.
    needs_review: bool
    manual: bool = False

    @property
    def key(self) -> MentionKey:
        return self.entity.key


def cluster_state(cluster_id: str, analysis: BatchAnalysis, decisions: ReviewDecisions) -> RowState:
    if cluster_id in decisions.rejected_clusters:
        return RowState.REJECTED
    if cluster_id in decisions.confirmed_clusters:
        return RowState.CONFIRMED
    cluster = analysis.clusters.get(cluster_id)
    if cluster is None:
        return RowState.MANUAL
    if cluster.review is ReviewStatus.MANUAL:
        return RowState.MANUAL
    return RowState.REVIEW if cluster.needs_review else RowState.AUTO


def entity_rows(
    analysis: BatchAnalysis,
    decisions: ReviewDecisions,
    effective: EffectiveState,
    document_ids: Iterable[str] | None = None,
) -> list[EntityRow]:
    """Every mention of the selected documents: detected ones (whatever their
    decision) plus mentions coming from manual additions."""
    wanted = set(document_ids) if document_ids is not None else None
    rows: list[EntityRow] = []
    known: set[MentionKey] = set()
    for e in analysis.entities:
        if wanted is not None and e.document_id not in wanted:
            continue
        cid = e.cluster_id or ""
        known.add(e.key)
        state = cluster_state(cid, analysis, decisions)
        if e.key in decisions.rejected_mentions and state is not RowState.REJECTED:
            state = RowState.MENTION_REJECTED
        rows.append(
            EntityRow(
                entity=e,
                entity_type=decisions.retyped_clusters.get(cid, e.entity_type),
                state=state,
                cluster_id=cid,
                needs_review=state is RowState.REVIEW,
            )
        )
    for e in effective.entities:
        if e.key in known or (wanted is not None and e.document_id not in wanted):
            continue
        rows.append(
            EntityRow(
                entity=e,
                entity_type=e.entity_type,
                state=RowState.MANUAL,
                cluster_id=e.cluster_id or "",
                needs_review=False,
                manual=True,
            )
        )
    rows.sort(key=lambda r: (r.entity.document_id, r.entity.segment_id, r.entity.start))
    return rows


def pending_review(analysis: BatchAnalysis, decisions: ReviewDecisions) -> list[EntityCluster]:
    """Clusters the analysis sent to review that the user has not decided yet."""
    decided = decisions.confirmed_clusters | decisions.rejected_clusters
    return [c for c in analysis.review_clusters() if c.id not in decided]


def order_documents(documents: Sequence[Document]) -> dict[str, int]:
    """Depth-first position of every document (to sort rows across the batch)."""
    order: dict[str, int] = {}
    for root in documents:
        for doc in root.walk():
            order[doc.id] = len(order)
    return order


# ---------------------------------------------------------------------------
# Preview text
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Span:
    start: int
    end: int
    entity_type: str
    #: Offsets of the replaced text in the original segment (preview only).
    source_start: int = -1
    source_end: int = -1


def preview_segments(document: Document, effective: EffectiveState) -> dict[str, list[Replacement]]:
    """Replacements per segment of ``document`` (children excluded)."""
    plan = build_plan(document, effective.for_document(document.id), effective.table)
    return plan.by_segment


def apply_with_spans(text: str, replacements: Sequence[Replacement]) -> tuple[str, list[Span]]:
    """Apply sorted replacements and return the substitutes' spans in the output."""
    out: list[str] = []
    spans: list[Span] = []
    cursor = 0
    length = 0
    for rep in replacements:
        if rep.start < cursor or rep.end > len(text):
            continue
        before = text[cursor : rep.start]
        out.append(before)
        length += len(before)
        out.append(rep.substitute)
        spans.append(
            Span(length, length + len(rep.substitute), rep.entity_type, rep.start, rep.end)
        )
        length += len(rep.substitute)
        cursor = rep.end
    out.append(text[cursor:])
    return "".join(out), spans


_TAG = re.compile(r"\[([A-Z][A-Z0-9_]*?)_\d{3,}\]")
_BLOCKS = re.compile(f"{re.escape(BLACKOUT_CHAR)}+")


def substitute_spans(text: str) -> list[Span]:
    """Spans of tags (``[PERSONNE_001]``) and blackout runs in a produced file."""
    spans = [Span(m.start(), m.end(), m.group(1)) for m in _TAG.finditer(text)]
    spans += [Span(m.start(), m.end(), "") for m in _BLOCKS.finditer(text)]
    return sorted(spans, key=lambda s: s.start)


def known_types(analysis: BatchAnalysis | None, extra: Iterable[str] = ()) -> list[str]:
    """Types worth offering in a type chooser: built-ins first, then custom ones."""
    from anonyma.models import EntityType

    types = [t.value for t in EntityType]
    seen = set(types)
    more: set[str] = set(extra)
    if analysis is not None:
        more |= {c.entity_type for c in analysis.clusters.values()}
    for t in sorted(more):
        if t and t not in seen:
            types.append(t)
            seen.add(t)
    return types


def clean_selection(text: str) -> str:
    """Selected text from a QTextDocument, made comparable with segment text."""
    return text.replace(" ", "\n").replace(" ", "\n").strip()


def same_value(a: str, b: str) -> bool:
    return normalize(a) == normalize(b)
