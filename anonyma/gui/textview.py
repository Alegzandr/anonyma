"""Text side-by-side rendering: segments grouped by kind, entities highlighted.

Offsets are preserved exactly (``display_text``) so that entity positions map to
``QTextDocument`` positions, including ``
`` and non-BMP characters.
"""

from __future__ import annotations

import bisect
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from PySide6.QtGui import QColor, QTextBlockFormat, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import QTextEdit

from anonyma.gui import theme
from anonyma.gui.labels import KIND_LABELS, detector_label, entity_type_label
from anonyma.gui.preview import STATE_LABELS, EntityRow, RowState, confidence_label
from anonyma.models import FILENAME_SEGMENT_ID, Document, Segment, SegmentContext, SegmentKind

#: Mail header names shown first, with their French label.
HEADER_LABELS: dict[str, str] = {
    "from": "De",
    "sender": "Expéditeur",
    "reply-to": "Répondre à",
    "to": "À",
    "cc": "Cc",
    "bcc": "Cci",
    "subject": "Objet",
    "date": "Date",
}
_HEADER_ORDER = list(HEADER_LABELS)

#: Document metadata keys (PDF Info, Office core properties…) -> French label.
METADATA_LABELS: dict[str, str] = {
    "title": "Titre",
    "author": "Auteur",
    "subject": "Sujet",
    "keywords": "Mots-clés",
    "creator": "Application d'origine",
    "producer": "Producteur",
    "creationdate": "Date de création",
    "moddate": "Date de modification",
    "trapped": "Recouvrement",
    "description": "Description",
    "lastmodifiedby": "Modifié par",
    "last_modified_by": "Modifié par",
    "company": "Société",
    "manager": "Responsable",
    "category": "Catégorie",
    "comments": "Commentaires",
    "created": "Date de création",
    "modified": "Date de modification",
    "initial-creator": "Créateur initial",
}


def header_label(label: str) -> str:
    return HEADER_LABELS.get(label.strip().lower(), label)


def metadata_label(label: str) -> str:
    key = label.strip()
    simple = key.rsplit(":", 1)[-1].lower().replace(" ", "")
    return METADATA_LABELS.get(simple, key)


# ---------------------------------------------------------------------------
# Text rendering
# ---------------------------------------------------------------------------

_FOLD = str.maketrans(
    {
        "\r": "\n",
        " ": "\n",
        " ": "\n",
        "\x0b": "\n",
        "\x0c": "\n",
        "\x85": "\n",
        "﷐": "\u200b",
        "﷑": "\u200b",
        **{chr(i): "\u200b" for i in range(32) if chr(i) not in "\n\t\r\x0b\x0c"},
    }
)


def display_text(text: str) -> str:
    """Same length as ``text``, safe for ``QTextCursor.insertText`` (which folds
    ``\\r\\n`` into one block separator and would shift every offset)."""
    return text.replace("\r\n", "\u200b\n").translate(_FOLD)


def _u16_offsets(text: str) -> Callable[[int], int] | None:
    """Python index -> UTF-16 index converter, or None when identical (BMP only)."""
    if not text or max(text) <= "￿":
        return None

    def convert(i: int) -> int:
        return len(text[:i].encode("utf-16-le")) // 2

    return convert


@dataclass(slots=True)
class StyledSpan:
    start: int
    end: int
    fmt: QTextCharFormat
    key: object = None  # MentionKey on the original side, (segment id, start, end) elsewhere


@dataclass(slots=True)
class RenderMap:
    """Document positions of the rendered spans."""

    positions: dict[object, tuple[int, int]] = field(default_factory=dict)
    starts: list[int] = field(default_factory=list)
    spans: list[tuple[int, int, object]] = field(default_factory=list)

    def add(self, key: object, start: int, end: int) -> None:
        self.positions[key] = (start, end)

    def finish(self) -> None:
        self.spans = sorted((s, e, k) for k, (s, e) in self.positions.items())
        self.starts = [s for s, _, _ in self.spans]

    def key_at(self, position: int) -> object:
        """Smallest span containing ``position``."""
        best: tuple[int, object] | None = None
        i = bisect.bisect_right(self.starts, position)
        for s, e, k in self.spans[max(0, i - 50) : i]:
            if s <= position < e and (best is None or e - s < best[0]):
                best = (e - s, k)
        return best[1] if best else None


