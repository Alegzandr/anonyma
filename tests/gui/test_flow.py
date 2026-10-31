"""Full analyze -> review -> apply runs through the GUI (pipeline in worker threads)."""

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
from anonyma.textutils import normalize
from tests.gui.conftest import DialogRecorder, analyze, prepare, select_text, settle

TIMEOUT = 30_000


def test_full_review_flow(
    qtbot: Any,
    window: MainWindow,
    batch: Path,
    tmp_path: Path,
    low_score: None,
    recorder: DialogRecorder,
) -> None:
    out = tmp_path / "sortie"
    prepare(qtbot, window, batch, out)
    assert [r.status for r in window.processing.model.rows] == ["En attente", "En attente"]
    window.show()
    analyze(qtbot, window)
    c = window.controller
    assert c.analysis is not None
    assert len(c.analysis.documents) == 2
    assert c.pending_review_count() == 1
    assert window.processing.model.rows[0].status == "Analysé"

    window.show_page("review")
    review = window.review
    # One row per entity, the uncertain one first, with occurrences and files.
    orsini = review.cluster_for_value("Orsini")
    assert review.model.ids[0] == orsini
    lagarde = review.cluster_for_value("Bérénice Lagarde")
    assert review.cluster_title(lagarde).replace(" ", " ") == (
        "Bérénice Lagarde — PERSONNE — 2 occurrences dans 2 fichiers"
    )
    view = review.model.view(orsini)
    assert view is not None and "confiance faible" in view.reason
    assert "0 / 3 décidées" in review.counter.text()
    assert "1 à revoir" in review.counter.text()
    assert window.nav.item(1).text() == "Revue (1)"
    assert "Bérénice Lagarde" in review.original.toPlainText()
    preview = review.anonymized.toPlainText()
    assert "Bérénice" not in preview and "[PERSONNE_" in preview and "[PROJET_" in preview

    # Reject « Hélios » (one decision for both occurrences), confirm « Orsini ».
    helios = review.cluster_for_value("Hélios")
    review.select_cluster(helios)
    review.reject_selected()
    review.select_cluster(orsini)
    review.confirm_selected()
    assert c.pending_review_count() == 0
    assert "2 / 3 décidées" in review.counter.text()

    # Add « Brest » from a text selection: propagated to the whole batch.
    review.select_document(c.analysis.documents[0].id)
    assert review.current_doc_id == c.analysis.documents[0].id
    select_text(window, "Brest")
    recorder.entity_type = "LIEU"
    review.add_entity_from_selection()
    assert [m.value for m in c.decisions.manual] == ["Brest"]
    settle(qtbot, window)
    preview = review.anonymized.toPlainText()
    assert "Brest" not in preview and "[LIEU_001]" in preview
    assert "Hélios" in preview  # rejected: kept
    brest = review.cluster_for_value("Brest")
    assert brest and review.cluster_title(brest).replace(" ", " ").endswith(
        "2 occurrences dans 2 fichiers"
    )

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
    tag = texts["a.txt"].split("signé par ")[1].split(" ")[0]
    assert tag.startswith("[PERSONNE_") and tag in texts["b.txt"]
    assert "Bérénice" in (batch / "a.txt").read_text(encoding="utf-8")

    # The anonymized side now shows the re-extracted produced file.
    assert review.source_combo.currentData() == "output"
    qtbot.waitUntil(lambda: "[LIEU_" in review.anonymized.toPlainText(), timeout=TIMEOUT)

    report = window.report
    assert report.report is not None
    assert report.files_table.rowCount() == 2
    assert report.status_counts[DocumentStatus.ANONYMIZED] == 2
    assert report.verdict.text() == "Les 2 fichiers sont anonymisés et vérifiés."
    assert report.counts_label.text() == ""  # a single status: the verdict says it all
    exported = report.export("json", tmp_path / "rapport.json")
    assert exported is not None and "Bérénice" not in exported.read_text(encoding="utf-8")


def test_occurrences_retype_and_rejected_mention(
    qtbot: Any, window: MainWindow, batch: Path, tmp_path: Path, recorder: DialogRecorder
) -> None:
    prepare(qtbot, window, batch, tmp_path / "sortie")
    analyze(qtbot, window)
    c = window.controller
    review = window.review
    helios = review.cluster_for_value("Hélios")
    review.select_cluster(helios)
    recorder.entity_type = "ORGANISATION"
    review.retype_selected()
    assert set(c.decisions.retyped_clusters.values()) == {"ORGANISATION"}

    # Expand « Bérénice Lagarde » and pick its occurrence in b.txt: the view jumps there.
    lagarde = review.cluster_for_value("Bérénice Lagarde")
    key = review.mention_index(lagarde, "b.txt")
    assert key is not None
    assert review.select_cluster(lagarde, key)
    assert review.current_row_kind() == "mention"
    assert review.current_doc_id == key[0]
    assert review.original.extraSelections()
    review.reject_selected_mentions()
    assert c.decisions.rejected_mentions == {key}
    rows = {r.key: r.state for r in review._doc_rows}
    assert rows[key] is RowState.MENTION_REJECTED

    with qtbot.waitSignal(c.result_ready, timeout=TIMEOUT):
        window.processing.start_apply()
    assert c.result is not None
    by_name = {Path(r.output_name).name: r for r in c.result.results}
    a_text = by_name["a.txt"].output_path.read_text(encoding="utf-8")  # type: ignore[union-attr]
    assert "[ORGANISATION_001]" in a_text and "Hélios" not in a_text
    b = by_name["b.txt"]
    assert b.output_path is not None
    assert "Bérénice Lagarde" in b.output_path.read_text(encoding="utf-8")


