"""Deterministic text scanning of XML trees, shared by extractors and writers.

The extractor scans the original tree and turns every :class:`Unit` into a
segment whose locator is ``(part, unit index)``. The writer re-parses the same
original bytes, runs the same scan (hence gets the same units in the same order)
and applies the replacements unit by unit.

Units:

* ``para``: a paragraph whose text is spread over several text nodes (runs).
  Its pieces are joined; the writer redistributes the replaced text over the
  pieces, each substitute landing in the run where the replacement starts so
  that its formatting is kept. Virtual pieces (tabs, breaks) are visible to
  detectors but never edited.
* ``text``: the text (or tail) of one element not covered by a paragraph.
* ``attr``: an attribute value; identical values within a part are grouped in
  one unit (fewer segments, consistent replacement).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from lxml import etree

from anonyma.extractors._xml import attr_items, is_element, remove_keep_tail
from anonyma.models import Replacement, apply_replacements

Element = etree._Element


@dataclass(slots=True)
class Piece:
    el: Element
    #: "text" or "tail" for real pieces, "" for virtual ones.
    slot: str
    text: str
    stream: str = ""

    @property
    def editable(self) -> bool:
        return bool(self.slot)


@dataclass(slots=True)
class Unit:
    kind: str  # "para" | "text" | "attr" | custom kinds
    text: str
    anchor: Element
    pieces: list[Piece] = field(default_factory=list)
    slot: str = ""  # for "text" units
    attrs: list[tuple[Element, str]] = field(default_factory=list)
    stream: str = ""
    payload: Any = None


VirtualRule = str | Callable[[Element], str]


def _no_stream(el: Element, para: Element) -> str:
    return ""


def _no_attr(el: Element, name: str, value: str) -> bool:
    return False


@dataclass(slots=True)
class ScanSpec:
    #: Paragraph-like elements whose pieces are joined into one unit.
    para_tags: frozenset[str]
    #: Piece mode (OOXML): only the ``.text`` of these elements are pieces.
    piece_tags: frozenset[str] = frozenset()
    #: Mixed mode (ODF, generic XML): every ``.text``/``.tail`` inside a paragraph
    #: is a piece, except inside ``exclude`` subtrees.
    mixed: bool = False
    virtual: Mapping[str, VirtualRule] = field(default_factory=dict)
    #: Name of the stream a piece belongs to ("" = main text). Non-main streams
    #: (field codes, deleted text) become separate units.
    stream_of: Callable[[Element, Element], str] = _no_stream
    #: Subtrees that do not belong to the enclosing paragraph (mixed mode).
    exclude: frozenset[str] = frozenset()
    #: Elements whose own text is never exposed (still walked).
    skip_text: frozenset[str] = frozenset()
    #: Subtrees ignored entirely by this scan (handled elsewhere).
    skip_subtree: frozenset[str] = frozenset()
    #: Whether an attribute value is exposed: ``(element, clark name, value)``.
    attr_filter: Callable[[Element, str, str], bool] = _no_attr
    #: Expose XML comments and processing instructions as text units.
    comments: bool = False


def _virtual_text(rule: VirtualRule, el: Element) -> str:
    return rule if isinstance(rule, str) else rule(el)


def _collect_piece_mode(para: Element, spec: ScanSpec) -> list[Piece]:
    pieces: list[Piece] = []

    def walk(el: Element) -> None:
        for child in el:
            if not is_element(child):
                continue
            tag = child.tag
            if tag in spec.para_tags or tag in spec.skip_subtree:
                continue
            if tag in spec.piece_tags:
                pieces.append(Piece(child, "text", child.text or "", spec.stream_of(child, para)))
            elif tag in spec.virtual:
                text = _virtual_text(spec.virtual[tag], child)
                pieces.append(Piece(child, "", text, spec.stream_of(child, para)))
            walk(child)

    walk(para)
    return pieces


def _collect_mixed_mode(para: Element, spec: ScanSpec) -> list[Piece]:
    pieces: list[Piece] = []

    def walk(el: Element) -> None:
        if el.text:
            pieces.append(Piece(el, "text", el.text))
        for child in el:
            if is_element(child):
                tag = child.tag
                if tag in spec.virtual:
                    pieces.append(Piece(child, "", _virtual_text(spec.virtual[tag], child)))
                elif not (tag in spec.para_tags or tag in spec.exclude or tag in spec.skip_subtree):
                    walk(child)
            if child.tail:
                pieces.append(Piece(child, "tail", child.tail))

    walk(para)
    return pieces


def _split_streams(pieces: list[Piece]) -> list[tuple[str, list[Piece]]]:
    """Main stream first, then each contiguous group of a secondary stream."""
    main = [p for p in pieces if not p.stream]
    groups: list[tuple[str, list[Piece]]] = [("", main)]
    current: list[Piece] = []
    current_stream = ""
    for piece in pieces:
        if piece.stream and piece.stream == current_stream:
            current.append(piece)
            continue
        if current:
            groups.append((current_stream, current))
        current = [piece] if piece.stream else []
        current_stream = piece.stream
    if current:
        groups.append((current_stream, current))
    return groups


def scan(
    root: Element,
    spec: ScanSpec,
    *,
    pre_units: Sequence[Unit] = (),
    claimed: set[tuple[Element, str]] | None = None,
) -> list[Unit]:
    """Return every text unit of ``root`` in a deterministic order.

    ``pre_units``/``claimed`` let a format handle some elements itself (e.g.
    spreadsheet cells): their (element, slot) pairs are not exposed again.
    """
    units: list[Unit] = list(pre_units)
    taken: set[tuple[Element, str]] = set(claimed or ())
    para_elements = [el for el in root.iter() if is_element(el) and el.tag in spec.para_tags]
    if root.tag in spec.para_tags and root not in para_elements:
        para_elements.insert(0, root)
    skipped = _skipped_elements(root, spec)
    for para in para_elements:
        if para in skipped:
            continue
        pieces = _collect_mixed_mode(para, spec) if spec.mixed else _collect_piece_mode(para, spec)
        pieces = [p for p in pieces if not p.editable or (p.el, p.slot) not in taken]
        for piece in pieces:
            if piece.editable:
                taken.add((piece.el, piece.slot))
        for stream, group in _split_streams(pieces):
            text = "".join(p.text for p in group)
            if text.strip():
                units.append(Unit("para", text, para, pieces=group, stream=stream))

    attr_units: dict[str, Unit] = {}

    def walk(el: Element) -> None:
        for node in el:
            if is_element(node):
                if node.tag in spec.skip_subtree:
                    _tail_unit(node, el)
                    continue
                if (
                    node.text
                    and node.text.strip()
                    and node.tag not in spec.skip_text
                    and (node, "text") not in taken
                ):
                    taken.add((node, "text"))
                    units.append(Unit("text", node.text, node, slot="text"))
                for name, value in attr_items(node):
                    if value.strip() and spec.attr_filter(node, name, value):
                        unit = attr_units.get(value)
                        if unit is None:
                            unit = attr_units[value] = Unit("attr", value, node)
                        unit.attrs.append((node, name))
                walk(node)
            elif (
                spec.comments
                and isinstance(node, (etree._Comment, etree._ProcessingInstruction))
                and node.text
                and node.text.strip()
            ):
                if (node, "text") not in taken:
                    taken.add((node, "text"))
                    units.append(Unit("text", node.text, node, slot="text"))
            _tail_unit(node, el)

    def _tail_unit(node: Element, parent: Element) -> None:
        if node.tail and node.tail.strip() and (node, "tail") not in taken:
            taken.add((node, "tail"))
            units.append(Unit("text", node.tail, parent, slot="tail", payload=node))

    if is_element(root) and root.tag not in spec.skip_subtree:
        if (
            root.text
            and root.text.strip()
            and (root, "text") not in taken
            and root.tag not in spec.skip_text
        ):
            taken.add((root, "text"))
            units.append(Unit("text", root.text, root, slot="text"))
        for name, value in attr_items(root):
            if value.strip() and spec.attr_filter(root, name, value):
                unit = attr_units.get(value)
                if unit is None:
                    unit = attr_units[value] = Unit("attr", value, root)
                unit.attrs.append((root, name))
        walk(root)
    units.extend(attr_units.values())
    return units


def _skipped_elements(root: Element, spec: ScanSpec) -> set[Element]:
    """Elements located inside a ``skip_subtree`` element."""
    skipped: set[Element] = set()
    if not spec.skip_subtree:
        return skipped
    for el in root.iter():
        if is_element(el) and el.tag in spec.skip_subtree:
            skipped.update(d for d in el.iter() if is_element(d))
    return skipped


def redistribute(
    pieces: Sequence[tuple[str, bool]], replacements: Sequence[Replacement]
) -> list[str]:
    """New text of each piece after applying ``replacements`` to the joined text.

    ``pieces`` are ``(text, editable)``. Each substitute goes into the editable
    piece holding the first replaced character (so the run formatting where the
    replacement starts is kept); the other replaced characters are removed from
    their pieces. Non-editable (virtual) pieces are returned unchanged.
    """
    owners: list[int] = []
    for index, (text, _editable) in enumerate(pieces):
        owners.extend([index] * len(text))
    joined = "".join(text for text, _ in pieces)
    out: list[list[str]] = [[] for _ in pieces]

    def emit(start: int, end: int) -> None:
        for pos in range(start, end):
            owner = owners[pos]
            if pieces[owner][1]:
                out[owner].append(joined[pos])

    cursor = 0
    for rep in replacements:
        if rep.start < cursor or rep.end > len(joined) or rep.start > rep.end:
            raise ValueError("replacements must be sorted, in range and non-overlapping")
        emit(cursor, rep.start)
        target = _target_piece(pieces, owners, rep.start, rep.end)
        out[target].append(rep.substitute)
        # Virtual characters inside the replaced range stay where they are.
        cursor = rep.end
    emit(cursor, len(joined))
    return [
        "".join(chunks) if editable else text
        for (text, editable), chunks in zip(pieces, out, strict=True)
    ]


def _target_piece(
    pieces: Sequence[tuple[str, bool]], owners: Sequence[int], start: int, end: int
) -> int:
    for pos in range(start, end):
        if pieces[owners[pos]][1]:
            return owners[pos]
    for pos in range(start - 1, -1, -1):
        if pieces[owners[pos]][1]:
            return owners[pos]
    for pos in range(end, len(owners)):
        if pieces[owners[pos]][1]:
            return owners[pos]
    for index, (_text, editable) in enumerate(pieces):
        if editable:
            return index
    raise ValueError("no editable piece to receive the substitute")


def apply_unit(
    unit: Unit,
    replacements: Sequence[Replacement],
    on_set: Callable[[Element, str], None] | None = None,
) -> None:
    """Apply ``replacements`` (offsets into ``unit.text``) to the tree."""
    if not replacements:
        return
    if unit.kind == "para":
        new = redistribute([(p.text, p.editable) for p in unit.pieces], replacements)
        for piece, text in zip(unit.pieces, new, strict=True):
            if piece.editable and text != piece.text:
                _set_slot(piece.el, piece.slot, text)
                if on_set is not None:
                    on_set(piece.el, piece.slot)
        # Virtual characters (spaces, tabs) entirely inside a replaced span go away.
        for index in swallowed_virtual([p.text for p in unit.pieces], unit.pieces, replacements):
            remove_keep_tail(unit.pieces[index].el)
        return
    new_text = apply_replacements(unit.text, replacements)
    if unit.kind == "text":
        if unit.slot == "tail":
            node: Element = unit.payload
            node.tail = new_text
        else:
            unit.anchor.text = new_text
            if on_set is not None:
                on_set(unit.anchor, "text")
        return
    if unit.kind == "attr":
        for el, name in unit.attrs:
            el.set(name, new_text)
        return
    raise ValueError(f"unknown unit kind {unit.kind}")


def swallowed_virtual(
    texts: Sequence[str], pieces: Sequence[Piece], replacements: Sequence[Replacement]
) -> list[int]:
    """Indexes of non-empty virtual pieces lying entirely inside a replacement."""
    out: list[int] = []
    offset = 0
    for index, (text, piece) in enumerate(zip(texts, pieces, strict=True)):
        start, end = offset, offset + len(text)
        offset = end
        if piece.editable or not text:
            continue
        if any(rep.start <= start and end <= rep.end for rep in replacements):
            out.append(index)
    return out


def _set_slot(el: Element, slot: str, text: str) -> None:
    if slot == "text":
        el.text = text
    else:
        el.tail = text
