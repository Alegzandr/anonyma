from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from anonyma.config import Config
from anonyma.gui import dialogs, theme
from anonyma.gui.context import AppContext
from anonyma.gui.main_window import MainWindow
from anonyma.model_catalog import ModelFile, ModelKind, ModelSpec
from anonyma.model_manager import ModelError, ModelManager, ModelStatus
from anonyma.models import Profile

pytest.importorskip("pytestqt")


@dataclass
class DialogRecorder:
    """Replaces every modal dialog; answers are configurable per test."""

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    infos: list[str] = field(default_factory=list)
    confirms: list[str] = field(default_factory=list)
    confirm_answer: bool = True
    passphrase: str | None = "phrase secrète de test"
    passphrase_calls: list[bool] = field(default_factory=list)
    entity_type: str | None = "PERSONNE"
    files: list[Path] = field(default_factory=list)
    folder: Path | None = None
    save_path: Path | None = None

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(dialogs, "error", lambda _p, text, title="": self.errors.append(text))
        monkeypatch.setattr(
            dialogs, "warning", lambda _p, text, title="": self.warnings.append(text)
        )
        monkeypatch.setattr(dialogs, "info", lambda _p, text, title="": self.infos.append(text))
        monkeypatch.setattr(dialogs, "confirm", self._confirm)
        monkeypatch.setattr(dialogs, "ask_passphrase", self._passphrase)
        monkeypatch.setattr(dialogs, "choose_entity_type", lambda *_a, **_k: self.entity_type)
        monkeypatch.setattr(dialogs, "open_files", lambda *_a, **_k: list(self.files))
        monkeypatch.setattr(dialogs, "open_folder", lambda *_a, **_k: self.folder)
        monkeypatch.setattr(dialogs, "save_file", lambda *_a, **_k: self.save_path)

    def _confirm(self, _parent: Any, text: str, *_a: Any, **_k: Any) -> bool:
        self.confirms.append(text)
        return self.confirm_answer

    def _passphrase(self, _parent: Any, create: bool, _path: Path) -> str | None:
        self.passphrase_calls.append(create)
        return self.passphrase


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> DialogRecorder:
    rec = DialogRecorder()
    rec.install(monkeypatch)
    return rec


def make_spec(model_id: str = "gliner-test", kind: ModelKind = ModelKind.GLINER) -> ModelSpec:
    return ModelSpec(
        id=model_id,
        kind=kind,
        repo_id="exemple/modele-test",
        revision="0" * 40,
        files=(ModelFile("model.onnx", "a" * 64, 3 * 1024 * 1024),),
        license="apache-2.0",
        title="Modèle de test",
        description="Modèle fictif pour les tests.",
    )


class FakeModelManager(ModelManager):
    """In-memory model manager (no network, no disk)."""

    def __init__(self, specs: Sequence[ModelSpec] = ()) -> None:
        super().__init__(Path("inexistant"))
        self.specs = list(specs)
        self.installed: set[str] = set()
        self.calls: list[tuple[str, str]] = []
        self.fail_download: Exception | None = None
        self.download_gate: Callable[[], None] | None = None

    def catalog(self) -> list[ModelSpec]:
        return list(self.specs)

    def status(self, model_id: str) -> ModelStatus:
        spec = next(s for s in self.specs if s.id == model_id)
        installed = model_id in self.installed
        return ModelStatus(
            spec,
            installed,
            Path("modeles") / model_id if installed else None,
            verified=installed,
            size_on_disk=3 * 1024 * 1024 if installed else 0,
        )

    def statuses(self) -> list[ModelStatus]:
        return [self.status(s.id) for s in self.specs]

    def download(self, model_id, progress=None, cancel=None):  # type: ignore[no-untyped-def]
        self.calls.append(("download", model_id))
        if self.fail_download is not None:
            raise self.fail_download
        for i in range(1, 5):
            if self.download_gate is not None:
                self.download_gate()
            if cancel is not None and cancel():
                raise ModelError("Téléchargement annulé.")
            if progress is not None:
                progress(i * 25, 100)
        self.installed.add(model_id)
        return Path("modeles") / model_id

    def import_files(self, model_id, files):  # type: ignore[no-untyped-def]
        self.calls.append(("import", model_id))
        self.installed.add(model_id)
        return Path("modeles") / model_id

    def remove(self, model_id: str) -> None:
        self.calls.append(("remove", model_id))
        self.installed.discard(model_id)


@pytest.fixture
def gui_config() -> Config:
    return Config(profile=Profile.LIGHT, workers=1)


@pytest.fixture
def context(gui_config: Config, portable_home: Path) -> AppContext:
    return AppContext(
        gui_config,
        config_path=portable_home / "data" / "config.json",
        model_manager=FakeModelManager([make_spec()]),
    )


@pytest.fixture
def window(qtbot: Any, context: AppContext, recorder: DialogRecorder) -> Iterator[MainWindow]:
    theme.apply_theme("clair")
    win = MainWindow(context)
    qtbot.addWidget(win)
    yield win
    win.controller.shutdown(10_000)
    win.models.runner.wait_all(10_000)
