"""« Revue » page: cluster-centric review queue and side-by-side view.

One row per entity (all its occurrences share one decision); rows expand to the
occurrences, each with a context snippet. The right-hand side shows the selected
document as text (any format, mail headers grouped) or visually (PDF pages and
images rendered off the UI thread, entity boxes drawn on the original).
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QModelIndex, QPoint, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QKeySequence,
    QShortcut,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QTabWidget,
    QTextEdit,
    QToolButton,
    QTreeView,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

from anonyma.frenchtext import NBSP, plural, typo
from anonyma.gui import dialogs, theme
from anonyma.gui.controller import BatchController
from anonyma.gui.itemmodels import (
    CLUSTER_ROLE,
    KIND_ROLE,
    MENTION_ROLE,
    SORTS,
    STATE_FILTERS,
    ClusterTreeModel,
    ReviewItemDelegate,
)
from anonyma.gui.labels import STATUS_LEVELS, detector_label, entity_type_label, status_label
from anonyma.gui.preview import (
    STATE_LABELS,
    EntityRow,
    MentionKey,
    RowState,
    apply_with_spans,
    clean_selection,
    entity_rows,
    known_types,
    pending_review,
    preview_segments,
    substitute_spans,
)
from anonyma.gui.textview import (
    RenderMap,
    StyledSpan,
    display_text,
    entity_format,
    ordered_segments,
    render_segments,
    row_tooltip,
)
from anonyma.gui.visual import Box, VisualPair, output_kind, visual_kind
from anonyma.gui.widgets import (
    BADGE_ROLE,
    BadgeDelegate,
    Banner,
    EmptyState,
    KeyButton,
    button,
    fit_badge_column,
    legend_html,
    muted,
    page_header,
    primary_button,
    section_title,
)
from anonyma.models import (
    BBox,
    Document,
    DocumentStatus,
    Entity,
    ReplacementMode,
    ReviewStatus,
    Segment,
)
from anonyma.textutils import normalize

__all__ = ["ReviewPage", "display_text"]

SCOPES = {"batch": "Tout le lot", "doc": "Document affiché"}
APPLY_LABEL = "Appliquer les décisions"


class ReviewPage(QWidget):
    apply_requested = Signal()
    pending_changed = Signal(int)

    def __init__(self, controller: BatchController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.controller = controller
        self.current_doc_id: str | None = None
        self._original_map = RenderMap()
        self._anonymized_map = RenderMap()
        self._doc_rows: list[EntityRow] = []
        self._segments: dict[str, Segment] = {}
        self._current_key: MentionKey | None = None
        self._syncing = False
        self._moving_cursor = False
        self._pending_residue: tuple[str, str, str] | None = None
        self._residue_value = ""
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(200)
        self._search_timer.timeout.connect(self._apply_filters)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 16)
        outer.setSpacing(10)
        head = QHBoxLayout()
        title = page_header("Revue")
        title.setToolTip(
            "Une ligne par entité : une décision vaut pour toutes ses occurrences, dans tout "
            "le lot. La relecture reste facultative."
        )
        head.addWidget(title, 1)
        self.counter = QLabel("")
        self.counter.setObjectName("ReviewCounter")
        head.addWidget(self.counter, 0, Qt.AlignmentFlag.AlignVCenter)
        head.addSpacing(8)
        self.next_undecided_button = KeyButton("Suivante non décidée", "Maj+N")
        self.next_undecided_button.setToolTip("Aller à la prochaine entité sans décision")
        self.next_undecided_button.clicked.connect(self.next_undecided)
        head.addWidget(self.next_undecided_button, 0, Qt.AlignmentFlag.AlignVCenter)
        self.apply_button = primary_button(APPLY_LABEL, "check")
        self.apply_button.setToolTip("Écrire les fichiers anonymisés (Ctrl+Entrée)")
        self.apply_button.clicked.connect(self.apply_requested.emit)
        head.addWidget(self.apply_button, 0, Qt.AlignmentFlag.AlignVCenter)
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

        self.tabs = QTabWidget()
        self.tabs.setMinimumWidth(390)
        self.tabs.addTab(self._build_queue(), "Entités")
        self.tabs.addTab(self._build_documents(), "Documents")
        main.addWidget(self.tabs)
        main.addWidget(self._build_viewer())
        main.setStretchFactor(0, 0)
        main.setStretchFactor(1, 1)
        main.setSizes([480, 940])

        self._shortcuts()
        c = self.controller
        c.analysis_ready.connect(self.reload)
        c.analysis_cleared.connect(self.reload)
        c.decisions_changed.connect(self._decisions_changed)
        c.effective_ready.connect(self._effective_ready)
        c.result_ready.connect(self._result_ready)
        c.output_extracted.connect(self._output_extracted)
        c.phase_changed.connect(lambda _p: self._update_actions())
        c.context.config_changed.connect(self._config_changed)
        theme.notifier().changed.connect(self._rerender)
        self.reload()

    # -- construction -----------------------------------------------------------------------------

    def _build_queue(self) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(6, 8, 6, 6)
        filters = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setObjectName("ReviewSearch")
        self.search.setPlaceholderText("Rechercher…")
        self.search.setToolTip("Rechercher une entité ou un type")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(lambda _t: self._search_timer.start())
        filters.addWidget(self.search, 1)
        self.state_filter = QComboBox()
        self.state_filter.setToolTip("Décision")
        for key, label in STATE_FILTERS.items():
            self.state_filter.addItem(label, key)
        filters.addWidget(self.state_filter)
        # Less frequent filters live in a menu, so that the row fits the narrowest window.
        self.type_filter = QComboBox()
        self.type_filter.addItem("Tous les types", "")
        self.detector_filter = QComboBox()
        self.detector_filter.addItem("Tous les détecteurs", "")
        self.scope_combo = QComboBox()
        for key, label in SCOPES.items():
            self.scope_combo.addItem(label, key)
        self.sort_combo = QComboBox()
        for key, label in SORTS.items():
            self.sort_combo.addItem(label, key)
        self.filters_button = QToolButton()
        self.filters_button.setText("Filtres")
        self.filters_button.setToolTip("Type, détecteur, portée et ordre")
        self.filters_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        filters_menu = QMenu(self.filters_button)
        panel = QWidget()
        form = QFormLayout(panel)
        form.setContentsMargins(12, 10, 12, 10)
        for label, combo in (
            ("&Type", self.type_filter),
            ("&Détecteur", self.detector_filter),
            ("&Portée", self.scope_combo),
            ("&Ordre", self.sort_combo),
        ):
            combo.setMinimumWidth(220)
            form.addRow(label, combo)
        reset = button("Réinitialiser les filtres")
        reset.clicked.connect(self.clear_filters)
        form.addRow("", reset)
        panel_action = QWidgetAction(filters_menu)
        panel_action.setDefaultWidget(panel)
        filters_menu.addAction(panel_action)
        self.filters_button.setMenu(filters_menu)
        filters.addWidget(self.filters_button)
        layout.addLayout(filters)
        for combo in (
            self.state_filter,
            self.type_filter,
            self.detector_filter,
            self.scope_combo,
            self.sort_combo,
        ):
            combo.currentIndexChanged.connect(lambda _i: self._apply_filters())

        self.model = ClusterTreeModel(self.controller)
        self.queue = QTreeView()
        self.queue.setObjectName("ReviewQueue")
        self.queue.setModel(self.model)
        self.queue.setItemDelegate(ReviewItemDelegate(self.queue))
        self.queue.setItemDelegateForColumn(1, BadgeDelegate(self.queue))
        self.queue.setUniformRowHeights(True)
        self.queue.setAlternatingRowColors(True)
        self.queue.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.queue.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.queue.setExpandsOnDoubleClick(True)
        header = self.queue.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        fit_badge_column(self.queue, 1, STATE_LABELS.values())
        self.queue.selectionModel().currentChanged.connect(self._queue_current_changed)
        layout.addWidget(self.queue, 1)
        self.rows_label = muted("")
        self.rows_label.setWordWrap(False)
        self.rows_row = QHBoxLayout()
        self.rows_row.addWidget(self.rows_label, 1)
        layout.addLayout(self.rows_row)

        # Decisions: the key of each action is shown on its button.
        actions = QGridLayout()
        actions.setHorizontalSpacing(6)
        actions.setVerticalSpacing(6)
        self.confirm_button = KeyButton("Masquer", "V", "check")
        self.confirm_button.setToolTip("Masquer cette entité partout dans le lot")
        self.reject_button = KeyButton("Ne pas masquer", "R", "reject")
        self.reject_button.setToolTip(
            "Faux positif : laisser cette valeur visible, dans tout le lot"
        )
        self.retype_button = KeyButton("Changer le type…", "T", "tag")
        self.retype_button.setToolTip("Corriger le type de l'entité")
        self.reset_button = KeyButton("Retirer la décision", "U", "undo")
        self.reset_button.setToolTip("Revenir à l'état détecté, sans décision")
        self.reject_mention_button = KeyButton("Ne pas masquer l'occurrence", "O")
        self.reject_mention_button.setToolTip(
            "Laisser visible cette occurrence seulement (dépliez l'entité pour la choisir)"
        )
        self.bulk_button = QToolButton()
        self.bulk_button.setText("Actions groupées")
        self.bulk_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        bulk = QMenu(self.bulk_button)
        bulk.addAction("Masquer tout ce type…", self.confirm_all_of_type)
        bulk.addAction("Masquer tout le reste…", self.confirm_all_remaining)
        bulk.addAction("Ne pas masquer la sélection…", self.reject_selection_bulk)
        self.bulk_button.setMenu(bulk)
        self.confirm_button.clicked.connect(self.confirm_selected)
        self.reject_button.clicked.connect(self.reject_selected)
        self.reject_mention_button.clicked.connect(self.reject_selected_mentions)
        self.retype_button.clicked.connect(self.retype_selected)
        self.reset_button.clicked.connect(self.reset_selected)
        actions.addWidget(self.confirm_button, 0, 0)
        actions.addWidget(self.reject_button, 0, 1)
        actions.addWidget(self.retype_button, 1, 0)
        actions.addWidget(self.reset_button, 1, 1)
        actions.addWidget(self.reject_mention_button, 2, 0, 1, 2)
        self.rows_row.addWidget(self.bulk_button)
        layout.addLayout(actions)
        hint = muted("N / P : entité suivante ou précédente")
        hint.setWordWrap(False)
        hint.setToolTip(
            "N / P : entité ou occurrence suivante / précédente\n"
            "Maj+N : prochaine entité sans décision\n"
            "V : masquer · R : ne pas masquer · O : ne pas masquer l'occurrence\n"
            "T : changer le type · U : retirer la décision\n"
            "A : ajouter la sélection du texte original comme entité\n"
            "Ctrl+Entrée : appliquer les décisions"
        )
        layout.addWidget(hint)
        return box

    def _build_documents(self) -> QWidget:
        self.tree = QTreeWidget()
        self.tree.setObjectName("DocumentTree")
        self.tree.setHeaderLabels(["Document", "Occurrences", "État"])
        tree_header = self.tree.header()
        tree_header.setStretchLastSection(False)
        tree_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        tree_header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.setItemDelegateForColumn(2, BadgeDelegate(self.tree))
        fit_badge_column(self.tree, 2, [status_label(status) for status in DocumentStatus])
        self.tree.currentItemChanged.connect(self._tree_changed)
        return self.tree

    def _build_viewer(self) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(6, 0, 0, 0)
        self.doc_title = QLabel("")
        self.doc_title.setObjectName("SectionTitle")
        self.doc_title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        title_row = QHBoxLayout()
        title_row.addWidget(self.doc_title, 1)
        layout.addLayout(title_row)
        bar = QHBoxLayout()
        self.text_mode = QPushButton("Texte")
        self.visual_mode = QPushButton("Visuel")
        self.mode_group = QButtonGroup(self)
        for i, b in enumerate((self.text_mode, self.visual_mode)):
            b.setCheckable(True)
            self.mode_group.addButton(b, i)
            bar.addWidget(b)
        self.text_mode.setChecked(True)
        self.text_mode.setToolTip("Texte extrait, valeurs surlignées")
        self.visual_mode.setToolTip("Pages PDF et images, zones détectées encadrées")
        for b in (self.text_mode, self.visual_mode):
            b.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.mode_group.idClicked.connect(lambda _i: self._render_current())
        bar.addStretch(1)
        self.source_combo = QComboBox()
        self.source_combo.addItem("Aperçu", "preview")
        self.source_combo.addItem("Fichier produit", "output")
        self.source_combo.setItemData(
            0, "Aperçu avec les décisions actuelles", Qt.ItemDataRole.ToolTipRole
        )
        self.source_combo.setItemData(
            1, "Fichier écrit lors de la dernière application", Qt.ItemDataRole.ToolTipRole
        )
        self.source_combo.setToolTip("Côté anonymisé : aperçu ou fichier produit")
        self.source_combo.currentIndexChanged.connect(lambda _i: self._render_current())
        bar.addWidget(self.source_combo)
        self.add_button = KeyButton("Ajouter une entité…", "A", "add")
        self.add_button.setToolTip(
            "Sélectionnez du texte dans l'original, puis ajoutez-le comme entité."
        )
        self.add_button.clicked.connect(self.add_entity_from_selection)
        title_row.addWidget(self.add_button)
        layout.addLayout(bar)

        self.view_stack = QStackedWidget()
        layout.addWidget(self.view_stack, 1)
        sides = QSplitter(Qt.Orientation.Horizontal)
        left_box = QWidget()
        left_layout = QVBoxLayout(left_box)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(section_title("Original"))
        self.original = QTextEdit()
        self.original.setObjectName("OriginalText")
        self.original.setReadOnly(True)
        self.original.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        self.original.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.original.customContextMenuRequested.connect(self._original_menu)
        self.original.selectionChanged.connect(self._update_actions)
        self.original.cursorPositionChanged.connect(self._original_clicked)
        left_layout.addWidget(self.original)
        sides.addWidget(left_box)
        right_box = QWidget()
        right_layout = QVBoxLayout(right_box)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addWidget(section_title("Anonymisé"))
        self.anonymized = QTextEdit()
        self.anonymized.setObjectName("AnonymizedText")
        self.anonymized.setReadOnly(True)
        right_layout.addWidget(self.anonymized)
        sides.addWidget(right_box)
        self.view_stack.addWidget(sides)
        self.visual = VisualPair()
        self.view_stack.addWidget(self.visual)
        self.original.verticalScrollBar().valueChanged.connect(
            lambda _v: self._sync_scroll(self.original, self.anonymized)
        )
        self.anonymized.verticalScrollBar().valueChanged.connect(
            lambda _v: self._sync_scroll(self.anonymized, self.original)
        )

        legend_row = QHBoxLayout()
        self.legend = QLabel("")
        self.legend.setWordWrap(True)
        self.legend.setTextFormat(Qt.TextFormat.RichText)
        legend_row.addWidget(self.legend, 1)
        self.sync_check = QCheckBox("Défilement synchronisé")
        self.sync_check.setChecked(True)
        legend_row.addWidget(self.sync_check)
        layout.addLayout(legend_row)
        return box

    def _shortcuts(self) -> None:
        bindings: list[tuple[str, Callable[[], None]]] = [
            ("N", lambda: self.move_selection(1)),
            ("P", lambda: self.move_selection(-1)),
            ("Alt+Down", lambda: self.move_selection(1)),
            ("Alt+Up", lambda: self.move_selection(-1)),
            ("Shift+N", self.next_undecided),
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

    # -- data -------------------------------------------------------------------------------------

    @property
    def _docs(self) -> dict[str, Document]:
        return self.controller.index.documents

    def reload(self) -> None:
        analysis = self.controller.analysis
        self.tree.clear()
        self.current_doc_id = None
        self._current_key = None
        if analysis is None:
            self.stack.setCurrentWidget(self.empty)
            self.model.refresh(reset=True)
            self.banner.clear()
            self._update_counter()
            self._update_actions()
            return
        self.stack.setCurrentIndex(1)
        for top in analysis.documents:
            self.tree.addTopLevelItem(self._tree_item(top))
        self.tree.expandAll()
        self._fill_filters()
        self.source_combo.setCurrentIndex(0)
        self._update_banner()
        self.model.refresh(reset=True)
        first = self.tree.topLevelItem(0)
        if first is not None:
            self.tree.setCurrentItem(first)
        self._update_tree_states()
        self._update_counter()
        self._update_actions()
        if self.model.ids:
            self.select_cluster(self.model.ids[0])

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
        if analysis is None:
            return
        effective = c.effective()
        pending = {cl.id for cl in pending_review(analysis, c.decisions)}
        review_docs = {e.document_id for cid in pending for e in c.index.by_cluster.get(cid, [])}
        for item in self._iter_items():
            doc_id = str(item.data(0, Qt.ItemDataRole.UserRole))
            doc = self._docs.get(doc_id)
            if doc is None:
                continue
            count = (
                len(effective.for_document(doc_id))
                if effective is not None
                else len(c.index.by_document.get(doc_id, []))
            )
            item.setText(1, str(count))
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
        c = self.controller
        types: set[str] = set()
        detectors: set[str] = set()
        if c.analysis is not None:
            types = {cl.entity_type for cl in c.analysis.clusters.values()}
            types |= set(c.decisions.retyped_clusters.values())
            types |= {m.entity_type for m in c.decisions.manual}
            for cl in c.analysis.clusters.values():
                detectors |= cl.detectors
        for combo, values, all_label in (
            (self.type_filter, sorted(types), "Tous les types"),
            (self.detector_filter, sorted(detectors), "Tous les détecteurs"),
        ):
            current = combo.currentData()
            combo.blockSignals(True)
            combo.clear()
            combo.addItem(all_label, "")
            for v in values:
                label = entity_type_label(v) if combo is self.type_filter else detector_label(v)
                combo.addItem(label, v)
            combo.setCurrentIndex(max(combo.findData(current), 0))
            combo.blockSignals(False)

    # -- queue ------------------------------------------------------------------------------------

    def _apply_filters(self) -> None:
        self.model.search = normalize(self.search.text())
        self.model.type_filter = str(self.type_filter.currentData() or "")
        self.model.detector_filter = str(self.detector_filter.currentData() or "")
        self.model.state_filter = str(self.state_filter.currentData() or "")
        self.model.sort_key = str(self.sort_combo.currentData() or "incertitude")
        scope = self.scope_combo.currentData()
        self.model.document_ids = (
            {self.current_doc_id} if scope == "doc" and self.current_doc_id else None
        )
        active = sum(
            1
            for combo in (self.type_filter, self.detector_filter, self.scope_combo)
            if combo.currentIndex() > 0
        )
        self.filters_button.setText(f"Filtres ({active})" if active else "Filtres")
        self._refresh_model()

    def _refresh_model(self, *, reset: bool | None = None) -> None:
        current = self.queue.currentIndex()
        cid = self.model.cluster_id_at(current)
        mention = current.data(MENTION_ROLE) if current.isValid() else None
        expanded = [
            self.model.ids[r]
            for r in range(len(self.model.ids))
            if self.queue.isExpanded(self.model.index(r, 0))
        ]
        was_reset = self.model.refresh(reset=reset, expanded=set(expanded))
        if was_reset:
            for e_cid in expanded:
                row = self.model.row_of(e_cid)
                if row >= 0:
                    self.queue.setExpanded(self.model.index(row, 0), True)
            if cid:
                self._select_index(
                    self._index_for(cid, mention.key if mention else None), notify=False
                )
        _decided, total = self.controller.decided_counts()
        shown = len(self.model.ids)
        self.rows_label.setText(
            plural(total, "entité")
            if shown == total
            else f"{shown}{NBSP}/{NBSP}{plural(total, 'entité')} affichées"
        )
        self.queue.viewport().update()

    def _index_for(self, cluster_id: str, key: MentionKey | None = None) -> QModelIndex:
        row = self.model.row_of(cluster_id)
        if row < 0:
            return QModelIndex()
        parent = self.model.index(row, 0)
        if key is not None:
            for i, e in enumerate(self.model.mentions(cluster_id)):
                if e.key == key:
                    return self.model.index(i, 0, parent)
        return parent

    def _select_index(self, index: QModelIndex, *, notify: bool = True) -> None:
        if not index.isValid():
            return
        if index.parent().isValid():
            self.queue.setExpanded(index.parent(), True)
        selection = self.queue.selectionModel()
        if not notify:
            selection.blockSignals(True)
        self.queue.setCurrentIndex(index)
        selection.select(
            index,
            selection.SelectionFlag.ClearAndSelect | selection.SelectionFlag.Rows,
        )
        if not notify:
            selection.blockSignals(False)
        self.queue.scrollTo(index)

    def select_cluster(self, cluster_id: str, key: MentionKey | None = None) -> bool:
        index = self._index_for(cluster_id, key)
        if not index.isValid():
            return False
        self._select_index(index)
        return True

    def current_cluster_id(self) -> str:
        return self.model.cluster_id_at(self.queue.currentIndex())

    def selected_cluster_ids(self) -> list[str]:
        rows = self.queue.selectionModel().selectedRows(0) or [self.queue.currentIndex()]
        ids: list[str] = []
        for index in rows:
            cid = self.model.cluster_id_at(index)
            if cid and cid not in ids:
                ids.append(cid)
        return ids

    def selected_mentions(self) -> list[Entity]:
        rows = self.queue.selectionModel().selectedRows(0) or [self.queue.currentIndex()]
        return [m for i in rows if isinstance(m := i.data(MENTION_ROLE), Entity)]

    def _is_manual(self, cluster_id: str) -> bool:
        analysis = self.controller.analysis
        return analysis is not None and cluster_id not in analysis.clusters

    def _queue_current_changed(self, current: QModelIndex, _previous: QModelIndex) -> None:
        if not current.isValid():
            return
        mention = current.data(MENTION_ROLE)
        if isinstance(mention, Entity):
            self.show_mention(mention)
        else:
            cid = self.model.cluster_id_at(current)
            mentions = self.model.mentions(cid)
            if mentions:
                here = [e for e in mentions if e.document_id == self.current_doc_id]
                self.show_mention(here[0] if here else mentions[0])
        self._update_actions()

    def move_selection(self, step: int) -> None:
        """Next / previous visible row (entities, or occurrences of an expanded one)."""
        current = self.queue.currentIndex()
        if not current.isValid():
            if self.model.ids:
                self._select_index(self.model.index(0, 0))
            return
        target = self.queue.indexBelow(current) if step > 0 else self.queue.indexAbove(current)
        if target.isValid():
            self._select_index(target.siblingAtColumn(0))

    def next_undecided(self) -> None:
        c = self.controller
        if c.analysis is None:
            return
        ids = self.model.ids
        start = self.model.row_of(self.current_cluster_id())
        for offset in range(1, len(ids) + 1):
            cid = ids[(start + offset) % len(ids)] if start >= 0 else ids[offset - 1]
            if not c.is_decided(cid):
                self.select_cluster(cid)
                return
        hidden = [cid for cid in c.cluster_ids() if not c.is_decided(cid)]
        if hidden and (self.model.ids != c.cluster_ids()):
            self.banner.show_message(
                "Des entités sans décision sont cachées par les filtres.",
                "info",
                [("Afficher tout", self.clear_filters)],
            )
        elif not hidden:
            self.banner.show_message("Toutes les entités ont une décision.", "success")

    def clear_filters(self) -> None:
        for combo in (self.state_filter, self.type_filter, self.detector_filter, self.scope_combo):
            combo.blockSignals(True)
            combo.setCurrentIndex(0)
            combo.blockSignals(False)
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        self._apply_filters()

    # -- documents --------------------------------------------------------------------------------

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
        self._set_document(doc_id)
        if self.scope_combo.currentData() == "doc":
            self._apply_filters()

    def _set_document(self, doc_id: str) -> None:
        self.current_doc_id = doc_id
        doc = self._docs.get(doc_id)
        self.doc_title.setText(doc.name if doc is not None else "")
        has_visual = doc is not None and visual_kind(doc) is not None
        self.visual_mode.setEnabled(has_visual)
        if not has_visual and self.visual_mode.isChecked():
            self.text_mode.setChecked(True)
        self._render_current(reset_view=True)

    def show_mention(self, entity: Entity) -> None:
        """Jump to an occurrence in the side-by-side view."""
        self._current_key = entity.key
        if entity.document_id != self.current_doc_id:
            self.tree.blockSignals(True)
            self.select_document(entity.document_id)
            self.tree.blockSignals(False)
            self._set_document(entity.document_id)
        if self.visual_mode.isChecked():
            seg = self._segments.get(entity.segment_id)
            page = seg.context.page if seg is not None and seg.context.page is not None else 0
            self.visual.go_to(page, entity.key)
        self._highlight_current()

    # -- rendering --------------------------------------------------------------------------------

    def _rerender(self) -> None:
        self._render_current()

    def _render_current(self, *, reset_view: bool = False, preview: bool = True) -> None:
        """Render the current document; ``preview=False`` keeps the anonymized side as
        it is (a decision: the side is re-rendered once its new preview is computed)."""
        doc = self._docs.get(self.current_doc_id or "")
        if doc is None:
            self.original.clear()
            self.anonymized.clear()
            self.legend.setText("")
            return
        self._segments = {s.id: s for s in doc.segments}
        c = self.controller
        if c.analysis is not None:
            self._doc_rows = entity_rows(
                c.analysis,
                c.decisions,
                c.effective(),
                {doc.id},
                entities=c.index.by_document.get(doc.id, []),
            )
        if self.visual_mode.isChecked() and visual_kind(doc) is not None:
            self.view_stack.setCurrentWidget(self.visual)
            self._render_visual(doc, reset_view)
        else:
            self.view_stack.setCurrentIndex(0)
            self._render_original(doc)
            if preview:
                self._render_anonymized(doc)
            self._highlight_current()
        types = {
            r.entity_type
            for r in self._doc_rows
            if r.state not in (RowState.REJECTED, RowState.MENTION_REJECTED)
        }
        self.legend.setText(legend_html(types) if types else "")

    def _render_original(self, doc: Document) -> None:
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

    def _render_anonymized(self, doc: Document) -> None:
        scroll = self.anonymized.verticalScrollBar().value()
        if self.source_combo.currentData() == "output":
            self._render_output(doc)
        else:
            self._render_preview(doc)
        self.anonymized.verticalScrollBar().setValue(scroll)

    def _render_preview(self, doc: Document) -> None:
        effective = self.controller.effective()
        self._anonymized_map = RenderMap()
        if effective is None:
            self._show_message("Calcul de l'aperçu…")
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

    def _output_document(self, doc: Document) -> tuple[str, Document | None]:
        """(message, matching document of the produced file)."""
        c = self.controller
        top_id = c.index.top_of.get(doc.id, doc.id)
        if c.result_for(top_id) is None:
            return "Aucun fichier produit : appliquez d'abord les décisions.", None
        known, output = c.cached_output(top_id)
        if not known:
            c.extract_output(top_id)
            return "Lecture du fichier produit…", None
        if output is None:
            return "Aucun fichier n'a été produit pour ce document.", None
        target = self._matching_output(doc, output)
        if target is None:
            return "Cet élément ne figure pas dans le fichier produit (omis).", None
        return "", target

    def _render_output(self, doc: Document) -> None:
        self._anonymized_map = RenderMap()
        top_id = self.controller.index.top_of.get(doc.id, doc.id)
        pending = self._pending_residue
        if pending is not None and pending[0] == top_id and self._follow_residue(doc):
            return
        message, target = self._output_document(doc)
        if target is None:
            self._show_message(message)
            return
        segments = []
        for seg in ordered_segments(target):
            spans = [
                StyledSpan(s.start, s.end, entity_format(s.entity_type or "MASQUE"))
                for s in substitute_spans(seg.text)
            ]
            segments.append((seg.id, seg.context, seg.text, spans))
        self._anonymized_map = render_segments(self.anonymized, segments)
        if self._residue_value:
            self._highlight_residue(self._residue_value)

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

    # -- visual mode ------------------------------------------------------------------------------

    def _render_visual(self, doc: Document, reset_view: bool) -> None:
        kind = visual_kind(doc)
        assert kind is not None
        c = self.controller
        left = (doc.source, kind)
        right: tuple[object, str] | None = left
        right_boxes: Callable[[int], list[Box]] | None = self._preview_boxes
        message = ""
        if self.source_combo.currentData() == "output":
            result = c.result_for(doc.id)
            out_kind = output_kind(result)
            right_boxes = None
            if result is not None and out_kind is not None and result.output_path is not None:
                right = (result.output_path, out_kind)
            else:
                right = None
                message = (
                    "Aucun fichier produit : appliquez d'abord les décisions."
                    if result is None
                    else "Le fichier produit n'a pas de rendu visuel."
                )
        page = 0
        if not reset_view:
            page = self.visual.current_page()
        elif self._current_key is not None and self._current_key[0] == doc.id:
            seg = self._segments.get(self._current_key[1])
            page = seg.context.page if seg is not None and seg.context.page is not None else 0
        self.visual.load(
            left,
            right,  # type: ignore[arg-type]
            self._original_boxes,
            right_boxes,
            message,
            page,
        )
        if self._current_key is not None:
            self.visual.go_to(page, self._current_key)

    def _span_box(self, entity: Entity, page: int) -> BBox | None:
        seg = self._segments.get(entity.segment_id)
        if seg is None or (seg.context.page or 0) != page:
            return None
        if seg.context.page is None and seg.char_boxes is None and seg.context.bbox is None:
            return None
        return seg.span_bbox(entity.start, entity.end)

    def _original_boxes(self, page: int) -> list[Box]:
        t = theme.tokens()
        boxes: list[Box] = []
        for r in self._doc_rows:
            rect = self._span_box(r.entity, page)
            if rect is None:
                continue
            rejected = r.state in (RowState.REJECTED, RowState.MENTION_REJECTED)
            accent = theme.entity_accent(r.entity_type)
            fill = None
            if not rejected:
                fill = QColor(accent)
                fill.setAlphaF(0.22)
            pen = QColor(t.warning) if r.state is RowState.REVIEW else accent
            if rejected:
                pen = QColor(t.muted)
            boxes.append(
                Box(
                    rect,
                    pen,
                    fill,
                    dashed=rejected or r.state is RowState.REVIEW,
                    key=r.key,
                    tooltip=row_tooltip(r),
                )
            )
        return boxes

    def _preview_boxes(self, page: int) -> list[Box]:
        effective = self.controller.effective()
        if effective is None or self.current_doc_id is None:
            return []
        blackout = self.controller.context.config.mode is ReplacementMode.BLACKOUT
        boxes: list[Box] = []
        for e in effective.for_document(self.current_doc_id):
            rect = self._span_box(e, page)
            if rect is None:
                continue
            if blackout:
                fill, label = QColor("#000000"), ""
            else:
                fill = theme.entity_colors(e.entity_type)[0]
                try:
                    label = effective.table.substitute_for(e)
                except KeyError:
                    label = ""
            boxes.append(Box(rect, fill, fill, key=e.key, label=label, tooltip=label))
        return boxes

    # -- highlighting -----------------------------------------------------------------------------

    def _highlight_current(self) -> None:
        key = self._current_key
        t = theme.tokens()
        fmt = QTextCharFormat()
        fmt.setBackground(QColor(t.accent))
        fmt.setForeground(QColor(t.accent_text))
        for edit, mapping, span_key in (
            (self.original, self._original_map, key),
            (self.anonymized, self._anonymized_map, self._preview_key(key)),
        ):
            selections: list[QTextEdit.ExtraSelection] = []
            span = mapping.positions.get(span_key) if span_key is not None else None
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
            if edit is self.original or not self._residue_value:
                edit.setExtraSelections(selections)

    def _preview_key(self, key: MentionKey | None) -> object:
        if key is None:
            return None
        _doc, seg_id, start, end = key
        for candidate in self._anonymized_map.positions:
            if (
                isinstance(candidate, tuple)
                and len(candidate) == 3
                and candidate[0] == seg_id
                and candidate[1] < end
                and start < candidate[2]
            ):
                return candidate
        return None

    def _original_clicked(self) -> None:
        if self._moving_cursor or self.original.textCursor().hasSelection():
            return
        key = self._original_map.key_at(self.original.textCursor().position())
        if not isinstance(key, tuple):
            return
        for r in self._doc_rows:
            if r.key == key:
                if self.select_cluster(r.cluster_id, r.key):
                    return
                self._current_key = r.key
                self._highlight_current()
                return

    def _original_menu(self, pos: QPoint) -> None:
        menu = self.original.createStandardContextMenu(pos)
        selected = clean_selection(self.original.textCursor().selectedText()).replace("\u200b", "")
        if selected:
            preview = " ".join(selected.split())
            if len(preview) > 40:
                preview = preview[:37] + "…"
            action = menu.addAction(typo(f"Ajouter « {preview} » comme entité…"))
            action.triggered.connect(self.add_entity_from_selection)
        menu.exec(self.original.mapToGlobal(pos))
        menu.deleteLater()

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

    # -- residues (post-apply) --------------------------------------------------------------------

    def show_residue(self, top_document_id: str, location: str, value: str) -> None:
        """Open the produced file of a document and highlight a residue."""
        self._pending_residue = (top_document_id, location, value)
        self._residue_value = value
        self.text_mode.setChecked(True)
        self.source_combo.blockSignals(True)
        self.source_combo.setCurrentIndex(self.source_combo.findData("output"))
        self.source_combo.blockSignals(False)
        if self.current_doc_id == top_document_id:
            self._render_current()
        else:
            self.select_document(top_document_id)

    def _follow_residue(self, doc: Document) -> bool:
        """Switch to the input document matching the residue location. True when the
        rendering was delegated."""
        assert self._pending_residue is not None
        top_id, location, _value = self._pending_residue
        known, output = self.controller.cached_output(top_id)
        if not known:
            self.controller.extract_output(top_id)
            self._show_message("Lecture du fichier produit…")
            return True
        self._pending_residue = None
        if output is None:
            return False
        path = _path_to(output, location)
        if path is None:
            return False
        target = self._docs.get(top_id)
        for i in path:
            if target is None or i >= len(target.children):
                return False
            target = target.children[i]
        if target is not None and target.id != doc.id:
            self.select_document(target.id)
            return True
        return False

    def _highlight_residue(self, value: str) -> None:
        document = self.anonymized.document()
        t = theme.tokens()
        fmt = QTextCharFormat()
        fmt.setBackground(QColor(t.danger))
        fmt.setForeground(QColor("#ffffff"))
        selections: list[QTextEdit.ExtraSelection] = []
        cursor = document.find(value)
        first: QTextCursor | None = None
        while not cursor.isNull() and len(selections) < 200:
            selection = QTextEdit.ExtraSelection()
            selection.cursor = cursor
            selection.format = fmt
            selections.append(selection)
            first = first or QTextCursor(cursor)
            cursor = document.find(value, cursor, QTextDocument.FindFlag(0))
        self.anonymized.setExtraSelections(selections)
        if first is not None:
            view = QTextCursor(document)
            view.setPosition(first.selectionStart())
            self.anonymized.setTextCursor(view)
            self.anonymized.ensureCursorVisible()
            shown = plural(len(selections), "occurrence surlignée", "occurrences surlignées")
            self.banner.show_message(
                f"Résidu repéré dans le fichier produit ({shown} en rouge). Corrigez les "
                "décisions puis réappliquez.",
                "warning",
            )
        else:
            self.banner.show_message(
                "Le résidu n'a pas pu être localisé précisément dans le texte produit.", "info"
            )

    # -- actions ----------------------------------------------------------------------------------

    def _can_act(self) -> bool:
        return self.controller.analysis is not None and not self.controller.busy

    def _advance(self, ids: list[str]) -> None:
        """After a decision on a single entity, go to the next undecided one."""
        if len(ids) == 1:
            QTimer.singleShot(0, self.next_undecided)

    def confirm_selected(self) -> None:
        if not self._can_act():
            return
        ids = [cid for cid in self.selected_cluster_ids() if not self._is_manual(cid)]
        if ids:
            self.controller.confirm(ids)
            self._advance(ids)

    def reject_selected(self) -> None:
        if not self._can_act():
            return
        ids = self.selected_cluster_ids()
        if not ids:
            return
        effective = self.controller.effective()
        manual_values: list[str] = []
        detected = []
        for cid in ids:
            if self._is_manual(cid):
                cluster = effective.clusters.get(cid) if effective is not None else None
                manual_values += list(cluster.variants) if cluster is not None else []
                manual_values += [e.value for e in self.model.mentions(cid)]
            else:
                detected.append(cid)
        if manual_values:
            self.controller.remove_manual(manual_values)
        if detected:
            self.controller.reject(detected)
            self._offer_whitelist(detected)
        self._advance(ids)

    def _variants(self, cluster_ids: list[str]) -> list[str]:
        analysis = self.controller.analysis
        values: list[str] = []
        seen: set[str] = set()
        for cid in cluster_ids:
            cluster = analysis.clusters.get(cid) if analysis is not None else None
            for v in [cluster.canonical, *sorted(cluster.variants)] if cluster else []:
                if normalize(v) not in seen:
                    seen.add(normalize(v))
                    values.append(v)
        return values

    def _offer_whitelist(self, cluster_ids: list[str]) -> None:
        values = self._variants(cluster_ids)
        if not values:
            return
        shown = values[0] if len(values) == 1 else f"{values[0]} (+{len(values) - 1})"

        def learn() -> None:
            undo = self.controller.add_to_whitelist(values)
            if undo is None:
                self.banner.show_message("Déjà parmi les valeurs à ne jamais masquer.", "info")
                return
            self.banner.show_message(
                f"« {shown} » ajouté aux valeurs à ne jamais masquer : il restera visible "
                "dans les prochains lots.",
                "success",
                [("Annuler", lambda: self._undo(undo))],
            )

        self.banner.show_message(
            f"« {shown} » reste visible dans ce lot.",
            "info",
            [("Ne jamais masquer", learn)],
        )

    def _undo(self, undo: object) -> None:
        from anonyma.gui.controller import ConfigUndo

        if isinstance(undo, ConfigUndo):
            self.controller.undo_learning(undo)
            self.banner.show_message(f"Ajout aux {undo.label} annulé.", "info")

    def reject_selected_mentions(self) -> None:
        if not self._can_act():
            return
        mentions = self.selected_mentions()
        if not mentions:
            self.banner.show_message(
                "Dépliez l'entité et choisissez l'occurrence à laisser visible.", "info"
            )
            return
        manual = [e for e in mentions if e.review is ReviewStatus.MANUAL]
        if manual:
            self.banner.show_message(
                "Une occurrence issue d'un ajout manuel ne peut pas être laissée visible "
                "seule : choisissez « Ne pas masquer » pour l'ajout entier.",
                "warning",
            )
        keys = [e.key for e in mentions if e.review is not ReviewStatus.MANUAL]
        if keys:
            self.controller.reject_mentions(keys)
            self.banner.show_message(
                "Occurrence laissée visible. Si la même valeur reste masquée ailleurs, la "
                "vérification la signalera comme résidu : pour la laisser visible partout, "
                "choisissez « Ne pas masquer » pour l'entité entière.",
                "info",
            )

    def retype_selected(self) -> None:
        if not self._can_act():
            return
        ids = self.selected_cluster_ids()
        if not ids:
            return
        view = self.model.view(ids[0])
        new_type = dialogs.choose_entity_type(
            self, self._types(), view.entity_type if view else "", "Changer le type de l'entité"
        )
        if not new_type:
            return
        detected = [cid for cid in ids if not self._is_manual(cid)]
        manual = [e.value for cid in ids if self._is_manual(cid) for e in self.model.mentions(cid)]
        if manual:
            self.controller.retype_manual(manual, new_type)
        if detected:
            self.controller.retype(detected, new_type)

    def reset_selected(self) -> None:
        if not self._can_act():
            return
        ids = [cid for cid in self.selected_cluster_ids() if not self._is_manual(cid)]
        keys = [e.key for e in self.selected_mentions()]
        if ids or keys:
            self.controller.reset(ids if not keys else [], keys)

    def confirm_all_of_type(self) -> None:
        if not self._can_act():
            return
        view = self.model.view(self.current_cluster_id())
        entity_type = str(self.type_filter.currentData() or "") or (
            view.entity_type if view else ""
        )
        if not entity_type:
            self.banner.show_message("Sélectionnez d'abord une entité du type voulu.", "info")
            return
        ids = [
            cid
            for cid in self.controller.cluster_ids()
            if not self.controller.is_decided(cid)
            and (v := self.model.view(cid)) is not None
            and v.entity_type == entity_type
        ]
        if not ids:
            self.banner.show_message("Aucune entité non décidée de ce type.", "info")
            return
        count = plural(len(ids), "entité", "entités")
        if dialogs.confirm(
            self,
            f"Masquer {count} de type « {entity_type_label(entity_type)} » encore sans "
            "décision, dans tout le lot ?",
            title="Masquer tout ce type",
            ok_label="Masquer",
        ):
            self.controller.confirm(ids)

    def confirm_all_remaining(self) -> None:
        if not self._can_act():
            return
        ids = [
            cid
            for cid in self.controller.cluster_ids()
            if not self.controller.is_decided(cid) and not self._is_manual(cid)
        ]
        if not ids:
            self.banner.show_message("Toutes les entités ont déjà une décision.", "info")
            return
        if dialogs.confirm(
            self,
            f"Masquer les {plural(len(ids), 'entité restante', 'entités restantes')} sans "
            "décision, dans tout le lot ?",
            title="Masquer tout le reste",
            ok_label="Masquer",
        ):
            self.controller.confirm(ids)

    def reject_selection_bulk(self) -> None:
        if not self._can_act():
            return
        ids = [cid for cid in self.selected_cluster_ids() if not self._is_manual(cid)]
        if not ids:
            return
        if dialogs.confirm(
            self,
            f"Ne pas masquer {plural(len(ids), 'entité')} ? Leurs valeurs resteront visibles "
            "dans les fichiers produits.",
            title="Ne pas masquer la sélection",
            ok_label="Ne pas masquer",
            warn=True,
        ):
            self.controller.reject(ids)
            self._offer_whitelist(ids)

    def add_entity_from_selection(self) -> None:
        if not self._can_act():
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

        def learn() -> None:
            undo = self.controller.add_always_mask(value, chosen)
            if undo is None:
                self.banner.show_message("Ce terme est déjà toujours masqué.", "info")
                return
            self.banner.show_message(
                f"« {value} » sera masqué dans tous les prochains lots.",
                "success",
                [("Annuler", lambda: self._undo(undo))],
            )

        self.banner.show_message(
            f"« {value} » ajouté : masqué dans tout le lot.",
            "success",
            [("Toujours masquer", learn)],
        )
        return True

    def _types(self) -> list[str]:
        config = self.controller.context.config
        return known_types(self.controller.analysis, config.custom_labels.values())

    # -- controller events ------------------------------------------------------------------------

    def _decisions_changed(self) -> None:
        self._fill_filters()
        self._refresh_model()
        # The preview still reflects the previous decisions: ``_effective_ready``
        # renders the anonymized side once the new one is computed.
        self._render_current(preview=False)
        if self.visual_mode.isChecked():
            self.visual.refresh_boxes()
        self._update_tree_states()
        self._update_counter()
        self._update_banner()

    def _effective_ready(self) -> None:
        self._refresh_model()
        self._render_current()
        self._update_tree_states()
        self._update_counter()

    def _result_ready(self) -> None:
        self._update_tree_states()
        self._update_banner()
        if self.controller.result is not None:
            self._residue_value = ""
            self.source_combo.setCurrentIndex(self.source_combo.findData("output"))
        self._render_current()

    def _output_extracted(self, top_id: str, _doc: object, error: str) -> None:
        if error:
            self.banner.show_message(error, "warning")
        if (
            self.current_doc_id
            and self.controller.index.top_of.get(self.current_doc_id) == top_id
            and self.source_combo.currentData() == "output"
        ):
            self._render_current()

    def _config_changed(self) -> None:
        if self.controller.analysis is not None:
            self.controller.refresh_preview()
            self._update_banner()

    def _update_counter(self) -> None:
        c = self.controller
        t = theme.tokens()
        pending = c.pending_review_count()
        if c.analysis is None:
            self.counter.setText("")
        else:
            decided, total = c.decided_counts()
            colour = t.success if decided == total else t.warning if pending else t.muted
            text = f"<b style='color:{colour}'>{decided} / {total} décidées</b>"
            if pending:
                text += f" · <span style='color:{t.warning}'>{pending} à revoir</span>"
            self.counter.setText(text)
        self.pending_changed.emit(pending)

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
        elif c.decisions_since_apply() and not self.banner.action_buttons:
            self.banner.show_message(
                "Des décisions ont changé depuis l'écriture : l'aperçu en tient compte, mais "
                "pas le fichier produit. Appliquez à nouveau pour mettre les fichiers à jour.",
                "info",
            )

    def _update_actions(self) -> None:
        c = self.controller
        ready = c.analysis is not None and not c.busy
        has_rows = bool(self.model.ids) and ready
        for b in (
            self.confirm_button,
            self.reject_button,
            self.reject_mention_button,
            self.retype_button,
            self.reset_button,
            self.bulk_button,
            self.next_undecided_button,
        ):
            b.setEnabled(has_rows)
        self.add_button.setEnabled(ready and self.original.textCursor().hasSelection())
        self.apply_button.setEnabled(ready)
        has_output = c.result is not None
        model = self.source_combo.model()
        item = getattr(model, "item", None)
        if callable(item):
            entry = item(1)
            if entry is not None:
                entry.setEnabled(has_output)
        if not has_output and self.source_combo.currentIndex() == 1:
            self.source_combo.setCurrentIndex(0)

    # -- compatibility helpers (tests, other pages) -----------------------------------------------

    def current_row_kind(self) -> str:
        return str(self.queue.currentIndex().data(KIND_ROLE) or "")

    def cluster_title(self, cluster_id: str) -> str:
        view = self.model.view(cluster_id)
        return view.title if view else ""

    def cluster_for_value(self, value: str) -> str:
        """Cluster id whose canonical form or a variant equals ``value`` (normalized)."""
        key = normalize(value)
        for cid in self.controller.cluster_ids():
            view = self.model.view(cid)
            cluster = self.model._cluster(cid)
            if view and (
                normalize(view.canonical) == key
                or (cluster is not None and any(normalize(v) == key for v in cluster.variants))
            ):
                return cid
        return ""

    def mention_index(self, cluster_id: str, document_suffix: str) -> MentionKey | None:
        for e in self.model.mentions(cluster_id):
            doc = self._docs.get(e.document_id)
            if doc is not None and doc.name.endswith(document_suffix):
                return e.key
        return None


def _path_to(root: Document, name: str) -> list[int] | None:
    """Child-index path to the document named ``name`` in ``root``'s tree."""
    if root.name == name:
        return []
    for i, child in enumerate(root.children):
        sub = _path_to(child, name)
        if sub is not None:
            return [i, *sub]
    return None


_ = (CLUSTER_ROLE, display_text)
