"""Full analyze -> review -> apply runs through the GUI (pipeline in a worker thread)."""

from __future__ import annotations

import threading
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest

from anonyma import registry
from anonyma.config import Config
from anonyma.gui import controller as controller_module
from anonyma.gui.main_window import MainWindow
from anonyma.gui.preview import RowState
from anonyma.models import Detector, DocumentStatus, Entity, Regime, Segment
from anonyma.store import InMemoryMappingStore
from tests.gui.conftest import DialogRecorder

TIMEOUT = 30_000


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


def _prepare(window: MainWindow, folder: Path, out: Path) -> None:
    window.controller.add_inputs([folder])
    assert window.processing.set_output_dir(out)


def _analyze(qtbot: Any, window: MainWindow) -> None:
    with qtbot.waitSignal(window.controller.analysis_ready, timeout=TIMEOUT):
        window.processing.analyze_button.click()
    assert not window.controller.busy


def _row_index(window: MainWindow, value: str, document: str | None = None) -> int:
    review = window.review
    for i, row in enumerate(review.model.rows):
        doc_name = review.model.doc_names.get(row.entity.document_id, "")
        if row.entity.value == value and (document is None or doc_name.endswith(document)):
            return i
    raise AssertionError(f"no row for {value!r}")


def _select_text(window: MainWindow, text: str) -> None:
    assert window.review.original.find(text)


def test_full_review_flow(
    qtbot: Any,
    window: MainWindow,
    batch: Path,
    tmp_path: Path,
    low_score: None,
    recorder: DialogRecorder,
) -> None:
    out = tmp_path / "sortie"
    _prepare(window, batch, out)
    window.show()
    _analyze(qtbot, window)
    c = window.controller
    assert c.analysis is not None
    assert len(c.analysis.documents) == 2
    assert c.pending_review_count() == 1
    assert window.processing.table.item(0, 2).text() == "Analysé"

    window.show_page("review")
    review = window.review
    assert "1 groupe" in review.counter.text()
    assert window.nav.item(1).text() == "Revue (1)"
    assert "Bérénice Lagarde" in review.original.toPlainText()
    # Before any decision, the preview masks the business-list terms.
    preview = review.anonymized.toPlainText()
    assert "Bérénice" not in preview and "[PERSONNE_" in preview and "[PROJET_" in preview

    review.scope_combo.setCurrentIndex(review.scope_combo.findData("batch"))
    # Reject « Hélios » (whole cluster), confirm « Orsini » (the review cluster).
    review.select_row(_row_index(window, "Hélios"))
    review.reject_selected()
    review.select_row(_row_index(window, "Orsini"))
    assert review.model.rows[review.table.currentIndex().row()].state is RowState.REVIEW
    review.confirm_selected()
    assert c.pending_review_count() == 0
    assert "Aucun groupe" in review.counter.text()

    # Add « Brest » from a text selection: propagated to the whole batch.
    review.select_document(c.analysis.documents[0].id)
    assert review.current_doc_id == c.analysis.documents[0].id
    _select_text(window, "Brest")
    recorder.entity_type = "LIEU"
    window.review.add_entity_from_selection()
    assert [m.value for m in c.decisions.manual] == ["Brest"]
    preview = review.anonymized.toPlainText()
    assert "Brest" not in preview and "[LIEU_001]" in preview
    assert "Hélios" in preview  # rejected: kept

    # Apply from the review page.
    with qtbot.waitSignal(c.result_ready, timeout=TIMEOUT):
        review.apply_button.click()
    assert c.result is not None
    assert {r.status for r in c.result.results} == {DocumentStatus.ANONYMIZED}
    texts = {}
    for r in c.result.results:
        assert r.output_path is not None and out in r.output_path.parents
        texts[r.output_path.name] = r.output_path.read_text(encoding="utf-8")
    assert set(texts) == {"a.txt", "b.txt"}
    for text in texts.values():
        assert "Bérénice" not in text and "Lagarde" not in text and "Brest" not in text
    assert "Hélios" in texts["a.txt"] and "HÉLIOS" in texts["b.txt"]
    assert "Orsini" not in texts["a.txt"]
    # Same entity, same tag across the batch.
    tag = texts["a.txt"].split("signé par ")[1].split(" ")[0]
    assert tag.startswith("[PERSONNE_") and tag in texts["b.txt"]
    # Originals untouched.
    assert "Bérénice" in (batch / "a.txt").read_text(encoding="utf-8")

    # The anonymized side now shows the re-extracted produced file.
    assert review.source_combo.currentData() == "output"
    qtbot.waitUntil(lambda: "[LIEU_" in review.anonymized.toPlainText(), timeout=TIMEOUT)
    assert "Brest" not in review.anonymized.toPlainText()

    # Report page.
    report = window.report
    assert report.report is not None
    assert report.files_table.rowCount() == 2
    assert report.cards[DocumentStatus.ANONYMIZED].value.text() == "2"
    exported = report.export("json", tmp_path / "rapport.json")
    assert exported is not None and "Bérénice" not in exported.read_text(encoding="utf-8")
    md = report.export("md", tmp_path / "rapport.md")
    assert md is not None and "# Rapport du lot" in md.read_text(encoding="utf-8")


