"""« Revue » page: original vs anonymized, entity list and review decisions."""

from __future__ import annotations

import bisect
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import override

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QPersistentModelIndex,
    QPoint,
    Qt,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QKeySequence,
    QShortcut,
    QTextBlockFormat,
    QTextCharFormat,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QSplitter,
    QStackedWidget,
    QTableView,
    QTextEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from anonyma.gui import dialogs, theme
from anonyma.gui.controller import BatchController
from anonyma.gui.labels import KIND_LABELS, STATUS_LEVELS, entity_type_label, status_label
from anonyma.gui.preview import (
    STATE_LABELS,
    STATE_LEVELS,
    EntityRow,
    MentionKey,
    RowState,
    apply_with_spans,
    clean_selection,
    entity_rows,
    known_types,
    order_documents,
    pending_review,
    preview_segments,
    substitute_spans,
)
from anonyma.gui.widgets import (
    BADGE_ROLE,
    BadgeDelegate,
    Banner,
    EmptyState,
    EntityTypeDelegate,
    button,
    legend_html,
    muted,
    page_header,
    primary_button,
)
from anonyma.models import (
    FILENAME_SEGMENT_ID,
    Document,
    ReviewStatus,
    Segment,
    SegmentContext,
    SegmentKind,
)

IndexLike = QModelIndex | QPersistentModelIndex

SCOPES = {
    "doc": "Document sélectionné",
    "tree": "Document et éléments inclus",
    "batch": "Tout le lot",
}

# ---------------------------------------------------------------------------
# Text rendering
# ---------------------------------------------------------------------------

_FOLD = str.maketrans(
    {
        "\r": "\n",
        " ": "\n",
        " ": "\n",
        "\x0b": "\n",
        "\x0c": "\n",
        "\x85": "\n",
        "﷐": "\u200b",
        "﷑": "\u200b",
        **{chr(i): "\u200b" for i in range(32) if chr(i) not in "\n\t\r\x0b\x0c"},
    }
)


def display_text(text: str) -> str:
    """Same length as ``text``, safe for ``QTextCursor.insertText`` (which folds
    ``\\r\\n`` into one block separator and would shift every offset)."""
    return text.replace("\r\n", "\u200b\n").translate(_FOLD)


def _u16_offsets(text: str) -> Callable[[int], int] | None:
    """Python index -> UTF-16 index converter, or None when identical (BMP only)."""
    if not text or max(text) <= "￿":
        return None

    def convert(i: int) -> int:
        return len(text[:i].encode("utf-16-le")) // 2

    return convert


@dataclass(slots=True)
class StyledSpan:
    start: int
    end: int
    fmt: QTextCharFormat
    key: object = None  # MentionKey on the original side, (segment id, start, end) elsewhere


@dataclass(slots=True)
class RenderMap:
    """Document positions of the rendered spans."""

    positions: dict[object, tuple[int, int]] = field(default_factory=dict)
    starts: list[int] = field(default_factory=list)
    spans: list[tuple[int, int, object]] = field(default_factory=list)

    def add(self, key: object, start: int, end: int) -> None:
        self.positions[key] = (start, end)

    def finish(self) -> None:
        self.spans = sorted((s, e, k) for k, (s, e) in self.positions.items())
        self.starts = [s for s, _, _ in self.spans]

    def key_at(self, position: int) -> object:
        """Smallest span containing ``position``."""
        best: tuple[int, object] | None = None
        i = bisect.bisect_right(self.starts, position)
        for s, e, k in self.spans[max(0, i - 50) : i]:
            if s <= position < e and (best is None or e - s < best[0]):
                best = (e - s, k)
        return best[1] if best else None


def ordered_segments(doc: Document) -> list[Segment]:
    """File name first, then the extraction order."""
    head = [s for s in doc.segments if s.id == FILENAME_SEGMENT_ID]
    return head + [s for s in doc.segments if s.id != FILENAME_SEGMENT_ID]


def render_segments(
    edit: QTextEdit,
    segments: Sequence[tuple[str, SegmentContext, str, list[StyledSpan]]],
) -> RenderMap:
    """Render ``(segment id, context, text, spans)`` into ``edit``, grouped by kind."""
    t = theme.tokens()
    document = edit.document()
    document.clear()
    mapping = RenderMap()
    cursor = QTextCursor(document)
    base = QTextCharFormat()
    base.setForeground(QColor(t.text))
    heading = QTextCharFormat()
    heading.setFontWeight(700)
    heading.setForeground(QColor(t.muted))
    caption = QTextCharFormat()
    caption.setForeground(QColor(t.muted))
    caption.setFontItalic(True)
    heading_block = QTextBlockFormat()
    heading_block.setTopMargin(12)
    heading_block.setBottomMargin(4)
    body_block = QTextBlockFormat()
    first = True
    previous_kind: SegmentKind | None = None
    previous_page: int | None = None
    need_break = False
    for _seg_id, context, text, spans in segments:
        kind = context.kind
        if kind != previous_kind:
            if first:
                cursor.setBlockFormat(heading_block)
            else:
                cursor.insertBlock(heading_block)
            cursor.insertText(KIND_LABELS.get(kind, str(kind)), heading)
            cursor.insertBlock(body_block, base)
            previous_kind = kind
            previous_page = None
            need_break = False
            first = False
        elif need_break:
            cursor.insertBlock(body_block, base)
        if kind is SegmentKind.BODY:
            if context.page is not None and context.page != previous_page:
                cursor.insertText(f"Page {context.page + 1}", caption)
                cursor.insertBlock(body_block, base)
                previous_page = context.page
        elif context.label and kind is not SegmentKind.FILENAME:
            cursor.insertText(f"{context.label} : ", caption)
        start = cursor.position()
        shown = display_text(text)
        cursor.insertText(shown, base)
        convert = _u16_offsets(text)
        for span in spans:
            s = start + (convert(span.start) if convert else span.start)
            e = start + (convert(span.end) if convert else span.end)
            if e <= s:
                continue
            c = QTextCursor(document)
            c.setPosition(s)
            c.setPosition(e, QTextCursor.MoveMode.KeepAnchor)
            c.mergeCharFormat(span.fmt)
            if span.key is not None:
                mapping.add(span.key, s, e)
        need_break = kind is not SegmentKind.BODY or not shown.endswith("\n")
    if first:
        cursor.insertText("Aucun texte extrait pour ce document.", caption)
    mapping.finish()
    edit.moveCursor(QTextCursor.MoveOperation.Start)
    return mapping


