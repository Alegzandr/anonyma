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
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from anonyma import paths
from anonyma.config import Config, load_business_list
from anonyma.gui import dialogs, theme
from anonyma.gui.context import AppContext
from anonyma.gui.controller import DEFAULT_STORE_NAME, REVIEW_POLICIES, review_policy
from anonyma.gui.dialogs import normalize_type
from anonyma.gui.labels import MODE_LABELS, PROFILE_LABELS, REGIME_LABELS
from anonyma.gui.widgets import (
    Banner,
    button,
    left_header,
    muted,
    pad_spin,
    page_header,
    primary_button,
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
    POLICY_EXCEPT_WHITELIST: (
        "Toutes, sauf la liste blanche (éditeurs, prestataires, sites publics)"
    ),
    POLICY_ALL: "Toutes, sans exception",
    POLICY_BLACKLIST: "Uniquement celles de la liste noire",
}
TAG_NUMBERING_LABELS: dict[str, str] = {
    "sequentiel": "Séquentielle par lot — [PERSONNE_001]",
    "aleatoire": "Aléatoire — [PERSONNE_K7Q2] (ne révèle ni le nombre ni l'ordre)",
}


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
        outer.addWidget(
            page_header(
                "Réglages",
                "Enregistrés dans le dossier de l'application (data/config.json).",
            )
        )
        self.banner = Banner()
        outer.addWidget(self.banner)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        scroll.setWidget(body)
        form_layout = QVBoxLayout(body)
        form_layout.setContentsMargins(0, 0, 12, 0)
        form_layout.setSpacing(12)
        outer.addWidget(scroll, 1)

        # Detection --------------------------------------------------------------------
        detection = QGroupBox("Détection")
        f = QFormLayout(detection)
        self.profile = QComboBox()
        for profile, label in PROFILE_LABELS.items():
            self.profile.addItem(label, profile.value if profile else "")
        f.addRow("Profil", self.profile)
        self.min_score = QDoubleSpinBox()
        self.review_threshold = QDoubleSpinBox()
        for spin in (self.min_score, self.review_threshold):
            spin.setRange(0.0, 1.0)
            spin.setSingleStep(0.05)
            spin.setDecimals(2)
        pad_spin(self.min_score, self.review_threshold)
        f.addRow("Score minimal retenu", self.min_score)
        f.addRow("Seuil d'envoi en revue", self.review_threshold)
        thresholds_hint = muted(
            "Sous le score minimal : ignoré. Sous le seuil de revue : masqué et signalé "
            "« À revoir »."
        )
        thresholds_hint.setWordWrap(False)
        f.addRow("", thresholds_hint)
        self.paranoid = QCheckBox(
            "Mode paranoïaque (masquer aussi noms propres et séquences suspectes)"
        )
        f.addRow("", self.paranoid)
        self.gliner_model = QComboBox()
        self.llm_model = QComboBox()
        f.addRow("Modèle GLiNER", self.gliner_model)
        f.addRow("Modèle de langue", self.llm_model)
        self.detect_dates = QCheckBox("Masquer toutes les dates")
        self.detect_dates.setObjectName("DetectDates")
        f.addRow("", self.detect_dates)
        f.addRow(
            "",
            muted("Les dates de naissance et les horodatages de signature sont toujours masqués."),
        )
        self.mask_opaque_ids = QCheckBox(
            "Masquer les identifiants techniques (empreintes, UUID, jetons, n° de transaction)"
        )
        self.mask_opaque_ids.setObjectName("MaskOpaqueIds")
        f.addRow("", self.mask_opaque_ids)
        form_layout.addWidget(detection)

        # Replacement --------------------------------------------------------------------
        replacement = QGroupBox("Remplacement")
        f = QFormLayout(replacement)
        self.mode = QComboBox()
        for mode, label in MODE_LABELS.items():
            self.mode.addItem(label, mode.value)
        f.addRow("Mode", self.mode)
        self.regime = QComboBox()
        for regime, label in REGIME_LABELS.items():
            self.regime.addItem(label, regime.value)
        f.addRow("Régime", self.regime)
        self.review_policy = QComboBox()
        self.review_policy.setObjectName("SettingsReviewPolicy")
        for key, label in REVIEW_POLICIES.items():
            self.review_policy.addItem(label, key)
        f.addRow("Relecture avant écriture", self.review_policy)
        self.tag_numbering = QComboBox()
        self.tag_numbering.setObjectName("TagNumbering")
        for key, label in TAG_NUMBERING_LABELS.items():
            self.tag_numbering.addItem(label, key)
        f.addRow("Numérotation des balises", self.tag_numbering)
        tags_hint = muted(
            "En pseudonymisation, une nouvelle valeur reçoit toujours un identifiant aléatoire."
        )
        tags_hint.setWordWrap(False)
        f.addRow("", tags_hint)
        self.neutral_filenames = QCheckBox(
            "Noms de fichiers neutres en sortie (document_0001.pdf, dossier_0001…)"
        )
        self.neutral_filenames.setObjectName("NeutralFilenames")
        f.addRow("", self.neutral_filenames)
        store_row = QHBoxLayout()
        self.store_path = QLineEdit()
        self.store_path.setPlaceholderText(str(paths.data_dir() / DEFAULT_STORE_NAME))
        store_button = button("Choisir…", "folder")
        store_button.clicked.connect(self._pick_store)
        store_row.addWidget(self.store_path, 1)
        store_row.addWidget(store_button)
        self.store_row_widget = QWidget()
        self.store_row_widget.setLayout(store_row)
        store_row.setContentsMargins(0, 0, 0, 0)
        f.addRow("Table de correspondance", self.store_row_widget)
        self.store_hint = muted(
            "Chiffrée par une phrase secrète demandée à chaque traitement. Vide : dossier "
            "de l'application."
        )
        self.store_hint.setWordWrap(False)
        f.addRow("", self.store_hint)
        form_layout.addWidget(replacement)

        # Lists ----------------------------------------------------------------------------
        lists = QGroupBox("Listes")
        lv = QVBoxLayout(lists)
        lv.addWidget(QLabel("<b>Liste blanche</b> — valeurs jamais masquées, une par ligne"))
        self.whitelist = QPlainTextEdit()
        self.whitelist.setObjectName("Whitelist")
        self.whitelist.setMinimumHeight(90)
        lv.addWidget(self.whitelist)
        lv.addWidget(QLabel("<b>Termes toujours masqués</b>"))
        self.always_mask = _two_column_table(("Valeur", "Type"))
        self.always_mask.setObjectName("AlwaysMask")
        lv.addWidget(self.always_mask)
        row = QHBoxLayout()
        add_term = button("Ajouter un terme", "add")
        add_term.clicked.connect(lambda: self._add_term_row("", EntityType.BUSINESS_TERM.value))
        del_term = button("Retirer", "trash")
        del_term.clicked.connect(lambda: self._remove_rows(self.always_mask))
        row.addWidget(add_term)
        row.addWidget(del_term)
        row.addStretch(1)
        lv.addLayout(row)
        lv.addWidget(
            QLabel("<b>Listes métier</b> — fichiers CSV « valeur[,type] » toujours masqués")
        )
        self.business_lists = QListWidget()
        self.business_lists.setObjectName("BusinessLists")
        self.business_lists.setMinimumHeight(80)
        lv.addWidget(self.business_lists)
        row = QHBoxLayout()
        import_csv = button("Importer un CSV…", "import")
        import_csv.clicked.connect(self._import_csv)
        remove_csv = button("Retirer", "trash")
        remove_csv.clicked.connect(self._remove_list)
        row.addWidget(import_csv)
        row.addWidget(remove_csv)
        row.addStretch(1)
        lv.addLayout(row)
        form_layout.addWidget(lists)

        # Organizations ----------------------------------------------------------------------
        organizations = QGroupBox("Organisations, sites et domaines")
        ov = QVBoxLayout(organizations)
        of = QFormLayout()
        self.organization_policy = QComboBox()
        self.organization_policy.setObjectName("OrganizationPolicy")
        for key, label in ORGANIZATION_POLICY_LABELS.items():
            self.organization_policy.addItem(label, key)
        of.addRow("Organisations masquées", self.organization_policy)
        ov.addLayout(of)
        ov.addWidget(
            muted(
                "Une décision vaut partout : nom dans le texte, logo, adresses web et domaines. "
                "Les données personnelles d'une adresse conservée restent masquées."
            )
        )
        ov.addWidget(
            QLabel("<b>Liste blanche</b> — organisations ou domaines jamais masqués, un par ligne")
        )
        self.organization_whitelist = QPlainTextEdit()
        self.organization_whitelist.setObjectName("OrganizationWhitelist")
        self.organization_whitelist.setMinimumHeight(110)
        ov.addWidget(self.organization_whitelist)
        row = QHBoxLayout()
        reset_whitelist = button("Rétablir la liste par défaut", "undo")
        reset_whitelist.clicked.connect(self._reset_organization_whitelist)
        row.addWidget(reset_whitelist)
        row.addStretch(1)
        ov.addLayout(row)
        ov.addWidget(QLabel("<b>Liste noire</b> — organisations ou domaines toujours masqués"))
        self.organization_blacklist = QPlainTextEdit()
        self.organization_blacklist.setObjectName("OrganizationBlacklist")
        self.organization_blacklist.setMinimumHeight(70)
        ov.addWidget(self.organization_blacklist)
        ov.addWidget(
            QLabel(
                "<b>Identifiants techniques jamais masqués</b> — valeurs exactes, ou "
                "expressions régulières préfixées « re: »"
            )
        )
        self.opaque_id_whitelist = QPlainTextEdit()
        self.opaque_id_whitelist.setObjectName("OpaqueIdWhitelist")
        self.opaque_id_whitelist.setMinimumHeight(60)
        self.opaque_id_whitelist.setPlaceholderText(r"ex. re:FACT-\d{6}")
        ov.addWidget(self.opaque_id_whitelist)
        form_layout.addWidget(organizations)

        # Advanced detection ------------------------------------------------------------------
        advanced = QGroupBox("Détection avancée")
        av = QVBoxLayout(advanced)
        av.addWidget(QLabel("<b>Libellés GLiNER personnalisés</b> — libellé → type d'entité"))
        self.custom_labels = _two_column_table(("Libellé", "Type"))
        self.custom_labels.setObjectName("CustomLabels")
        av.addWidget(self.custom_labels)
        row = QHBoxLayout()
        add_label = button("Ajouter un libellé", "add")
        add_label.clicked.connect(lambda: self._add_row(self.custom_labels, "", "PROJET"))
        del_label = button("Retirer", "trash")
        del_label.clicked.connect(lambda: self._remove_rows(self.custom_labels))
        row.addWidget(add_label)
        row.addWidget(del_label)
        row.addStretch(1)
        av.addLayout(row)
        av.addWidget(
            QLabel("<b>Numéros de dossier internes</b> — expressions régulières, une par ligne")
        )
        self.case_patterns = QPlainTextEdit()
        self.case_patterns.setObjectName("CasePatterns")
        self.case_patterns.setMinimumHeight(70)
        self.case_patterns.setPlaceholderText(r"ex. DOS-\d{4}-\d{5}")
        av.addWidget(self.case_patterns)
        self.pattern_status = QLabel("")
        self.pattern_status.setWordWrap(True)
        av.addWidget(self.pattern_status)
        self.case_patterns.textChanged.connect(self._check_patterns)
        form_layout.addWidget(advanced)

        # System ---------------------------------------------------------------------------
        system = QGroupBox("Système")
        f = QFormLayout(system)
        self.workers = QSpinBox()
        self.workers.setRange(0, 64)
        self.workers.setSpecialValueText("Automatique")
        pad_spin(self.workers)
        f.addRow("Processus de traitement", self.workers)
        self.ocr = QCheckBox("Reconnaître le texte des images et des documents numérisés (OCR)")
        f.addRow("", self.ocr)
        self.ocr_full_pages = QCheckBox(
            "PDF : reconnaître aussi le texte de chaque page rendue (tampons, signatures, "
            "champs aplatis)"
        )
        self.ocr_full_pages.setObjectName("OcrFullPages")
        f.addRow("", self.ocr_full_pages)
        self.decode_barcodes = QCheckBox("Lire et masquer les QR codes et codes-barres")
        self.decode_barcodes.setObjectName("DecodeBarcodes")
        f.addRow("", self.decode_barcodes)
        self.remove_invisible_text = QCheckBox("PDF : supprimer le texte invisible")
        self.remove_invisible_text.setObjectName("RemoveInvisibleText")
        f.addRow("", self.remove_invisible_text)
        self.use_libreoffice = QCheckBox(
            "Utiliser LibreOffice pour les anciens formats (.doc, .xls…)"
        )
        f.addRow("", self.use_libreoffice)
        lo_row = QHBoxLayout()
        lo_row.setContentsMargins(0, 0, 0, 0)
        self.libreoffice_path = QLineEdit()
        self.libreoffice_path.setPlaceholderText("Détection automatique")
        lo_button = button("Choisir…", "file")
        lo_button.clicked.connect(self._pick_libreoffice)
        lo_row.addWidget(self.libreoffice_path, 1)
        lo_row.addWidget(lo_button)
        lo_widget = QWidget()
        lo_widget.setLayout(lo_row)
        f.addRow("Chemin de LibreOffice", lo_widget)
        self.theme = QComboBox()
        for key, label in theme.THEMES.items():
            self.theme.addItem(label, key)
        self.theme.currentIndexChanged.connect(self._theme_changed)
        f.addRow("Thème", self.theme)
        form_layout.addWidget(system)
        form_layout.addStretch(1)

        buttons = QHBoxLayout()
        self.dirty_label = muted("")
        buttons.addWidget(self.dirty_label, 1)
        self.revert_button = button("Rétablir", "undo")
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
        self.load()

    # -- load / collect ---------------------------------------------------------------

    def _fill_models(self, combo: QComboBox, kind: ModelKind, current: str) -> None:
        combo.clear()
        combo.addItem("Automatique", "")
        try:
            specs = [s for s in self.context.model_manager.catalog() if s.kind is kind]
        except Exception:
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
        self._set_dirty(False)

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
            text = f"{path.name} — {count} terme(s)"
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
            f"Liste importée : {len(terms)} terme(s). Enregistrez pour l'activer.", "success"
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
            self.pattern_status.setText(f"{count} expression(s) valide(s)." if count else "")

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
