from __future__ import annotations

import contextlib
import sys
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
    Severity,
)
from anonyma.pipeline import (
    CancelToken,
    OutputDirError,
    Pipeline,
    ProgressEvent,
    collect_inputs,
    is_quarantined,
    quarantine_dir,
    resolve_decisions,
    validate_output_dir,
    walk_inputs,
)
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
    assert is_quarantined(top, tmp_path / "out")
    assert top.verification is not None and top.verification.residues
    assert "Quintavalle" not in repr(top)
    # The quarantine is a sibling of the output folder: sharing the output folder
    # never shares a file known to contain residues.
    assert not list((tmp_path / "out").rglob("*.txt"))
    assert top.output_path.is_relative_to(tmp_path / "out_quarantaine")


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
    # Every « À revoir » says why, without the value (D365: problemes: [] before).
    for r in first.results:
        [issue] = [i for i in r.issues if i.code == "review.uncertain"]
        assert issue.message.startswith("1 entité incertaine (Projet) masquée par précaution")
        assert "zones : « " in issue.message and "lios" not in issue.message

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


def test_rejected_mention_survives_manual_propagation(
    tmp_path: Path, canary_config: Config
) -> None:
    src = tmp_path / "in"
    src.mkdir()
    (src / "a.txt").write_text("Bérénice écrit à Quintavalle.\n", encoding="utf-8")
    canary_config.always_mask.append({"value": "Quintavalle", "type": "PERSONNE"})
    pipeline = Pipeline(canary_config)
    analysis = pipeline.analyze([src])
    mention = next(e for e in analysis.entities if e.value == "Quintavalle")
    decisions = ReviewDecisions(
        rejected_mentions={mention.key}, manual=[ManualEntity("Bérénice", "PERSONNE")]
    )
    resolved = resolve_decisions(analysis, decisions)
    assert mention.key not in {e.key for e in resolved.entities}
    assert "Quintavalle" in resolved.allowed
    result = pipeline.apply(analysis, tmp_path / "out", decisions)
    top = result.results[0]
    assert top.status is DocumentStatus.ANONYMIZED, [i.message for i in top.issues]
    assert top.output_path is not None
    assert top.output_path.read_text(encoding="utf-8") == "[PERSONNE_001] écrit à Quintavalle.\n"
    pipeline.close(analysis)


def test_progress_reports_item_index(tmp_path: Path, canary_config: Config) -> None:
    src = tmp_path / "in"
    src.mkdir()
    for i in range(2):
        (src / f"{i}.txt").write_text("x", encoding="utf-8")
    events = []
    Pipeline(canary_config).run([src], tmp_path / "out", progress=events.append)
    assert [e.index for e in events if e.stage == "analyse"] == [None, 0, 1]


def test_unreadable_item_name_is_anonymized(tmp_path: Path, canary_config: Config) -> None:
    src = tmp_path / "in"
    src.mkdir()
    (src / f"{CANARY_PERSON}.xyz").write_bytes(b"\x00")
    result = Pipeline(canary_config).run([src], tmp_path / "out")
    top = result.results[0]
    assert top.status is DocumentStatus.UNSUPPORTED
    assert "uintavalle" not in top.output_name.casefold()


class _FolderWriter(TextWriter):
    """Writes a txt as a folder holding the anonymized text twice."""

    directory_output = True

    def output_extension(self, document):  # type: ignore[no-untyped-def]
        return ""

    def output_format(self, document):  # type: ignore[no-untyped-def]
        return "dossier"

    def write(self, document, plan, destination, ctx):  # type: ignore[no-untyped-def]
        (destination / "sous").mkdir(parents=True)
        super().write(document, plan, destination / "a.txt", ctx)
        super().write(document, plan, destination / "sous" / "b.txt", ctx)
        return []


