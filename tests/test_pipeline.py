from __future__ import annotations

from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

from anonyma import registry
from anonyma.config import Config
from anonyma.models import (
    Detector,
    DocumentStatus,
    Entity,
    ManualEntity,
    Profile,
    Regime,
    ReviewDecisions,
    Segment,
)
from anonyma.pipeline import QUARANTINE_DIR, CancelToken, Pipeline, collect_inputs
from anonyma.store import InMemoryMappingStore
from anonyma.writers.text import TextWriter
from tests.canary import CANARY_EMAIL, CANARY_PERSON


def _make_inputs(root: Path) -> Path:
    folder = root / "Dossier Quintavalle"
    (folder / "sous").mkdir(parents=True)
    (folder / "a.txt").write_text(
        f"Courrier de {CANARY_PERSON} <{CANARY_EMAIL}>.\n", encoding="utf-8"
    )
    (folder / "sous" / f"CV {CANARY_PERSON}.txt").write_text(
        "ZÉPHYRIN QUINTAVALLE, né à Lyon.\n", encoding="utf-8"
    )
    (folder / "inconnu.xyz").write_bytes(b"\x00\x01")
    return folder


def test_collect_inputs_keeps_folder_name(tmp_path: Path) -> None:
    folder = _make_inputs(tmp_path)
    names = [n for _, n in collect_inputs([folder])]
    assert names == [
        "Dossier Quintavalle/a.txt",
        "Dossier Quintavalle/inconnu.xyz",
        f"Dossier Quintavalle/sous/CV {CANARY_PERSON}.txt",
    ]


def test_end_to_end(tmp_path: Path, canary_config: Config) -> None:
    folder = _make_inputs(tmp_path)
    canary_config.always_mask.append({"value": "Quintavalle", "type": "PERSONNE"})
    result = Pipeline(canary_config).run([folder], tmp_path / "out")
    statuses = [r.status for r in result.results]
    assert DocumentStatus.UNSUPPORTED in statuses
    written = [r for r in result.results if r.status is DocumentStatus.ANONYMIZED]
    assert len(written) == 2
    for r in written:
        assert r.output_path is not None
        text = r.output_path.read_text(encoding="utf-8")
        assert "uintavalle" not in text.casefold()
        assert "uintavalle" not in r.output_name.casefold()
        assert "[PERSONNE_" in text
    # Folder names are anonymized too and the original tree is untouched.
    assert all("uintavalle" not in p.name for p in (tmp_path / "out").iterdir())
    assert (folder / "a.txt").read_text(encoding="utf-8").startswith("Courrier de Zéphyrin")


def test_same_entity_same_tag_across_batch(tmp_path: Path, canary_config: Config) -> None:
    src = tmp_path / "in"
    src.mkdir()
    (src / "a.txt").write_text(f"{CANARY_PERSON}\n", encoding="utf-8")
    (src / "b.txt").write_text(f"x {CANARY_PERSON.upper()}\n", encoding="utf-8")
    result = Pipeline(canary_config).run([src], tmp_path / "out")
    texts = [r.output_path.read_text(encoding="utf-8") for r in result.results if r.output_path]
    assert texts == ["[PERSONNE_001]\n", "x [PERSONNE_001]\n"]


class _LeakyWriter(TextWriter):
    def write(self, document, plan, destination, ctx):  # type: ignore[no-untyped-def]
        destination.write_bytes(document.source.read_bytes())
        return []


