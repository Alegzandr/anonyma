"""Review state computed locally from a ``BatchAnalysis`` and ``ReviewDecisions``.

Uses ``pipeline.resolve_decisions`` (the exact decision handling of
``Pipeline.apply``) so that the review screen can show an anonymized *preview*
without writing anything. Substitutes are assigned with a throw-away ``Replacer``
in the anonymization regime: no mapping store is ever touched, so pseudonymization
tags shown here are only indicative.

Qt-free: unit-testable on its own.
"""

from __future__ import annotations

import enum
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace

from anonyma.config import UNCERTAIN_SCORE
from anonyma.frenchtext import plural
from anonyma.gui.labels import detector_label
from anonyma.models import (
    FILENAME_SEGMENT_ID,
    Document,
    Entity,
    EntityCluster,
    Regime,
    Replacement,
    ReplacementMode,
    ReviewDecisions,
    ReviewStatus,
)
from anonyma.pipeline import BatchAnalysis, open_links, resolve_decisions
from anonyma.replacer import BLACKOUT_CHAR, Replacer, SubstitutionTable, build_plan
from anonyma.textutils import normalize

MentionKey = tuple[str, str, int, int]


class RowState(enum.StrEnum):
    AUTO = "auto"
    CONFIRMED = "valide"
    REJECTED = "rejete"
    MENTION_REJECTED = "occurrence_rejetee"
    MANUAL = "manuel"


#: State of an entity (« entité », feminine). ``AUTO`` is the default (masked
#: without a decision): the review list shows no badge for it.
STATE_LABELS: dict[RowState, str] = {
    RowState.AUTO: "Détectée",
    RowState.CONFIRMED: "Masquée",
    RowState.REJECTED: "Non masquée",
    RowState.MENTION_REJECTED: "Occurrence non masquée",
    RowState.MANUAL: "Ajout manuel",
}

