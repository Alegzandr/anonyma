"""JSON (and JSON Lines) and generic XML extractors.

JSON: every string value, every key and every number (phone numbers are often
stored as numbers) is a segment; labels are JSON paths with keys masked.

XML: element text and tails, attribute values, comments, CDATA and processing
instructions. Parsing never resolves entities nor touches the network.
"""

from __future__ import annotations

from pathlib import Path

from lxml import etree

from anonyma.codec import decode_bytes
from anonyma.extractors._tabular import tokenize_json
from anonyma.extractors._xml import local_name, parse_xml
from anonyma.extractors._xmlscan import ScanSpec, Unit, scan
from anonyma.models import (
    Document,
    ExtractionContext,
    Extractor,
    Segment,
    SegmentContext,
    SegmentKind,
)
from anonyma.registry import register_extractor


@register_extractor
class JsonExtractor(Extractor):
    name = "json"
    extensions = {".json": "json", ".jsonl": "json", ".ndjson": "json", ".geojson": "json"}

    def extract(self, source: Path, document: Document, ctx: ExtractionContext) -> None:
        text, encoding = decode_bytes(source.read_bytes())
        document.properties["encoding"] = encoding
        for i, token in enumerate(tokenize_json(text)):
            kind = SegmentKind.ATTRIBUTE if token.kind == "key" else SegmentKind.BODY
            label = f"{token.path} (clé)" if token.kind == "key" else token.path
            document.segments.append(
                Segment(
                    id=f"j{i}",
                    text=token.value,
                    locator=(token.start, token.end),
                    context=SegmentContext(kind, label=label),
                )
            )


def _all_attributes(el: etree._Element, name: str, value: str) -> bool:
    return True


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
        tree = parse_xml(source.read_bytes())
        for i, unit in enumerate(xml_tree_units(tree)):
            if unit.kind == "attr":
                kind = SegmentKind.ATTRIBUTE
            elif not isinstance(unit.anchor.tag, str):
                kind = SegmentKind.COMMENT
            else:
                kind = SegmentKind.BODY
            document.segments.append(
                Segment(
                    id=f"x{i}",
                    text=unit.text,
                    locator=(i,),
                    context=SegmentContext(kind, label=_label(tree, unit)),
                )
            )
