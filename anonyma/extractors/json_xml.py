"""JSON (and JSON Lines) and generic XML extractors.

JSON: every string value, every number (phone numbers are often stored as numbers)
and every key that is not a structural name (:mod:`anonyma.structure`: ``createdon``,
``@odata.etag``... are schema, never exposed nor rewritten) is a segment; labels are
JSON paths with keys masked, ``where`` the real path (structural keys shown) and its
line. A value whose key is a known field name (``login``, ``telephone``...) carries
its field hint (:mod:`anonyma.fieldhints`), never the key itself. A value holding a
file (base64, ``data:`` URI: :mod:`anonyma.extractors._embedded`) has it extracted as
a child; the rest of the value is exposed with a mark in its place.

XML: element text and tails, attribute values, comments, CDATA and processing
instructions. Parsing never resolves entities nor touches the network.
"""

from __future__ import annotations

import bisect
import re
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from lxml import etree

from anonyma.codec import decode_bytes
from anonyma.extractors._embedded import PROPERTY as EMBEDDED
from anonyma.extractors._embedded import (
    EmbeddedRef,
    Hint,
    extract_blobs,
    hint_from,
    masked,
    name_key,
    visible,
)
from anonyma.extractors._html import HtmlSource, build_segments, extension_for
from anonyma.extractors._tabular import JsonToken, tokenize_json
from anonyma.extractors._xml import doctype_ids, local_name, parse_xml
from anonyma.extractors._xmlscan import ScanSpec, Unit, scan
from anonyma.fieldhints import field_hint, with_key_hint
from anonyma.models import (
    Document,
    ExtractionContext,
    Extractor,
    Segment,
    SegmentContext,
    SegmentKind,
)
from anonyma.registry import register_extractor
from anonyma.structure import PROPERTY as STRUCTURE
from anonyma.structure import is_structural, schema_valued, vocabulary

_SIMPLE_KEY = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")


def json_path(keys: Sequence[str | int]) -> str:
    """Readable JSON path: structural keys shown (``$.value[27].description``,
    ``$.value[0]['@odata.etag']``), any other key masked as ``*``."""
    document = keys[0] if keys else 0
    out = [f"${document}" if document else "$"]
    for key in keys[1:]:
        if isinstance(key, int):
            out.append(f"[{key}]")
        elif not is_structural(key):
            out.append(".*")
        elif _SIMPLE_KEY.fullmatch(key):
            out.append(f".{key}")
        else:
            out.append(f"['{key}']")
    return "".join(out)


def _nearest_key(keys: Sequence[str | int]) -> str:
    return next((k for k in reversed(keys) if isinstance(k, str)), "")


@register_extractor
class JsonExtractor(Extractor):
    name = "json"
    extensions = {".json": "json", ".jsonl": "json", ".ndjson": "json", ".geojson": "json"}

    def extract(self, source: Path, document: Document, ctx: ExtractionContext) -> None:
        text, encoding = decode_bytes(source.read_bytes())
        document.properties["encoding"] = encoding
        tokens = tokenize_json(text)
        lines = [m.start() for m in re.finditer("\n", text)]
        siblings: dict[int, dict[str, str]] = {}
        for token in tokens:
            if token.kind == "string" and token.keys and isinstance(token.keys[-1], str):
                siblings.setdefault(token.parent, {})[name_key(token.keys[-1])] = token.value
        key_hints: dict[str, str | None] = {}
        structural: set[str] = set()
        embedded: dict[int, list[EmbeddedRef]] = {}
        html_values: dict[int, dict[int, str]] = {}
        for i, token in enumerate(tokens):
            line = bisect.bisect_left(lines, token.start) + 1
            where = f"{json_path(token.keys)} (ligne {line} du fichier)"
            value = token.value
            if token.kind == "key":
                key_hints[token.path] = field_hint(token.value)
                if is_structural(token.value):
                    structural.add(token.value)
                    continue  # schema: never exposed, never rewritten
                kind = SegmentKind.ATTRIBUTE
                label = f"{token.path} (clé)"
                where = f"{json_path(token.keys)} (clé, ligne {line} du fichier)"
            else:
                kind = SegmentKind.BODY
                label = with_key_hint(token.path, key_hints.get(token.path))
            if token.kind == "string" and schema_valued(_nearest_key(token.keys), value):
                continue  # « systemuser » under a lookup annotation: schema, kept as is
            if token.kind == "string" and looks_like_html(value):
                # An HTML body (Dataverse ``description``): read as HTML, entities
                # decoded, markup and styles left out, data: URIs as children.
                html_values[i] = _html_value(value, i, label, where, document, ctx)
                continue
            if token.kind == "string":
                hint = hint_from(_nearest_key(token.keys), siblings.get(token.parent, {}))
                refs = extract_blobs(value, hint, document, ctx)
                if refs:
                    embedded[i] = refs
                    value = masked(value, refs)
                    if not visible(value):
                        continue
            document.segments.append(
                Segment(
                    id=f"j{i}",
                    text=value,
                    locator=(token.start, token.end),
                    context=SegmentContext(kind, label=label, where=where),
                )
            )
        document.properties[EMBEDDED] = embedded
        document.properties[HTML_VALUES] = html_values
        document.properties[SIZES] = _size_fields(tokens, embedded)
        document.properties[STRUCTURE] = vocabulary(structural)


