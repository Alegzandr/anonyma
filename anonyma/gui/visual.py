"""Visual side-by-side for PDF and images: pages rendered off the UI thread,
entity boxes drawn from ``Segment.char_boxes`` / ``span_bbox``.

Rendering uses PyMuPDF (PDF) and Pillow (images) in worker threads; only
``QImage`` objects cross back to the UI thread. MuPDF is not thread-safe, so all
renders are serialised by a lock.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import override

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetricsF,
    QImage,
    QPen,
    QPixmap,
    QResizeEvent,
    QTransform,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from anonyma import registry
from anonyma.gui import theme
from anonyma.gui.tasks import Report, TaskRunner
from anonyma.gui.widgets import button
from anonyma.models import BBox, Document, DocumentResult, UserError

_LOCK = threading.Lock()
PDF_ZOOM = 2.0
MAX_SIDE = 2400
CACHE_SIZE = 8
#: Smallest automatic zoom (a PDF rendered at ``PDF_ZOOM`` shown at about 100 %).
MIN_FIT_ZOOM = 0.5
#: Smallest label of a box, in page pixels: below, the label is cut with « … ».
MIN_LABEL_PX = 6


def visual_kind(doc: Document) -> str | None:
    """ "pdf", "image" or None when the document has no visual rendering."""
    if doc.format == "pdf":
        return "pdf"
    if doc.extractor == "image":
        return "image"
    return None


def output_kind(result: DocumentResult | None) -> str | None:
    if result is None or result.output_path is None or not result.output_path.is_file():
        return None
    if result.output_format == "pdf":
        return "pdf"
    extractor = registry.extractor_for(result.output_format)
    return "image" if extractor is not None and extractor.name == "image" else None


@dataclass(slots=True)
class PageImage:
    image: QImage
    page: int
    page_count: int
    #: Page-space -> pixel affine matrix (a, b, c, d, e, f), already zoomed.
    matrix: tuple[float, float, float, float, float, float]

    def to_pixels(self, box: BBox) -> QRectF:
        a, b, c, d, e, f = self.matrix
        points = [
            QPointF(x * a + y * c + e, x * b + y * d + f)
            for x, y in ((box.x0, box.y0), (box.x1, box.y0), (box.x0, box.y1), (box.x1, box.y1))
        ]
        xs = [p.x() for p in points]
        ys = [p.y() for p in points]
        return QRectF(min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))


def _qimage(data: bytes, width: int, height: int, stride: int) -> QImage:
    return QImage(data, width, height, stride, QImage.Format.Format_RGB888).copy()


def render_page(path: Path, kind: str, page: int) -> PageImage:
    """Render one page (worker thread). Raises ``ValueError`` with a French message."""
    with _LOCK:
        if kind == "pdf":
            return _render_pdf(path, page)
        return _render_image(path, page)


def _render_pdf(path: Path, page: int) -> PageImage:
    import pymupdf

    try:
        doc = pymupdf.open(path)
    except Exception as exc:
        raise UserError("Ce PDF ne peut pas être affiché.") from exc
    try:
        if doc.needs_pass:
            raise UserError("PDF protégé par mot de passe : aperçu visuel impossible.")
        count = doc.page_count
        if count == 0:
            raise UserError("Ce PDF ne contient aucune page.")
        number = max(0, min(page, count - 1))
        pdf_page = doc[number]
        rect = pdf_page.rect
        zoom = min(PDF_ZOOM, MAX_SIDE / max(rect.width, rect.height, 1))
        pix = pdf_page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
        image = _qimage(bytes(pix.samples), pix.width, pix.height, pix.stride)
        m = pdf_page.rotation_matrix
        matrix = (m.a * zoom, m.b * zoom, m.c * zoom, m.d * zoom, m.e * zoom, m.f * zoom)
        return PageImage(image, number, count, matrix)
    finally:
        doc.close()


def _render_image(path: Path, page: int) -> PageImage:
    from PIL import Image

    from anonyma.extractors.image import ocr_view, prepare_frame

    try:
        with Image.open(path) as source:
            count = int(getattr(source, "n_frames", 1) or 1)
            number = max(0, min(page, count - 1))
            source.seek(number)
            frame = ocr_view(prepare_frame(source))
    except Exception as exc:
        raise UserError("Cette image ne peut pas être affichée.") from exc
    scale = min(1.0, MAX_SIDE / max(frame.size))
    if scale < 1.0:
        frame = frame.resize(
            (max(1, round(frame.width * scale)), max(1, round(frame.height * scale)))
        )
    image = _qimage(frame.tobytes("raw", "RGB"), frame.width, frame.height, frame.width * 3)
    return PageImage(image, number, count, (scale, 0.0, 0.0, scale, 0.0, 0.0))


def _render_job(path: Path, kind: str, page: int) -> Callable[[Report], PageImage]:
    def job(_report: Report) -> PageImage:
        return render_page(path, kind, page)

    return job


# ---------------------------------------------------------------------------
# Widgets
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Box:
    """An overlay in page coordinates."""

    rect: BBox
    pen: QColor
    fill: QColor | None = None
    dashed: bool = False
    key: object = None
    label: str = ""
    tooltip: str = ""


BoxProvider = Callable[[int], list[Box]]


def _box_label(label: str, rect: QRectF) -> QGraphicsSimpleTextItem:
    """The substitute written in a box: shrunk to the box width like the writers
    shrink it in the file, then cut with « … » if even the smallest size is too wide
    (the tooltip keeps it whole). Never left out: an empty box reads as a blank."""
    font = QFont()
    size = max(MIN_LABEL_PX, min(int(rect.height() * 0.7), 18))
    font.setPixelSize(size)
    room = rect.width() - 2
    width = QFontMetricsF(font).horizontalAdvance(label)
    if width > room:
        # Proportional first guess, then down a pixel at a time (hinting is not linear).
        size = max(MIN_LABEL_PX, min(size, int(size * room / width)))
        font.setPixelSize(size)
        while size > MIN_LABEL_PX and QFontMetricsF(font).horizontalAdvance(label) > room:
            size -= 1
            font.setPixelSize(size)
    metrics = QFontMetricsF(font)
    if metrics.horizontalAdvance(label) > room:
        label = metrics.elidedText(label, Qt.TextElideMode.ElideRight, max(room, 0.0))
    text = QGraphicsSimpleTextItem(label)
    text.setFont(font)
    return text


class PageView(QGraphicsView):
    """One rendered page with box overlays; Ctrl+wheel requests a zoom."""

    zoom_requested = Signal(float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        self.page: PageImage | None = None
        self.items_by_key: dict[object, QGraphicsRectItem] = {}
        self._highlight: QGraphicsRectItem | None = None

    def show_message(self, text: str) -> None:
        scene = self.scene()
        scene.clear()
        self.page = None
        self.items_by_key = {}
        self._highlight = None
        item = scene.addText(text)
        item.setDefaultTextColor(QColor(theme.tokens().muted))
        scene.setSceneRect(item.boundingRect())

    def set_page(self, page: PageImage, boxes: list[Box]) -> None:
        scene = self.scene()
        scene.clear()
        self.page = page
        self.items_by_key = {}
        self._highlight = None
        scene.addPixmap(QPixmap.fromImage(page.image))
        for box in boxes:
            rect = page.to_pixels(box.rect).adjusted(-1, -1, 1, 1)
            pen = QPen(box.pen, 2)
            if box.dashed:
                pen.setStyle(Qt.PenStyle.DashLine)
            item = scene.addRect(rect, pen, QBrush(box.fill) if box.fill else QBrush())
            if box.tooltip:
                item.setToolTip(box.tooltip)
            if box.label and box.fill is not None and rect.height() >= 9:
                text = _box_label(box.label, rect)
                text.setBrush(
                    QColor(theme.tokens().text if box.fill.lightness() > 128 else "#ffffff")
                )
                bounds = text.boundingRect()
                text.setParentItem(item)
                text.setPos(rect.x() + 2, rect.y() + (rect.height() - bounds.height()) / 2)
            if box.key is not None:
                self.items_by_key[box.key] = item
        scene.setSceneRect(QRectF(page.image.rect()))

    def highlight(self, key: object) -> bool:
        scene = self.scene()
        if self._highlight is not None:
            scene.removeItem(self._highlight)
            self._highlight = None
        item = self.items_by_key.get(key)
        if item is None:
            return False
        rect = item.rect().adjusted(-4, -4, 4, 4)
        self._highlight = scene.addRect(rect, QPen(QColor(theme.tokens().accent), 3))
        self.centerOn(rect.center())
        return True

    @override
    def wheelEvent(self, event: QWheelEvent) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.zoom_requested.emit(1.15 if event.angleDelta().y() > 0 else 1 / 1.15)
            event.accept()
            return
        super().wheelEvent(event)


class VisualPair(QWidget):
    """Original (left) and anonymized (right) pages, synchronised page/zoom/scroll."""

    rendered = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.runner = TaskRunner(self)
        self._cache: OrderedDict[tuple[str, int], PageImage] = OrderedDict()
        self._sources: dict[str, tuple[Path, str] | None] = {"left": None, "right": None}
        self._messages: dict[str, str] = {"left": "", "right": ""}
        self._boxes: dict[str, BoxProvider | None] = {"left": None, "right": None}
        self._pending_highlight: object = None
        self._generation = 0
        self._syncing = False
        self.zoom = 1.0
        self.page_count = 1
        self._fit_pending = False
        #: Follow the view width (default) until the user zooms by hand.
        self._auto_fit = True

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        bar = QHBoxLayout()
        self.previous_button = button("", "previous")
        self.previous_button.setToolTip("Page précédente")
        self.next_button = button("", "next")
        self.next_button.setToolTip("Page suivante")
        self.page_spin = QSpinBox()
        self.page_spin.setMinimum(1)
        self.page_spin.setPrefix("Page ")
        self.page_label = QLabel("/ 1")
        self.zoom_out = button("−")
        self.zoom_out.setToolTip("Réduire (Ctrl+molette)")
        self.zoom_in = button("+")
        self.zoom_in.setToolTip("Agrandir (Ctrl+molette)")
        self.fit_button = button("Ajuster")
        self.fit_button.setToolTip("Ajuster la page à la largeur de la vue")
        for small in (self.previous_button, self.next_button, self.zoom_out, self.zoom_in):
            small.setFixedWidth(34)
        self.page_spin.setMaximumWidth(96)
        self.zoom_label = QLabel("100\u00a0%")
        for w in (self.previous_button, self.page_spin, self.page_label, self.next_button):
            bar.addWidget(w)
        bar.addStretch(1)
        for w in (self.zoom_out, self.zoom_label, self.zoom_in, self.fit_button):
            bar.addWidget(w)
        layout.addLayout(bar)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.left = PageView()
        self.left.setObjectName("VisualOriginal")
        self.right = PageView()
        self.right.setObjectName("VisualAnonymized")
        splitter.addWidget(self.left)
        splitter.addWidget(self.right)
        layout.addWidget(splitter, 1)

        self.previous_button.clicked.connect(lambda: self.set_page(self.current_page() - 1))
        self.next_button.clicked.connect(lambda: self.set_page(self.current_page() + 1))
        self.page_spin.valueChanged.connect(lambda v: self._render(v - 1))
        self.zoom_in.clicked.connect(lambda: self.zoom_by(1.25))
        self.zoom_out.clicked.connect(lambda: self.zoom_by(1 / 1.25))
        self.fit_button.clicked.connect(self.fit)
        for view in (self.left, self.right):
            view.zoom_requested.connect(self.zoom_by)
        for a, b in ((self.left, self.right), (self.right, self.left)):
            a.verticalScrollBar().valueChanged.connect(
                lambda _v, a=a, b=b: self._sync(a.verticalScrollBar(), b.verticalScrollBar())
            )
            a.horizontalScrollBar().valueChanged.connect(
                lambda _v, a=a, b=b: self._sync(a.horizontalScrollBar(), b.horizontalScrollBar())
            )

    # -- configuration ------------------------------------------------------------------

    def current_page(self) -> int:
        return self.page_spin.value() - 1

    def load(
        self,
        left: tuple[Path, str] | None,
        right: tuple[Path, str] | None,
        left_boxes: BoxProvider | None,
        right_boxes: BoxProvider | None,
        right_message: str = "",
        page: int = 0,
    ) -> None:
        """Show a document: ``(path, kind)`` per side; box providers per page."""
        self._sources = {"left": left, "right": right}
        self._boxes = {"left": left_boxes, "right": right_boxes}
        self._messages = {"left": "Aperçu visuel indisponible.", "right": right_message}
        self._fit_pending = True
        self.page_spin.blockSignals(True)
        self.page_spin.setValue(page + 1)
        self.page_spin.blockSignals(False)
        self._render(page)

    def refresh_boxes(self) -> None:
        """Redraw overlays (decisions changed) without re-rendering pages."""
        self._render(self.current_page())

    def set_page(self, page: int) -> None:
        page = max(0, min(page, self.page_count - 1))
        if page != self.current_page():
            self.page_spin.setValue(page + 1)

    def go_to(self, page: int, key: object) -> None:
        self._pending_highlight = key
        if page != self.current_page():
            self.set_page(page)
        else:
            self._apply_highlight()

    def set_zoom(self, zoom: float) -> None:
        self.zoom = max(0.1, min(zoom, 8.0))
        for view in (self.left, self.right):
            view.setTransform(QTransform.fromScale(self.zoom, self.zoom))
        self.zoom_label.setText(f"{round(self.zoom * 100)}\u00a0%")

    def zoom_by(self, factor: float) -> None:
        """Manual zoom: the page stops following the view width."""
        self._auto_fit = False
        self.set_zoom(self.zoom * factor)

    def fit(self) -> None:
        """Fit the page to the view width (and keep fitting it on resize)."""
        self._auto_fit = True
        page = self.left.page or self.right.page
        if page is None:
            return
        viewport = self.left.viewport().width()
        if viewport < 120:  # not laid out yet: fitted again on the first resize
            return
        width = viewport - self.left.verticalScrollBar().sizeHint().width() - 8
        # Never below text-reading size: a narrow pane scrolls instead (the current
        # entity is centred by ``highlight``).
        self.set_zoom(max(width / max(page.image.width(), 1), MIN_FIT_ZOOM))

    @override
    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if self._auto_fit:
            self.fit()

    # -- rendering ------------------------------------------------------------------------------

    def _cached(self, path: Path, page: int) -> PageImage | None:
        key = (str(path), page)
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        return None

    def _store(self, path: Path, page: PageImage, requested: int) -> None:
        self._cache[(str(path), requested)] = page
        while len(self._cache) > CACHE_SIZE:
            self._cache.popitem(last=False)

    def _render(self, page: int) -> None:
        self._generation += 1
        generation = self._generation
        for side, view in (("left", self.left), ("right", self.right)):
            source = self._sources[side]
            if source is None:
                view.show_message(self._messages[side] or "Aperçu visuel indisponible.")
                continue
            path, kind = source
            cached = self._cached(path, page)
            if cached is not None:
                self._show(side, cached)
                continue
            view.show_message("Rendu de la page…")
            task = self.runner.create(_render_job(path, kind, page))
            task.succeeded.connect(
                lambda value, s=side, p=path: self._rendered(s, p, page, value, generation)
            )
            task.failed.connect(
                lambda message, _t, v=view: (
                    v.show_message(message) if generation == self._generation else None
                )
            )
            task.start()

    def _rendered(self, side: str, path: Path, page: int, value: object, generation: int) -> None:
        if not isinstance(value, PageImage):
            return
        self._store(path, value, page)
        if generation != self._generation:
            return
        self._show(side, value)

    def _show(self, side: str, page: PageImage) -> None:
        view = self.left if side == "left" else self.right
        provider = self._boxes[side]
        view.set_page(page, provider(page.page) if provider else [])
        view.setTransform(QTransform.fromScale(self.zoom, self.zoom))
        if side == "left" or self._sources["left"] is None:
            self.page_count = page.page_count
            self.page_spin.blockSignals(True)
            self.page_spin.setMaximum(max(1, page.page_count))
            self.page_spin.blockSignals(False)
            self.page_label.setText(f"/ {page.page_count}")
        if self._fit_pending or self._auto_fit:
            self._fit_pending = False
            self.fit()
        self._apply_highlight()
        self.rendered.emit()

    def _apply_highlight(self) -> None:
        key = self._pending_highlight
        if key is None:
            return
        self.left.highlight(key)
        self.right.highlight(key)

    def _sync(self, source: object, target: object) -> None:
        from PySide6.QtWidgets import QScrollBar

        if (
            self._syncing
            or not isinstance(source, QScrollBar)
            or not isinstance(target, QScrollBar)
        ):
            return
        if source.maximum() <= 0:
            return
        self._syncing = True
        try:
            target.setValue(round(source.value() / source.maximum() * target.maximum()))
        finally:
            self._syncing = False

    def wait(self, msecs: int = 10_000) -> bool:
        return self.runner.wait_all(msecs)


__all__ = [
    "Box",
    "PageImage",
    "PageView",
    "VisualPair",
    "output_kind",
    "render_page",
    "visual_kind",
]
