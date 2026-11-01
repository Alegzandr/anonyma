"""Corrective pass: what the verification still finds is masked and its files rewritten,
without asking anyone and without changing the substitutes already written."""

from __future__ import annotations

import copy
import re
from collections.abc import Sequence
from pathlib import Path

import pytest

from anonyma import registry
from anonyma.config import Config
from anonyma.models import (
    Detector,
    DocumentStatus,
    Entity,
    EntityType,
    ReplacementMode,
    Segment,
    Severity,
)
from anonyma.pipeline import CORRECTED_ISSUE, Pipeline
from anonyma.replacer import Replacer
from anonyma.resolver import resolve
from anonyma.verifier import _outside_substitutes
from tests.canary import CANARY_PERSON

MISSED = "Tiburce"
_TAG = re.compile(r"\[[A-Z_]+_\d{3}\]")


class _ContextDetector(Detector):
    """A statistical model that only sees « Tiburce » once a substitute stands next to
    it, as GLiNER does with a lone first name next to a tag."""

    name = "contexte"
    deterministic = False

    def detect(self, segments: Sequence[Segment], document_id: str) -> list[Entity]:
        out = []
        for s in segments:
            i = s.text.find(MISSED)
            if i >= 0 and _TAG.search(s.text):
                out.append(
                    Entity(EntityType.PERSON, MISSED, document_id, s.id, i, i + 7, 0.5, self.name)
                )
        return out


@pytest.fixture
def context_detector(monkeypatch: pytest.MonkeyPatch) -> None:
    registry.load_plugins()
    monkeypatch.setitem(registry._detectors, "contexte", _ContextDetector)


def test_possible_residue_is_masked_and_only_its_file_rewritten(
    tmp_path: Path,
    canary_config: Config,
    context_detector: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    src = tmp_path / "in"
    src.mkdir()
    (src / "a.txt").write_text(f"Contact : {CANARY_PERSON}, puis {MISSED}.\n", encoding="utf-8")
    (src / "b.txt").write_text(f"Réunion avec {CANARY_PERSON}.\n", encoding="utf-8")
    pipeline = Pipeline(canary_config)
    written: list[list[int]] = []
    original_write = Pipeline._write

    def spy(self: Pipeline, plan, indices, progress, cancel):  # type: ignore[no-untyped-def]
        written.append(list(indices))
        return original_write(self, plan, indices, progress, cancel)

    monkeypatch.setattr(Pipeline, "_write", spy)
    analysis = pipeline.analyze([src])
    try:
        assert not any(e.value == MISSED for e in analysis.entities)  # missed at analysis
        result = pipeline.apply(analysis, tmp_path / "out")
    finally:
        pipeline.close(analysis)

    first, second = result.results
    names = [d.name for d in analysis.documents]
    assert written == [[0, 1], [names.index(next(n for n in names if n.endswith("a.txt")))]]
    by_name = {Path(r.output_name).name: r for r in result.results}
    a, b = by_name["a.txt"], by_name["b.txt"]
    assert a.status is DocumentStatus.ANONYMIZED and b.status is DocumentStatus.ANONYMIZED
    assert a.output_path is not None and b.output_path is not None
    text_a = a.output_path.read_text(encoding="utf-8")
    text_b = b.output_path.read_text(encoding="utf-8")
    assert MISSED not in text_a and CANARY_PERSON not in text_a
    assert a.corrected_count >= 1 and b.corrected_count == 0
    [info] = [i for i in a.issues if i.code == CORRECTED_ISSUE]
    assert info.severity is Severity.INFO and MISSED not in info.message
    assert not [i for i in b.issues if i.code == CORRECTED_ISSUE]
    # The untouched file keeps the substitute the rewritten one still uses.
    tag = _TAG.findall(text_b)[0]
    assert tag in text_a
    # Only the rewritten outputs exist: no leftover of the first pass.
    assert sorted(p.name for p in (tmp_path / "out").rglob("*.txt")) == ["a.txt", "b.txt"]
    assert not (tmp_path / "out_quarantaine").exists()
    assert {first.document_id, second.document_id} == {d.id for d in analysis.documents}


def _people(*names: str, start: int = 0) -> list[Entity]:
    return [
        Entity(EntityType.PERSON, n, f"d{start + i}", "s", 0, len(n), 0.9, "t")
        for i, n in enumerate(names)
    ]


def test_kept_table_pins_tags_and_numbers_new_clusters_after() -> None:
    config = Config()
    kept, clusters = resolve(_people("Eulalie Vermandois", "Octave Brunel", start=1), config)
    replacer = Replacer(config)
    before = replacer.assign(clusters, kept)
    old = {e.value: before.substitute_for(e) for e in kept}
    assert sorted(old.values()) == ["[PERSONNE_001]", "[PERSONNE_002]"]

    new = _people("Ysaline Tournesol")
    kept2, clusters2 = resolve([*kept, *new], config, copy.deepcopy(clusters))
    order = [e for e in kept2 if e.value == "Ysaline Tournesol"] + [
        e for e in kept2 if e.value != "Ysaline Tournesol"
    ]
    # Without ``keep`` the newcomer, first in the batch, would take [PERSONNE_001].
    fresh = Replacer(config).assign(clusters2, order)
    assert {e.value: fresh.substitute_for(e) for e in order}["Ysaline Tournesol"] == (
        "[PERSONNE_001]"
    )
    after = replacer.assign(clusters2, order, keep=before)
    tags = {e.value: after.substitute_for(e) for e in order}
    assert {v: tags[v] for v in old} == old
    assert tags["Ysaline Tournesol"] == "[PERSONNE_003]"


def test_kept_table_pins_realistic_substitutes() -> None:
    config = Config(mode=ReplacementMode.REALISTIC)
    kept, clusters = resolve(_people("Eulalie Vermandois", "Octave Brunel", start=1), config)
    replacer = Replacer(config)
    before = replacer.assign(clusters, kept)
    kept2, clusters2 = resolve(
        [*kept, *_people("Ysaline Tournesol")], config, copy.deepcopy(clusters)
    )
    after = replacer.assign(clusters2, kept2, keep=before)
    for cid, substitute in before.by_cluster.items():
        assert after.by_cluster[cid] == substitute
    new_id = next(cid for cid in after.by_cluster if cid not in before.by_cluster)
    assert after.by_cluster[new_id] not in before.by_cluster.values()


@pytest.mark.parametrize(
    ("text", "start", "end", "spans", "expected"),
    [
        ("[PERSONNE_001] Sophie", 0, 21, [(0, 14)], (True, ["Sophie"])),
        ("M. [PERSONNE_001]", 0, 17, [(3, 17)], (True, [])),
        ("Projets IRVE [EMAIL_001]", 0, 24, [(13, 24)], (True, ["Projets IRVE"])),
        ("Isère", 0, 5, [], (False, [])),
    ],
)
def test_outside_substitutes(
    text: str,
    start: int,
    end: int,
    spans: list[tuple[int, int]],
    expected: tuple[bool, list[str]],
) -> None:
    assert _outside_substitutes(text, start, end, spans) == expected