def test_counter_next_undecided_bulk_and_search(
    qtbot: Any, window: MainWindow, batch: Path, tmp_path: Path, recorder: DialogRecorder
) -> None:
    prepare(qtbot, window, batch, tmp_path / "sortie")
    analyze(qtbot, window)
    c = window.controller
    review = window.review
    assert c.decided_counts() == (0, 2)

    review.search.setText("lagarde")
    review._apply_filters()
    assert [review.model.view(cid).canonical for cid in review.model.ids] == ["Bérénice Lagarde"]
    review.clear_filters()
    assert len(review.model.ids) == 2

    # Validating moves on to the next entity without a decision.
    first = review.current_cluster_id()
    review.confirm_selected()
    qtbot.waitUntil(lambda: review.current_cluster_id() != first, timeout=TIMEOUT)
    second = review.current_cluster_id()
    assert not c.is_decided(second)
    assert "1 / 2 décidées" in review.counter.text()

    view = review.model.view(second)
    assert view is not None
    review.confirm_all_of_type()
    assert recorder.confirms and view.entity_type.capitalize()[:4] in recorder.confirms[-1]
    assert c.decided_counts() == (2, 2)
    assert "2 / 2 décidées" in review.counter.text()
    review.confirm_all_remaining()
    assert "déjà une décision" in review.banner.text()
    review.next_undecided()
    assert "Toutes les entités" in review.banner.text()

    review.state_filter.setCurrentIndex(review.state_filter.findData("non_decidees"))
    assert review.model.ids == []
    review.clear_filters()

    # « Valider tout le reste » and « Rejeter la sélection » ask for confirmation.
    review.select_cluster(review.cluster_for_value("Hélios"))
    review.reset_selected()
    assert c.decided_counts() == (1, 2)
    recorder.confirm_answer = False
    review.confirm_all_remaining()
    assert c.decided_counts() == (1, 2)
    review.reject_selection_bulk()
    assert not c.decisions.rejected_clusters
    recorder.confirm_answer = True
    review.confirm_all_remaining()
    assert c.decided_counts() == (2, 2)


def test_learning_whitelist_and_always_mask_with_undo(
    qtbot: Any, window: MainWindow, batch: Path, tmp_path: Path, recorder: DialogRecorder
) -> None:
    prepare(qtbot, window, batch, tmp_path / "sortie")
    analyze(qtbot, window)
    c = window.controller
    review = window.review
    path = window.context.config_path
    assert path is not None

    review.select_cluster(review.cluster_for_value("Hélios"))
    review.reject_selected()
    labels = [b.text() for b in review.banner.action_buttons]
    assert labels == ["Ne jamais masquer"]
    review.banner.action_buttons[0].click()
    saved = Config.load(path)
    assert [normalize(v) for v in saved.whitelist] == ["helios"]
    assert not c.analysis_is_stale()  # the current analysis already reflects it
    assert [b.text() for b in review.banner.action_buttons] == ["Annuler"]
    review.banner.action_buttons[0].click()
    assert Config.load(path).whitelist == []

    review.select_document(c.analysis.documents[0].id)  # type: ignore[union-attr]
    select_text(window, "Brest")
    recorder.entity_type = "LIEU"
    review.add_entity_from_selection()
    assert [b.text() for b in review.banner.action_buttons] == ["Toujours masquer"]
    review.banner.action_buttons[0].click()
    assert {"value": "Brest", "type": "LIEU"} in Config.load(path).always_mask
    review.banner.action_buttons[0].click()  # Annuler
    assert {"value": "Brest", "type": "LIEU"} not in Config.load(path).always_mask


