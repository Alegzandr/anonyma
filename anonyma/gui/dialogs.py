"""Modal dialogs. Every modal interaction goes through the module-level functions
here, so that tests can replace them.

File choosers are built on ``QFileSystemModel`` instead of ``QFileDialog``:
``QFileDialog`` (native or not) persists its history and last visited folder
through ``QSettings`` in the native format, i.e. in the Windows registry, which
would leak sensitive folder names outside the portable folder.
"""

from __future__ import annotations

import re
from pathlib import Path

from PySide6.QtCore import QDir, QModelIndex, QStandardPaths, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileSystemModel,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from anonyma.gui import icons
from anonyma.gui.labels import entity_type_label

MIN_PASSPHRASE_LENGTH = 8
_TYPE_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,39}$")


# ---------------------------------------------------------------------------
# Message boxes
# ---------------------------------------------------------------------------


def error(parent: QWidget | None, text: str, title: str = "Erreur") -> None:
    QMessageBox.critical(parent, title, text)


def warning(parent: QWidget | None, text: str, title: str = "Attention") -> None:
    QMessageBox.warning(parent, title, text)


def info(parent: QWidget | None, text: str, title: str = "Information") -> None:
    QMessageBox.information(parent, title, text)


def confirm(
    parent: QWidget | None,
    text: str,
    title: str = "Confirmation",
    ok_label: str = "Continuer",
    cancel_label: str = "Annuler",
) -> bool:
    box = QMessageBox(QMessageBox.Icon.Question, title, text, parent=parent)
    ok = box.addButton(ok_label, QMessageBox.ButtonRole.AcceptRole)
    box.addButton(cancel_label, QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(ok)
    box.exec()
    return box.clickedButton() is ok


def ask_text(parent: QWidget | None, title: str, label: str, value: str = "") -> str | None:
    text, ok = QInputDialog.getText(parent, title, label, QLineEdit.EchoMode.Normal, value)
    return text if ok else None


# ---------------------------------------------------------------------------
# Passphrase
# ---------------------------------------------------------------------------


class PassphraseDialog(QDialog):
    def __init__(self, parent: QWidget | None, create: bool, store_path: Path) -> None:
        super().__init__(parent)
        self.creating = create
        self.setWindowTitle("Table de correspondance chiffrée")
        self.setModal(True)
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        if create:
            intro = (
                "Une nouvelle table de correspondance chiffrée va être créée. Choisissez une "
                "phrase secrète d'au moins 8 caractères : sans elle, la table est inutilisable "
                "et les pseudonymes ne pourront plus être rapprochés de leurs originaux."
            )
        else:
            intro = "Saisissez la phrase secrète de la table de correspondance chiffrée."
        text = QLabel(intro)
        text.setWordWrap(True)
        layout.addWidget(text)
        location = QLabel(f"Fichier : {store_path}")
        location.setProperty("muted", True)
        location.setWordWrap(True)
        location.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(location)
        form = QFormLayout()
        self.passphrase = QLineEdit()
        self.passphrase.setEchoMode(QLineEdit.EchoMode.Password)
        self.passphrase.setObjectName("passphrase")
        form.addRow("Phrase secrète", self.passphrase)
        self.confirmation = QLineEdit()
        self.confirmation.setEchoMode(QLineEdit.EchoMode.Password)
        self.confirmation.setObjectName("confirmation")
        if create:
            form.addRow("Confirmation", self.confirmation)
        layout.addLayout(form)
        self.error_label = QLabel("")
        self.error_label.setStyleSheet("color: palette(bright-text);")
        self.error_label.setWordWrap(True)
        layout.addWidget(self.error_label)
        buttons = QDialogButtonBox()
        buttons.addButton("Valider", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton("Annuler", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self._check)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.resize(460, self.sizeHint().height())

    def validation_error(self) -> str:
        value = self.passphrase.text()
        if not value:
            return "La phrase secrète est obligatoire."
        if self.creating:
            if len(value) < MIN_PASSPHRASE_LENGTH:
                return (
                    f"La phrase secrète doit comporter au moins {MIN_PASSPHRASE_LENGTH} caractères."
                )
            if value != self.confirmation.text():
                return "Les deux saisies ne correspondent pas."
        return ""

    def _check(self) -> None:
        message = self.validation_error()
        if message:
            self.error_label.setText(message)
            return
        self.accept()


def ask_passphrase(parent: QWidget | None, create: bool, store_path: Path) -> str | None:
    dialog = PassphraseDialog(parent, create, store_path)
    try:
        if dialog.exec() == QDialog.DialogCode.Accepted:
            return dialog.passphrase.text()
        return None
    finally:
        dialog.passphrase.clear()
        dialog.confirmation.clear()
        dialog.deleteLater()


# ---------------------------------------------------------------------------
# Entity type chooser
# ---------------------------------------------------------------------------


def normalize_type(text: str) -> str:
    """``"nom de projet"`` -> ``"NOM_DE_PROJET"``; "" when invalid."""
    code = text.split("—", maxsplit=1)[0].strip().upper().replace(" ", "_").replace("-", "_")
    return code if _TYPE_RE.match(code) else ""


class TypeDialog(QDialog):
    def __init__(
        self, parent: QWidget | None, types: list[str], current: str = "", title: str = ""
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title or "Type d'entité")
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Choisissez un type, ou saisissez un nouveau type (ex. PROJET) :"))
        self.combo = QComboBox()
        self.combo.setEditable(True)
        self.combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        for t in types:
            self.combo.addItem(f"{t} — {entity_type_label(t)}", t)
        if current:
            index = self.combo.findData(current)
            if index >= 0:
                self.combo.setCurrentIndex(index)
        layout.addWidget(self.combo)
        self.error_label = QLabel("")
        layout.addWidget(self.error_label)
        buttons = QDialogButtonBox()
        buttons.addButton("Valider", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton("Annuler", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self._check)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.resize(380, self.sizeHint().height())

    def value(self) -> str:
        index = self.combo.currentIndex()
        text = self.combo.currentText()
        if index >= 0 and self.combo.itemText(index) == text:
            return str(self.combo.itemData(index))
        return normalize_type(text)

    def _check(self) -> None:
        if not self.value():
            self.error_label.setText(
                "Type invalide : lettres majuscules, chiffres et « _ » uniquement."
            )
            return
        self.accept()


def choose_entity_type(
    parent: QWidget | None, types: list[str], current: str = "", title: str = ""
) -> str | None:
    dialog = TypeDialog(parent, types, current, title)
    try:
        return dialog.value() if dialog.exec() == QDialog.DialogCode.Accepted else None
    finally:
        dialog.deleteLater()


# ---------------------------------------------------------------------------
# File chooser (no QFileDialog: see module docstring)
# ---------------------------------------------------------------------------


class FileBrowser(QDialog):
    """Minimal file/folder chooser. ``mode``: "files", "folder" or "save"."""

    def __init__(
        self,
        parent: QWidget | None,
        title: str,
        mode: str,
        name_filters: list[str] | None = None,
        start: Path | None = None,
        default_name: str = "",
    ) -> None:
        super().__init__(parent)
        self.mode = mode
        self.setWindowTitle(title)
        self.resize(820, 520)
        self.model = QFileSystemModel(self)
        dir_filter = QDir.Filter.AllDirs | QDir.Filter.NoDotAndDotDot | QDir.Filter.Drives
        if mode != "folder":
            dir_filter |= QDir.Filter.Files
        self.model.setFilter(dir_filter)
        if name_filters:
            self.model.setNameFilters(name_filters)
            self.model.setNameFilterDisables(False)
        self.model.setRootPath("")

        layout = QVBoxLayout(self)
        bar = QHBoxLayout()
        self.up_button = QPushButton(icons.icon("previous"), "Dossier parent")
        self.up_button.clicked.connect(self._go_up)
        self.path_edit = QLineEdit()
        self.path_edit.returnPressed.connect(lambda: self.navigate(Path(self.path_edit.text())))
        bar.addWidget(self.up_button)
        bar.addWidget(self.path_edit, 1)
        if mode in ("folder", "save"):
            new_folder = QPushButton(icons.icon("add"), "Nouveau dossier")
            new_folder.clicked.connect(self._new_folder)
            bar.addWidget(new_folder)
        layout.addLayout(bar)

        splitter = QSplitter()
        self.places = QListWidget()
        self.places.setMaximumWidth(220)
        for label, place in self._places():
            item = QListWidgetItem(icons.icon("folder"), label)
            item.setData(Qt.ItemDataRole.UserRole, str(place))
            self.places.addItem(item)
        self.places.itemClicked.connect(
            lambda item: self.navigate(Path(item.data(Qt.ItemDataRole.UserRole)))
        )
        splitter.addWidget(self.places)
        self.view = QTreeView()
        self.view.setModel(self.model)
        self.view.setRootIsDecorated(False)
        self.view.setItemsExpandable(False)
        self.view.setSortingEnabled(True)
        self.view.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self.view.setColumnWidth(0, 320)
        self.view.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
            if mode == "files"
            else QAbstractItemView.SelectionMode.SingleSelection
        )
        self.view.doubleClicked.connect(self._activated)
        splitter.addWidget(self.view)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, 1)

        self.name_edit = QLineEdit(default_name)
        if mode == "save":
            row = QHBoxLayout()
            row.addWidget(QLabel("Nom du fichier"))
            row.addWidget(self.name_edit, 1)
            layout.addLayout(row)
        if name_filters:
            hint = QLabel("Fichiers affichés : " + ", ".join(name_filters))
            hint.setProperty("muted", True)
            layout.addWidget(hint)

        buttons = QDialogButtonBox()
        ok_label = {"files": "Ouvrir", "folder": "Choisir ce dossier", "save": "Enregistrer"}[mode]
        buttons.addButton(ok_label, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton("Annuler", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.selected: list[Path] = []
        self.current = Path.home()
        self.navigate(start if start is not None and start.is_dir() else Path.home())

    @staticmethod
    def _places() -> list[tuple[str, Path]]:
        places: list[tuple[str, Path]] = [("Dossier personnel", Path.home())]
        for label, location in (
            ("Bureau", QStandardPaths.StandardLocation.DesktopLocation),
            ("Documents", QStandardPaths.StandardLocation.DocumentsLocation),
            ("Téléchargements", QStandardPaths.StandardLocation.DownloadLocation),
        ):
            path = QStandardPaths.writableLocation(location)
            if path and Path(path).is_dir():
                places.append((label, Path(path)))
        for drive in QDir.drives():
            places.append((drive.absoluteFilePath(), Path(drive.absoluteFilePath())))
        return places

    def navigate(self, path: Path) -> None:
        if not path.is_dir():
            return
        self.current = path.resolve()
        self.path_edit.setText(str(self.current))
        self.view.setRootIndex(self.model.index(str(self.current)))
        self.view.clearSelection()

    def _go_up(self) -> None:
        if self.current.parent != self.current:
            self.navigate(self.current.parent)

    def _new_folder(self) -> None:
        name = ask_text(self, "Nouveau dossier", "Nom du dossier :")
        if not name or not name.strip():
            return
        target = self.current / name.strip()
        try:
            target.mkdir()
        except OSError:
            error(self, "Impossible de créer ce dossier.")
            return
        self.navigate(target)

    def _activated(self, index: QModelIndex) -> None:
        path = Path(self.model.filePath(index))
        if path.is_dir():
            self.navigate(path)
        elif self.mode == "files":
            self.selected = [path]
            self.accept()
        elif self.mode == "save":
            self.name_edit.setText(path.name)

    def _selection(self) -> list[Path]:
        rows = self.view.selectionModel().selectedRows(0)
        return [Path(self.model.filePath(i)) for i in rows]

    def _accept(self) -> None:
        if self.mode == "files":
            files = [p for p in self._selection() if p.is_file()]
            if not files:
                return
            self.selected = files
        elif self.mode == "folder":
            chosen = [p for p in self._selection() if p.is_dir()]
            self.selected = chosen[:1] or [self.current]
        else:
            name = self.name_edit.text().strip()
            if not name or "/" in name or "\\" in name:
                error(self, "Indiquez un nom de fichier valide.")
                return
            self.selected = [self.current / name]
        self.accept()


def _browse(
    parent: QWidget | None,
    title: str,
    mode: str,
    name_filters: list[str] | None = None,
    start: Path | None = None,
    default_name: str = "",
) -> list[Path]:
    dialog = FileBrowser(parent, title, mode, name_filters, start, default_name)
    try:
        return dialog.selected if dialog.exec() == QDialog.DialogCode.Accepted else []
    finally:
        dialog.deleteLater()


def open_files(
    parent: QWidget | None,
    title: str = "Ajouter des fichiers",
    name_filters: list[str] | None = None,
    start: Path | None = None,
) -> list[Path]:
    return _browse(parent, title, "files", name_filters, start)


def open_folder(
    parent: QWidget | None, title: str = "Choisir un dossier", start: Path | None = None
) -> Path | None:
    chosen = _browse(parent, title, "folder", None, start)
    return chosen[0] if chosen else None


def save_file(
    parent: QWidget | None,
    title: str,
    default_name: str,
    start: Path | None = None,
) -> Path | None:
    chosen = _browse(parent, title, "save", None, start, default_name)
    return chosen[0] if chosen else None