def entity_format(
    entity_type: str, state: RowState | None = None, tooltip: str = ""
) -> QTextCharFormat:
    t = theme.tokens()
    fmt = QTextCharFormat()
    if state in (RowState.REJECTED, RowState.MENTION_REJECTED):
        fmt.setFontStrikeOut(True)
        fmt.setForeground(QColor(t.muted))
    else:
        bg, fg = theme.entity_colors(entity_type)
        fmt.setBackground(bg)
        fmt.setForeground(fg)
        if state is RowState.REVIEW:
            fmt.setUnderlineStyle(QTextCharFormat.UnderlineStyle.DashUnderline)
            fmt.setUnderlineColor(QColor(t.warning))
            fmt.setFontUnderline(True)
    if tooltip:
        fmt.setToolTip(tooltip)
    return fmt


def row_tooltip(row: EntityRow) -> str:
    e = row.entity
    return (
        f"{entity_type_label(row.entity_type)} · {STATE_LABELS[row.state]} · "
        f"détecteur « {e.detector} » · score {e.score:.2f}"
    )


# ---------------------------------------------------------------------------
# Entity table
# ---------------------------------------------------------------------------


class EntityTableModel(QAbstractTableModel):
    COLUMNS = ("Valeur", "Type", "Statut", "Détecteur", "Score", "Emplacement", "Document")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.rows: list[EntityRow] = []
        self.locations: dict[tuple[str, str], str] = {}
        self.doc_names: dict[str, str] = {}

    def set_rows(self, rows: list[EntityRow]) -> None:
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()

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
        return None

    def data(self, index: IndexLike, role: int = Qt.ItemDataRole.DisplayRole) -> object:
        if not index.isValid():
            return None
        row = self.rows[index.row()]
        col = index.column()
        e = row.entity
        if role == Qt.ItemDataRole.DisplayRole:
            if col == 0:
                return " ".join(e.value.split())
            if col == 1:
                return entity_type_label(row.entity_type)
            if col == 2:
                return STATE_LABELS[row.state]
            if col == 3:
                return e.detector
            if col == 4:
                return f"{e.score:.2f}"
            if col == 5:
                return self.locations.get((e.document_id, e.segment_id), "")
            if col == 6:
                return self.doc_names.get(e.document_id, "")
        if role == Qt.ItemDataRole.UserRole and col == 1:
            return row.entity_type
        if role == BADGE_ROLE and col == 2:
            return STATE_LEVELS[row.state]
        if role == Qt.ItemDataRole.ToolTipRole:
            return row_tooltip(row)
        if role == Qt.ItemDataRole.ForegroundRole and row.state in (
            RowState.REJECTED,
            RowState.MENTION_REJECTED,
        ):
            return QColor(theme.tokens().muted)
        if role == Qt.ItemDataRole.TextAlignmentRole and col == 4:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return None

    def sort(self, column: int, order: Qt.SortOrder = Qt.SortOrder.AscendingOrder) -> None:
        keys: dict[int, Callable[[EntityRow], object]] = {
            0: lambda r: r.entity.value.casefold(),
            1: lambda r: r.entity_type,
            2: lambda r: r.state.value,
            3: lambda r: r.entity.detector,
            4: lambda r: r.entity.score,
            5: lambda r: self.locations.get((r.entity.document_id, r.entity.segment_id), ""),
            6: lambda r: self.doc_names.get(r.entity.document_id, ""),
        }
        key = keys.get(column)
        if key is None:
            return
        self.layoutAboutToBeChanged.emit()
        self.rows.sort(key=key, reverse=order is Qt.SortOrder.DescendingOrder)  # type: ignore[arg-type]
        self.layoutChanged.emit()


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------


