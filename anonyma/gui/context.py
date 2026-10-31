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

#: Config fields that do not influence detection (changing them needs no new analysis).
NON_DETECTION_FIELDS = frozenset({"ui", "mode", "regime", "store_path", "workers"})


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
            return Config.load(self.config_path)
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
            self._model_manager = ModelManager()
        return self._model_manager

    def update_config(self, config: Config, *, save: bool = True) -> None:
        """Replace the configuration (validated) and persist it. Raises ValueError."""
        config.validate()
        self._config = config
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
