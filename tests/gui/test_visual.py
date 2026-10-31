"""Visual side-by-side (PDF/images) and grouped mail headers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from anonyma.config import Config
from anonyma.gui.main_window import MainWindow
from anonyma.gui.textview import header_label, ordered_segments
from anonyma.gui.visual import render_page
from anonyma.models import BBox, Document, Segment, SegmentContext, SegmentKind
from tests.gui.conftest import prepare

pymupdf = pytest.importorskip("pymupdf")

TIMEOUT = 30_000


def _pdf(path: Path) -> Path:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "Contrat signé par Bérénice Lagarde.", fontsize=12)
    page = doc.new_page()
    page.insert_text((72, 200), "Annexe : Bérénice Lagarde, encore.", fontsize=12)
    doc.save(path)
    doc.close()
    return path


@pytest.fixture
def pdf_batch(tmp_path: Path, gui_config: Config) -> Path:
    folder = tmp_path / "pdf"
    folder.mkdir()
    _pdf(folder / "contrat.pdf")
    gui_config.ocr_enabled = False
    gui_config.always_mask = [{"value": "Bérénice Lagarde", "type": "PERSONNE"}]
    return folder


def test_pdf_visual_side_by_side(
    qtbot: Any, window: MainWindow, pdf_batch: Path, tmp_path: Path
) -> None:
    prepare(qtbot, window, pdf_batch, tmp_path / "sortie")
    with qtbot.waitSignal(window.controller.analysis_ready, timeout=TIMEOUT):
        window.processing.start_analyze()
    qtbot.waitUntil(window.controller.effective_is_current, timeout=TIMEOUT)
    window.show()
    window.show_page("review")
    review = window.review
    doc = window.controller.analysis.documents[0]  # type: ignore[union-attr]
    assert doc.format == "pdf"
    review.select_document(doc.id)
    assert review.visual_mode.isEnabled()
    review.visual_mode.click()
    visual = review.visual
    assert review.view_stack.currentWidget() is visual
    qtbot.waitUntil(lambda: visual.left.page is not None, timeout=TIMEOUT)
    qtbot.waitUntil(lambda: visual.right.page is not None, timeout=TIMEOUT)
    assert visual.page_count == 2
    # Entity boxes on the original, masked boxes with tags on the preview side.
    assert visual.left.items_by_key
    assert visual.right.items_by_key
    key = next(iter(visual.left.items_by_key))
    rect = visual.left.items_by_key[key].rect()
    page_rect = visual.left.page.image.rect()  # type: ignore[union-attr]
    assert 0 < rect.x() < page_rect.width() and 0 < rect.y() < page_rect.height() / 2

    # Jumping to the occurrence on page 2 turns the page on both sides.
    cid = review.cluster_for_value("Bérénice Lagarde")
    mentions = review.model.mentions(cid)
    second = next(e for e in mentions if review._segments[e.segment_id].context.page == 1)
    review.select_cluster(cid, second.key)
    qtbot.waitUntil(lambda: visual.current_page() == 1, timeout=TIMEOUT)
    qtbot.waitUntil(lambda: second.key in visual.left.items_by_key, timeout=TIMEOUT)

    # Zoom and scrolling are shared.
    visual.set_zoom(2.0)
    assert visual.left.transform().m11() == visual.right.transform().m11() == 2.0

    # After apply, the right side shows the produced PDF.
    with qtbot.waitSignal(window.controller.result_ready, timeout=TIMEOUT):
        window.processing.start_apply()
    assert review.source_combo.currentData() == "output"
    qtbot.waitUntil(
        lambda: visual.right.page is not None and not visual.right.items_by_key, timeout=TIMEOUT
    )
    visual.wait()


def test_render_image_off_thread_helper(tmp_path: Path) -> None:
    from PIL import Image

    path = tmp_path / "scan.png"
    Image.new("RGB", (200, 100), "white").save(path)
    page = render_page(path, "image", 0)
    assert (page.image.width(), page.image.height()) == (200, 100)
    assert page.page_count == 1
    rect = page.to_pixels(BBox(10, 20, 30, 40))
    assert (rect.x(), rect.y(), rect.width(), rect.height()) == (10, 20, 20, 20)
    with pytest.raises(ValueError):
        render_page(tmp_path / "absent.png", "image", 0)


def test_mail_headers_grouped_with_french_labels(tmp_path: Path) -> None:
    doc = Document(source=tmp_path / "m.eml", name="m.eml", format="eml")
    for i, label in enumerate(["Subject", "X-Mailer", "To", "From"]):
        doc.segments.append(
            Segment(f"h{i}", "x", ("h", i), SegmentContext(SegmentKind.HEADER, label))
        )
    doc.segments.append(Segment("b", "corps", ("b",), SegmentContext(SegmentKind.BODY)))
    labels = [s.context.label for s in ordered_segments(doc)]
    assert labels == ["From", "To", "Subject", "X-Mailer", ""]
    assert [header_label(x) for x in ("From", "to", "Subject")] == ["De", "À", "Objet"]


def test_only_user_errors_are_shown_verbatim() -> None:
    from anonyma.gui.tasks import user_message
    from anonyma.models import UserError

    assert user_message(UserError("Ce PDF ne contient aucune page.")) == (
        "Ce PDF ne contient aucune page."
    )
    # A library ValueError may quote a value of the document.
    leaked = user_message(ValueError("invalid literal for int(): 'Quintavalle'"))
    assert "Quintavalle" not in leaked and "ValueError" in leaked
