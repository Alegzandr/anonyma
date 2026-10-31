from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from anonyma import hardware
from anonyma.gui.context import AppContext
from anonyma.gui.main_window import MainWindow
from anonyma.gui.pages.models import ModelsPage
from anonyma.model_catalog import ModelKind
from anonyma.model_manager import LicenseRefusedError, ModelManager
from anonyma.models import Profile
from tests.gui.conftest import DialogRecorder, FakeModelManager, make_spec

TIMEOUT = 10_000


def _page(qtbot: Any, manager: ModelManager) -> ModelsPage:
    page = ModelsPage(AppContext(model_manager=manager))

    def settle(_widget: object) -> None:
        # Closes over ``page`` on purpose: pytest-qt keeps widgets by weak reference, so
        # the page was freed as the test returned, with the refresh started by its last
        # action still running: a QThread destroyed while running aborts the process.
        page.runner.wait_all(TIMEOUT)
        qtbot.waitUntil(lambda: not page.busy(), timeout=TIMEOUT)

    qtbot.addWidget(page, before_close_func=settle)
    page.show()
    qtbot.waitUntil(lambda: not page.busy(), timeout=TIMEOUT)
    return page


def test_hardware_and_recommendation(
    qtbot: Any, recorder: DialogRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    info = hardware.HardwareInfo(
        ram_bytes=16 * 1024**3,
        cpu_count=8,
        providers=("DmlExecutionProvider", "CPUExecutionProvider"),
        gpu_name="Carte de test",
        vram_bytes=4 * 1024**3,
    )
    monkeypatch.setattr(hardware, "detect_hardware", lambda: info)
    monkeypatch.setattr(hardware, "recommend_profile", lambda _i: Profile.STANDARD)
    monkeypatch.setattr(hardware, "recommend_llm", lambda _i: "llm-test")
    manager = FakeModelManager([make_spec(), make_spec("llm-test", ModelKind.LLM)])
    page = _page(qtbot, manager)
    assert page.hw_labels["ram"].text() == "16,0 Go"
    assert page.hw_labels["gpu"].text() == "Carte de test"
    assert "Dml" in page.hw_labels["providers"].text()
    assert "Standard" in page.profile_label.text()
    assert page.table.rowCount() == 2


def test_empty_catalogue(qtbot: Any, recorder: DialogRecorder) -> None:
    page = _page(qtbot, FakeModelManager([]))
    assert page.cat_stack.currentWidget() is page.empty
    assert not page.download_button.isEnabled()


def test_download_import_remove(qtbot: Any, recorder: DialogRecorder, tmp_path: Path) -> None:
    manager = FakeModelManager([make_spec()])
    page = _page(qtbot, manager)
    assert page.table.item(0, 4).text() == "Absent"
    assert page.table.item(0, 2).text() == "3,0 Mo"
    assert page.table.item(0, 3).text() == "apache-2.0"
    page.table.selectRow(0)
    assert page.download_button.isEnabled() and not page.remove_button.isEnabled()

    progress: list[object] = []
    page._download_progress = progress.append  # type: ignore[method-assign]
    page.download_selected()
    assert recorder.confirms and "réseau" in recorder.confirms[0]
    qtbot.waitUntil(lambda: "installé" in page.banner.text(), timeout=TIMEOUT)
    qtbot.waitUntil(lambda: page.table.item(0, 4).text() == "Installé · vérifié", timeout=TIMEOUT)
    assert manager.calls == [("download", "gliner-test")]

    page.table.selectRow(0)
    assert page.remove_button.isEnabled()
    page.remove_selected()
    qtbot.waitUntil(lambda: page.table.item(0, 4).text() == "Absent", timeout=TIMEOUT)

    recorder.files = [tmp_path / "model.onnx"]
    page.table.selectRow(0)
    page.import_files()
    qtbot.waitUntil(lambda: "importé" in page.banner.text(), timeout=TIMEOUT)
    assert ("import", "gliner-test") in manager.calls


def test_download_refused_is_reported(qtbot: Any, recorder: DialogRecorder) -> None:
    manager = FakeModelManager([make_spec()])
    manager.fail_download = LicenseRefusedError(
        "Licence non autorisée pour un usage professionnel."
    )
    page = _page(qtbot, manager)
    page.table.selectRow(0)
    page.download_selected()
    qtbot.waitUntil(lambda: "Licence" in page.banner.text(), timeout=TIMEOUT)
    assert page.banner.level == "error"


def test_download_cancel(qtbot: Any, recorder: DialogRecorder) -> None:
    import threading

    gate = threading.Event()
    manager = FakeModelManager([make_spec()])
    manager.download_gate = lambda: gate.wait(5)
    page = _page(qtbot, manager)
    page.table.selectRow(0)
    page.download_selected()
    assert page.cancel_button.isVisible()
    page.cancel_download()
    gate.set()
    qtbot.waitUntil(lambda: "annulé" in page.banner.text(), timeout=TIMEOUT)
    assert "gliner-test" not in manager.installed


def test_download_error_message_is_shown(qtbot: Any, recorder: DialogRecorder) -> None:
    """A ModelError raised by the manager is shown with its French message."""
    from anonyma.model_manager import LicenseRefusedError

    class Refusing(FakeModelManager):
        def download(self, model_id, progress=None, cancel=None):  # type: ignore[no-untyped-def]
            raise LicenseRefusedError("Licence non commerciale : téléchargement refusé.")

    page = _page(qtbot, Refusing([make_spec()]))
    page.table.selectRow(0)
    page.download_selected()
    qtbot.waitUntil(lambda: "Licence non commerciale" in page.banner.text(), timeout=TIMEOUT)


def test_models_page_in_window(window: MainWindow, qtbot: Any) -> None:
    window.show()
    window.show_page("models")
    qtbot.waitUntil(lambda: window.models.table.rowCount() == 1, timeout=TIMEOUT)
