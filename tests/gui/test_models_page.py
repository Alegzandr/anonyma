from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from anonyma import hardware, model_setup
from anonyma.config import Config
from anonyma.gui.context import AppContext
from anonyma.gui.main_window import MainWindow
from anonyma.gui.pages.models import ModelsPage
from anonyma.model_catalog import ModelKind, ModelSpec
from anonyma.model_manager import LicenseRefusedError, ModelManager
from tests.gui.conftest import DialogRecorder, FakeModelManager, make_spec

TIMEOUT = 10_000

INFO = hardware.HardwareInfo(
    ram_bytes=16 * 1024**3,
    cpu_count=8,
    providers=("DmlExecutionProvider", "CPUExecutionProvider"),
    gpu_name="Carte de test",
    vram_bytes=4 * 1024**3,
)


@pytest.fixture
def specs(monkeypatch: pytest.MonkeyPatch) -> list[ModelSpec]:
    wanted = [make_spec(), make_spec("llm-test", ModelKind.LLM)]
    monkeypatch.setattr(hardware, "detect_hardware", lambda: INFO)
    monkeypatch.setattr(model_setup, "wanted_models", lambda _info: list(wanted))
    return wanted


def _page(qtbot: Any, manager: ModelManager, *, automatic: bool = True) -> ModelsPage:
    page = ModelsPage(AppContext(Config(check_updates=automatic), model_manager=manager))

    def settle(_widget: object) -> None:
        # Closes over ``page`` on purpose: pytest-qt keeps widgets by weak reference, so
        # the page was freed as the test returned, with the refresh started by its last
        # action still running: a QThread destroyed while running aborts the process.
        page.runner.wait_all(TIMEOUT)
        qtbot.waitUntil(lambda: not page.busy(), timeout=TIMEOUT)

    qtbot.addWidget(page, before_close_func=settle)
    return page


def _idle(qtbot: Any, page: ModelsPage) -> None:
    qtbot.waitUntil(lambda: not page.busy(), timeout=TIMEOUT)


def test_state_and_hardware_without_installing(
    qtbot: Any, recorder: DialogRecorder, specs: list[ModelSpec]
) -> None:
    manager = FakeModelManager(specs)
    page = _page(qtbot, manager)
    page.show()
    _idle(qtbot, page)
    assert page.hw_labels["ram"].text() == "16,0 Go"
    assert "Carte de test" in page.hw_labels["gpu"].text()
    assert page.hw_labels["providers"].text() == "DirectML (GPU), processeur"
    assert page.table.rowCount() == 2
    assert page.table.item(0, 3).text() == "À installer"
    assert "à télécharger" in page.headline.text()
    assert manager.calls == []  # showing the page never downloads


def test_automatic_install_and_cleanup(
    qtbot: Any, recorder: DialogRecorder, specs: list[ModelSpec]
) -> None:
    manager = FakeModelManager(specs)
    manager.installed.add("ancien-modele")
    page = _page(qtbot, manager)
    changed: list[bool] = []
    page.models_changed.connect(lambda: changed.append(True))
    page.start_automatic()
    _idle(qtbot, page)
    assert ("download", "gliner-test") in manager.calls
    assert ("download", "llm-test") in manager.calls
    assert ("remove", "ancien-modele") in manager.calls
    assert not recorder.confirms  # nothing asked
    assert "à jour" in page.headline.text()
    assert {page.table.item(r, 3).text() for r in range(page.table.rowCount())} == {"Installé"}
    assert changed


def test_automatic_connections_off_waits_for_the_user(
    qtbot: Any, recorder: DialogRecorder, specs: list[ModelSpec]
) -> None:
    manager = FakeModelManager(specs)
    page = _page(qtbot, manager, automatic=False)
    page.show()
    page.start_automatic()
    _idle(qtbot, page)
    assert manager.calls == []
    assert "Réglages" in page.detail.text()
    assert page.install_button.isVisibleTo(page)
    page.install_button.click()
    _idle(qtbot, page)
    assert ("download", "gliner-test") in manager.calls


def test_failure_is_shown_with_retry(
    qtbot: Any, recorder: DialogRecorder, specs: list[ModelSpec]
) -> None:
    manager = FakeModelManager(specs)
    manager.fail_download = LicenseRefusedError("Licence refusée : usage non commercial.")
    page = _page(qtbot, manager)
    page.show()
    page.start_automatic()
    _idle(qtbot, page)
    assert "Licence refusée" in page.detail.text()
    assert page.install_button.text() == "Réessayer"


def test_cancel(qtbot: Any, recorder: DialogRecorder, specs: list[ModelSpec]) -> None:
    gate = threading.Event()
    manager = FakeModelManager(specs)
    manager.download_gate = lambda: gate.wait(5)
    page = _page(qtbot, manager)
    page.show()
    page.start_automatic()
    qtbot.waitUntil(lambda: page.cancel_button.isVisibleTo(page), timeout=TIMEOUT)
    page.cancel_download()
    gate.set()
    _idle(qtbot, page)
    assert "llm-test" not in manager.installed


def test_offline_import(
    qtbot: Any, recorder: DialogRecorder, specs: list[ModelSpec], tmp_path: Path
) -> None:
    manager = FakeModelManager(specs)
    page = _page(qtbot, manager, automatic=False)
    page.show()
    _idle(qtbot, page)
    recorder.folder = tmp_path
    page.import_folder()
    _idle(qtbot, page)
    assert ("import", "gliner-test") in manager.calls
    assert "importés" in page.banner.text()


def test_models_page_in_window(window: MainWindow, qtbot: Any) -> None:
    window.show()
    window.show_page("models")
    qtbot.waitUntil(lambda: window.models.setup is not None, timeout=TIMEOUT)
    window.models.runner.wait_all(TIMEOUT)


def test_sidebar_reaches_the_bottom(window: MainWindow) -> None:
    """The status bar sits under the pages, not under the side column."""
    assert window.stack.parentWidget() is window.status_bar.parentWidget()
