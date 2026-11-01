"""Application-wide state shared by the pages: configuration and services."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Signal

from anonyma.config import Config
from anonyma.logs import get_logger
from anonyma.model_manager import ModelManager

log = get_logger("gui.context")


def managed(config: Config) -> Config:
    """``config`` with the choices the application makes itself: the profile the
    hardware and installed models allow, automatic workers, OCR and models. Older
    versions showed them as settings; a value saved then must not linger unseen."""
    config.profile = None
    config.workers = 0
    config.ocr_enabled = True
    config.gliner_model = ""
    config.llm_model = ""
    return config


#: Config fields that do not influence detection (changing them needs no new analysis).
NON_DETECTION_FIELDS = frozenset(
    {"ui", "mode", "regime", "store_path", "workers", "check_updates", "proxy"}
)

#: ``Config.ui`` key of the folders remembered per file chooser.
LAST_DIRS_KEY = "last_dirs"
#: ``Config.ui`` key of the last valid output folder.
OUTPUT_DIR_KEY = "output_dir"


def detection_fingerprint(config: Config) -> str:
    data = {k: v for k, v in config.to_dict().items() if k not in NON_DETECTION_FIELDS}
    return repr(sorted(data.items(), key=lambda kv: kv[0]))


class AppContext(QObject):
    """Holds the live ``Config`` (saved to ``data/config.json``) and the model manager."""

    config_changed = Signal()

    def __init__(
        self,
        config: Config | None = None,
        *,
        config_path: Path | None = None,
        model_manager: ModelManager | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.config_path = config_path
        #: French message when ``data/config.json`` could not be read.
        self.load_error = ""
        self._config = config if config is not None else self._load()
        self._model_manager = model_manager

    def _load(self) -> Config:
        try:
            return managed(Config.load(self.config_path))
        except Exception as exc:  # corrupt file: start with defaults, keep the file
            log.warning("config unreadable, defaults used: %s", type(exc).__name__)
            self.load_error = (
                "Le fichier de réglages est illisible : les réglages par défaut sont utilisés."
            )
            return Config()

    @property
    def config(self) -> Config:
        return self._config

    def config_copy(self) -> Config:
        return copy.deepcopy(self._config)

    @property
    def model_manager(self) -> ModelManager:
        if self._model_manager is None:
            self._model_manager = ModelManager(proxy=self._config.proxy)
        return self._model_manager

    def update_config(self, config: Config, *, save: bool = True) -> None:
        """Replace the configuration (validated) and persist it. Raises ValueError."""
        config.validate()
        self._config = config
        if self._model_manager is not None:
            self._model_manager.proxy = config.proxy
        if save:
            config.save(self.config_path)
        self.config_changed.emit()

    def ui_pref(self, key: str, default: Any = None) -> Any:
        return self._config.ui.get(key, default)

    def set_ui_pref(self, key: str, value: Any, *, save: bool = True) -> None:
        """Store a GUI preference in ``Config.ui`` (never in QSettings)."""
        if self._config.ui.get(key) == value:
            return
        self._config.ui[key] = value
        if save:
            try:
                self._config.save(self.config_path)
            except OSError as exc:
                log.warning("could not save preferences: %s", type(exc).__name__)

    # -- remembered folders (file choosers, output folder) -------------------------------
    # Kept in data/config.json, inside the portable folder (never in QSettings or the
    # registry, see gui/dialogs.py), and never logged.

    def last_dir(self, key: str) -> Path | None:
        """Folder last used by the chooser ``key``, if it still exists."""
        dirs = self._config.ui.get(LAST_DIRS_KEY)
        raw = dirs.get(key) if isinstance(dirs, dict) else None
        if not isinstance(raw, str) or not raw:
            return None
        path = Path(raw)
        try:
            return path if path.is_dir() else None
        except OSError:
            return None

    def remember_dir(self, key: str, folder: Path) -> None:
        """Remember ``folder`` as the starting point of the chooser ``key``."""
        dirs = self._config.ui.get(LAST_DIRS_KEY)
        updated = dict(dirs) if isinstance(dirs, dict) else {}
        updated[key] = str(folder)
        self.set_ui_pref(LAST_DIRS_KEY, updated)

    def remember_selection(self, key: str, chosen: list[Path] | Path | None) -> None:
        """Remember the folder holding the chosen files or folder (nothing when cancelled)."""
        if chosen is None or (isinstance(chosen, list) and not chosen):
            return
        first = chosen[0] if isinstance(chosen, list) else chosen
        self.remember_dir(key, first.parent if first.parent != first else first)
