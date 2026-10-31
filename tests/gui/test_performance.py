"""Large batches: background inventory and preview, virtualised models."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest

from anonyma.config import Config
from anonyma.gui import controller as controller_module
from anonyma.gui.main_window import MainWindow
from anonyma.gui.preview import compute_effective

TIMEOUT = 60_000
FILES = 150
TERMS = 600


def test_large_batch_stays_off_the_ui_thread(
    qtbot: Any,
    window: MainWindow,
    tmp_path: Path,
    gui_config: Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    src = tmp_path / "lot"
    src.mkdir()
    terms = [f"Client{i:04d}" for i in range(TERMS)]
    for f in range(FILES):
        words = " ".join(terms[(f * 7 + k) % TERMS] for k in range(12))
        (src / f"note{f:03d}.txt").write_text(f"Dossier {f} : {words}.\n", encoding="utf-8")
    gui_config.always_mask = [{"value": t, "type": "ORGANISATION"} for t in terms]

    threads: list[str] = []

    def spy(*args: Any, **kwargs: Any) -> Any:
        threads.append(threading.current_thread().name)
        return compute_effective(*args, **kwargs)

    monkeypatch.setattr(controller_module, "compute_effective", spy)

    c = window.controller
    c.add_inputs([src])
    assert c.queue_loading  # inventory runs in the background
    qtbot.waitUntil(lambda: not c.queue_loading, timeout=TIMEOUT)
    assert window.processing.model.rowCount() == FILES
    assert window.processing.set_output_dir(tmp_path / "sortie")

    with qtbot.waitSignal(c.analysis_ready, timeout=TIMEOUT):
        window.processing.start_analyze()
    qtbot.waitUntil(c.effective_is_current, timeout=TIMEOUT)
    main = threading.main_thread().name
    assert threads and all(name != main for name in threads)

    review = window.review
    assert len(review.model.ids) == len(c.analysis.clusters)  # type: ignore[union-attr]
    started = time.perf_counter()
    review.search.setText("client0042")
    review._apply_filters()
    assert len(review.model.ids) == 1
    review.clear_filters()
    review.state_filter.setCurrentIndex(review.state_filter.findData("non_decidees"))
    review.clear_filters()
    assert time.perf_counter() - started < 5

    # A manual addition is propagated batch-wide in the background.
    threads.clear()
    assert c.add_manual("Dossier", "IDENTIFIANT")
    qtbot.waitUntil(c.effective_is_current, timeout=TIMEOUT)
    assert threads and all(name != main for name in threads)
    manual = review.cluster_for_value("Dossier")
    assert manual
    assert review.model.view(manual).occurrences == FILES  # type: ignore[union-attr]