@pytest.mark.parametrize("policy", ["aucune", "incertaines"])
def test_review_policy(
    qtbot: Any,
    window: MainWindow,
    batch: Path,
    tmp_path: Path,
    low_score: None,
    policy: str,
) -> None:
    prepare(qtbot, window, batch, tmp_path / "sortie")
    window.processing.set_review_policy(policy)
    assert window.context.config.review_policy == policy
    assert Config.load(window.context.config_path).review_policy == policy
    c = window.controller
    if policy == "aucune":
        with qtbot.waitSignal(c.result_ready, timeout=TIMEOUT):
            window.processing.run_button.click()
        assert c.result is not None
        # Written directly; the uncertain entity is masked and flagged « À revoir ».
        assert DocumentStatus.NEEDS_REVIEW in {r.status for r in c.result.results}
    else:
        with qtbot.waitSignal(c.review_required, timeout=TIMEOUT):
            window.processing.run_button.click()
        assert c.result is None
        assert window.current_page() == "review"
        assert "incertaine" in window.processing.banner.text()


def test_systematic_review_even_without_uncertainty(
    qtbot: Any, window: MainWindow, batch: Path, tmp_path: Path
) -> None:
    prepare(qtbot, window, batch, tmp_path / "sortie")
    window.processing.set_review_policy("systematique")
    assert Config.load(window.context.config_path).review_policy == "systematique"
    with qtbot.waitSignal(window.controller.review_required, timeout=TIMEOUT):
        window.processing.run_button.click()
    assert window.controller.pending_review_count() == 0
    assert window.controller.result is None
    assert window.current_page() == "review"
    assert "systématique" in window.processing.banner.text()


def test_primary_button_applies_decisions_and_reanalysis_asks_first(
    qtbot: Any, window: MainWindow, batch: Path, tmp_path: Path, recorder: DialogRecorder
) -> None:
    prepare(qtbot, window, batch, tmp_path / "sortie")
    page = window.processing
    assert page.run_button.text() == "Anonymiser"
    analyze(qtbot, window)
    c = window.controller
    assert page.run_button.text() == "Anonymiser"  # no decision yet: nothing to lose
    review = window.review
    review.select_cluster(review.cluster_for_value("Hélios"))
    review.reject_selected()
    assert c.decision_count() == 1
    assert page.run_button.text() == "Appliquer les décisions"

    # Re-analysing would discard the decision: the user is asked, and can keep it.
    recorder.confirm_answer = False
    page.start_analyze()
    assert recorder.confirms[-1].startswith("Relancer l'analyse efface 1")
    assert not c.busy and c.decision_count() == 1

    # The primary button applies the decisions with the existing analysis.
    recorder.confirm_answer = True
    analysis = c.analysis
    with qtbot.waitSignal(c.result_ready, timeout=TIMEOUT):
        page.run_button.click()
    assert c.analysis is analysis and c.result is not None
    assert c.decisions.rejected_clusters


def test_one_click_anonymize(
    qtbot: Any, window: MainWindow, batch: Path, tmp_path: Path, recorder: DialogRecorder
) -> None:
    prepare(qtbot, window, batch, tmp_path / "sortie")
    with qtbot.waitSignal(window.controller.result_ready, timeout=TIMEOUT):
        window.processing.run_button.click()
    result = window.controller.result
    assert result is not None
    assert {r.status for r in result.results} == {DocumentStatus.ANONYMIZED}
    assert "Terminé" in window.processing.banner.text()
    assert recorder.passphrase_calls == []
    assert window.processing.report_button.isEnabled()


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
    prepare(qtbot, window, batch, tmp_path / "sortie")
    with qtbot.waitSignal(window.controller.result_ready, timeout=TIMEOUT):
        window.processing.run_button.click()
    assert recorder.passphrase_calls == [True]
    assert opened and opened[0][0].name == "correspondances.db" and opened[0][2] is True
    assert opened[0][1] == recorder.passphrase
    assert store.used_substitutes()


def test_wrong_passphrase_is_reported(
    qtbot: Any,
    window: MainWindow,
    batch: Path,
    tmp_path: Path,
    recorder: DialogRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from anonyma.store import WrongPassphraseError

    def fake_open(*_a: Any) -> InMemoryMappingStore:
        raise WrongPassphraseError("Phrase secrète incorrecte.")

    monkeypatch.setattr(controller_module, "open_store", fake_open)
    window.context.config.regime = Regime.PSEUDONYMIZATION
    prepare(qtbot, window, batch, tmp_path / "sortie")
    with qtbot.waitSignal(window.controller.failed, timeout=TIMEOUT):
        window.processing.run_button.click()
    assert recorder.errors == ["Phrase secrète incorrecte."]
    assert not window.controller.busy


def test_cancel_analysis(
    qtbot: Any, window: MainWindow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
    prepare(qtbot, window, src, tmp_path / "sortie")
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
    prepare(qtbot, window, batch, tmp_path / "sortie")
    with qtbot.waitSignal(window.controller.failed, timeout=TIMEOUT):
        window.processing.analyze_button.click()
    assert recorder.errors == ["Une erreur inattendue s'est produite (RuntimeError)."]
    assert "Bérénice" not in window.processing.banner.text()
    assert "Bérénice" not in window.processing.stage_label.text()
