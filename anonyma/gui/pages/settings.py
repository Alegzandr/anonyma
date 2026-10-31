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
from anonyma.gui.controller import DEFAULT_STORE_NAME
from anonyma.gui.dialogs import normalize_type
from anonyma.gui.labels import MODE_LABELS, PROFILE_LABELS, REGIME_LABELS
from anonyma.gui.widgets import Banner, button, muted, page_header, primary_button
from anonyma.logs import get_logger
from anonyma.model_catalog import ModelKind
from anonyma.models import EntityType, Profile, Regime, ReplacementMode

log = get_logger("gui.settings")

LISTS_DIR = "listes"


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
        f.addRow("Processus de traitement", self.workers)
        self.ocr = QCheckBox("Reconnaître le texte des images et des documents numérisés (OCR)")
        f.addRow("", self.ocr)
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
            self.gliner_model,
            self.llm_model,
        ):
            w.currentIndexChanged.connect(self._mark_dirty)
        for spin in (self.min_score, self.review_threshold):
            spin.valueChanged.connect(self._mark_dirty)
        self.workers.valueChanged.connect(self._mark_dirty)
        for check in (self.paranoid, self.ocr, self.use_libreoffice):
            check.toggled.connect(self._mark_dirty)
        for edit in (self.store_path, self.libreoffice_path):
            edit.textChanged.connect(self._mark_dirty)
        for text in (self.whitelist, self.case_patterns):
            text.textChanged.connect(self._mark_dirty)
        for table in (self.always_mask, self.custom_labels):
            table.itemChanged.connect(self._mark_dirty)
        self.regime.currentIndexChanged.connect(self._update_store_visibility)
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
        finally:
            self._loading = False
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
        config.validate()
        return config

    def save(self) -> bool:
        try:
            config = self.collect()
        except ValueError as exc:
            self.banner.show_message(str(exc), "error")
            return False
        previous = copy.deepcopy(self.context.config.business_lists)
        try:
            self.context.update_config(config)
        except (OSError, ValueError) as exc:
            log.warning("settings not saved: %s", type(exc).__name__)
            message = str(exc) if isinstance(exc, ValueError) else "Enregistrement impossible."
            self.banner.show_message(message, "error")
            return False
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