def test_residue_goes_to_quarantine(
    tmp_path: Path, canary_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry.load_plugins()
    monkeypatch.setitem(registry._writers, "txt", _LeakyWriter)
    src = tmp_path / "in" / "x.txt"
    src.parent.mkdir()
    src.write_text(f"{CANARY_PERSON}\n", encoding="utf-8")
    result = Pipeline(canary_config).run([src], tmp_path / "out")
    top = result.results[0]
    assert top.status is DocumentStatus.RESIDUE
    assert top.output_path is not None and QUARANTINE_DIR in top.output_path.parts
    assert top.verification is not None and top.verification.residues
    assert "Quintavalle" not in repr(top)


class _LowScoreDetector(Detector):
    name = "faible"

    def detect(self, segments: Sequence[Segment], document_id: str) -> list[Entity]:
        out = []
        for s in segments:
            i = s.text.find("Hélios")
            if i >= 0:
                out.append(Entity("PROJET", "Hélios", document_id, s.id, i, i + 6, 0.4, self.name))
        return out


@pytest.fixture
def low_score_detector(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    registry.load_plugins()
    monkeypatch.setitem(registry._detectors, "faible", _LowScoreDetector)
    yield


def test_review_flow(tmp_path: Path, canary_config: Config, low_score_detector: None) -> None:
    src = tmp_path / "in"
    src.mkdir()
    (src / "a.txt").write_text("Le projet Hélios avance. Signé Bérénice.\n", encoding="utf-8")
    (src / "b.txt").write_text("Rien sur HÉLIOS ici, mais Bérénice relit.\n", encoding="utf-8")
    pipeline = Pipeline(canary_config)
    analysis = pipeline.analyze([src])
    clusters = analysis.review_clusters()
    assert len(clusters) == 1
    # Propagation found the case variant in the second file.
    assert sum(1 for e in analysis.entities if e.cluster_id == clusters[0].id) == 2

    first = pipeline.apply(analysis, tmp_path / "o1")
    assert {r.status for r in first.results} == {DocumentStatus.NEEDS_REVIEW}

    decisions = ReviewDecisions(
        confirmed_clusters={clusters[0].id}, manual=[ManualEntity("Bérénice", "PERSONNE")]
    )
    second = pipeline.apply(analysis, tmp_path / "o2", decisions)
    assert {r.status for r in second.results} == {DocumentStatus.ANONYMIZED}
    for r in second.results:
        assert r.output_path is not None
        text = r.output_path.read_text(encoding="utf-8")
        assert "Bérénice" not in text
        assert "élios" not in text and "ÉLIOS" not in text

    rejected = pipeline.apply(
        analysis, tmp_path / "o3", ReviewDecisions(rejected_clusters={clusters[0].id})
    )
    assert {r.status for r in rejected.results} == {DocumentStatus.ANONYMIZED}
    assert any(
        "Hélios" in r.output_path.read_text(encoding="utf-8")
        for r in rejected.results
        if r.output_path
    )
    pipeline.close(analysis)
    assert not analysis.work_dir.exists()


def test_pseudonymization_is_stable_across_batches(tmp_path: Path) -> None:
    config = Config(
        profile=Profile.LIGHT,
        workers=1,
        regime=Regime.PSEUDONYMIZATION,
        always_mask=[
            {"value": "Quintavalle", "type": "PERSONNE"},
            {"value": "Bérénice", "type": "PERSONNE"},
        ],
    )
    first_src = tmp_path / "in0" / "f.txt"
    first_src.parent.mkdir()
    first_src.write_text("Bérénice puis Quintavalle", encoding="utf-8")
    with pytest.raises(ValueError):
        Pipeline(config).run([first_src], tmp_path / "x")

    store = InMemoryMappingStore()
    outputs = []
    for i, text in enumerate(["Bérénice puis Quintavalle", "Quintavalle seul"]):
        src = tmp_path / f"in{i}" / "f.txt"
        src.parent.mkdir(exist_ok=True)
        src.write_text(text, encoding="utf-8")
        result = Pipeline(config, store=store).run([src], tmp_path / f"out{i}")
        assert result.results[0].output_path is not None
        outputs.append(result.results[0].output_path.read_text(encoding="utf-8"))
    tag = outputs[0].split(" puis ")[1]
    assert outputs[1] == f"{tag} seul"
    assert store.reverse(tag) == "Quintavalle"


def test_output_inside_input_is_refused(tmp_path: Path, canary_config: Config) -> None:
    src = tmp_path / "in"
    src.mkdir()
    (src / "a.txt").write_text("x", encoding="utf-8")
    with pytest.raises(ValueError):
        Pipeline(canary_config).run([src], src / "out")


def test_cancel_before_start(tmp_path: Path, canary_config: Config) -> None:
    src = tmp_path / "a.txt"
    src.write_text("x", encoding="utf-8")
    token = CancelToken()
    token.cancel()
    result = Pipeline(canary_config).run([src], tmp_path / "out", cancel=token)
    assert result.results[0].status is DocumentStatus.CANCELLED


def test_process_pool(tmp_path: Path, canary_config: Config) -> None:
    src = tmp_path / "in"
    src.mkdir()
    for i in range(3):
        (src / f"{i}.txt").write_text(f"{CANARY_PERSON} {i}\n", encoding="utf-8")
    canary_config.workers = 2
    result = Pipeline(canary_config).run([src], tmp_path / "out")
    assert [r.status for r in result.results] == [DocumentStatus.ANONYMIZED] * 3