class ReviewPage(QWidget):
    apply_requested = Signal()
    pending_changed = Signal(int)

    def __init__(self, controller: BatchController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.controller = controller
        self.current_doc_id: str | None = None
        self._docs: dict[str, Document] = {}
        self._order: dict[str, int] = {}
        self._top_of: dict[str, str] = {}
        self._original_map = RenderMap()
        self._anonymized_map = RenderMap()
        self._doc_rows: list[EntityRow] = []
        self._syncing = False
        self._moving_cursor = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 20)
        outer.setSpacing(10)
        head = QHBoxLayout()
        head.addWidget(
            page_header(
                "Revue",
                "Contrôlez les entités détectées, corrigez-les, puis appliquez. Les décisions "
                "valent pour tout le lot.",
            ),
            1,
        )
        self.counter = QLabel("")
        self.counter.setObjectName("ReviewCounter")
        head.addWidget(self.counter, 0, Qt.AlignmentFlag.AlignBottom)
        self.apply_button = primary_button("Appliquer", "check")
        self.apply_button.setToolTip("Écrire les fichiers anonymisés (Ctrl+Entrée)")
        self.apply_button.clicked.connect(self.apply_requested.emit)
        head.addWidget(self.apply_button, 0, Qt.AlignmentFlag.AlignBottom)
        outer.addLayout(head)
        self.banner = Banner()
        outer.addWidget(self.banner)

        self.stack = QStackedWidget()
        outer.addWidget(self.stack, 1)
        self.empty = EmptyState(
            "Aucune analyse",
            "Lancez une analyse depuis « Traitement » pour revoir les entités détectées.",
        )
        self.stack.addWidget(self.empty)
        content = QWidget()
        self.stack.addWidget(content)
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)

        main = QSplitter(Qt.Orientation.Horizontal)
        content_layout.addWidget(main, 1)

        # Document tree
        self.tree = QTreeWidget()
        self.tree.setObjectName("DocumentTree")
        self.tree.setHeaderLabels(["Document", "Entités", "État"])
        tree_header = self.tree.header()
        tree_header.setStretchLastSection(False)
        tree_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        tree_header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        tree_header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self.tree.setColumnWidth(2, 104)
        self.tree.setMinimumWidth(280)
        self.tree.setItemDelegateForColumn(2, BadgeDelegate(self.tree))
        self.tree.currentItemChanged.connect(self._tree_changed)
        main.addWidget(self.tree)

        right = QSplitter(Qt.Orientation.Vertical)
        main.addWidget(right)
        main.setStretchFactor(0, 0)
        main.setStretchFactor(1, 1)
        main.setSizes([330, 1000])

        # Texts
        texts = QWidget()
        texts_layout = QVBoxLayout(texts)
        texts_layout.setContentsMargins(0, 0, 0, 0)
        sides = QSplitter(Qt.Orientation.Horizontal)
        left_box = QWidget()
        left_layout = QVBoxLayout(left_box)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_head = QHBoxLayout()
        left_head.addWidget(QLabel("<b>Original</b>"))
        left_head.addStretch(1)
        self.add_button = button("Ajouter une entité…", "add")
        self.add_button.setToolTip(
            "Sélectionnez du texte dans l'original, puis ajoutez-le comme entité (A)."
        )
        self.add_button.clicked.connect(self.add_entity_from_selection)
        left_head.addWidget(self.add_button)
        left_layout.addLayout(left_head)
        self.original = QTextEdit()
        self.original.setObjectName("OriginalText")
        self.original.setReadOnly(True)
        self.original.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        self.original.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.original.customContextMenuRequested.connect(self._original_menu)
        self.original.selectionChanged.connect(self._selection_changed)
        self.original.cursorPositionChanged.connect(self._original_clicked)
        left_layout.addWidget(self.original)
        sides.addWidget(left_box)

        right_box = QWidget()
        right_layout = QVBoxLayout(right_box)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_head = QHBoxLayout()
        right_head.addWidget(QLabel("<b>Anonymisé</b>"))
        right_head.addStretch(1)
        self.source_combo = QComboBox()
        self.source_combo.addItem("Aperçu (décisions actuelles)", "preview")
        self.source_combo.addItem("Fichier produit", "output")
        self.source_combo.currentIndexChanged.connect(lambda _i: self._render_anonymized())
        right_head.addWidget(self.source_combo)
        right_layout.addLayout(right_head)
        self.anonymized = QTextEdit()
        self.anonymized.setObjectName("AnonymizedText")
        self.anonymized.setReadOnly(True)
        right_layout.addWidget(self.anonymized)
        sides.addWidget(right_box)
        texts_layout.addWidget(sides, 1)
        legend_row = QHBoxLayout()
        self.legend = QLabel("")
        self.legend.setWordWrap(True)
        self.legend.setTextFormat(Qt.TextFormat.RichText)
        legend_row.addWidget(self.legend, 1)
        self.sync_check = QCheckBox("Défilement synchronisé")
        self.sync_check.setChecked(True)
        legend_row.addWidget(self.sync_check)
        texts_layout.addLayout(legend_row)
        right.addWidget(texts)
        self.original.verticalScrollBar().valueChanged.connect(
            lambda v: self._sync_scroll(self.original, self.anonymized)
        )
        self.anonymized.verticalScrollBar().valueChanged.connect(
            lambda v: self._sync_scroll(self.anonymized, self.original)
        )

        # Entity list
        entities_box = QWidget()
        entities_layout = QVBoxLayout(entities_box)
        entities_layout.setContentsMargins(0, 6, 0, 0)
        filters = QHBoxLayout()
        self.scope_combo = QComboBox()
        for key, label in SCOPES.items():
            self.scope_combo.addItem(label, key)
        self.type_filter = QComboBox()
        self.detector_filter = QComboBox()
        self.state_filter = QComboBox()
        self.state_filter.addItem("Tous les statuts", "")
        for state, label in STATE_LABELS.items():
            self.state_filter.addItem(label, state.value)
        self.review_only = QCheckBox("À revoir uniquement")
        for w in (self.scope_combo, self.type_filter, self.detector_filter, self.state_filter):
            w.currentIndexChanged.connect(lambda _i: self._refresh_rows())
            filters.addWidget(w)
        self.review_only.toggled.connect(lambda _c: self._refresh_rows())
        filters.addWidget(self.review_only)
        filters.addStretch(1)
        self.rows_label = muted("")
        self.rows_label.setWordWrap(False)
        filters.addWidget(self.rows_label)
        entities_layout.addLayout(filters)

        self.model = EntityTableModel(self)
        self.table = QTableView()
        self.table.setObjectName("EntityTable")
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setSortingEnabled(True)
        # Document order by default; a click on a header sorts.
        self.table.horizontalHeader().setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.verticalHeader().hide()
        self.table.setItemDelegateForColumn(1, EntityTypeDelegate(self.table))
        self.table.setItemDelegateForColumn(2, BadgeDelegate(self.table))
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col, width in ((1, 170), (2, 150), (3, 120), (4, 60), (5, 160), (6, 160)):
            self.table.setColumnWidth(col, width)
        self.table.selectionModel().currentRowChanged.connect(self._row_changed)
        entities_layout.addWidget(self.table, 1)

        actions = QHBoxLayout()
        self.confirm_button = button("Valider", "check")
        self.confirm_button.setToolTip("Confirmer l'entité et toutes ses occurrences (V)")
        self.reject_button = button("Rejeter (faux positif)", "reject")
        self.reject_button.setToolTip("Ne pas masquer cette valeur, dans tout le lot (R)")
        self.reject_mention_button = button("Rejeter l'occurrence", "cancel")
        self.reject_mention_button.setToolTip("Ne pas masquer cette occurrence seulement (O)")
        self.retype_button = button("Changer le type…", "tag")
        self.retype_button.setToolTip("Corriger le type de l'entité (T)")
        self.reset_button = button("Annuler la décision", "undo")
        self.reset_button.setToolTip("Revenir à la détection d'origine (U)")
        self.confirm_button.clicked.connect(self.confirm_selected)
        self.reject_button.clicked.connect(self.reject_selected)
        self.reject_mention_button.clicked.connect(self.reject_selected_mentions)
        self.retype_button.clicked.connect(self.retype_selected)
        self.reset_button.clicked.connect(self.reset_selected)
        for b in (
            self.confirm_button,
            self.reject_button,
            self.reject_mention_button,
            self.retype_button,
            self.reset_button,
        ):
            actions.addWidget(b)
        actions.addStretch(1)
        hint = muted("N / P : naviguer · Maj+N : à revoir · V / R / O / T / U · A : ajouter")
        hint.setWordWrap(False)
        hint.setToolTip(
            "N / P : occurrence suivante / précédente\n"
            "Maj+N : prochaine occurrence à revoir\n"
            "V : valider · R : rejeter · O : rejeter l'occurrence\n"
            "T : changer le type · U : annuler la décision\n"
            "A : ajouter la sélection comme entité\n"
            "Ctrl+Entrée : appliquer"
        )
        entities_layout.addLayout(actions)
        entities_layout.addWidget(hint, 0, Qt.AlignmentFlag.AlignRight)
        right.addWidget(entities_box)
        right.setSizes([520, 300])

        self._shortcuts()
        c = self.controller
        c.analysis_ready.connect(self.reload)
        c.analysis_cleared.connect(self.reload)
        c.decisions_changed.connect(self._decisions_changed)
        c.result_ready.connect(self._result_ready)
        c.output_extracted.connect(self._output_extracted)
        c.phase_changed.connect(lambda _p: self._update_actions())
        c.context.config_changed.connect(self._config_changed)
        theme.notifier().changed.connect(self._rerender)
        self.reload()

    # -- setup ---------------------------------------------------------------------

    def _shortcuts(self) -> None:
        bindings: list[tuple[str, Callable[[], None]]] = [
            ("N", lambda: self.move_selection(1)),
            ("P", lambda: self.move_selection(-1)),
            ("Alt+Down", lambda: self.move_selection(1)),
            ("Alt+Up", lambda: self.move_selection(-1)),
            ("Shift+N", self.next_review),
            ("V", self.confirm_selected),
            ("R", self.reject_selected),
            ("O", self.reject_selected_mentions),
            ("T", self.retype_selected),
            ("U", self.reset_selected),
            ("A", self.add_entity_from_selection),
            ("Ctrl+Return", self.apply_requested.emit),
        ]
        self._shortcut_objects = []
        for keys, slot in bindings:
            shortcut = QShortcut(QKeySequence(keys), self)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(slot)
            self._shortcut_objects.append(shortcut)

    # -- data ------------------------------------------------------------------------

    def reload(self) -> None:
        analysis = self.controller.analysis
        self.tree.clear()
        self._docs.clear()
        self._top_of.clear()
        self.current_doc_id = None
        if analysis is None:
            self.stack.setCurrentWidget(self.empty)
            self.model.set_rows([])
            self.banner.clear()
            self._update_counter()
            self._update_actions()
            return
        self.stack.setCurrentIndex(1)
        self._order = order_documents(analysis.documents)
        locations: dict[tuple[str, str], str] = {}
        names: dict[str, str] = {}
        for top in analysis.documents:
            for doc in top.walk():
                self._docs[doc.id] = doc
                self._top_of[doc.id] = top.id
                names[doc.id] = doc.name
                for seg in doc.segments:
                    kind = KIND_LABELS.get(seg.context.kind, str(seg.context.kind))
                    label = seg.context.label
                    locations[(doc.id, seg.id)] = f"{kind} · {label}" if label else kind
        self.model.locations = locations
        self.model.doc_names = names
        for top in analysis.documents:
            self.tree.addTopLevelItem(self._tree_item(top))
        self.tree.expandAll()
        self._fill_filters()
        self.source_combo.setCurrentIndex(0)
        self._update_banner()
        first = self.tree.topLevelItem(0)
        if first is not None:
            self.tree.setCurrentItem(first)
        else:
            self._refresh_rows()
        self._update_tree_states()
        self._update_counter()
        self._update_actions()

    def _tree_item(self, doc: Document) -> QTreeWidgetItem:
        name = doc.name.rsplit("/", 1)[-1] if doc.depth == 0 else doc.name
        item = QTreeWidgetItem([name, "", ""])
        item.setToolTip(0, doc.name)
        item.setData(0, Qt.ItemDataRole.UserRole, doc.id)
        for child in doc.children:
            item.addChild(self._tree_item(child))
        return item

    def _iter_items(self) -> list[QTreeWidgetItem]:
        out: list[QTreeWidgetItem] = []

        def walk(item: QTreeWidgetItem) -> None:
            out.append(item)
            for i in range(item.childCount()):
                child = item.child(i)
                if child is not None:
                    walk(child)

        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            if top is not None:
                walk(top)
        return out

    def _update_tree_states(self) -> None:
        c = self.controller
        analysis = c.analysis
        effective = c.effective()
        if analysis is None or effective is None:
            return
        pending = {cl.id for cl in pending_review(analysis, c.decisions)}
        review_docs = {e.document_id for e in analysis.entities if e.cluster_id in pending}
        for item in self._iter_items():
            doc_id = str(item.data(0, Qt.ItemDataRole.UserRole))
            doc = self._docs.get(doc_id)
            if doc is None:
                continue
            item.setText(1, str(len(effective.for_document(doc_id))))
            result = c.result_for(doc_id)
            if doc_id in review_docs and (result is None or c.decisions_since_apply()):
                text, level = "À revoir", "warning"
            elif result is not None:
                text, level = (
                    status_label(result.status),
                    STATUS_LEVELS.get(result.status, "neutral"),
                )
            else:
                text, level = status_label(doc.status), STATUS_LEVELS.get(doc.status, "neutral")
            item.setText(2, text)
            item.setData(2, BADGE_ROLE, level)

    def _fill_filters(self) -> None:
        analysis = self.controller.analysis
        types: set[str] = set()
        detectors: set[str] = set()
        if analysis is not None:
            types = {e.entity_type for e in analysis.entities}
            types |= set(self.controller.decisions.retyped_clusters.values())
            types |= {m.entity_type for m in self.controller.decisions.manual}
            detectors = {e.detector for e in analysis.entities}
        effective = self.controller.effective()
        if effective is not None:
            detectors |= {e.detector for e in effective.entities}
        for combo, values, all_label in (
            (self.type_filter, sorted(types), "Tous les types"),
            (self.detector_filter, sorted(detectors), "Tous les détecteurs"),
        ):
            current = combo.currentData()
            combo.blockSignals(True)
            combo.clear()
            combo.addItem(all_label, "")
            for v in values:
                combo.addItem(entity_type_label(v) if combo is self.type_filter else v, v)
            index = combo.findData(current)
            combo.setCurrentIndex(max(index, 0))
            combo.blockSignals(False)

    def _scope_ids(self) -> set[str] | None:
        scope = self.scope_combo.currentData()
        if scope == "batch" or self.current_doc_id is None:
            return None if scope == "batch" else set()
        doc = self._docs.get(self.current_doc_id)
        if doc is None:
            return set()
        if scope == "tree":
            return {d.id for d in doc.walk()}
        return {doc.id}

    def _all_rows(self, document_ids: set[str] | None) -> list[EntityRow]:
        c = self.controller
        effective = c.effective()
        if c.analysis is None or effective is None:
            return []
        rows = entity_rows(c.analysis, c.decisions, effective, document_ids)
        rows.sort(
            key=lambda r: (
                self._order.get(r.entity.document_id, 0),
                r.entity.segment_id != FILENAME_SEGMENT_ID,
                r.entity.segment_id,
                r.entity.start,
            )
        )
        return rows

    def _refresh_rows(self, keep: MentionKey | None = None) -> None:
        if keep is None:
            current = self.current_row()
            keep = current.key if current is not None else None
        rows = self._all_rows(self._scope_ids())
        type_filter = self.type_filter.currentData()
        detector_filter = self.detector_filter.currentData()
        state_filter = self.state_filter.currentData()
        review_only = self.review_only.isChecked()
        filtered = [
            r
            for r in rows
            if (not type_filter or r.entity_type == type_filter)
            and (not detector_filter or r.entity.detector == detector_filter)
            and (not state_filter or r.state.value == state_filter)
            and (not review_only or r.state is RowState.REVIEW)
        ]
        self.model.set_rows(filtered)
        header = self.table.horizontalHeader()
        if header.sortIndicatorSection() >= 0:
            self.model.sort(header.sortIndicatorSection(), header.sortIndicatorOrder())
        self.table.setColumnHidden(6, self.scope_combo.currentData() == "doc")
        self.rows_label.setText(f"{len(filtered)} occurrence(s)")
        if keep is not None:
            for i, r in enumerate(filtered):
                if r.key == keep:
                    self.select_row(i)
                    break
        self._update_actions()

    # -- rendering ----------------------------------------------------------------------

    def _rerender(self) -> None:
        self._render_original()
        self._render_anonymized()
        self._highlight_current()

    def _render_original(self) -> None:
        doc = self._docs.get(self.current_doc_id or "")
        if doc is None:
            self.original.clear()
            self._original_map = RenderMap()
            self.legend.setText("")
            return
        self._doc_rows = self._all_rows({doc.id})
        by_segment: dict[str, list[EntityRow]] = {}
        for r in self._doc_rows:
            by_segment.setdefault(r.entity.segment_id, []).append(r)
        segments = []
        for seg in ordered_segments(doc):
            rows = by_segment.get(seg.id, [])
            rows.sort(
                key=lambda r: (
                    r.state not in (RowState.REJECTED, RowState.MENTION_REJECTED),
                    -(r.entity.end - r.entity.start),
                )
            )
            spans = [
                StyledSpan(
                    r.entity.start,
                    r.entity.end,
                    entity_format(r.entity_type, r.state, row_tooltip(r)),
                    r.key,
                )
                for r in rows
            ]
            segments.append((seg.id, seg.context, seg.text, spans))
        scroll = self.original.verticalScrollBar().value()
        self._moving_cursor = True
        try:
            self._original_map = render_segments(self.original, segments)
        finally:
            self._moving_cursor = False
        self.original.verticalScrollBar().setValue(scroll)
        types = {
            r.entity_type
            for r in self._doc_rows
            if r.state not in (RowState.REJECTED, RowState.MENTION_REJECTED)
        }
        self.legend.setText(legend_html(types) if types else "")

    def _render_anonymized(self) -> None:
        doc = self._docs.get(self.current_doc_id or "")
        scroll = self.anonymized.verticalScrollBar().value()
        if doc is None:
            self.anonymized.clear()
            self._anonymized_map = RenderMap()
            return
        if self.source_combo.currentData() == "output":
            self._render_output(doc)
        else:
            self._render_preview(doc)
        self.anonymized.verticalScrollBar().setValue(scroll)

    def _render_preview(self, doc: Document) -> None:
        effective = self.controller.effective()
        if effective is None:
            return
        by_segment = preview_segments(doc, effective)
        segments = []
        for seg in ordered_segments(doc):
            reps = by_segment.get(seg.id, [])
            text, spans = apply_with_spans(seg.text, reps)
            styled = [
                StyledSpan(
                    s.start,
                    s.end,
                    entity_format(s.entity_type),
                    (seg.id, s.source_start, s.source_end),
                )
                for s in spans
            ]
            segments.append((seg.id, seg.context, text, styled))
        self._anonymized_map = render_segments(self.anonymized, segments)

    def _render_output(self, doc: Document) -> None:
        c = self.controller
        top_id = self._top_of.get(doc.id, doc.id)
        result = c.result_for(top_id)
        self._anonymized_map = RenderMap()
        if result is None:
            self._show_message("Aucun fichier produit : appliquez d'abord les décisions.")
            return
        known, output = c.cached_output(top_id)
        if not known:
            self._show_message("Lecture du fichier produit…")
            c.extract_output(top_id)
            return
        if output is None:
            self._show_message("Aucun fichier n'a été produit pour ce document.")
            return
        target = self._matching_output(doc, output)
        if target is None:
            self._show_message("Cet élément ne figure pas dans le fichier produit (omis).")
            return
        segments = []
        for seg in ordered_segments(target):
            spans = [
                StyledSpan(s.start, s.end, entity_format(s.entity_type or "MASQUE"))
                for s in substitute_spans(seg.text)
            ]
            segments.append((seg.id, seg.context, seg.text, spans))
        self._anonymized_map = render_segments(self.anonymized, segments)

    def _matching_output(self, doc: Document, output: Document) -> Document | None:
        """Follow ``doc``'s child-index path from its top-level document in ``output``."""
        path: list[int] = []
        current = doc
        while current.parent_id is not None:
            parent = self._docs.get(current.parent_id)
            if parent is None:
                return None
            path.append(parent.children.index(current))
            current = parent
        target = output
        for i in reversed(path):
            if i >= len(target.children):
                return None
            target = target.children[i]
        return target

    def _show_message(self, text: str) -> None:
        self.anonymized.clear()
        self.anonymized.setPlainText(text)

    def _sync_scroll(self, source: QTextEdit, target: QTextEdit) -> None:
        if self._syncing or not self.sync_check.isChecked():
            return
        src = source.verticalScrollBar()
        dst = target.verticalScrollBar()
        if src.maximum() <= 0:
            return
        self._syncing = True
        try:
            dst.setValue(round(src.value() / src.maximum() * dst.maximum()))
        finally:
            self._syncing = False

    # -- selection ------------------------------------------------------------------------

    def current_row(self) -> EntityRow | None:
        index = self.table.currentIndex()
        if not index.isValid() or index.row() >= len(self.model.rows):
            return None
        return self.model.rows[index.row()]

    def selected_rows(self) -> list[EntityRow]:
        rows = sorted({i.row() for i in self.table.selectionModel().selectedRows()})
        selected = [self.model.rows[r] for r in rows if r < len(self.model.rows)]
        if not selected:
            current = self.current_row()
            return [current] if current is not None else []
        return selected

    def select_row(self, row: int) -> None:
        if not 0 <= row < len(self.model.rows):
            return
        index = self.model.index(row, 0)
        self.table.setCurrentIndex(index)
        self.table.selectRow(row)
        self.table.scrollTo(index)

    def select_document(self, document_id: str) -> None:
        for item in self._iter_items():
            if item.data(0, Qt.ItemDataRole.UserRole) == document_id:
                self.tree.setCurrentItem(item)
                return

    def _tree_changed(self, current: QTreeWidgetItem | None, _previous: object) -> None:
        if current is None:
            return
        doc_id = str(current.data(0, Qt.ItemDataRole.UserRole))
        if doc_id == self.current_doc_id:
            return
        self.current_doc_id = doc_id
        self._render_original()
        self._render_anonymized()
        if self.scope_combo.currentData() != "batch":
            self._refresh_rows()
        else:
            # Keep the list in step with the text: first mention of this document.
            first = next(
                (i for i, r in enumerate(self.model.rows) if r.entity.document_id == doc_id), -1
            )
            if first >= 0:
                self.table.selectionModel().blockSignals(True)
                self.select_row(first)
                self.table.selectionModel().blockSignals(False)
            else:
                self.table.clearSelection()
                self.table.setCurrentIndex(QModelIndex())
            self._highlight_current()
        self._update_actions()

    def _row_changed(self, current: QModelIndex, _previous: QModelIndex) -> None:
        if not current.isValid():
            return
        row = self.model.rows[current.row()]
        if row.entity.document_id != self.current_doc_id:
            keep = row.key
            self.current_doc_id = row.entity.document_id
            self.tree.blockSignals(True)
            self.select_document(row.entity.document_id)
            self.tree.blockSignals(False)
            self._render_original()
            self._render_anonymized()
            if self.scope_combo.currentData() != "batch":
                self._refresh_rows(keep)
        self._highlight_current()
        self._update_actions()

    def _highlight_current(self) -> None:
        row = self.current_row()
        t = theme.tokens()
        fmt = QTextCharFormat()
        fmt.setBackground(QColor(t.accent))
        fmt.setForeground(QColor(t.accent_text))
        for edit, mapping, key in (
            (self.original, self._original_map, row.key if row else None),
            (self.anonymized, self._anonymized_map, self._preview_key(row)),
        ):
            selections: list[QTextEdit.ExtraSelection] = []
            span = mapping.positions.get(key) if key is not None else None
            if span is not None:
                selection = QTextEdit.ExtraSelection()
                cursor = QTextCursor(edit.document())
                cursor.setPosition(span[0])
                cursor.setPosition(span[1], QTextCursor.MoveMode.KeepAnchor)
                selection.cursor = cursor
                selection.format = fmt
                selections.append(selection)
                self._moving_cursor = True
                try:
                    view = QTextCursor(edit.document())
                    view.setPosition(span[0])
                    edit.setTextCursor(view)
                    edit.ensureCursorVisible()
                finally:
                    self._moving_cursor = False
            edit.setExtraSelections(selections)

    def _preview_key(self, row: EntityRow | None) -> object:
        if row is None:
            return None
        e = row.entity
        for key in self._anonymized_map.positions:
            if (
                isinstance(key, tuple)
                and len(key) == 3
                and key[0] == e.segment_id
                and key[1] < e.end
                and e.start < key[2]
            ):
                return key
        return None

    def _original_clicked(self) -> None:
        if self._moving_cursor or self.original.textCursor().hasSelection():
            return
        key = self._original_map.key_at(self.original.textCursor().position())
        if key is None:
            return
        for i, r in enumerate(self.model.rows):
            if r.key == key:
                if self.table.currentIndex().row() != i:
                    self.select_row(i)
                return

    def _selection_changed(self) -> None:
        self._update_actions()

    def _original_menu(self, pos: QPoint) -> None:
        menu = self.original.createStandardContextMenu(pos)
        selected = clean_selection(self.original.textCursor().selectedText()).replace("\u200b", "")
        if selected:
            preview = " ".join(selected.split())
            if len(preview) > 40:
                preview = preview[:37] + "…"
            action = menu.addAction(f"Ajouter « {preview} » comme entité…")
            action.triggered.connect(self.add_entity_from_selection)
        menu.exec(self.original.mapToGlobal(pos))
        menu.deleteLater()

    # -- actions --------------------------------------------------------------------------

    def _after_action(self, rows: list[EntityRow]) -> None:
        if len(rows) != 1:
            return
        index = self.table.currentIndex().row()
        if self.review_only.isChecked() or self.state_filter.currentData():
            self.select_row(min(index, len(self.model.rows) - 1))
        else:
            self.select_row(min(index + 1, len(self.model.rows) - 1))

    def _rows_or_warn(self) -> list[EntityRow]:
        if self.controller.busy:
            return []
        rows = self.selected_rows()
        return rows

    def confirm_selected(self) -> None:
        rows = self._rows_or_warn()
        detected = [r.cluster_id for r in rows if not r.manual]
        if not detected:
            return
        self.controller.confirm(detected)
        self._after_action(rows)

    def reject_selected(self) -> None:
        rows = self._rows_or_warn()
        if not rows:
            return
        manual_values = [r.entity.value for r in rows if r.manual]
        detected = [r.cluster_id for r in rows if not r.manual]
        if manual_values:
            self._remove_manual(manual_values)
        if detected:
            self.controller.reject(detected)
        self._after_action(rows)

    def _remove_manual(self, values: list[str]) -> None:
        effective = self.controller.effective()
        if effective is None:
            return
        variants: list[str] = list(values)
        for r in self.selected_rows():
            cluster = effective.clusters.get(r.cluster_id)
            if r.manual and cluster is not None:
                variants.extend(cluster.variants)
        self.controller.remove_manual(variants)

    def reject_selected_mentions(self) -> None:
        rows = self._rows_or_warn()
        if not rows:
            return
        if any(r.manual for r in rows):
            self.banner.show_message(
                "Une occurrence issue d'un ajout manuel ne peut pas être rejetée seule : "
                "rejetez l'ajout entier.",
                "warning",
            )
        keys = [r.key for r in rows if not r.manual]
        if keys:
            self.controller.reject_mentions(keys)
            self._after_action(rows)

    def retype_selected(self) -> None:
        rows = self._rows_or_warn()
        if not rows:
            return
        current = rows[0].entity_type
        new_type = dialogs.choose_entity_type(
            self, self._types(), current, "Changer le type de l'entité"
        )
        if not new_type:
            return
        detected = [r.cluster_id for r in rows if not r.manual]
        manual = [r.entity.value for r in rows if r.manual]
        if manual:
            self.controller.retype_manual(manual, new_type)
        if detected:
            self.controller.retype(detected, new_type)

    def reset_selected(self) -> None:
        rows = self._rows_or_warn()
        if not rows:
            return
        self.controller.reset(
            [r.cluster_id for r in rows if not r.manual], [r.key for r in rows if not r.manual]
        )

    def add_entity_from_selection(self) -> None:
        if self.controller.busy or self.controller.analysis is None:
            return
        value = clean_selection(self.original.textCursor().selectedText()).replace("\u200b", "")
        if len(value) < 2:
            self.banner.show_message(
                "Sélectionnez d'abord, dans le texte original, la valeur à masquer.", "info"
            )
            return
        self.add_entity(value)

    def add_entity(self, value: str, entity_type: str | None = None) -> bool:
        chosen = entity_type or dialogs.choose_entity_type(
            self, self._types(), "", "Ajouter une entité"
        )
        if not chosen:
            return False
        if not self.controller.add_manual(value, chosen):
            self.banner.show_message("Cette valeur a déjà été ajoutée avec ce type.", "info")
            return False
        effective = self.controller.effective()
        count = 0
        if effective is not None:
            count = sum(1 for e in effective.entities if e.review is ReviewStatus.MANUAL)
        self.banner.show_message(
            f"Entité ajoutée : elle sera masquée dans tout le lot ({count} occurrence(s) "
            "manuelle(s) au total).",
            "success",
        )
        return True

    def _types(self) -> list[str]:
        config = self.controller.context.config
        return known_types(self.controller.analysis, config.custom_labels.values())

    # -- navigation -----------------------------------------------------------------------

    def move_selection(self, step: int) -> None:
        if not self.model.rows:
            return
        index = self.table.currentIndex().row()
        target = 0 if index < 0 else max(0, min(len(self.model.rows) - 1, index + step))
        self.select_row(target)

    def next_review(self) -> None:
        rows = self.model.rows
        if not rows:
            return
        start = self.table.currentIndex().row()
        for offset in range(1, len(rows) + 1):
            i = (start + offset) % len(rows)
            if rows[i].state is RowState.REVIEW:
                self.select_row(i)
                return
        if self.scope_combo.currentData() != "batch" and self.controller.pending_review_count():
            self.scope_combo.setCurrentIndex(self.scope_combo.findData("batch"))
            self.next_review()

    # -- controller events -------------------------------------------------------------------

    def _decisions_changed(self) -> None:
        self._fill_filters()
        self._rerender_keep()
        self._update_tree_states()
        self._update_counter()
        self._update_banner()

    def _rerender_keep(self) -> None:
        current = self.current_row()
        keep = current.key if current is not None else None
        self._render_original()
        self._render_anonymized()
        self._refresh_rows(keep)
        self._highlight_current()

    def _result_ready(self) -> None:
        self._update_tree_states()
        self._update_banner()
        if self.controller.result is not None:
            self.source_combo.setCurrentIndex(self.source_combo.findData("output"))
        self._render_anonymized()

    def _output_extracted(self, top_id: str, _doc: object, error: str) -> None:
        if error:
            self.banner.show_message(error, "warning")
        if (
            self.current_doc_id
            and self._top_of.get(self.current_doc_id) == top_id
            and self.source_combo.currentData() == "output"
        ):
            self._render_anonymized()
            self._highlight_current()

    def _config_changed(self) -> None:
        if self.controller.analysis is not None:
            self.controller.refresh_preview()

    def _update_counter(self) -> None:
        n = self.controller.pending_review_count()
        t = theme.tokens()
        if self.controller.analysis is None:
            self.counter.setText("")
        elif n:
            self.counter.setText(f"<b style='color:{t.warning}'>{n} groupe(s) à revoir</b>")
        else:
            self.counter.setText(f"<span style='color:{t.success}'>Aucun groupe à revoir</span>")
        self.pending_changed.emit(n)

    def _update_banner(self) -> None:
        c = self.controller
        if c.analysis is None:
            return
        if c.analysis_is_stale():
            self.banner.show_message(
                "Les réglages de détection ont changé depuis l'analyse : relancez l'analyse "
                "pour en tenir compte.",
                "warning",
            )
        elif c.decisions_since_apply():
            self.banner.show_message(
                "Des décisions ont changé depuis l'écriture : l'aperçu en tient compte, mais "
                "pas le fichier produit. Appliquez à nouveau pour mettre les fichiers à jour.",
                "info",
            )
        elif self.banner.level in ("warning", "info") and not self.banner.isHidden():
            self.banner.clear()

    def _update_actions(self) -> None:
        c = self.controller
        has_analysis = c.analysis is not None and not c.busy
        has_rows = bool(self.model.rows) and has_analysis
        for b in (
            self.confirm_button,
            self.reject_button,
            self.reject_mention_button,
            self.retype_button,
            self.reset_button,
        ):
            b.setEnabled(has_rows)
        self.add_button.setEnabled(has_analysis and self.original.textCursor().hasSelection())
        self.apply_button.setEnabled(has_analysis)
        has_output = c.result is not None
        model = self.source_combo.model()
        item = getattr(model, "item", None)
        if callable(item):
            entry = item(1)
            if entry is not None:
                entry.setEnabled(has_output)
        if not has_output and self.source_combo.currentIndex() == 1:
            self.source_combo.setCurrentIndex(0)
