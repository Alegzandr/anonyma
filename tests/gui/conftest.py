from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from anonyma import registry
from anonyma.config import Config
from anonyma.gui import dialogs, theme
from anonyma.gui.context import AppContext
from anonyma.gui.main_window import MainWindow
from anonyma.model_catalog import ModelFile, ModelKind, ModelSpec, get_spec
from anonyma.model_manager import ModelError, ModelManager, ModelStatus
from anonyma.models import Detector, Entity, Profile, Segment

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
        spec = next((s for s in self.specs if s.id == model_id), None) or get_spec(model_id)
        assert spec is not None
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

    def installed_folders(self) -> list[str]:
        return sorted(self.installed)

    def remove_folder(self, name: str) -> None:
        self.remove(name)

    def clean_staging(self, keep: Sequence[ModelSpec]) -> None:
        pass


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


@pytest.fixture(autouse=True)
def _delete_windows(qapp: Any) -> Iterator[None]:
    """Delete the main windows a test leaves behind (once qtbot has closed them).

    A closed window is only hidden: without this, every test adds its ~800 widgets to
    the application, and each theme change (one per ``window`` fixture) restyles all
    of them, so the GUI tests got slower and slower (88 s instead of 14 s)."""
    yield
    from PySide6.QtCore import QCoreApplication, QEvent

    for widget in qapp.topLevelWidgets():
        if isinstance(widget, MainWindow):
            widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


@pytest.fixture
def window(qtbot: Any, context: AppContext, recorder: DialogRecorder) -> Iterator[MainWindow]:
    theme.apply_theme("clair")
    win = MainWindow(context)
    qtbot.addWidget(win)
    yield win
    win.controller.shutdown(10_000)
    win.settings.models.runner.wait_all(10_000)


# ---------------------------------------------------------------------------
# Batch fixtures and helpers shared by the flow tests
# ---------------------------------------------------------------------------

FLOW_TIMEOUT = 30_000


class _LowScoreDetector(Detector):
    """Flags « Orsini » with a score below the review threshold."""

    name = "faible"

    def detect(self, segments: Sequence[Segment], document_id: str) -> list[Entity]:
        out = []
        for s in segments:
            i = s.text.find("Orsini")
            if i >= 0:
                out.append(
                    Entity("PERSONNE", "Orsini", document_id, s.id, i, i + 6, 0.4, self.name)
                )
        return out


@pytest.fixture
def low_score(monkeypatch: pytest.MonkeyPatch) -> None:
    registry.load_plugins()
    monkeypatch.setitem(registry._detectors, "faible", _LowScoreDetector)


@pytest.fixture
def batch(tmp_path: Path, gui_config: Config) -> Path:
    """Two txt files and a business list (CSV) referenced by the configuration."""
    folder = tmp_path / "Dossiers"
    folder.mkdir()
    (folder / "a.txt").write_text(
        "Contrat Hélios signé par Bérénice Lagarde à Brest.\nContact : Orsini.\n",
        encoding="utf-8",
    )
    (folder / "b.txt").write_text(
        "Le projet HÉLIOS avance à Brest. Bérénice Lagarde relit.\n", encoding="utf-8"
    )
    csv = tmp_path / "liste.csv"
    csv.write_text("valeur,type\nBérénice Lagarde,PERSONNE\nHélios,PROJET\n", encoding="utf-8")
    gui_config.business_lists = [str(csv)]
    return folder


def prepare(qtbot: Any, window: MainWindow, folder: Path, out: Path) -> None:
    window.controller.add_inputs([folder])
    qtbot.waitUntil(lambda: not window.controller.queue_loading, timeout=FLOW_TIMEOUT)
    assert window.processing.set_output_dir(out)


def analyze(qtbot: Any, window: MainWindow) -> None:
    with qtbot.waitSignal(window.controller.analysis_ready, timeout=FLOW_TIMEOUT):
        window.processing.analyze_button.click()
    assert not window.controller.busy
    qtbot.waitUntil(window.controller.effective_is_current, timeout=FLOW_TIMEOUT)


def select_text(window: MainWindow, text: str) -> None:
    from PySide6.QtGui import QTextCursor

    window.review.original.moveCursor(QTextCursor.MoveOperation.Start)
    assert window.review.original.find(text)


def settle(qtbot: Any, window: MainWindow) -> None:
    """Wait for the background preview of the latest decisions."""
    qtbot.waitUntil(window.controller.effective_is_current, timeout=FLOW_TIMEOUT)
