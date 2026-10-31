"""Virtualised item models: processing queue and cluster-centric review queue.

Both only hold light references and compute display data on demand, so that
batches with thousands of files and mentions stay responsive.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, override

from PySide6.QtCore import (
    QAbstractItemModel,
    QAbstractTableModel,
    QModelIndex,
    QPersistentModelIndex,
    QRectF,
    QSize,
    Qt,
)
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter
from PySide6.QtWidgets import QStyle, QStyledItemDelegate, QStyleOptionViewItem

from anonyma.frenchtext import typo
from anonyma.gui import theme
from anonyma.gui.labels import KIND_LABELS, entity_type_label
from anonyma.gui.preview import (
    STATE_LABELS,
    STATE_LEVELS,
    RowState,
    cluster_state,
    flag_reason,
    is_decided,
    occurrences_label,
    score_detail,
    snippet,
)
from anonyma.gui.widgets import BADGE_ROLE
from anonyma.models import Entity, EntityCluster
from anonyma.textutils import normalize

if TYPE_CHECKING:
    from anonyma.gui.controller import BatchController

IndexLike = QModelIndex | QPersistentModelIndex

KIND_ROLE = Qt.ItemDataRole.UserRole + 20  # "cluster" | "mention"
CLUSTER_ROLE = Qt.ItemDataRole.UserRole + 21  # cluster id
MENTION_ROLE = Qt.ItemDataRole.UserRole + 22  # Entity
VIEW_ROLE = Qt.ItemDataRole.UserRole + 23  # ClusterView / MentionView


# ---------------------------------------------------------------------------
# Processing queue
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class QueueRow:
    name: str
    format: str
    status: str
    level: str
    #: Occurrences (masked mentions) in the file, "—" before the analysis.
    occurrences: str
    root: str = ""
    document_id: str = ""


class QueueModel(QAbstractTableModel):
    COLUMNS = ("Fichier", "Format", "Statut", "Occurrences")

    def __init__(self, parent: object = None) -> None:
        super().__init__(None)
        self.rows: list[QueueRow] = []

    def set_rows(self, rows: list[QueueRow]) -> None:
        if len(rows) == len(self.rows) and rows:
            self.rows = rows
            self.dataChanged.emit(
                self.index(0, 0), self.index(len(rows) - 1, len(self.COLUMNS) - 1)
            )
            return
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()

    def update_row(self, row: int, **changes: object) -> None:
        if not 0 <= row < len(self.rows):
            return
        for key, value in changes.items():
            setattr(self.rows[row], key, value)
        self.dataChanged.emit(self.index(row, 0), self.index(row, len(self.COLUMNS) - 1))

    @override
    def rowCount(self, parent: IndexLike = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self.rows)

    @override
    def columnCount(self, parent: IndexLike = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self.COLUMNS)

    @override
    def headerData(
        self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole
    ) -> object:
        if orientation is Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.COLUMNS[section]
        if (
            orientation is Qt.Orientation.Horizontal
            and role == Qt.ItemDataRole.TextAlignmentRole
            and section == 3
        ):
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return None

    @override
    def data(self, index: IndexLike, role: int = Qt.ItemDataRole.DisplayRole) -> object:
        if not index.isValid() or index.row() >= len(self.rows):
            return None
        row = self.rows[index.row()]
        col = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            return (row.name, row.format, row.status, row.occurrences)[col]
        if role == Qt.ItemDataRole.ToolTipRole and col == 0:
            return row.name
        if role == BADGE_ROLE and col == 2:
            return row.level
        if role == Qt.ItemDataRole.TextAlignmentRole and col == 3:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.UserRole:
            return row.document_id
        if role == Qt.ItemDataRole.UserRole + 1:
            return row.root
        return None


# ---------------------------------------------------------------------------
# Review queue: one row per entity (cluster), children = occurrences
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ClusterView:
    id: str
    canonical: str
    entity_type: str
    state: RowState
    decided: bool
    needs_review: bool
    occurrences: int
    documents: int
    max_score: float
    detectors: tuple[str, ...]
    reason: str
    detail: str
    manual: bool

    @property
    def title(self) -> str:
        label = occurrences_label(self.occurrences, self.documents)
        return f"{self.canonical} — {self.entity_type} — {label}"


@dataclass(frozen=True, slots=True)
class MentionView:
    entity: Entity
    before: str
    value: str
    after: str
    location: str
    rejected: bool


SORTS = {
    "incertitude": "Incertitude d'abord",
    "occurrences": "Plus fréquentes d'abord",
    "alpha": "Ordre alphabétique",
    "type": "Par type",
}

STATE_FILTERS = {
    "": "Toutes les entités",
    "non_decidees": "Sans décision",
    "a_revoir": "À revoir",
    "decidees": "Avec décision",
    "validees": "Masquées",
    "rejetees": "Non masquées",
    "manuelles": "Ajouts manuels",
}


class ClusterTreeModel(QAbstractItemModel):
    """Top-level rows: entities; children: their occurrences (created lazily)."""

    COLUMNS = ("Entité", "Statut")

    def __init__(self, controller: BatchController) -> None:
        super().__init__(None)
        self.controller = controller
        self.ids: list[str] = []
        self._views: dict[str, ClusterView] = {}
        self._mentions: dict[str, list[Entity]] = {}
        self._mention_views: dict[tuple[str, int], MentionView] = {}
        self.search = ""
        self.type_filter = ""
        self.detector_filter = ""
        self.state_filter = ""
        self.document_ids: set[str] | None = None
        self.sort_key = "incertitude"

    # -- data source ----------------------------------------------------------------

    def _cluster(self, cid: str) -> EntityCluster | None:
        c = self.controller
        if c.analysis is None:
            return None
        cluster = c.analysis.clusters.get(cid)
        if cluster is None and (effective := c.effective()) is not None:
            cluster = effective.clusters.get(cid)
        return cluster

    def mentions(self, cid: str) -> list[Entity]:
        if cid in self._mentions:
            return self._mentions[cid]
        mentions = self.controller.index.by_cluster.get(cid)
        if mentions is None:
            effective = self.controller.effective()
            order = self.controller.index.doc_order
            mentions = sorted(
                (e for e in (effective.entities if effective else []) if e.cluster_id == cid),
                key=lambda e: (order.get(e.document_id, 0), e.segment_id, e.start),
            )
        self._mentions[cid] = mentions
        return mentions

    def view(self, cid: str) -> ClusterView | None:
        if cid in self._views:
            return self._views[cid]
        c = self.controller
        cluster = self._cluster(cid)
        if cluster is None or c.analysis is None:
            return None
        manual = cid not in c.analysis.clusters
        mentions = self.mentions(cid)
        state = RowState.MANUAL if manual else cluster_state(cid, c.analysis, c.decisions)
        view = ClusterView(
            id=cid,
            canonical=" ".join(cluster.canonical.split()),
            entity_type=c.decisions.retyped_clusters.get(cid, cluster.entity_type),
            state=state,
            decided=is_decided(cid, c.analysis, c.decisions),
            needs_review=state is RowState.REVIEW,
            occurrences=len(mentions),
            documents=len({e.document_id for e in mentions}),
            max_score=cluster.max_score,
            detectors=tuple(sorted(cluster.detectors)),
            reason=flag_reason(cluster, c.analysis.config.review_threshold, manual),
            detail="" if manual else score_detail(cluster, c.analysis.config.review_threshold),
            manual=manual,
        )
        self._views[cid] = view
        return view

    def mention_view(self, cid: str, row: int) -> MentionView | None:
        key = (cid, row)
        if key in self._mention_views:
            return self._mention_views[key]
        mentions = self.mentions(cid)
        if row >= len(mentions):
            return None
        e = mentions[row]
        doc = self.controller.index.documents.get(e.document_id)
        text = ""
        location = ""
        if doc is not None:
            for seg in doc.segments:
                if seg.id == e.segment_id:
                    text = seg.text
                    kind = KIND_LABELS.get(seg.context.kind, str(seg.context.kind))
                    label = seg.context.label
                    location = f"{kind} · {label}" if label else kind
                    break
            location = f"{doc.name} · {location}" if location else doc.name
        before, value, after = snippet(text, e.start, e.end) if text else ("", e.value, "")
        view = MentionView(
            e, before, value, after, location, e.key in self.controller.decisions.rejected_mentions
        )
        self._mention_views[key] = view
        return view

    # -- filtering --------------------------------------------------------------------------

    def _matches(self, view: ClusterView) -> bool:
        if self.type_filter and view.entity_type != self.type_filter:
            return False
        if self.detector_filter and self.detector_filter not in view.detectors:
            return False
        state = self.state_filter
        if state == "non_decidees" and view.decided:
            return False
        if state == "a_revoir" and not view.needs_review:
            return False
        if state == "decidees" and not view.decided:
            return False
        if state == "validees" and view.state is not RowState.CONFIRMED:
            return False
        if state == "rejetees" and view.state is not RowState.REJECTED:
            return False
        if state == "manuelles" and view.state is not RowState.MANUAL:
            return False
        if self.document_ids is not None and not any(
            e.document_id in self.document_ids for e in self.mentions(view.id)
        ):
            return False
        if self.search:
            cluster = self._cluster(view.id)
            haystack = [view.canonical, view.entity_type, entity_type_label(view.entity_type)]
            if cluster is not None:
                haystack += list(cluster.variants)
            if not any(self.search in normalize(h) for h in haystack):
                return False
        return True

    def _sorted(self, ids: list[str]) -> list[str]:
        if self.sort_key == "incertitude":
            return ids
        views = {cid: self.view(cid) for cid in ids}
        if self.sort_key == "occurrences":
            return sorted(ids, key=lambda c: -(v.occurrences if (v := views[c]) else 0))
        if self.sort_key == "alpha":
            return sorted(ids, key=lambda c: normalize(v.canonical) if (v := views[c]) else "")
        return sorted(
            ids,
            key=lambda c: (v.entity_type, normalize(v.canonical)) if (v := views[c]) else ("", ""),
        )

    def refresh(self, *, reset: bool | None = None) -> bool:
        """Recompute visible rows. Returns True when the model was reset."""
        self._views.clear()
        self._mentions.clear()
        self._mention_views.clear()
        ids = [
            cid
            for cid in self.controller.cluster_ids()
            if (v := self.view(cid)) and self._matches(v)
        ]
        ids = self._sorted(ids)
        if reset is None:
            reset = ids != self.ids
        if reset:
            self.beginResetModel()
            self.ids = ids
            self.endResetModel()
        elif self.ids:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.ids) - 1, 1))
            for row, cid in enumerate(self.ids):
                n = len(self.mentions(cid))
                if n:
                    parent = self.index(row, 0)
                    self.dataChanged.emit(self.index(0, 0, parent), self.index(n - 1, 1, parent))
        return reset

    # -- Qt model API ------------------------------------------------------------------------

    @override
    def index(self, row: int, column: int, parent: IndexLike = QModelIndex()) -> QModelIndex:  # noqa: B008
        if column < 0 or column >= len(self.COLUMNS) or row < 0:
            return QModelIndex()
        if not parent.isValid():
            if row >= len(self.ids):
                return QModelIndex()
            return self.createIndex(row, column, 0)
        if parent.internalId() != 0:
            return QModelIndex()
        if row >= len(self.mentions(self.ids[parent.row()])):
            return QModelIndex()
        return self.createIndex(row, column, parent.row() + 1)

    @override
    def parent(self, child: IndexLike = QModelIndex()) -> QModelIndex:  # type: ignore[override]  # noqa: B008
        if not child.isValid() or child.internalId() == 0:
            return QModelIndex()
        return self.createIndex(int(child.internalId()) - 1, 0, 0)

    @override
    def rowCount(self, parent: IndexLike = QModelIndex()) -> int:  # noqa: B008
        if not parent.isValid():
            return len(self.ids)
        if parent.internalId() != 0 or parent.column() != 0:
            return 0
        return len(self.mentions(self.ids[parent.row()]))

    @override
    def columnCount(self, parent: IndexLike = QModelIndex()) -> int:  # noqa: B008
        return len(self.COLUMNS)

    @override
    def hasChildren(self, parent: IndexLike = QModelIndex()) -> bool:  # noqa: B008
        if not parent.isValid():
            return bool(self.ids)
        return parent.internalId() == 0 and parent.column() == 0

    @override
    def headerData(
        self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole
    ) -> object:
        if orientation is Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.COLUMNS[section]
        return None

    def cluster_id_at(self, index: IndexLike) -> str:
        if not index.isValid():
            return ""
        if index.internalId() == 0:
            return self.ids[index.row()] if index.row() < len(self.ids) else ""
        parent_row = int(index.internalId()) - 1
        return self.ids[parent_row] if parent_row < len(self.ids) else ""

    def row_of(self, cluster_id: str) -> int:
        try:
            return self.ids.index(cluster_id)
        except ValueError:
            return -1

    @override
    def data(self, index: IndexLike, role: int = Qt.ItemDataRole.DisplayRole) -> object:
        if not index.isValid():
            return None
        cid = self.cluster_id_at(index)
        if index.internalId() == 0:
            view = self.view(cid)
            if view is None:
                return None
            if role == KIND_ROLE:
                return "cluster"
            if role == CLUSTER_ROLE:
                return cid
            if role == VIEW_ROLE:
                return view
            if index.column() == 0:
                if role == Qt.ItemDataRole.DisplayRole:
                    return view.title
                if role == Qt.ItemDataRole.ToolTipRole:
                    lines = [view.title, f"Motif : {view.reason}"]
                    if view.detail:
                        lines.append(view.detail)
                    return typo("\n".join(lines))
            if index.column() == 1:
                if role == Qt.ItemDataRole.DisplayRole:
                    # Masked without a decision is the default: no badge.
                    return "" if view.state is RowState.AUTO else STATE_LABELS[view.state]
                if role == Qt.ItemDataRole.ToolTipRole:
                    return STATE_LABELS[view.state]
                if role == BADGE_ROLE:
                    return STATE_LEVELS[view.state]
            return None
        mention = self.mention_view(cid, index.row())
        if mention is None:
            return None
        if role == KIND_ROLE:
            return "mention"
        if role == CLUSTER_ROLE:
            return cid
        if role == MENTION_ROLE:
            return mention.entity
        if role == VIEW_ROLE:
            return mention
        if index.column() == 0 and role == Qt.ItemDataRole.DisplayRole:
            return f"{mention.before}{mention.value}{mention.after}"
        if index.column() == 0 and role == Qt.ItemDataRole.ToolTipRole:
            return mention.location
        if index.column() == 1:
            if role == Qt.ItemDataRole.DisplayRole:
                return "Non masquée" if mention.rejected else ""
            if role == BADGE_ROLE:
                return "neutral" if mention.rejected else None
        return None


# ---------------------------------------------------------------------------
# Delegate
# ---------------------------------------------------------------------------


def _draw_runs(
    painter: QPainter,
    rect: QRectF,
    runs: list[tuple[str, QFont, QColor, QColor | None]],
) -> float:
    """Draw consecutive text runs on one line, eliding at the right edge."""
    x = rect.x()
    right = rect.right()
    for raw, font, colour, background in runs:
        if not raw or x >= right:
            continue
        metrics = QFontMetrics(font)
        text = raw
        width = metrics.horizontalAdvance(text)
        if x + width > right:
            text = metrics.elidedText(text, Qt.TextElideMode.ElideRight, int(right - x))
            width = metrics.horizontalAdvance(text)
        painter.setFont(font)
        if background is not None:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(background)
            painter.drawRoundedRect(QRectF(x - 1, rect.y(), width + 2, rect.height()), 3, 3)
        painter.setPen(colour)
        painter.drawText(
            QRectF(x, rect.y(), width + 1, rect.height()),
            int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
            text,
        )
        x += width
    return x


class ReviewItemDelegate(QStyledItemDelegate):
    """Two-line rendering of entities (title + why) and occurrences (snippet + place)."""

    @override
    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: IndexLike) -> None:
        view = index.data(VIEW_ROLE)
        if index.column() != 0 or view is None:
            super().paint(painter, option, index)
            return
        self.initStyleOption(option, index)
        option.text = ""
        style = option.widget.style() if option.widget else None
        if style is not None:
            style.drawControl(QStyle.ControlElement.CE_ItemViewItem, option, painter, option.widget)
        t = theme.tokens()
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(option.rect).adjusted(6, 4, -6, -4)
        line = rect.height() / 2
        base = QFont(option.font)
        bold = QFont(base)
        bold.setBold(True)
        small = QFont(base)
        small.setPointSizeF(max(base.pointSizeF() - 1, 7))
        text = QColor(t.text)
        muted = QColor(t.muted)
        top = QRectF(rect.x(), rect.y(), rect.width(), line)
        bottom = QRectF(rect.x(), rect.y() + line, rect.width(), line)
        if isinstance(view, ClusterView):
            if view.needs_review:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(t.warning))
                painter.drawRoundedRect(
                    QRectF(option.rect.x() + 1, rect.y(), 3, rect.height()), 1, 1
                )
            bg, fg = theme.entity_colors(view.entity_type)
            x = _draw_runs(painter, top, [(view.canonical, bold, text, None)])
            pill = QRectF(x + 8, top.y() + 1, rect.right() - x - 8, top.height() - 2)
            _draw_runs(painter, pill, [(f" {entity_type_label(view.entity_type)} ", small, fg, bg)])
            detail = f"{occurrences_label(view.occurrences, view.documents)} · {view.reason}"
            _draw_runs(painter, bottom, [(detail, small, muted, None)])
        elif isinstance(view, MentionView):
            bg, fg = theme.entity_colors(
                index.parent().data(VIEW_ROLE).entity_type
                if isinstance(index.parent().data(VIEW_ROLE), ClusterView)
                else view.entity.entity_type
            )
            colour = muted if view.rejected else text
            highlight = None if view.rejected else bg
            _draw_runs(
                painter,
                top,
                [
                    (view.before, base, colour, None),
                    (view.value, bold, fg if highlight else muted, highlight),
                    (view.after, base, colour, None),
                ],
            )
            _draw_runs(painter, bottom, [(view.location, small, muted, None)])
        painter.restore()

    @override
    def sizeHint(self, option: QStyleOptionViewItem, index: IndexLike) -> QSize:
        size = super().sizeHint(option, index)
        metrics = QFontMetrics(option.font)
        return QSize(size.width(), metrics.height() * 2 + 12)
