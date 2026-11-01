"""PDF writer layout: painted boxes spare the neighbouring lines, substitutes stay
readable (synthetic documents only)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pymupdf
import pytest

from anonyma.config import Config
from anonyma.models import (
    BBox,
    DocumentResult,
    DocumentStatus,
    Replacement,
    ReplacementMode,
    Segment,
    SegmentContext,
    SegmentKind,
)
from anonyma.pipeline import Pipeline
from anonyma.writers.pdf import (
    HARD_SIZE_RATIO,
    MIN_CONDENSE,
    MIN_SIZE_RATIO,
    TextLayout,
    layout_text,
    line_parts,
)

#: Layout is the subject here, not OCR: rendered pages are read by a blank engine.
pytestmark = pytest.mark.usefixtures("blank_ocr")

SIZE = 10.0
LEADING = 11.5  # tight: glyph boxes (13.7 pt) overlap the neighbouring lines
X0, Y0 = 20.0, 30.0
ORG = "Vexmora Signe"
SHORT = "Zorvik"
UPPER = "ZORVAK"
TINY = "VKM"
LINE_1 = "En face à face, à distance ou en ligne, signez avec"
LINE_3 = "Une troisième ligne pour voir le dessous du texte."


def _pdf(path: Path, lines: list[str], *, border: float | None = None) -> None:
    doc = pymupdf.open()
    page = doc.new_page(width=300, height=120)
    for index, line in enumerate(lines):
        page.insert_text((X0, Y0 + index * LEADING), line, fontsize=SIZE, fontname="helv")
    if border is not None:  # a table cell's right edge
        page.draw_line((border, 10), (border, 80), color=(0, 0, 0), width=0.5)
    doc.save(path)


def _run(source: Path, out: Path, config: Config, *terms: tuple[str, str]) -> DocumentResult:
    config.always_mask.extend({"value": value, "type": kind} for value, kind in terms)
    pipeline = Pipeline(config)
    analysis = pipeline.analyze([source])
    try:
        result = pipeline.apply(analysis, out)
    finally:
        pipeline.close(analysis)
    document = result.results[0]
    assert document.status is DocumentStatus.ANONYMIZED
    assert document.output_path is not None
    return document


def _pixels(path: Path, top: float, bottom: float) -> np.ndarray[Any, Any]:
    with pymupdf.open(path) as doc:
        clip = pymupdf.Rect(0, top, doc[0].rect.width, bottom)
        pix = doc[0].get_pixmap(dpi=288, clip=clip, alpha=False, colorspace=pymupdf.csGRAY)
        return np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)


def _spans(path: Path) -> list[dict[str, Any]]:
    with pymupdf.open(path) as doc:
        data = doc[0].get_text("dict")
    return [s for b in data["blocks"] for line in b.get("lines", []) for s in line["spans"]]


def _tag(path: Path, tag: str) -> dict[str, Any]:
    spans = [s for s in _spans(path) if s["text"].strip() == tag]
    assert len(spans) == 1, "the tag is written whole, once"
    return spans[0]


def _height(span: dict[str, Any]) -> float:
    """Vertical font size of a span (its reported size mixes in the condensing)."""
    box = pymupdf.Rect(span["bbox"])
    return float(box.height / (span["ascender"] - span["descender"]))


@pytest.mark.parametrize("mode", [ReplacementMode.TAG, ReplacementMode.BLACKOUT])
def test_box_spares_the_neighbouring_lines(
    tmp_path: Path, canary_config: Config, mode: ReplacementMode
) -> None:
    source = tmp_path / "paragraphe.pdf"
    _pdf(source, [LINE_1, f"{ORG} votre contrat aujourd'hui.", LINE_3])
    config = replace(canary_config, mode=mode)
    result = _run(source, tmp_path / "out", config, (ORG, "ORGANISATION"))
    output = result.output_path
    assert output is not None
    baseline_2 = Y0 + LEADING
    # Line 1 down to its descenders, line 3 from its accents: pixel for pixel.
    above = (Y0 - SIZE, Y0 + 0.24 * SIZE)
    below = (baseline_2 + 0.3 * SIZE, Y0 + 3 * LEADING)
    for top, bottom in (above, below):
        assert np.array_equal(_pixels(source, top, bottom), _pixels(output, top, bottom))
    with pymupdf.open(output) as doc:
        text = doc[0].get_text()
    assert LINE_1 in text and LINE_3 in text
    # The original glyphs are gone from the content stream, not only covered.
    assert "Vexmora" not in text and "Signe" not in text
    # The replaced word itself is covered by the box or the tag.
    word = (baseline_2 - 0.5 * SIZE, baseline_2)
    assert not np.array_equal(_pixels(source, *word), _pixels(output, *word))


def _next_word(path: Path, word: str) -> pymupdf.Rect:
    with pymupdf.open(path) as doc:
        return doc[0].search_for(word)[0]


def test_tag_keeps_a_readable_size(tmp_path: Path, canary_config: Config) -> None:
    source = tmp_path / "court.pdf"
    _pdf(source, [LINE_1, f"Contrat de {UPPER} aujourd'hui signé.", LINE_3])
    output = _run(source, tmp_path / "out", canary_config, (UPPER, "PERSONNE")).output_path
    assert output is not None
    tag = _tag(output, "[PERSONNE_001]")
    assert _height(tag) >= MIN_SIZE_RATIO * SIZE - 0.05
    # Room was found without running over the next word, which is intact.
    assert tag["bbox"][2] <= _next_word(output, "aujourd").x0
    assert "aujourd'hui signé." in "".join(s["text"] for s in _spans(output))


def test_tag_shrinks_rather_than_hiding_words(tmp_path: Path, canary_config: Config) -> None:
    source = tmp_path / "plus_court.pdf"
    _pdf(source, [LINE_1, f"Contrat de {SHORT} aujourd'hui.", LINE_3])
    output = _run(source, tmp_path / "out", canary_config, (SHORT, "PERSONNE")).output_path
    assert output is not None
    tag = _tag(output, "[PERSONNE_001]")
    assert HARD_SIZE_RATIO * SIZE - 0.05 <= _height(tag) < MIN_SIZE_RATIO * SIZE
    assert tag["bbox"][2] <= _next_word(output, "aujourd").x0
    original = _pixels(source, Y0 + 2, Y0 + LEADING + 3)
    after = _pixels(output, Y0 + 2, Y0 + LEADING + 3)
    start = int(_next_word(source, "aujourd").x0 * 4)  # 288 dpi
    assert np.array_equal(original[:, start:], after[:, start:])


def test_tag_overflows_whole_glyphs_as_a_last_resort(tmp_path: Path, canary_config: Config) -> None:
    source = tmp_path / "sigle.pdf"
    _pdf(source, [LINE_1, f"Le {TINY} signe aujourd'hui.", LINE_3])
    output = _run(source, tmp_path / "out", canary_config, (TINY, "ORGANISATION")).output_path
    assert output is not None
    tag = _tag(output, "[ORGANISATION_001]")
    assert _height(tag) >= HARD_SIZE_RATIO * SIZE - 0.05
    # The words it runs into stay in the text layer (only their look is covered).
    text = "".join(s["text"] for s in _spans(output))
    assert "signe aujourd'hui." in text


def test_tag_at_line_end_extends_into_the_margin(tmp_path: Path, canary_config: Config) -> None:
    source = tmp_path / "marge.pdf"
    _pdf(source, [LINE_1, f"Le contrat est signé par {TINY}", LINE_3])
    output = _run(source, tmp_path / "out", canary_config, (TINY, "ORGANISATION")).output_path
    assert output is not None
    tag = _tag(output, "[ORGANISATION_001]")
    assert _height(tag) == pytest.approx(SIZE, abs=0.05)  # room enough: original size
    assert tag["bbox"][2] <= 300


def test_tag_stops_at_a_cell_border(tmp_path: Path, canary_config: Config) -> None:
    source = tmp_path / "cellule.pdf"
    line = f"Titulaire : {SHORT}"
    border = X0 + pymupdf.get_text_length(line, fontname="helv", fontsize=SIZE) + 8
    _pdf(source, [LINE_1, line, LINE_3], border=border)
    output = _run(source, tmp_path / "out", canary_config, (SHORT, "PERSONNE")).output_path
    assert output is not None
    tag = _tag(output, "[PERSONNE_001]")
    assert tag["bbox"][2] <= border
    assert _height(tag) >= MIN_SIZE_RATIO * SIZE - 0.05


def test_layout_tiers() -> None:
    rect = pymupdf.Rect(0, 0, 15, 13.74)
    tag = "[ORGANISATION_001]"  # 97 pt wide at 10 pt

    def layout(room: float, *, overflow: bool = True) -> TextLayout:
        result = layout_text(rect, tag, size=SIZE, baseline=10.75, room=room, overflow=overflow)
        assert result is not None
        return result

    roomy = layout(200)
    assert (roomy.size, roomy.scale) == (SIZE, 1.0)
    balanced = layout(60)  # shrunk and condensed alike, above the floor
    assert MIN_SIZE_RATIO * SIZE < balanced.size < SIZE
    assert balanced.scale == pytest.approx(balanced.size / SIZE)
    assert balanced.width == pytest.approx(60)
    smaller = layout(40)  # below the readable floor, still inside the room
    assert HARD_SIZE_RATIO * SIZE <= smaller.size < MIN_SIZE_RATIO * SIZE
    assert smaller.scale == pytest.approx(MIN_CONDENSE)
    assert smaller.width == pytest.approx(40)
    overflowing = layout(15)  # extends rather than shrinking further
    assert overflowing.size == pytest.approx(HARD_SIZE_RATIO * SIZE)
    assert overflowing.width > 15
    inside = layout(15, overflow=False)  # OCR boxes: stays within its box
    assert inside.width <= rect.width + 1e-6


def test_value_cut_by_a_line_break_is_painted_on_each_line() -> None:
    """A number cut at a line end, its end at the next line start: each part of the
    substitute goes where its part of the value was, not after the first line."""
    text = "n° FR 00 123 \n456 789. Suite"
    cut = text.index("\n")
    boxes = []
    for index in range(len(text)):
        line, column = (0, index) if index < cut else (1, index - cut - 1)
        boxes.append(BBox(column * 5.0, line * 12.0, column * 5.0 + 5, line * 12.0 + 10))
    segment = Segment("s", text, ("text", 0, 0), SegmentContext(SegmentKind.BODY), tuple(boxes))
    start, end = text.index("123"), text.index(". Suite")
    rep = Replacement(start, end, text[start:end], "987 \n654 321", "SIREN", "c")
    parts = line_parts(segment, rep)
    assert parts is not None
    assert [part for _, part in parts] == ["987", "654 321"]
    assert [round(boxes[0].y0) for boxes, _ in parts] == [0, 12]
    assert parts[1][0][0].x0 == 0.0  # the second part starts the second line
    # A substitute on one line is painted whole on the first line.
    assert line_parts(segment, Replacement(start, end, "", "987 654 321", "SIREN", "c")) is None