STATE_LEVELS: dict[RowState, str] = {
    RowState.AUTO: "info",
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

    #: ``cluster id -> entities``, built on the first :meth:`for_cluster` call.
    _by_cluster: dict[str, list[Entity]] | None = field(default=None, init=False, repr=False)

    def for_document(self, document_id: str) -> list[Entity]:
        return self.by_document.get(document_id, [])

    def for_cluster(self, cluster_id: str) -> list[Entity]:
        """Entities of a cluster (manual ones included), in ``entities`` order."""
        if self._by_cluster is None:
            index: dict[str, list[Entity]] = {}
            for e in self.entities:
                if e.cluster_id is not None:
                    index.setdefault(e.cluster_id, []).append(e)
            self._by_cluster = index
        return self._by_cluster.get(cluster_id, [])


def compute_effective(
    analysis: BatchAnalysis, decisions: ReviewDecisions, mode: ReplacementMode | None = None
) -> EffectiveState:
    """Replay ``Pipeline.apply``'s decision handling without writing anything."""
    config = analysis.config
    resolved = resolve_decisions(analysis, decisions)
    entities, active = resolved.entities, resolved.clusters
    clusters = {**analysis.clusters, **active}
    preview_config = replace(
        config, regime=Regime.ANONYMIZATION, mode=mode if mode is not None else config.mode
    )
    # The same fake values as the output: the link memory of realistic anonymisation.
    links = open_links(replace(config, mode=preview_config.mode))
    try:
        table = Replacer(preview_config, links).assign(active, entities)
    finally:
        if links is not None:
            links.close()
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
    #: Low-confidence cluster (masked all the same), not confirmed by the user.
    uncertain: bool
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
    return RowState.AUTO


def entity_rows(
    analysis: BatchAnalysis,
    decisions: ReviewDecisions,
    effective: EffectiveState | None,
    document_ids: Iterable[str] | None = None,
    entities: Sequence[Entity] | None = None,
) -> list[EntityRow]:
    """Every mention of the selected documents: detected ones (whatever their
    decision) plus mentions coming from manual additions.

    ``entities``: the analysis entities to consider (e.g. ``ReviewIndex.by_document``),
    to avoid scanning the whole batch."""
    wanted = set(document_ids) if document_ids is not None else None
    rows: list[EntityRow] = []
    known: set[MentionKey] = set()
    for e in analysis.entities if entities is None else entities:
        if wanted is not None and e.document_id not in wanted:
            continue
        cid = e.cluster_id or ""
        known.add(e.key)
        state = cluster_state(cid, analysis, decisions)
        cluster = analysis.clusters.get(cid)
        if e.key in decisions.rejected_mentions and state is not RowState.REJECTED:
            state = RowState.MENTION_REJECTED
        rows.append(
            EntityRow(
                entity=e,
                entity_type=decisions.retyped_clusters.get(cid, e.entity_type),
                state=state,
                cluster_id=cid,
                uncertain=state is RowState.AUTO and cluster is not None and cluster.uncertain,
            )
        )
    extra: Sequence[Entity] = []
    if effective is not None:
        extra = (
            effective.entities
            if wanted is None
            else [e for d in wanted for e in effective.for_document(d)]
        )
    for e in extra:
        if e.key in known or (wanted is not None and e.document_id not in wanted):
            continue
        rows.append(
            EntityRow(
                entity=e,
                entity_type=e.entity_type,
                state=RowState.MANUAL,
                cluster_id=e.cluster_id or "",
                uncertain=False,
                manual=True,
            )
        )
    rows.sort(key=lambda r: (r.entity.document_id, r.entity.segment_id, r.entity.start))
    return rows


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


#: Sequential (``[PERSONNE_001]``) and random (``[PERSONNE_K7Q2]``) tags.
_TAG = re.compile(r"\[([A-Z][A-Z0-9_]*?)_(?:\d{3,}|(?=[A-Z2-9]*[2-9])[A-Z2-9]{4,5})\]")
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


# ---------------------------------------------------------------------------
# Cluster-centric review queue
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ReviewIndex:
    """Lookups built once per analysis (in the analysis thread)."""

    by_cluster: dict[str, list[Entity]] = field(default_factory=dict)
    by_document: dict[str, list[Entity]] = field(default_factory=dict)
    documents: dict[str, Document] = field(default_factory=dict)
    top_of: dict[str, str] = field(default_factory=dict)
    doc_order: dict[str, int] = field(default_factory=dict)
    cluster_documents: dict[str, int] = field(default_factory=dict)
    #: Cluster ids, most uncertain first (review needed, then lowest score).
    order: list[str] = field(default_factory=list)


def build_index(analysis: BatchAnalysis) -> ReviewIndex:
    index = ReviewIndex()
    for top in analysis.documents:
        for doc in top.walk():
            index.documents[doc.id] = doc
            index.top_of[doc.id] = top.id
            index.doc_order[doc.id] = len(index.doc_order)
    for e in analysis.entities:
        index.by_cluster.setdefault(e.cluster_id or "", []).append(e)
        index.by_document.setdefault(e.document_id, []).append(e)

    def position(e: Entity) -> tuple[int, bool, str, int]:
        return (
            index.doc_order.get(e.document_id, 0),
            e.segment_id != FILENAME_SEGMENT_ID,
            e.segment_id,
            e.start,
        )

    for mentions in (*index.by_cluster.values(), *index.by_document.values()):
        mentions.sort(key=position)
    for cid, mentions in index.by_cluster.items():
        index.cluster_documents[cid] = len({e.document_id for e in mentions})
    index.order = sorted(
        (cid for cid in analysis.clusters if cid in index.by_cluster),
        key=lambda cid: (
            not analysis.clusters[cid].uncertain,
            analysis.clusters[cid].max_score,
            normalize(analysis.clusters[cid].canonical),
        ),
    )
    return index


def is_decided(cluster_id: str, analysis: BatchAnalysis, decisions: ReviewDecisions) -> bool:
    """Explicit decision taken (manual additions count as decided)."""
    return (
        cluster_id in decisions.confirmed_clusters
        or cluster_id in decisions.rejected_clusters
        or cluster_id in decisions.retyped_clusters
        or cluster_id not in analysis.clusters
        or analysis.clusters[cluster_id].review is ReviewStatus.MANUAL
    )


def _num(value: float) -> str:
    return f"{value:.2f}".replace(".", ",")


#: Score from which a detection reads « confiance élevée ».
HIGH_CONFIDENCE = 0.85


def confidence_label(score: float, uncertain: bool) -> str:
    """Plain-French reading of a detection score."""
    if uncertain:
        return "confiance faible"
    return "confiance élevée" if score >= HIGH_CONFIDENCE else "confiance moyenne"


def flag_reason(cluster: EntityCluster, manual: bool = False) -> str:
    """Why the entity is masked (French, value-free): confidence and which detectors
    found it, in words."""
    if manual or cluster.review is ReviewStatus.MANUAL:
        return "ajout manuel"
    detectors = sorted(cluster.detectors - {"propagation"}) or sorted(cluster.detectors)
    who = ", ".join(detector_label(d) for d in detectors)
    if detectors == ["listes_metier"] and not cluster.uncertain:
        return "liste métier ou valeur à toujours masquer"
    return f"{confidence_label(cluster.max_score, cluster.uncertain)} · {who}"


def score_detail(cluster: EntityCluster) -> str:
    """Numbers behind ``flag_reason`` (tooltip)."""
    return f"Score {_num(cluster.max_score)}, confiance faible sous {_num(UNCERTAIN_SCORE)}"


def occurrences_label(occurrences: int, documents: int) -> str:
    return f"{plural(occurrences, 'occurrence')} dans {plural(documents, 'fichier')}"


def snippet(
    text: str, start: int, end: int, width: int = 40, before_width: int = 24
) -> tuple[str, str, str]:
    """(before, value, after) around ``text[start:end]``, whitespace collapsed."""
    before = " ".join(text[max(0, start - before_width * 2) : start].split())[-before_width:]
    after = " ".join(text[end : end + width * 2].split())[:width]
    if start > 0 and text[:start].strip() and len(before) == before_width:
        before = "…" + before
    if end < len(text) and len(after) == width:
        after += "…"
    if text[start - 1 : start].isspace() and before:
        before += " "
    if text[end : end + 1].isspace() and after:
        after = " " + after
    return before, " ".join(text[start:end].split()), after