def ordered_segments(doc: Document) -> list[Segment]:
    """File name first, then mail headers grouped (De, À, Cc, Objet, Date first),
    then the extraction order."""
    head = [s for s in doc.segments if s.id == FILENAME_SEGMENT_ID]
    headers = [s for s in doc.segments if s.context.kind is SegmentKind.HEADER]
    rest = [
        s
        for s in doc.segments
        if s.id != FILENAME_SEGMENT_ID and s.context.kind is not SegmentKind.HEADER
    ]

    def rank(seg: Segment) -> int:
        name = seg.context.label.strip().lower()
        return _HEADER_ORDER.index(name) if name in _HEADER_ORDER else len(_HEADER_ORDER)

    return head + sorted(headers, key=rank) + rest


def render_segments(
    edit: QTextEdit,
    segments: Sequence[tuple[str, SegmentContext, str, list[StyledSpan]]],
) -> RenderMap:
    """Render ``(segment id, context, text, spans)`` into ``edit``, grouped by kind."""
    t = theme.tokens()
    document = edit.document()
    # Read-only views: no undo history, and one layout pass at the end instead of one
    # per inserted piece (ten times faster on a document of thousands of segments).
    document.setUndoRedoEnabled(False)
    document.clear()
    mapping = RenderMap()
    cursor = QTextCursor(document)
    cursor.beginEditBlock()
    base = QTextCharFormat()
    base.setForeground(QColor(t.text))
    heading = QTextCharFormat()
    heading.setFontWeight(700)
    heading.setForeground(QColor(t.muted))
    caption = QTextCharFormat()
    caption.setForeground(QColor(t.muted))
    caption.setFontItalic(True)
    label_format = QTextCharFormat()
    label_format.setForeground(QColor(t.muted))
    label_format.setFontWeight(600)
    heading_block = QTextBlockFormat()
    heading_block.setTopMargin(12)
    heading_block.setBottomMargin(4)
    body_block = QTextBlockFormat()
    first = True
    previous_kind: SegmentKind | None = None
    previous_page: int | None = None
    need_break = False
    for _seg_id, context, text, spans in segments:
        kind = context.kind
        if kind != previous_kind:
            if first:
                cursor.setBlockFormat(heading_block)
            else:
                cursor.insertBlock(heading_block)
            cursor.insertText(KIND_LABELS.get(kind, str(kind)), heading)
            cursor.insertBlock(body_block, base)
            previous_kind = kind
            previous_page = None
            need_break = False
            first = False
        elif need_break:
            cursor.insertBlock(body_block, base)
        if kind is SegmentKind.BODY:
            if context.page is not None and context.page != previous_page:
                cursor.insertText(f"Page {context.page + 1}", caption)
                cursor.insertBlock(body_block, base)
                previous_page = context.page
        elif kind is SegmentKind.HEADER and context.label:
            cursor.insertText(f"{header_label(context.label)}\u00a0: ", label_format)
        elif context.label and kind is SegmentKind.METADATA:
            cursor.insertText(f"{metadata_label(context.label)}\u00a0: ", label_format)
        elif context.label and kind is not SegmentKind.FILENAME:
            cursor.insertText(f"{context.label}\u00a0: ", caption)
        start = cursor.position()
        shown = display_text(text)
        cursor.insertText(shown, base)
        convert = _u16_offsets(text)
        for span in spans:
            s = start + (convert(span.start) if convert else span.start)
            e = start + (convert(span.end) if convert else span.end)
            if e <= s:
                continue
            c = QTextCursor(document)
            c.setPosition(s)
            c.setPosition(e, QTextCursor.MoveMode.KeepAnchor)
            c.mergeCharFormat(span.fmt)
            if span.key is not None:
                mapping.add(span.key, s, e)
        need_break = kind is not SegmentKind.BODY or not shown.endswith("\n")
    if first:
        cursor.insertText("Aucun texte extrait pour ce document.", caption)
    cursor.endEditBlock()
    mapping.finish()
    edit.moveCursor(QTextCursor.MoveOperation.Start)
    return mapping


def entity_format(
    entity_type: str, state: RowState | None = None, tooltip: str = ""
) -> QTextCharFormat:
    t = theme.tokens()
    fmt = QTextCharFormat()
    if state in (RowState.REJECTED, RowState.MENTION_REJECTED):
        fmt.setFontStrikeOut(True)
        fmt.setForeground(QColor(t.muted))
    else:
        bg, fg = theme.entity_colors(entity_type)
        fmt.setBackground(bg)
        fmt.setForeground(fg)
    if tooltip:
        fmt.setToolTip(tooltip)
    return fmt


def row_tooltip(row: EntityRow) -> str:
    e = row.entity
    return (
        f"{entity_type_label(row.entity_type)} · {STATE_LABELS[row.state]} · "
        f"{detector_label(e.detector)} · {confidence_label(e.score, row.uncertain)}"
    )