def test_retype_and_rejected_mention_reach_the_output(
    qtbot: Any, window: MainWindow, batch: Path, tmp_path: Path, recorder: DialogRecorder
) -> None:
    _prepare(window, batch, tmp_path / "sortie")
    _analyze(qtbot, window)
    c = window.controller
    review = window.review
    review.scope_combo.setCurrentIndex(review.scope_combo.findData("batch"))
    # Retype « Hélios » (PROJET -> ORGANISATION) through the type chooser.
    review.select_row(_row_index(window, "Hélios"))
    recorder.entity_type = "ORGANISATION"
    review.retype_selected()
    assert set(c.decisions.retyped_clusters.values()) == {"ORGANISATION"}
    # Reject one single mention of « Bérénice Lagarde » in b.txt.
    review.select_row(_row_index(window, "Bérénice Lagarde", "b.txt"))
    review.reject_selected_mentions()
    assert len(c.decisions.rejected_mentions) == 1
    row = review.model.rows[_row_index(window, "Bérénice Lagarde", "b.txt")]
    assert row.state is RowState.MENTION_REJECTED

    with qtbot.waitSignal(c.result_ready, timeout=TIMEOUT):
        window.processing.start_apply()
    assert c.result is not None
    by_name = {Path(r.output_name).name: r for r in c.result.results}
    a = by_name["a.txt"]
    assert a.output_path is not None
    a_text = a.output_path.read_text(encoding="utf-8")
    assert "[ORGANISATION_001]" in a_text and "Hélios" not in a_text
    # The kept mention is still in b.txt (the verifier flags it as a residue).
    b = by_name["b.txt"]
    assert b.output_path is not None
    assert "Bérénice Lagarde" in b.output_path.read_text(encoding="utf-8")


def test_one_click_anonymize(
    qtbot: Any, window: MainWindow, batch: Path, tmp_path: Path, recorder: DialogRecorder
) -> None:
    _prepare(window, batch, tmp_path / "sortie")
    with qtbot.waitSignal(window.controller.result_ready, timeout=TIMEOUT):
        window.processing.run_button.click()
    result = window.controller.result
    assert result is not None
    assert {r.status for r in result.results} == {DocumentStatus.ANONYMIZED}
    assert "Terminé" in window.processing.banner.text()
    assert recorder.passphrase_calls == []
    assert window.processing.report_button.isEnabled()


def test_one_click_stops_for_review(
    qtbot: Any, window: MainWindow, batch: Path, tmp_path: Path, low_score: None
) -> None:
    _prepare(window, batch, tmp_path / "sortie")
    with qtbot.waitSignal(window.controller.review_required, timeout=TIMEOUT):
        window.processing.run_button.click()
    assert window.controller.result is None
    assert window.current_page() == "review"
    assert "revue" in window.processing.banner.text()