def test_folder_output(
    tmp_path: Path, canary_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    import zipfile

    registry.load_plugins()
    monkeypatch.setitem(registry._writers, "txt", _FolderWriter)
    src = tmp_path / "in"
    src.mkdir()
    (src / "boite.txt").write_text(f"{CANARY_PERSON}\n", encoding="utf-8")
    with zipfile.ZipFile(src / "lot.zip", "w") as z:
        z.writestr("boite.txt", f"{CANARY_PERSON}\n")
    result = Pipeline(canary_config).run([src], tmp_path / "out")
    folder, archive = sorted(result.results, key=lambda r: r.format)
    assert folder.status is DocumentStatus.ANONYMIZED, [i.message for i in folder.issues]
    assert folder.output_path is not None and folder.output_path.is_dir()
    assert (folder.output_path / "sous" / "b.txt").read_text(encoding="utf-8") == (
        "[PERSONNE_001]\n"
    )
    assert archive.status is DocumentStatus.ANONYMIZED, [i.message for i in archive.issues]
    assert archive.output_path is not None
    with zipfile.ZipFile(archive.output_path) as z:
        assert z.namelist() == ["boite.zip"]


def test_whitelisted_value_is_kept_intact(tmp_path: Path) -> None:
    config = Config(
        profile=Profile.LIGHT,
        workers=1,
        whitelist=[CANARY_EMAIL],
        always_mask=[{"value": CANARY_PERSON, "type": "PERSONNE"}],
    )
    src = tmp_path / "in"
    src.mkdir()
    (src / "a.txt").write_text(f"{CANARY_PERSON} : {CANARY_EMAIL}\n", encoding="utf-8")
    result = Pipeline(config).run([src], tmp_path / "out")
    top = result.results[0]
    assert top.status is DocumentStatus.ANONYMIZED, [i.message for i in top.issues]
    assert top.output_path is not None
    assert top.output_path.read_text(encoding="utf-8") == f"[PERSONNE_001] : {CANARY_EMAIL}\n"


class _OutputOnlyStatistical(Detector):
    """Statistical detector that only fires on produced text (contains a tag)."""

    name = "statistique"
    deterministic = False

    def detect(self, segments: Sequence[Segment], document_id: str) -> list[Entity]:
        out = []
        for s in segments:
            i = s.text.find("Relu")
            if i >= 0 and "[PERSONNE_" in s.text:
                out.append(Entity("PERSONNE", "Relu", document_id, s.id, i, i + 4, 0.9, self.name))
        return out


def test_possible_residue_means_review_not_quarantine(
    tmp_path: Path, canary_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry.load_plugins()
    monkeypatch.setitem(registry._detectors, "statistique", _OutputOnlyStatistical)
    src = tmp_path / "in"
    src.mkdir()
    (src / "a.txt").write_text(f"Relu par {CANARY_PERSON}.\n", encoding="utf-8")
    top = Pipeline(canary_config).run([src], tmp_path / "out").results[0]
    assert top.status is DocumentStatus.NEEDS_REVIEW
    assert top.possible_residue_count == 1 and top.residue_count == 0
    assert top.output_path is not None and not is_quarantined(top, tmp_path / "out")
    assert "verify.possible_residue" in [i.code for i in top.issues]


def _crash_in_worker(path: Path, *rest: object) -> object:
    """Simulates native crashes: the first item run kills its worker (and the pool);
    the "poison" item kills every worker it runs in."""
    import os

    marker = path.parent / "premier_plantage"
    if path.stem == "poison" or not marker.exists():
        marker.write_text("x", encoding="utf-8")
        os._exit(3)
    return path.name, os.getpid()


def test_broken_pool_retries_in_fresh_workers_never_inline(
    tmp_path: Path, canary_config: Config
) -> None:
    import os

    canary_config.workers = 2
    pipeline = Pipeline(canary_config)
    jobs = [(tmp_path / f"{n}.txt",) for n in ("a", "poison", "b", "c")]
    events: list[ProgressEvent] = []
    results = pipeline._map(
        _crash_in_worker, jobs, "analyse", events.append, None, lambda path: ("echec", 0)
    )
    names = [r[0] for r in results if r is not None]
    assert names == ["a.txt", "echec", "b.txt", "c.txt"]
    assert all(r[1] != os.getpid() for r in results if r is not None and r[0] != "echec")
    assert events[-1].done == 4


def _crash_twice_then_work(path: Path, *rest: object) -> object:
    """A worker dying for a reason outside the item: « instable » kills the shared pool,
    then its first single-worker pool, then works."""
    import os

    if path.stem == "instable":
        runs = path.parent / "essais"
        count = int(runs.read_text(encoding="utf-8")) if runs.exists() else 0
        runs.write_text(str(count + 1), encoding="utf-8")
        if count < 2:
            os._exit(3)
    return path.name, os.getpid()


def test_one_isolated_crash_does_not_fail_an_item(tmp_path: Path, canary_config: Config) -> None:
    canary_config.workers = 2
    jobs = [(tmp_path / f"{n}.txt",) for n in ("a", "instable", "b")]
    results = Pipeline(canary_config)._map(
        _crash_twice_then_work, jobs, "analyse", None, None, lambda path: ("echec", 0)
    )
    assert [r[0] for r in results if r is not None] == ["a.txt", "instable.txt", "b.txt"]


def test_crashing_document_fails_with_anonymized_name(
    tmp_path: Path, canary_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    from anonyma import pipeline as pipeline_module

    inputs = tmp_path / "in"
    inputs.mkdir()
    (inputs / f"{CANARY_PERSON}.txt").write_text(f"Courrier de {CANARY_PERSON}.", "utf-8")
    (inputs / "b.txt").write_text("Bonjour.", "utf-8")
    pipeline = Pipeline(canary_config)
    analysis = pipeline.analyze([inputs])  # inline: only the writing stage is under test
    pipeline.config.workers = 2
    # Writing crashes every worker: the documents fail, nothing runs in this process.
    monkeypatch.setattr(pipeline, "_pool", _always_broken)
    monkeypatch.setattr(pipeline, "_serial", lambda fn, jobs, indices, cancel, done: indices[0])
    try:
        result = pipeline.apply(analysis, tmp_path / "out")
    finally:
        pipeline.close(analysis)
    assert {r.status for r in result.results} == {DocumentStatus.FAILED}
    assert all("uintavalle" not in r.output_name.casefold() for r in result.results)
    assert all(i.code == "worker.crash" for r in result.results for i in r.issues)
    assert not (tmp_path / "out").exists() or not any((tmp_path / "out").iterdir())
    assert pipeline_module.CRASH_MESSAGE


def _always_broken(*args: object, **kwargs: object) -> None:
    from concurrent.futures.process import BrokenProcessPool

    raise BrokenProcessPool("test")


def test_job_title_is_verified_in_its_own_file_only(tmp_path: Path) -> None:
    """A job title found next to a person is not a residue of every other file."""
    from anonyma.resolver import resolve

    inputs = tmp_path / "in"
    inputs.mkdir()
    (inputs / "a.txt").write_text("Mme Vidal, présidente, a signé.\n", encoding="utf-8")
    (inputs / "b.txt").write_text("La présidente du club est absente.\n", encoding="utf-8")
    config = Config(profile=Profile.LIGHT, workers=1)
    pipeline = Pipeline(config)
    analysis = pipeline.analyze([inputs / "a.txt", inputs / "b.txt"])
    try:
        doc_a = next(d for d in analysis.documents if d.name.endswith("a.txt"))
        body = next(s for s in doc_a.segments if "présidente" in s.text)
        start = body.text.index("présidente")
        title = Entity("FONCTION", "présidente", doc_a.id, body.id, start, start + 10, 0.8, "test")
        analysis.entities, analysis.clusters = resolve(
            [*analysis.entities, title], config, analysis.clusters
        )
        result = pipeline.apply(analysis, tmp_path / "out")
    finally:
        pipeline.close(analysis)
    by_name = {r.output_name: r for r in result.results}
    assert by_name["b.txt"].status is DocumentStatus.ANONYMIZED
    assert "présidente" in (tmp_path / "out" / "b.txt").read_text(encoding="utf-8")
    assert "présidente" not in (tmp_path / "out" / "a.txt").read_text(encoding="utf-8")


def test_person_file_name_keeps_one_substitute(tmp_path: Path) -> None:
    folder = tmp_path / "in" / "Ophélie GAUTHIER"
    folder.mkdir(parents=True)
    (folder / "CV Ophélie Gauthier.txt").write_text("Rien.\n", encoding="utf-8")
    pipeline = Pipeline(Config(profile=Profile.LIGHT, workers=1))
    result = pipeline.run([tmp_path / "in"], tmp_path / "out")
    names = [r.output_name for r in result.results]
    assert len(names) == 1
    folder_part, file_part = names[0].split("/")[-2:]
    assert file_part == f"CV {folder_part}.txt"  # same tag, extension intact


# -- file names ------------------------------------------------------------------------


def test_names_are_anonymized_whatever_the_separators(
    tmp_path: Path, canary_config: Config
) -> None:
    src = tmp_path / "in"
    src.mkdir()
    body = f"Courrier de {CANARY_PERSON}.\n"
    for name in (
        "Contrat_zephyrin_QUINTAVALLE_signe.txt",
        "QUINTAVALLE-Zephyrin.txt",
        "zephyrin.quintavalle.txt",
    ):
        (src / name).write_text(body, encoding="utf-8")
    result = Pipeline(canary_config).run([src], tmp_path / "out")
    names = sorted(r.output_name for r in result.results)
    assert all("zephyrin" not in n.casefold() and "uintavalle" not in n.casefold() for n in names)
    assert any(n.endswith("Contrat_[PERSONNE_001]_signe.txt") for n in names), names
    assert {r.status for r in result.results} == {DocumentStatus.ANONYMIZED}


def test_original_name_in_content_is_a_residue(
    tmp_path: Path, canary_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A copy of the original file name left in the output (e.g. a title property)."""
    registry.load_plugins()

    class _NameLeakWriter(TextWriter):
        def write(self, document, plan, destination, ctx):  # type: ignore[no-untyped-def]
            destination.write_text(f"Titre : {Path(document.name).stem}\n", encoding="utf-8")
            return []

    monkeypatch.setitem(registry._writers, "txt", _NameLeakWriter)
    src = tmp_path / "in" / "Projet_Zorvalix_v3.txt"
    src.parent.mkdir()
    src.write_text("Rien de personnel.\n", encoding="utf-8")
    config = Config(profile=Profile.LIGHT, workers=1, neutral_filenames=True)
    result = Pipeline(config).run([src], tmp_path / "out")
    top = result.results[0]
    assert top.output_name == "document_0001.txt"
    assert top.status is DocumentStatus.RESIDUE
    assert top.verification is not None
    assert "nom_origine" in {r.detector for r in top.verification.residues}


def test_neutral_filenames(tmp_path: Path, canary_config: Config) -> None:
    import zipfile

    src = tmp_path / f"Dossier {CANARY_PERSON}"
    (src / "sous").mkdir(parents=True)
    (src / f"CV {CANARY_PERSON}.txt").write_text(f"{CANARY_PERSON}\n", encoding="utf-8")
    (src / "sous" / "note.txt").write_text("Note.\n", encoding="utf-8")
    with zipfile.ZipFile(src / "pieces.zip", "w") as archive:
        archive.writestr(f"dir/{CANARY_PERSON}.txt", "Bonjour.\n")
        archive.writestr("autre.txt", "Rien.\n")
    canary_config.neutral_filenames = True
    canary_config.regime = Regime.PSEUDONYMIZATION
    store = InMemoryMappingStore()
    result = Pipeline(canary_config, store=store).run([src], tmp_path / "out")
    names = [r.output_name for r in result.results]
    assert names == [
        "dossier_0001/document_0001.txt",
        "dossier_0001/document_0002.zip",
        "dossier_0001/dossier_0002/document_0003.txt",
    ]
    archive_result = result.results[1]
    assert [c.output_name for c in archive_result.children] == [
        "piece_jointe_0001.txt",
        "piece_jointe_0002.txt",
    ]
    assert archive_result.output_path is not None
    with zipfile.ZipFile(archive_result.output_path) as archive:
        assert sorted(archive.namelist()) == ["piece_jointe_0001.txt", "piece_jointe_0002.txt"]
    assert {r.status for r in result.results} == {DocumentStatus.ANONYMIZED}
    mapping = {e.output_name: e.original for e in store.filenames()}
    assert (
        mapping["dossier_0001/document_0001.txt"]
        == f"Dossier {CANARY_PERSON}/CV {CANARY_PERSON}.txt"
    )
    assert (
        mapping["dossier_0001/document_0002.zip/piece_jointe_0001.txt"]
        == f"Dossier {CANARY_PERSON}/pieces.zip/dir/{CANARY_PERSON}.txt"
    )
    assert len({e.batch_id for e in store.filenames()}) == 1


def test_neutral_names_not_stored_in_anonymization(tmp_path: Path, canary_config: Config) -> None:
    src = tmp_path / "in" / f"{CANARY_PERSON}.txt"
    src.parent.mkdir()
    src.write_text("x\n", encoding="utf-8")
    canary_config.neutral_filenames = True
    store = InMemoryMappingStore()
    result = Pipeline(canary_config, store=store).run([src], tmp_path / "out")
    assert result.results[0].output_name == "document_0001.txt"
    assert store.filenames() == []


def test_ocr_gap_at_analysis_is_never_anonymized(
    tmp_path: Path, canary_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    from anonyma.extractors.text import TextExtractor

    registry.load_plugins()

    class _GapExtractor(TextExtractor):
        def extract(self, source, document, ctx):  # type: ignore[no-untyped-def]
            super().extract(source, document, ctx)
            document.add_issue(Severity.WARNING, "pdf.ocr_unavailable", "OCR indisponible.")

    monkeypatch.setitem(registry._extractors, "txt", _GapExtractor)
    src = tmp_path / "in" / "a.txt"
    src.parent.mkdir()
    src.write_text("Rien.\n", encoding="utf-8")
    top = Pipeline(canary_config).run([src], tmp_path / "out").results[0]
    assert top.status is DocumentStatus.NEEDS_REVIEW
    assert "analyse.ocr_incomplete" in {i.code for i in top.issues}


def test_ocr_gap_at_verification_goes_to_quarantine(
    tmp_path: Path, canary_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    from anonyma import pipeline as pipeline_module
    from anonyma.extractors.text import TextExtractor

    registry.load_plugins()
    verifying = {"on": False}

    class _GapExtractor(TextExtractor):
        def extract(self, source, document, ctx):  # type: ignore[no-untyped-def]
            super().extract(source, document, ctx)
            if verifying["on"]:
                document.add_issue(Severity.ERROR, "ocr.failed", "Échec de l'OCR.")

    original = pipeline_module._verify_output

    def verify(*args, **kwargs):  # type: ignore[no-untyped-def]
        verifying["on"] = True
        try:
            return original(*args, **kwargs)
        finally:
            verifying["on"] = False

    monkeypatch.setitem(registry._extractors, "txt", _GapExtractor)
    monkeypatch.setattr(pipeline_module, "_verify_output", verify)
    src = tmp_path / "in" / "a.txt"
    src.parent.mkdir()
    src.write_text(f"{CANARY_PERSON}\n", encoding="utf-8")
    top = Pipeline(canary_config).run([src], tmp_path / "out").results[0]
    assert top.status is DocumentStatus.NEEDS_REVIEW
    assert is_quarantined(top, tmp_path / "out")
    assert "verify.incomplete" in {i.code for i in top.issues}
    assert top.verification is not None and top.verification.unread == 1


def test_nothing_reaches_the_output_folder_before_verification(
    tmp_path: Path, canary_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    from anonyma import pipeline as pipeline_module

    original = pipeline_module._verify_output
    seen: list[list[Path]] = []

    def verify(path, name, job):  # type: ignore[no-untyped-def]
        seen.append(sorted(job.output_dir.rglob("*")))
        return original(path, name, job)

    monkeypatch.setattr(pipeline_module, "_verify_output", verify)
    src = tmp_path / "in" / "a.txt"
    src.parent.mkdir()
    src.write_text(f"{CANARY_PERSON}\n", encoding="utf-8")
    top = Pipeline(canary_config).run([src], tmp_path / "out").results[0]
    assert seen == [[]]
    assert top.status is DocumentStatus.ANONYMIZED
    assert top.output_path is not None and top.output_path.parent == (tmp_path / "out").resolve()


def test_failure_while_placing_leaves_no_output(
    tmp_path: Path, canary_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    from anonyma import pipeline as pipeline_module

    def broken(*_args: object) -> Path:
        raise OSError(f"disque plein pour {CANARY_PERSON}")

    monkeypatch.setattr(pipeline_module, "_place", broken)
    src = tmp_path / "in" / "a.txt"
    src.parent.mkdir()
    src.write_text(f"{CANARY_PERSON}\n", encoding="utf-8")
    top = Pipeline(canary_config).run([src], tmp_path / "out").results[0]
    assert top.status is DocumentStatus.FAILED
    assert top.output_path is None
    assert not list((tmp_path / "out").rglob("*.txt"))
    assert CANARY_PERSON not in " ".join(i.message for i in top.issues)


def test_same_output_name_is_never_overwritten(tmp_path: Path) -> None:
    from anonyma.pipeline import _place

    out = tmp_path / "out"
    for content in ("un", "deux"):
        written = tmp_path / content / "x.txt"
        written.parent.mkdir()
        written.write_text(content, encoding="utf-8")
        _place(written, out, "x.txt")
    folder = tmp_path / "dossier"
    (folder / "sous").mkdir(parents=True)
    (folder / "sous" / "m.eml").write_text("trois", encoding="utf-8")
    placed = _place(folder, out, "x.txt")
    assert sorted(p.name for p in out.iterdir()) == ["x (2).txt", "x (3).txt", "x.txt"]
    assert (out / "x.txt").read_text(encoding="utf-8") == "un"
    assert (placed / "sous" / "m.eml").read_text(encoding="utf-8") == "trois"
    assert not folder.exists()


def test_place_copies_across_volumes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from anonyma import pipeline as pipeline_module

    def cross_device(*_args: object) -> None:
        raise OSError("cross-device link")

    monkeypatch.setattr(pipeline_module.os, "replace", cross_device)
    written = tmp_path / "w" / "x.txt"
    written.parent.mkdir()
    written.write_text("un", encoding="utf-8")
    placed = pipeline_module._place(written, tmp_path / "out", "x.txt")
    assert placed.read_text(encoding="utf-8") == "un" and not written.exists()


def test_quarantine_folder_must_not_overlap_the_inputs(tmp_path: Path) -> None:
    assert quarantine_dir(tmp_path / "sortie") == tmp_path / "sortie_quarantaine"
    inputs = tmp_path / "sortie_quarantaine"
    inputs.mkdir()
    with pytest.raises(OutputDirError):
        validate_output_dir([inputs], tmp_path / "sortie")


def _dir_link(link: Path, target: Path) -> None:
    """A junction on Windows (no privilege needed), a symbolic link elsewhere."""
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


def test_links_in_input_folders_are_never_followed(tmp_path: Path, canary_config: Config) -> None:
    outside = tmp_path / f"Dossier {CANARY_PERSON}"
    outside.mkdir()
    (outside / "secret.txt").write_text(f"Courrier de {CANARY_PERSON}.", encoding="utf-8")
    folder = tmp_path / "entree"
    (folder / "sous").mkdir(parents=True)
    (folder / "sous" / "a.txt").write_text("Bonjour.", encoding="utf-8")
    _dir_link(folder / "lien", outside)
    with contextlib.suppress(OSError):  # file links need a privilege on Windows
        (folder / "fichier.txt").symlink_to(outside / "secret.txt")

    files, links = walk_inputs([folder])
    assert [n for _, n in files] == ["entree/sous/a.txt"]
    assert "entree/lien" in [n for _, n in links]
    result = Pipeline(canary_config).run([folder], tmp_path / "out")
    by_status = {r.status for r in result.results}
    assert DocumentStatus.UNSUPPORTED in by_status
    skipped = [r for r in result.results if r.status is DocumentStatus.UNSUPPORTED]
    for r in skipped:
        assert [i.code for i in r.issues] == ["input.link"]
        assert "uintavalle" not in r.output_name.casefold()
        assert all("uintavalle" not in i.message.casefold() for i in r.issues)
    written = [p for p in (tmp_path / "out").rglob("*") if p.is_file()]
    assert [p.name for p in written] == ["a.txt"]


class _CancelInsideItem(CancelToken):
    """Not cancelled between items, but cancelled once an item is being read."""

    def check(self) -> None:
        from concurrent.futures import CancelledError

        raise CancelledError()


def test_inline_extraction_sees_the_cancel_token(tmp_path: Path, canary_config: Config) -> None:
    import zipfile

    inputs = tmp_path / "in"
    inputs.mkdir()
    with zipfile.ZipFile(inputs / "a.zip", "w") as archive:
        archive.writestr("x.txt", "Bonjour.")
    pipeline = Pipeline(canary_config)
    analysis = pipeline.analyze([inputs / "a.zip"], cancel=_CancelInsideItem())
    pipeline.close(analysis)
    assert [d.status for d in analysis.documents] == [DocumentStatus.CANCELLED]


@pytest.mark.parametrize(
    "name", ["vide.docx", "vide.xlsx", "vide.pptx", "vide.odt", "vide.zip", "vide.pdf", "vide.msg"]
)
def test_empty_binary_file_is_reported_as_empty(
    tmp_path: Path, canary_config: Config, name: str
) -> None:
    src = tmp_path / "in"
    src.mkdir()
    (src / name).write_bytes(b"")
    top = Pipeline(canary_config).run([src / name], tmp_path / "out").results[0]
    assert top.status is DocumentStatus.FAILED
    assert [i.code for i in top.issues if i.severity is Severity.ERROR] == ["extract.empty"]
    [message] = [i.message for i in top.issues if i.code == "extract.empty"]
    assert message.startswith("Fichier vide") and "Error" not in message
    assert not (tmp_path / "out" / name).exists()


class _AcrossFoldersDetector(Detector):
    """Reads ``Exports/sous`` (two folder names) as one organisation."""

    name = "dossiers"

    def detect(self, segments: Sequence[Segment], document_id: str) -> list[Entity]:
        out = []
        for s in segments:
            i = s.text.find("Exports/sous")
            if i >= 0:
                end = i + len("Exports/sous")
                out.append(Entity("ORGANISATION", "", document_id, s.id, i, end, 0.9, self.name))
        return out


def test_entity_across_folders_keeps_the_tree_depth(
    tmp_path: Path, canary_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry.load_plugins()
    monkeypatch.setitem(registry._detectors, "dossiers", _AcrossFoldersDetector)
    folder = tmp_path / "Exports" / "sous"
    folder.mkdir(parents=True)
    (folder / "a.txt").write_text("Rien.\n", encoding="utf-8")
    (tmp_path / "Exports" / "b.txt").write_text("Rien.\n", encoding="utf-8")
    result = Pipeline(canary_config).run([tmp_path / "Exports"], tmp_path / "out")
    names = sorted(r.output_name for r in result.results)
    # One substitute per component: never ``[ORGANISATION_001]/a.txt`` (folders merged),
    # and the input folder has one output name for the whole batch.
    assert names == [
        "[ORGANISATION_001]/[ORGANISATION_001]/a.txt",
        "[ORGANISATION_001]/b.txt",
    ]
    written = sorted(
        p.relative_to(tmp_path / "out").as_posix() for p in (tmp_path / "out").rglob("*.txt")
    )
    assert written == names


def test_worker_logs_go_to_the_log_file_not_stderr(
    tmp_path: Path, portable_home: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    src = tmp_path / "in"
    src.mkdir()
    (src / "abime.docx").write_bytes(b"PK\x03\x04 pas un document")
    for i in range(2):
        (src / f"{i}.txt").write_text(f"{CANARY_PERSON} {i}\n", encoding="utf-8")
    mask = [{"value": CANARY_PERSON, "type": "PERSONNE"}]
    config = Config(profile=Profile.LIGHT, workers=2, always_mask=mask)
    result = Pipeline(config).run([src], tmp_path / "out")
    statuses = {r.output_name.split("/")[-1]: r.status for r in result.results}
    assert statuses.pop("abime.docx") is DocumentStatus.FAILED
    # The other documents went through the worker pool, anonymized as inline.
    assert set(statuses.values()) == {DocumentStatus.ANONYMIZED} and len(statuses) == 2
    for r in result.results:
        if r.output_path is not None:
            assert "[PERSONNE_001]" in r.output_path.read_text(encoding="utf-8")
    captured = capfd.readouterr()
    assert "extraction failed" not in captured.err + captured.out
    log = (portable_home / "data" / "logs" / "anonyma.log").read_text(encoding="utf-8")
    assert "extraction failed" in log


def _linger() -> int:
    """A job that leaves a non-daemon thread behind: its worker can never exit."""
    import threading
    import time

    threading.Thread(target=time.sleep, args=(3600,)).start()
    return 1


def test_pool_shutdown_is_bounded() -> None:
    import time
    from concurrent.futures import ProcessPoolExecutor

    from anonyma.pipeline import _worker_init, shutdown_pool

    pool = ProcessPoolExecutor(1, initializer=_worker_init)
    assert pool.submit(_linger).result() == 1
    processes = list(pool._processes.values())
    start = time.monotonic()
    shutdown_pool(pool, timeout=1.0)
    assert time.monotonic() - start < 20
    assert not any(p.is_alive() for p in processes)


def test_full_profile_without_its_model_is_not_single_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry.load_plugins()
    llm = registry.detector_classes()["llm"]
    monkeypatch.setattr(llm, "is_available", classmethod(lambda cls, config: False))
    monkeypatch.setattr("os.cpu_count", lambda: 4)
    pipeline = Pipeline(Config(profile=Profile.FULL))
    assert pipeline.config.profile is Profile.FULL  # still reported as chosen
    assert pipeline.detection_profile is not Profile.FULL
    assert pipeline._workers(10) > 1


def test_neutral_names_drop_empty_archive_folders(tmp_path: Path, canary_config: Config) -> None:
    import zipfile

    src = tmp_path / "in" / "a.zip"
    src.parent.mkdir()
    with zipfile.ZipFile(src, "w") as zf:
        zf.writestr(f"Dossier {CANARY_PERSON}/", b"")
        zf.writestr("notes/a.txt", b"Rien.")
    canary_config.neutral_filenames = True
    top = Pipeline(canary_config).run([src], tmp_path / "out").results[0]
    assert top.status is DocumentStatus.ANONYMIZED
    assert top.output_path is not None
    with zipfile.ZipFile(top.output_path) as zf:
        assert zf.namelist() == ["piece_jointe_0001.txt"]


def test_worker_stderr_goes_to_the_log_as_exception_type_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from anonyma.pipeline import _StderrToLog

    sink = _StderrToLog()
    with caplog.at_level("WARNING", logger="anonyma"):
        sink.write("Process SpawnProcess-7:\nTraceback (most recent call last):\n")
        sink.write('  File "queues.py", line 104, in get\n')
        sink.write("OSError: [WinError 6] secret-value\n")
    assert [r.getMessage() for r in caplog.records] == ["worker process error: OSError"]
