from __future__ import annotations

import pickle
from pathlib import Path

import pytest

from anonyma.models import (
    ArchiveBombError,
    BBox,
    Document,
    Entity,
    ExpansionBudget,
    Replacement,
    ReplacementPlan,
    Segment,
    SegmentContext,
    SegmentKind,
    apply_replacements,
)


def _rep(start: int, end: int, sub: str) -> Replacement:
    return Replacement(start, end, "x", sub, "PERSONNE", "c1")


def test_apply_replacements() -> None:
    text = "Bonjour Jean Dupont, voici Marie."
    reps = [_rep(8, 19, "[PERSONNE_001]"), _rep(27, 32, "[PERSONNE_002]")]
    assert apply_replacements(text, reps) == "Bonjour [PERSONNE_001], voici [PERSONNE_002]."


def test_apply_replacements_rejects_overlap() -> None:
    with pytest.raises(ValueError):
        apply_replacements("abcdef", [_rep(0, 3, "x"), _rep(2, 4, "y")])


def test_sensitive_values_not_in_repr() -> None:
    seg = Segment("s", "Jean Dupont", (0,), SegmentContext(SegmentKind.BODY))
    ent = Entity("PERSONNE", "Jean Dupont", "d", "s", 0, 11, 1.0, "t")
    plan = ReplacementPlan("d", {"s": [_rep(0, 11, "[PERSONNE_001]")]})
    doc = Document(source=Path("x"), name="Jean Dupont.txt", format="txt", segments=[seg])
    for obj in (seg, ent, plan, doc):
        assert "Dupont" not in repr(obj)


def test_types_are_picklable() -> None:
    seg = Segment("s", "t", (0, "a"), SegmentContext(SegmentKind.OCR, bbox=BBox(0, 0, 1, 1)))
    doc = Document(source=Path("x"), name="n", format="txt", segments=[seg])
    doc.children.append(Document(source=Path("y"), name="m", format="txt"))
    clone = pickle.loads(pickle.dumps(doc))
    assert clone.segments[0].context.bbox == BBox(0, 0, 1, 1)
    assert [d.name for d in clone.walk()] == ["n", "m"]


def test_span_bbox() -> None:
    boxes = tuple(BBox(i, 0, i + 1, 10) for i in range(5))
    seg = Segment("s", "abcde", (), SegmentContext(SegmentKind.OCR), char_boxes=boxes)
    assert seg.span_bbox(1, 4) == BBox(1, 0, 4, 10)


def test_budget_limits() -> None:
    budget = ExpansionBudget(max_total_bytes=100, max_files=2, max_ratio=10, max_depth=3)
    budget.consume(50)
    with pytest.raises(ArchiveBombError):
        budget.consume(1000, compressed=10)
    with pytest.raises(ArchiveBombError):
        budget.consume(1, depth=4)
    budget2 = ExpansionBudget(max_total_bytes=100, max_files=1, max_ratio=10, max_depth=3)
    budget2.consume(1)
    with pytest.raises(ArchiveBombError):
        budget2.consume(1)
