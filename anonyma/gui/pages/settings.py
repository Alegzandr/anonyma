"""« Réglages » page: every ``Config`` field, saved to ``data/config.json``."""

from __future__ import annotations

import copy
import re
import shutil
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from anonyma import libreoffice, paths
from anonyma.config import Config, load_business_list
from anonyma.frenchtext import plural, typo
from anonyma.gui import dialogs, theme
from anonyma.gui.context import AppContext
from anonyma.gui.controller import (
    DEFAULT_STORE_NAME,
    REVIEW_POLICIES,
    REVIEW_POLICY_GLOSSES,
    review_policy,
)
from anonyma.gui.dialogs import normalize_type
from anonyma.gui.labels import (
    MODE_GLOSSES,
    MODE_LABELS,
    PROFILE_GLOSSES,
    PROFILE_LABELS,
    REGIME_GLOSSES,
    REGIME_LABELS,
)
from anonyma.gui.widgets import (
    Banner,
    button,
    left_header,
    muted,
    pad_spin,
    page_header,
    primary_button,
    section_title,
)
from anonyma.logs import get_logger
from anonyma.model_catalog import ModelKind
from anonyma.models import EntityType, Profile, Regime, ReplacementMode
from anonyma.policy import (
    POLICY_ALL,
    POLICY_BLACKLIST,
    POLICY_EXCEPT_WHITELIST,
    default_whitelist,
    effective_whitelist,
)

log = get_logger("gui.settings")

LISTS_DIR = "listes"
ORGANIZATION_POLICY_LABELS: dict[str, str] = {
    POLICY_EXCEPT_WHITELIST: "Toutes, sauf exceptions",
    POLICY_ALL: "Toutes, sans exception",
    POLICY_BLACKLIST: "Seulement celles de la liste",
}
ORGANIZATION_POLICY_GLOSSES: dict[str, str] = {
    POLICY_EXCEPT_WHITELIST: "Sauf les organisations et domaines « à ne jamais masquer ».",
    POLICY_ALL: "Même les éditeurs, prestataires et sites publics.",
    POLICY_BLACKLIST: "Seulement les organisations et domaines « à toujours masquer ».",
}
TAG_NUMBERING_LABELS: dict[str, str] = {
    "sequentiel": "Séquentielle",
    "aleatoire": "Aléatoire",
}
TAG_NUMBERING_GLOSSES: dict[str, str] = {
    "sequentiel": "[PERSONNE_001], [PERSONNE_002]… dans l'ordre du lot.",
    "aleatoire": (
        "[PERSONNE_K7Q2] : ne révèle ni le nombre ni l'ordre. En pseudonymisation, une "
        "nouvelle valeur reçoit toujours un identifiant aléatoire."
    ),
}
#: Width of the label column of every settings form, and cap of the content.
LABEL_WIDTH = 200
CONTENT_MAX_WIDTH = 820


def business_lists_dir() -> Path:
    return paths.data_dir() / LISTS_DIR


def validate_patterns(lines: list[str]) -> list[tuple[int, str]]:
    """(1-based line, French message) for every invalid regular expression."""
    errors: list[tuple[int, str]] = []
    for i, pattern in enumerate(lines, start=1):
        try:
            re.compile(pattern)
        except re.error as exc:
            errors.append((i, f"ligne {i} : expression invalide ({exc.msg})"))
    return errors


def _lines(edit: QPlainTextEdit) -> list[str]:
    return [line.strip() for line in edit.toPlainText().splitlines() if line.strip()]


def _two_column_table(headers: tuple[str, str]) -> QTableWidget:
    table = QTableWidget(0, 2)
    table.setHorizontalHeaderLabels(list(headers))
    table.verticalHeader().hide()
    left_header(table)
    table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
    table.setColumnWidth(1, 200)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setMinimumHeight(140)
    return table


#: What the legacy formats become without LibreOffice.
NO_LIBREOFFICE_EFFECT = (
    "les .doc et .ppt ne sont pas pris en charge, les .xls sont restitués en .xlsx."
)


def libreoffice_status(enabled: bool, path: str) -> str:
    """Status line under the LibreOffice settings: the executable that will be used
    (configured path, bundled copy or installation found), or the consequence of
    having none."""
    if not enabled:
        return f"LibreOffice désactivé : {NO_LIBREOFFICE_EFFECT}"
    found = libreoffice.find_soffice(Config(use_libreoffice=True, libreoffice_path=path))
    if found is None:
        where = "à ce chemin ni ailleurs" if path else "trouvé"
        return f"Aucun LibreOffice {where} : {NO_LIBREOFFICE_EFFECT}"
    if path and libreoffice.configured_soffice(path) is None:
        return f"Aucun LibreOffice à ce chemin ; celui-ci sera utilisé : {found}"
    return f"LibreOffice utilisé : {found}"