#: Document properties of JSON: HTML values (token index -> data: URI unit ->
#: child id) and size fields of embedded files (token index -> child id).
HTML_VALUES = "html_values"
SIZES = "embedded_sizes"
_HTML_START = re.compile(r"\s*<(?:!doctype|[a-z][\w:-]*)[\s/>]", re.I)
_HTML_TAG = re.compile(r"</[a-z][\w:-]*\s*>|<br\s*/?>", re.I)
#: Normalized names of the field giving the size of a sibling embedded file.
_SIZE_NAMES = frozenset({"filesize", "file size", "size", "length", "taille"})


def looks_like_html(value: str) -> bool:
    """A JSON string holding an HTML document or fragment."""
    return bool(_HTML_START.match(value)) and bool(_HTML_TAG.search(value))


def html_segment_prefix(index: int) -> str:
    return f"j{index}|h"


def _html_value(
    value: str, index: int, label: str, where: str, document: Document, ctx: ExtractionContext
) -> dict[int, str]:
    """Segments of an HTML value; its data: URIs extracted as children."""
    source = HtmlSource(value)
    prefix = f"{label} : "
    for segment in build_segments(source, html_segment_prefix(index), prefix):
        zone = segment.context.label[len(prefix) :]
        context = replace(segment.context, where=f"{where}, {zone}")
        document.segments.append(replace(segment, context=context))
    children: dict[int, str] = {}
    for uri in source.data_uris:
        ctx.check_cancelled()
        ctx.budget.consume(len(uri.data), depth=ctx.depth + 1)
        number = document.properties.get("embedded_count", 0) + 1
        document.properties["embedded_count"] = number
        name = f"fichier-incorpore-{number}{extension_for(uri.mime)}"
        children[uri.unit] = ctx.extract_child(uri.data, name, document).id
    return children


def _size_fields(
    tokens: Sequence[JsonToken], embedded: dict[int, list[EmbeddedRef]]
) -> dict[int, str]:
    """Number fields (``filesize``) next to a whole-value embedded file, giving its
    size: rewritten with the size of the anonymized file."""
    by_parent: dict[int, str] = {}
    for index, refs in embedded.items():
        if len(refs) == 1 and refs[0].start == 0:
            by_parent[tokens[index].parent] = refs[0].child_id
    sizes: dict[int, str] = {}
    for i, token in enumerate(tokens):
        if token.kind != "number" or token.parent not in by_parent or not token.keys:
            continue
        key = token.keys[-1]
        if isinstance(key, str) and name_key(key) in _SIZE_NAMES:
            sizes[i] = by_parent[token.parent]
    return sizes


def _all_attributes(el: etree._Element, name: str, value: str) -> bool:
    return True


