"""« Réglages » page: the choices only the user can make (replacement, lists,
organizations, internal patterns, appearance), saved to ``data/config.json``. Everything
else (profile, models, OCR, workers, LibreOffice, thresholds) is decided by the
application; the « Application » tab only shows the models it installed for this
machine (``ModelsSection``)."""

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
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from anonyma import paths
from anonyma.config import Config, load_business_list
from anonyma.frenchtext import plural, typo
from anonyma.gui import dialogs, theme
from anonyma.gui.context import AppContext
from anonyma.gui.controller import DEFAULT_STORE_NAME
from anonyma.gui.dialogs import normalize_type
from anonyma.gui.labels import (
    MODE_GLOSSES,
    MODE_LABELS,
    REGIME_GLOSSES,
    REGIME_LABELS,
)
from anonyma.gui.models_section import ModelsSection
from anonyma.gui.widgets import (
    Banner,
    button,
    left_header,
    muted,
    page_header,
    primary_button,
    section_title,
)
from anonyma.logs import get_logger
from anonyma.models import EntityType, Regime, ReplacementMode
from anonyma.network import normalize_proxy
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
        forget = button("Repartir avec de nouveaux faux noms")
        forget.setObjectName("ForgetSubstitutes")
        forget.clicked.connect(self._forget_substitutes)
        self._row(
            f,
            "Substituts",
            forget,
            "Une même valeur garde le même faux nom d'un lot à l'autre. Ce bouton efface "
            "ce souvenir : les lots suivants n'auront plus de lien avec les précédents.",
        )

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

        # Application ------------------------------------------------------------------------
        f, app_body = self._tab("Application")
        self.theme = QComboBox()
        for key, label in theme.THEMES.items():
            self.theme.addItem(label, key)
        self.theme.setMaximumWidth(200)
        self.theme.currentIndexChanged.connect(self._theme_changed)
        self._row(f, "T&hème", self.theme)
        self.check_updates = QCheckBox("Mettre à jour Anonyma et ses modèles au démarrage")
        self.check_updates.setObjectName("CheckUpdates")
        self.check_updates.setToolTip(
            "Seules connexions automatiques d'Anonyma : la dernière version sur GitHub et "
            "les modèles d'IA adaptés à ce poste sur Hugging Face. Aucune donnée de "
            "document n'est envoyée."
        )
        self._row(f, "Mises à jour", self.check_updates)
        self.proxy = QLineEdit()
        self.proxy.setObjectName("Proxy")
        self.proxy.setPlaceholderText("Automatique (réglages du système)")
        self.proxy.setToolTip(
            "Utilisé seulement pour les mises à jour et le téléchargement des modèles. "
            "Laissez vide : Anonyma reprend le proxy de Windows ou de macOS, comme le "
            "navigateur. Sinon, adresse donnée par votre service informatique, "
            "ex. http://proxy.entreprise.fr:8080 (ou http://identifiant:mot-de-passe@…)."
        )
        self._row(
            f,
            "&Proxy",
            self.proxy,
            "À remplir seulement si les mises à jour ou les téléchargements échouent sur "
            "un poste d'entreprise.",
        )
        # What the application installed for this machine (decided, not a setting).
        self.models = ModelsSection(context)
        column = app_body.layout()
        assert isinstance(column, QVBoxLayout)
        column.insertSpacing(column.count() - 1, 22)
        column.insertWidget(column.count() - 1, self.models)

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

        for w in (self.mode, self.regime, self.organization_policy):
            w.currentIndexChanged.connect(self._mark_dirty)
        for check in (self.neutral_filenames, self.check_updates):
            check.toggled.connect(self._mark_dirty)
        self.store_path.textChanged.connect(self._mark_dirty)
        self.proxy.textChanged.connect(self._mark_dirty)
        for text in (
            self.whitelist,
            self.case_patterns,
            self.organization_whitelist,
            self.organization_blacklist,
            self.opaque_id_whitelist,
        ):
            text.textChanged.connect(self._mark_dirty)
        self.organization_policy.currentIndexChanged.connect(self._update_organization_lists)
        for table in (self.always_mask, self.custom_labels):
            table.itemChanged.connect(self._mark_dirty)
        self.regime.currentIndexChanged.connect(self._update_store_visibility)
        self.context.config_changed.connect(self._external_change)
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

    @staticmethod
    def _select(combo: QComboBox, value: str) -> None:
        combo.setCurrentIndex(max(combo.findData(value), 0))

    def load(self) -> None:
        """Fill the widgets from the live configuration (discarding edits)."""
        config = self.context.config
        self._loading = True
        try:
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
            self.check_updates.setChecked(config.check_updates)
            self.proxy.setText(config.proxy)
            self._select(self.theme, str(config.ui.get("theme", theme.DEFAULT_THEME)))
            self.opaque_id_whitelist.setPlainText("\n".join(config.opaque_id_whitelist))
            self.neutral_filenames.setChecked(config.neutral_filenames)
            self._select(self.organization_policy, config.organization_policy)
            self.organization_whitelist.setPlainText("\n".join(effective_whitelist(config)))
            self.organization_blacklist.setPlainText("\n".join(config.organization_blacklist))
        finally:
            self._loading = False
        self._update_organization_lists()
        self._update_store_visibility()
        self._check_patterns()
        self._set_dirty(False)

    def collect(self) -> Config:
        """Build a new ``Config`` from the widgets. Raises ValueError (French)."""
        config = self.context.config_copy()
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
        config.check_updates = self.check_updates.isChecked()
        config.proxy = normalize_proxy(self.proxy.text())
        config.ui["theme"] = str(self.theme.currentData())
        opaque = _lines(self.opaque_id_whitelist)
        errors = validate_patterns([p[3:] for p in opaque if p.startswith("re:")])
        if errors:
            raise ValueError("Identifiants techniques : " + " ; ".join(m for _, m in errors) + ".")
        config.opaque_id_whitelist = opaque
        config.neutral_filenames = self.neutral_filenames.isChecked()
        config.organization_policy = str(self.organization_policy.currentData())
        whitelist = _lines(self.organization_whitelist)
        # The bundled list is stored as None so that application updates extend it.
        config.organization_whitelist = (
            None if whitelist == list(default_whitelist()) else whitelist
        )
        config.organization_blacklist = _lines(self.organization_blacklist)
        config.validate()
        return config

    def _external_change(self) -> None:
        """Another page changed the configuration (learned lists)."""
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
        files = dialogs.open_files(
            self, "Importer une liste métier", ["*.csv", "*.txt"], self.context.last_dir("listes")
        )
        self.context.remember_selection("listes", files)
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

    def _reset_organization_whitelist(self) -> None:
        self.organization_whitelist.setPlainText("\n".join(default_whitelist()))

    def _update_organization_lists(self, *_args: object) -> None:
        """Only the list the chosen policy uses is editable."""
        policy = self.organization_policy.currentData()
        self.organization_whitelist.setEnabled(policy == POLICY_EXCEPT_WHITELIST)
        self.organization_blacklist.setEnabled(policy != POLICY_ALL)

    def _forget_substitutes(self) -> None:
        from anonyma.store import LinkMemory

        if not dialogs.confirm(
            self,
            "Les prochains lots recevront de nouveaux faux noms, sans lien avec les lots "
            "déjà traités. Continuer ?",
            ok_label="Repartir de zéro",
            warn=True,
        ):
            return
        if LinkMemory.forget(paths.links_path()):
            self.banner.show_message(
                "Substituts oubliés : nouveaux faux noms au prochain lot.", "success"
            )
        else:
            self.banner.show_message("Aucun substitut à oublier.", "info")

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