class _Form(QGridLayout):
    """Two-column form: a fixed label column (same in every tab) and the fields. Each
    label sits on the first line of its field, never on the field's helper text."""

    def __init__(self) -> None:
        super().__init__()
        self.setContentsMargins(0, 0, 0, 0)
        self.setHorizontalSpacing(16)
        self.setVerticalSpacing(12)
        self.setColumnMinimumWidth(0, LABEL_WIDTH)
        # Fields with a capped width leave the extra room to an empty last column, not
        # to the labels.
        self.setColumnStretch(1, 1)
        self.setColumnStretch(2, 1)
        self._rows = 0

    def add_row(
        self, label: str, field: QWidget, *, buddy: QWidget | None = None, lead: int = 0
    ) -> None:
        text = QLabel(label)
        text.setFixedWidth(LABEL_WIDTH)
        text.setWordWrap(True)
        text.setBuddy(buddy or field)
        height = lead or field.sizeHint().height()
        text.setContentsMargins(0, max(0, (height - text.sizeHint().height()) // 2), 0, 0)
        top = Qt.AlignmentFlag.AlignTop
        self.addWidget(text, self._rows, 0, top | Qt.AlignmentFlag.AlignLeft)
        self.addWidget(field, self._rows, 1, top)
        self._rows += 1


class SettingsPage(QWidget):
    saved = Signal()

    def __init__(self, context: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.context = context
        self._loading = False
        self._dirty = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 12)
        outer.setSpacing(10)
        outer.addWidget(page_header("Réglages"))
        self.banner = Banner()
        outer.addWidget(self.banner)
        self.tabs = QTabWidget()
        self.tabs.setObjectName("SettingsTabs")
        outer.addWidget(self.tabs, 1)

        # Detection --------------------------------------------------------------------
        f, _box = self._tab("Détection")
        self.profile = QComboBox()
        for profile, label in PROFILE_LABELS.items():
            self.profile.addItem(label, profile.value if profile else "")
        self._gloss(
            f,
            "&Profil",
            self.profile,
            {(p.value if p else ""): g for p, g in PROFILE_GLOSSES.items()},
        )
        self.min_score = QDoubleSpinBox()
        self.review_threshold = QDoubleSpinBox()
        for spin in (self.min_score, self.review_threshold):
            spin.setRange(0.0, 1.0)
            spin.setSingleStep(0.05)
            spin.setDecimals(2)
            spin.setMaximumWidth(120)
        pad_spin(self.min_score, self.review_threshold)
        self._row(f, "Score &minimal retenu", self.min_score)
        self._row(
            f,
            "Seuil d'envoi en &revue",
            self.review_threshold,
            "Sous le score minimal : ignoré. Sous le seuil de revue : masqué et signalé "
            "« À revoir ».",
        )
        self.paranoid = QCheckBox("Mode paranoïaque")
        self._row(f, "", self.paranoid, "Masque aussi les noms propres et les séquences suspectes.")
        self.gliner_model = QComboBox()
        self.llm_model = QComboBox()
        for combo in (self.gliner_model, self.llm_model):
            combo.setMaximumWidth(360)
        self._row(f, "Modèle &GLiNER", self.gliner_model)
        self._row(f, "Modèle de &langue", self.llm_model)
        self.detect_dates = QCheckBox("Masquer toutes les dates")
        self.detect_dates.setObjectName("DetectDates")
        self._row(
            f,
            "Dates",
            self.detect_dates,
            "Les dates de naissance et les horodatages de signature sont toujours masqués.",
        )
        self.mask_opaque_ids = QCheckBox("Masquer les identifiants techniques")
        self.mask_opaque_ids.setObjectName("MaskOpaqueIds")
        self._row(
            f,
            "Identifiants",
            self.mask_opaque_ids,
            "Empreintes, UUID, jetons, numéros de transaction.",
        )

        # Replacement --------------------------------------------------------------------
        f, _box = self._tab("Remplacement")
        self.mode = QComboBox()
        for mode, label in MODE_LABELS.items():
            self.mode.addItem(label, mode.value)
        self._gloss(f, "&Mode", self.mode, {m.value: g for m, g in MODE_GLOSSES.items()})
        self.regime = QComboBox()
        for regime, label in REGIME_LABELS.items():
            self.regime.addItem(label, regime.value)
        self._gloss(f, "Ré&gime", self.regime, {r.value: g for r, g in REGIME_GLOSSES.items()})
        self.review_policy = QComboBox()
        self.review_policy.setObjectName("SettingsReviewPolicy")
        for key, label in REVIEW_POLICIES.items():
            self.review_policy.addItem(label, key)
        self._gloss(f, "&Relecture avant écriture", self.review_policy, REVIEW_POLICY_GLOSSES)
        self.tag_numbering = QComboBox()
        self.tag_numbering.setObjectName("TagNumbering")
        for key, label in TAG_NUMBERING_LABELS.items():
            self.tag_numbering.addItem(label, key)
        self._gloss(f, "&Numérotation des balises", self.tag_numbering, TAG_NUMBERING_GLOSSES)
        self.neutral_filenames = QCheckBox("Noms de fichiers neutres")
        self.neutral_filenames.setObjectName("NeutralFilenames")
        self._row(
            f,
            "Sortie",
            self.neutral_filenames,
            "document_0001.pdf, dossier_0001… au lieu des noms anonymisés.",
        )
        store_row = QHBoxLayout()
        self.store_path = QLineEdit()
        self.store_path.setPlaceholderText(str(paths.data_dir() / DEFAULT_STORE_NAME))
        store_button = button("Choisir…")
        store_button.clicked.connect(self._pick_store)
        store_row.addWidget(self.store_path, 1)
        store_row.addWidget(store_button)
        self.store_row_widget = QWidget()
        self.store_row_widget.setLayout(store_row)
        store_row.setContentsMargins(0, 0, 0, 0)
        self.store_hint = muted(
            "Chiffrée par une phrase secrète demandée à chaque traitement. Vide : dossier "
            "de l'application."
        )
        self._row(f, "&Table de correspondance", self.store_row_widget, self.store_hint)

        # Lists ----------------------------------------------------------------------------
        lv = self._list_tab("Listes")
        self.whitelist = QPlainTextEdit()
        self.whitelist.setObjectName("Whitelist")
        self.whitelist.setMinimumHeight(90)
        self._list_section(
            lv, "Valeurs à ne jamais masquer", "Une valeur par ligne.", self.whitelist
        )
        self.always_mask = _two_column_table(("Valeur", "Type"))
        self.always_mask.setObjectName("AlwaysMask")
        add_term = button("Ajouter une valeur")
        add_term.clicked.connect(lambda: self._add_term_row("", EntityType.BUSINESS_TERM.value))
        del_term = button("Retirer")
        del_term.clicked.connect(lambda: self._remove_rows(self.always_mask))
        self._list_section(
            lv,
            "Valeurs à toujours masquer",
            "Avec leur type (ex. PERSONNE, PROJET).",
            self.always_mask,
            (add_term, del_term),
        )
        self.business_lists = QListWidget()
        self.business_lists.setObjectName("BusinessLists")
        self.business_lists.setMinimumHeight(80)
        import_csv = button("Importer un CSV…")
        import_csv.clicked.connect(self._import_csv)
        remove_csv = button("Retirer")
        remove_csv.clicked.connect(self._remove_list)
        self._list_section(
            lv,
            "Listes métier",
            "Fichiers CSV « valeur[,type] » : chaque valeur est toujours masquée.",
            self.business_lists,
            (import_csv, remove_csv),
        )
        lv.addStretch(1)

        # Organizations ----------------------------------------------------------------------
        ov = self._list_tab("Organisations")
        of = _Form()
        self.organization_policy = QComboBox()
        self.organization_policy.setObjectName("OrganizationPolicy")
        for key, label in ORGANIZATION_POLICY_LABELS.items():
            self.organization_policy.addItem(label, key)
        self._gloss(
            of, "Organisations &masquées", self.organization_policy, ORGANIZATION_POLICY_GLOSSES
        )
        ov.addLayout(of)
        ov.addWidget(
            muted(
                "Une décision vaut partout : nom dans le texte, logo, adresses web et domaines. "
                "Les données personnelles d'une adresse conservée restent masquées."
            )
        )
        self.organization_whitelist = QPlainTextEdit()
        self.organization_whitelist.setObjectName("OrganizationWhitelist")
        self.organization_whitelist.setMinimumHeight(110)
        reset_whitelist = button("Rétablir la liste par défaut")
        reset_whitelist.clicked.connect(self._reset_organization_whitelist)
        self._list_section(
            ov,
            "Organisations et domaines à ne jamais masquer",
            "Éditeurs, prestataires, sites publics. Un par ligne.",
            self.organization_whitelist,
            (reset_whitelist,),
        )
        self.organization_blacklist = QPlainTextEdit()
        self.organization_blacklist.setObjectName("OrganizationBlacklist")
        self.organization_blacklist.setMinimumHeight(70)
        self._list_section(
            ov,
            "Organisations et domaines à toujours masquer",
            "Un par ligne.",
            self.organization_blacklist,
        )
        self.opaque_id_whitelist = QPlainTextEdit()
        self.opaque_id_whitelist.setObjectName("OpaqueIdWhitelist")
        self.opaque_id_whitelist.setMinimumHeight(60)
        self.opaque_id_whitelist.setPlaceholderText(r"ex. re:FACT-\d{6}")
        self._list_section(
            ov,
            "Identifiants techniques à ne jamais masquer",
            "Valeurs exactes, ou expressions régulières préfixées « re: ».",
            self.opaque_id_whitelist,
        )
        ov.addStretch(1)

        # Advanced detection ------------------------------------------------------------------
        av = self._list_tab("Avancé")
        self.custom_labels = _two_column_table(("Libellé", "Type"))
        self.custom_labels.setObjectName("CustomLabels")
        add_label = button("Ajouter un libellé")
        add_label.clicked.connect(lambda: self._add_row(self.custom_labels, "", "PROJET"))
        del_label = button("Retirer")
        del_label.clicked.connect(lambda: self._remove_rows(self.custom_labels))
        self._list_section(
            av,
            "Libellés GLiNER personnalisés",
            "Ce que le modèle GLiNER doit chercher, et le type d'entité à attribuer.",
            self.custom_labels,
            (add_label, del_label),
        )
        self.case_patterns = QPlainTextEdit()
        self.case_patterns.setObjectName("CasePatterns")
        self.case_patterns.setMinimumHeight(70)
        self.case_patterns.setPlaceholderText(r"ex. DOS-\d{4}-\d{5}")
        self._list_section(
            av,
            "Numéros de dossier internes",
            "Expressions régulières, une par ligne.",
            self.case_patterns,
        )
        self.pattern_status = QLabel("")
        self.pattern_status.setWordWrap(True)
        av.addWidget(self.pattern_status)
        self.case_patterns.textChanged.connect(self._check_patterns)
        av.addStretch(1)

        # System ---------------------------------------------------------------------------
        f, _box = self._tab("Système")
        self.workers = QSpinBox()
        self.workers.setRange(0, 64)
        self.workers.setSpecialValueText("Automatique")
        self.workers.setMaximumWidth(160)
        pad_spin(self.workers)
        self._row(f, "&Processus de traitement", self.workers)
        self.ocr = QCheckBox("Reconnaître le texte des images et des documents numérisés")
        self._row(f, "OCR", self.ocr)
        self.ocr_full_pages = QCheckBox("PDF : relire aussi chaque page rendue")
        self.ocr_full_pages.setObjectName("OcrFullPages")
        self._row(f, "", self.ocr_full_pages, "Tampons, signatures, champs aplatis.")
        self.decode_barcodes = QCheckBox("Lire et masquer les QR codes et codes-barres")
        self.decode_barcodes.setObjectName("DecodeBarcodes")
        self._row(f, "Codes-barres", self.decode_barcodes)
        self.remove_invisible_text = QCheckBox("Supprimer le texte invisible")
        self.remove_invisible_text.setObjectName("RemoveInvisibleText")
        self._row(f, "PDF", self.remove_invisible_text)
        self.use_libreoffice = QCheckBox("Utiliser LibreOffice pour les anciens formats")
        self._row(f, "LibreOffice", self.use_libreoffice, ".doc, .xls, .ppt…")
        lo_row = QHBoxLayout()
        lo_row.setContentsMargins(0, 0, 0, 0)
        self.libreoffice_path = QLineEdit()
        self.libreoffice_path.setPlaceholderText("Détection automatique")
        lo_button = button("Choisir…")
        lo_button.clicked.connect(self._pick_libreoffice)
        lo_row.addWidget(self.libreoffice_path, 1)
        lo_row.addWidget(lo_button)
        lo_widget = QWidget()
        lo_widget.setLayout(lo_row)
        self.libreoffice_status = muted("")
        self.libreoffice_status.setObjectName("LibreOfficeStatus")
        self.libreoffice_status.setWordWrap(True)
        self.libreoffice_status.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self._row(f, "&Chemin de LibreOffice", lo_widget, self.libreoffice_status)
        self.theme = QComboBox()
        for key, label in theme.THEMES.items():
            self.theme.addItem(label, key)
        self.theme.setMaximumWidth(200)
        self.theme.currentIndexChanged.connect(self._theme_changed)
        self._row(f, "T&hème", self.theme)

        buttons = QHBoxLayout()
        self.dirty_label = muted("")
        buttons.addWidget(self.dirty_label, 1)
        self.revert_button = button("Annuler les modifications")
        self.revert_button.clicked.connect(self.load)
        self.save_button = primary_button("Enregistrer", "check")
        self.save_button.clicked.connect(self.save)
        buttons.addWidget(self.revert_button)
        buttons.addWidget(self.save_button)
        outer.addLayout(buttons)

        for w in (
            self.profile,
            self.mode,
            self.regime,
            self.review_policy,
            self.gliner_model,
            self.llm_model,
            self.tag_numbering,
            self.organization_policy,
        ):
            w.currentIndexChanged.connect(self._mark_dirty)
        for spin in (self.min_score, self.review_threshold):
            spin.valueChanged.connect(self._mark_dirty)
        self.workers.valueChanged.connect(self._mark_dirty)
        for check in (
            self.paranoid,
            self.ocr,
            self.use_libreoffice,
            self.detect_dates,
            self.mask_opaque_ids,
            self.neutral_filenames,
            self.ocr_full_pages,
            self.decode_barcodes,
            self.remove_invisible_text,
        ):
            check.toggled.connect(self._mark_dirty)
        for edit in (self.store_path, self.libreoffice_path):
            edit.textChanged.connect(self._mark_dirty)
        for text in (
            self.whitelist,
            self.case_patterns,
            self.organization_whitelist,
            self.organization_blacklist,
            self.opaque_id_whitelist,
        ):
            text.textChanged.connect(self._mark_dirty)
        self.organization_policy.currentIndexChanged.connect(self._update_organization_lists)
        self.mask_opaque_ids.toggled.connect(self.opaque_id_whitelist.setEnabled)
        self.ocr.toggled.connect(self.ocr_full_pages.setEnabled)
        for table in (self.always_mask, self.custom_labels):
            table.itemChanged.connect(self._mark_dirty)
        self.regime.currentIndexChanged.connect(self._update_store_visibility)
        self.context.config_changed.connect(self._external_change)
        self.use_libreoffice.toggled.connect(lo_widget.setEnabled)
        self.use_libreoffice.toggled.connect(lambda _on: self._update_libreoffice_status())
        self.libreoffice_path.textChanged.connect(lambda _t: self._update_libreoffice_status())
        self.load()

    # -- layout helpers -------------------------------------------------------------

    def _page(self, title: str) -> QWidget:
        """A scrollable tab whose content keeps a readable width."""
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(4, 14, 12, 8)
        body = QWidget()
        body.setMaximumWidth(CONTENT_MAX_WIDTH)
        row.addWidget(body, 1)
        row.addStretch(0)
        scroll.setWidget(holder)
        self.tabs.addTab(scroll, title)
        return body

    def _tab(self, title: str) -> tuple[_Form, QWidget]:
        body = self._page(title)
        column = QVBoxLayout(body)
        column.setContentsMargins(0, 0, 0, 0)
        form = _Form()
        column.addLayout(form)
        column.addStretch(1)
        return form, body

    def _list_tab(self, title: str) -> QVBoxLayout:
        column = QVBoxLayout(self._page(title))
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(6)
        return column

    @staticmethod
    def _list_section(
        column: QVBoxLayout,
        title: str,
        hint: str,
        editor: QWidget,
        actions: tuple[QPushButton, ...] = (),
    ) -> None:
        if column.count():
            column.addSpacing(14)
        heading = section_title(title)
        heading.setBuddy(editor)
        column.addWidget(heading)
        column.addWidget(muted(hint))
        column.addWidget(editor)
        if actions:
            row = QHBoxLayout()
            for action in actions:
                row.addWidget(action)
            row.addStretch(1)
            column.addLayout(row)

    @staticmethod
    def _row(form: _Form, label: str, field: QWidget, hint: str | QLabel | None = None) -> None:
        """A form row; ``hint`` sits right under the field (muted helper text)."""
        if hint is None:
            form.add_row(label, field)
            return
        box = QWidget()
        column = QVBoxLayout(box)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(3)
        column.addWidget(field)
        column.addWidget(hint if isinstance(hint, QLabel) else muted(hint))
        form.add_row(label, box, buddy=field, lead=field.sizeHint().height())

    def _gloss(self, form: _Form, label: str, combo: QComboBox, glosses: dict[str, str]) -> None:
        """Add ``combo`` with a short label; the gloss of the current choice shows in a
        muted line below, and every choice's gloss in its tooltip."""
        for i in range(combo.count()):
            gloss = glosses.get(str(combo.itemData(i)), "")
            if gloss:
                combo.setItemData(i, typo(gloss), Qt.ItemDataRole.ToolTipRole)
        combo.setMaximumWidth(360)
        hint = muted("")
        self._row(form, label, combo, hint)

        def update(*_args: object) -> None:
            hint.setText(typo(glosses.get(str(combo.currentData()), "")))

        combo.currentIndexChanged.connect(update)
        update()

    # -- load / collect ---------------------------------------------------------------

    def _fill_models(self, combo: QComboBox, kind: ModelKind, current: str) -> None:
        combo.clear()
        combo.addItem("Automatique", "")
        try:
            specs = [s for s in self.context.model_manager.catalog() if s.kind is kind]
        except Exception as exc:  # unreadable catalog: only "Automatique" is offered
            log.warning("model catalog unreadable: %s", type(exc).__name__)
            specs = []
        for spec in specs:
            combo.addItem(spec.title or spec.id, spec.id)
        if current and combo.findData(current) < 0:
            combo.addItem(f"{current} (hors catalogue)", current)
        combo.setCurrentIndex(max(combo.findData(current), 0))

    @staticmethod
    def _select(combo: QComboBox, value: str) -> None:
        combo.setCurrentIndex(max(combo.findData(value), 0))

    def load(self) -> None:
        """Fill the widgets from the live configuration (discarding edits)."""
        config = self.context.config
        self._loading = True
        try:
            self._select(self.profile, config.profile.value if config.profile else "")
            self.min_score.setValue(config.min_score)
            self.review_threshold.setValue(config.review_threshold)
            self.paranoid.setChecked(config.paranoid)
            self._fill_models(self.gliner_model, ModelKind.GLINER, config.gliner_model)
            self._fill_models(self.llm_model, ModelKind.LLM, config.llm_model)
            self._select(self.mode, config.mode.value)
            self._select(self.regime, config.regime.value)
            self._select(self.review_policy, review_policy(config))
            self.store_path.setText(config.store_path)
            self.whitelist.setPlainText("\n".join(config.whitelist))
            self.always_mask.setRowCount(0)
            for term in config.always_mask:
                self._add_term_row(term.get("value", ""), term.get("type", ""))
            self.business_lists.clear()
            for name in config.business_lists:
                self._add_list_item(Path(name))
            self.custom_labels.setRowCount(0)
            for label, entity_type in config.custom_labels.items():
                self._add_row(self.custom_labels, label, entity_type)
            self.case_patterns.setPlainText("\n".join(config.case_number_patterns))
            self.workers.setValue(config.workers)
            self.ocr.setChecked(config.ocr_enabled)
            self.use_libreoffice.setChecked(config.use_libreoffice)
            self.libreoffice_path.setText(config.libreoffice_path)
            self._select(self.theme, str(config.ui.get("theme", theme.DEFAULT_THEME)))
            self.detect_dates.setChecked(config.detect_dates)
            self.mask_opaque_ids.setChecked(config.mask_opaque_ids)
            self.opaque_id_whitelist.setPlainText("\n".join(config.opaque_id_whitelist))
            self._select(self.tag_numbering, config.tag_numbering)
            self.neutral_filenames.setChecked(config.neutral_filenames)
            self._select(self.organization_policy, config.organization_policy)
            self.organization_whitelist.setPlainText("\n".join(effective_whitelist(config)))
            self.organization_blacklist.setPlainText("\n".join(config.organization_blacklist))
            self.ocr_full_pages.setChecked(config.ocr_full_pages)
            self.decode_barcodes.setChecked(config.decode_barcodes)
            self.remove_invisible_text.setChecked(config.remove_invisible_text)
        finally:
            self._loading = False
        self.opaque_id_whitelist.setEnabled(self.mask_opaque_ids.isChecked())
        self.ocr_full_pages.setEnabled(self.ocr.isChecked())
        self._update_organization_lists()
        self._update_store_visibility()
        self._check_patterns()
        self._update_libreoffice_status()
        self._set_dirty(False)

    def _update_libreoffice_status(self) -> None:
        """Say which LibreOffice the current settings use, or what happens without."""
        self.libreoffice_status.setText(
            typo(
                libreoffice_status(
                    self.use_libreoffice.isChecked(), self.libreoffice_path.text().strip()
                )
            )
        )

    def collect(self) -> Config:
        """Build a new ``Config`` from the widgets. Raises ValueError (French)."""
        config = self.context.config_copy()
        profile = self.profile.currentData()
        config.profile = Profile(profile) if profile else None
        config.min_score = round(self.min_score.value(), 4)
        config.review_threshold = round(self.review_threshold.value(), 4)
        if config.min_score > config.review_threshold:
            raise ValueError("Le score minimal ne peut pas dépasser le seuil de revue.")
        config.paranoid = self.paranoid.isChecked()
        config.gliner_model = str(self.gliner_model.currentData() or "")
        config.llm_model = str(self.llm_model.currentData() or "")
        config.mode = ReplacementMode(self.mode.currentData())
        config.regime = Regime(self.regime.currentData())
        config.review_policy = str(self.review_policy.currentData())
        config.store_path = self.store_path.text().strip()
        config.whitelist = _lines(self.whitelist)
        terms: list[dict[str, str]] = []
        for row in range(self.always_mask.rowCount()):
            value = self._cell(self.always_mask, row, 0)
            if not value:
                continue
            entity_type = normalize_type(self._cell(self.always_mask, row, 1)) or (
                EntityType.BUSINESS_TERM.value
            )
            terms.append({"value": value, "type": entity_type})
        config.always_mask = terms
        config.business_lists = [
            str(self.business_lists.item(i).data(Qt.ItemDataRole.UserRole))
            for i in range(self.business_lists.count())
        ]
        labels: dict[str, str] = {}
        for row in range(self.custom_labels.rowCount()):
            label = self._cell(self.custom_labels, row, 0)
            entity_type = normalize_type(self._cell(self.custom_labels, row, 1))
            if not label:
                continue
            if not entity_type:
                raise ValueError(
                    f"Libellé « {label} » : type invalide (majuscules, chiffres et « _ »)."
                )
            labels[label] = entity_type
        config.custom_labels = labels
        patterns = _lines(self.case_patterns)
        errors = validate_patterns(patterns)
        if errors:
            raise ValueError("Numéros de dossier : " + " ; ".join(m for _, m in errors) + ".")
        config.case_number_patterns = patterns
        config.workers = self.workers.value()
        config.ocr_enabled = self.ocr.isChecked()
        config.use_libreoffice = self.use_libreoffice.isChecked()
        config.libreoffice_path = self.libreoffice_path.text().strip()
        config.ui["theme"] = str(self.theme.currentData())
        config.detect_dates = self.detect_dates.isChecked()
        config.mask_opaque_ids = self.mask_opaque_ids.isChecked()
        opaque = _lines(self.opaque_id_whitelist)
        errors = validate_patterns([p[3:] for p in opaque if p.startswith("re:")])
        if errors:
            raise ValueError("Identifiants techniques : " + " ; ".join(m for _, m in errors) + ".")
        config.opaque_id_whitelist = opaque
        config.tag_numbering = str(self.tag_numbering.currentData())
        config.neutral_filenames = self.neutral_filenames.isChecked()
        config.organization_policy = str(self.organization_policy.currentData())
        whitelist = _lines(self.organization_whitelist)
        # The bundled list is stored as None so that application updates extend it.
        config.organization_whitelist = (
            None if whitelist == list(default_whitelist()) else whitelist
        )
        config.organization_blacklist = _lines(self.organization_blacklist)
        config.ocr_full_pages = self.ocr_full_pages.isChecked()
        config.decode_barcodes = self.decode_barcodes.isChecked()
        config.remove_invisible_text = self.remove_invisible_text.isChecked()
        config.validate()
        return config

    def _external_change(self) -> None:
        """Another page changed the configuration (review policy, learned lists)."""
        if not self._dirty and not self._saving:
            self.load()

    _saving = False

    def save(self) -> bool:
        try:
            config = self.collect()
        except ValueError as exc:
            self.banner.show_message(str(exc), "error")
            return False
        previous = copy.deepcopy(self.context.config.business_lists)
        self._saving = True
        try:
            self.context.update_config(config)
        except (OSError, ValueError) as exc:
            self._saving = False
            log.warning("settings not saved: %s", type(exc).__name__)
            message = str(exc) if isinstance(exc, ValueError) else "Enregistrement impossible."
            self.banner.show_message(message, "error")
            return False
        self._saving = False
        self._delete_unused_lists(previous, config.business_lists)
        self._set_dirty(False)
        self.banner.show_message("Réglages enregistrés.", "success")
        self.saved.emit()
        return True

    # -- helpers ---------------------------------------------------------------------------

    @staticmethod
    def _cell(table: QTableWidget, row: int, col: int) -> str:
        item = table.item(row, col)
        return item.text().strip() if item is not None else ""

    def _add_row(self, table: QTableWidget, first: str, second: str) -> None:
        row = table.rowCount()
        table.insertRow(row)
        table.setItem(row, 0, QTableWidgetItem(first))
        table.setItem(row, 1, QTableWidgetItem(second))
        if not self._loading:
            table.setCurrentCell(row, 0)
            item = table.item(row, 0)
            if item is not None:
                table.editItem(item)
            self._mark_dirty()

    def _add_term_row(self, value: str, entity_type: str) -> None:
        self._add_row(self.always_mask, value, entity_type)

    def _remove_rows(self, table: QTableWidget) -> None:
        rows = sorted({i.row() for i in table.selectionModel().selectedRows()}, reverse=True)
        if not rows and table.currentRow() >= 0:
            rows = [table.currentRow()]
        for row in rows:
            table.removeRow(row)
        if rows:
            self._mark_dirty()

    def _add_list_item(self, path: Path) -> None:
        try:
            count = len(load_business_list(path))
            text = f"{path.name} · {plural(count, 'terme')}"
        except (OSError, UnicodeDecodeError, ValueError):
            text = f"{path.name} — fichier illisible ou introuvable"
        item = QListWidgetItem(text)
        item.setData(Qt.ItemDataRole.UserRole, str(path))
        item.setToolTip(str(path))
        self.business_lists.addItem(item)

    def _import_csv(self) -> None:
        files = dialogs.open_files(self, "Importer une liste métier", ["*.csv", "*.txt"])
        for file in files:
            self.import_business_list(file)

    def import_business_list(self, source: Path) -> bool:
        """Copy a CSV into ``data/listes`` (portable) and list it. False on error."""
        try:
            terms = load_business_list(source)
        except (OSError, UnicodeDecodeError, ValueError):
            self.banner.show_message(
                "Ce fichier n'est pas une liste CSV lisible (encodage UTF-8 attendu).", "error"
            )
            return False
        if not terms:
            self.banner.show_message("Cette liste ne contient aucun terme.", "warning")
            return False
        folder = business_lists_dir()
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / source.name
        i = 2
        while target.exists():
            target = folder / f"{source.stem} ({i}){source.suffix}"
            i += 1
        shutil.copyfile(source, target)
        self._add_list_item(target)
        self._mark_dirty()
        self.banner.show_message(
            f"Liste importée : {plural(len(terms), 'terme')}. Enregistrez pour l'activer.",
            "success",
        )
        return True

    def _remove_list(self) -> None:
        for item in self.business_lists.selectedItems():
            self.business_lists.takeItem(self.business_lists.row(item))
            self._mark_dirty()

    @staticmethod
    def _delete_unused_lists(previous: list[str], current: list[str]) -> None:
        """Delete copies in ``data/listes`` that are no longer referenced."""
        folder = business_lists_dir().resolve()
        for name in set(previous) - set(current):
            path = Path(name)
            try:
                if path.resolve().parent == folder:
                    path.unlink(missing_ok=True)
            except OSError:
                continue

    def _pick_store(self) -> None:
        current = Path(self.store_path.text()) if self.store_path.text().strip() else None
        start = current.parent if current else paths.data_dir()
        chosen = dialogs.save_file(
            self, "Table de correspondance", current.name if current else DEFAULT_STORE_NAME, start
        )
        if chosen is not None:
            self.store_path.setText(str(chosen))

    def _pick_libreoffice(self) -> None:
        files = dialogs.open_files(self, "Exécutable de LibreOffice", ["soffice*", "libreoffice*"])
        if files:
            self.libreoffice_path.setText(str(files[0]))

    def _reset_organization_whitelist(self) -> None:
        self.organization_whitelist.setPlainText("\n".join(default_whitelist()))

    def _update_organization_lists(self, *_args: object) -> None:
        """Only the list the chosen policy uses is editable."""
        policy = self.organization_policy.currentData()
        self.organization_whitelist.setEnabled(policy == POLICY_EXCEPT_WHITELIST)
        self.organization_blacklist.setEnabled(policy != POLICY_ALL)

    def _update_store_visibility(self) -> None:
        pseudo = self.regime.currentData() == Regime.PSEUDONYMIZATION.value
        self.store_row_widget.setEnabled(pseudo)
        self.store_hint.setVisible(pseudo)

    def _check_patterns(self) -> None:
        errors = validate_patterns(_lines(self.case_patterns))
        if errors:
            t = theme.tokens()
            self.pattern_status.setText(
                f"<span style='color:{t.danger}'>" + " ; ".join(m for _, m in errors) + "</span>"
            )
        else:
            count = len(_lines(self.case_patterns))
            self.pattern_status.setText(
                f"{plural(count, 'expression valide', 'expressions valides')}." if count else ""
            )

    def _theme_changed(self) -> None:
        if self._loading:
            return
        mode = str(self.theme.currentData())
        theme.apply_theme(mode)
        self.context.set_ui_pref("theme", mode)

    def _mark_dirty(self, *_args: object) -> None:
        if not self._loading:
            self._set_dirty(True)

    def _set_dirty(self, dirty: bool) -> None:
        self._dirty = dirty
        self.save_button.setEnabled(dirty)
        self.revert_button.setEnabled(dirty)
        self.dirty_label.setText("Modifications non enregistrées." if dirty else "")

    @property
    def dirty(self) -> bool:
        return self._dirty