def test_pseudonymization_asks_passphrase(
    qtbot: Any,
    window: MainWindow,
    batch: Path,
    tmp_path: Path,
    recorder: DialogRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window.context.config.regime = Regime.PSEUDONYMIZATION
    store = InMemoryMappingStore()
    opened: list[tuple[Path, str, bool]] = []

    def fake_open(path: Path, passphrase: str, create: bool) -> InMemoryMappingStore:
        opened.append((path, passphrase, create))
        return store

    monkeypatch.setattr(controller_module, "open_store", fake_open)
    _prepare(window, batch, tmp_path / "sortie")
    with qtbot.waitSignal(window.controller.result_ready, timeout=TIMEOUT):
        window.processing.run_button.click()
    assert recorder.passphrase_calls == [True]
    assert opened and opened[0][0].name == "correspondances.db" and opened[0][2] is True
    assert opened[0][1] == recorder.passphrase
    assert store.used_substitutes()
    assert window.controller.result is not None


def test_pseudonymization_with_encrypted_store(
    qtbot: Any, window: MainWindow, batch: Path, tmp_path: Path, recorder: DialogRecorder
) -> None:
    """Real EncryptedMappingStore: created on first run, wrong passphrase refused."""
    from anonyma.gui.controller import default_store_path

    window.context.config.regime = Regime.PSEUDONYMIZATION
    _prepare(window, batch, tmp_path / "sortie")
    with qtbot.waitSignal(window.controller.result_ready, timeout=TIMEOUT):
        window.processing.run_button.click()
    assert default_store_path(window.context.config).exists()
    assert recorder.passphrase_calls == [True]

    recorder.passphrase = "mauvaise phrase"
    window.controller.set_output_dir(tmp_path / "sortie2")
    window.processing.set_output_dir(tmp_path / "sortie2")
    with qtbot.waitSignal(window.controller.failed, timeout=TIMEOUT):
        window.processing.run_button.click()
    assert recorder.errors and "phrase" in recorder.errors[-1].lower()
    assert not window.controller.busy


def test_cancel_analysis(
    qtbot: Any,
    window: MainWindow,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = threading.Event()
    release = threading.Event()

    class _Blocking(Detector):
        name = "bloquant"

        def detect(self, segments: Sequence[Segment], document_id: str) -> list[Entity]:
            started.set()
            release.wait(10)
            return []

    registry.load_plugins()
    monkeypatch.setitem(registry._detectors, "bloquant", _Blocking)
    src = tmp_path / "in"
    src.mkdir()
    for i in range(3):
        (src / f"{i}.txt").write_text(f"texte {i}", encoding="utf-8")
    _prepare(window, src, tmp_path / "sortie")
    window.processing.analyze_button.click()
    assert started.wait(10)
    assert window.processing.cancel_button.isEnabled()
    with qtbot.waitSignal(window.controller.cancelled, timeout=TIMEOUT):
        window.processing.cancel_button.click()
        release.set()
    analysis = window.controller.analysis
    assert analysis is not None
    assert DocumentStatus.CANCELLED in {d.status for d in analysis.documents}
    assert "annulé" in window.processing.stage_label.text()


def test_failed_batch_shows_french_message(
    qtbot: Any,
    window: MainWindow,
    batch: Path,
    tmp_path: Path,
    recorder: DialogRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from anonyma import pipeline

    def boom(*_a: Any, **_k: Any) -> Iterator[None]:
        raise RuntimeError("Bérénice Lagarde")

    monkeypatch.setattr(pipeline.Pipeline, "analyze", boom)
    _prepare(window, batch, tmp_path / "sortie")
    with qtbot.waitSignal(window.controller.failed, timeout=TIMEOUT):
        window.processing.analyze_button.click()
    assert recorder.errors == ["Une erreur inattendue s'est produite (RuntimeError)."]
    assert "Bérénice" not in window.processing.banner.text()