#: Segment of the DOCTYPE public and system identifiers.
DOCTYPE_SEGMENT = "xdoctype"
XML_SPEC = ScanSpec(para_tags=frozenset(), attr_filter=_all_attributes, comments=True)


def xml_units(root: etree._Element) -> list[Unit]:
    return scan(root, XML_SPEC)


def xml_tree_units(tree: etree._ElementTree) -> list[Unit]:
    """Units of the root element, then of top-level comments/PIs around it."""
    root = tree.getroot()
    units = xml_units(root)
    for sibling in (*root.itersiblings(preceding=True), *root.itersiblings()):
        if sibling.text and sibling.text.strip():
            units.append(Unit("text", sibling.text, sibling, slot="text"))
    return units


def xml_hint(unit: Unit) -> Hint:
    """What the element (or attribute) holding a unit and its neighbours say about
    the unit's value: an ``<attachment filename="a.pdf">`` body, a ``<mimetype>``
    sibling element..."""
    if unit.kind == "attr" and unit.attrs:
        element, attr = unit.attrs[0]
        own = local_name(attr)
    elif unit.kind == "text" and unit.slot == "text" and isinstance(unit.anchor.tag, str):
        element, attr = unit.anchor, ""
        own = local_name(element.tag)
    else:
        return Hint()
    siblings = {name_key(local_name(k)): str(v) for k, v in element.attrib.items() if k != attr}
    parent = element.getparent()
    if not attr and parent is not None:
        for other in parent:
            if other is not element and isinstance(other.tag, str) and other.text:
                siblings.setdefault(name_key(local_name(other.tag)), other.text)
    return hint_from(own, siblings)


def _label(tree: etree._ElementTree, unit: Unit) -> str:
    node = unit.anchor
    if not isinstance(node.tag, str):
        return "commentaire" if isinstance(node, etree._Comment) else "instruction"
    path = tree.getpath(node)
    if unit.kind == "attr" and unit.attrs:
        return f"{path}/@{local_name(unit.attrs[0][1])}"
    return path


@register_extractor
class XmlExtractor(Extractor):
    name = "xml"
    extensions = {
        ".xml": "xml",
        ".xsd": "xml",
        ".xsl": "xml",
        ".xslt": "xml",
        ".rss": "xml",
        ".atom": "xml",
        ".kml": "xml",
        ".gpx": "xml",
        ".xlf": "xml",
        ".xliff": "xml",
    }

    def extract(self, source: Path, document: Document, ctx: ExtractionContext) -> None:
        tree = parse_xml(source.read_bytes(), doctype=True)
        ids = doctype_ids(tree)
        if ids:
            document.segments.append(
                Segment(
                    id=DOCTYPE_SEGMENT,
                    text=ids,
                    locator=(),
                    context=SegmentContext(SegmentKind.HIDDEN, label="DOCTYPE"),
                )
            )
        embedded: dict[int, list[EmbeddedRef]] = {}
        for i, unit in enumerate(xml_tree_units(tree)):
            if unit.kind == "attr":
                kind = SegmentKind.ATTRIBUTE
            elif not isinstance(unit.anchor.tag, str):
                kind = SegmentKind.COMMENT
            else:
                kind = SegmentKind.BODY
            text = unit.text
            if kind is not SegmentKind.COMMENT:
                refs = extract_blobs(text, xml_hint(unit), document, ctx)
                if refs:
                    embedded[i] = refs
                    text = masked(text, refs)
                    if not visible(text):
                        continue
            line: int | None = getattr(unit.anchor, "sourceline", None)
            document.segments.append(
                Segment(
                    id=f"x{i}",
                    text=text,
                    locator=(i,),
                    context=SegmentContext(kind, label=_label(tree, unit), line=line or None),
                )
            )
        document.properties[EMBEDDED] = embedded
        document.properties[STRUCTURE] = vocabulary(
            name
            for el in tree.getroot().iter()
            if isinstance(el.tag, str)
            for name in (local_name(el.tag), *(local_name(k) for k in el.attrib))
            if is_structural(name)
        )
