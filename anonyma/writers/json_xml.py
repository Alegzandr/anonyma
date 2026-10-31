"""JSON and XML writers.

JSON: replacements are spliced into the original text (formatting, key order and
escaping style kept); a replaced number becomes a string.

XML: the tree is re-serialized with its declaration, encoding and DOCTYPE; CDATA
sections stay CDATA.
"""

from __future__ import annotations

import codecs
from pathlib import Path

from lxml import etree

from anonyma.codec import TextEncoding, decode_bytes
from anonyma.extractors._tabular import encode_json_string, tokenize_json
from anonyma.extractors._xml import parse_xml
from anonyma.extractors._xmlscan import Unit, apply_unit
from anonyma.extractors.json_xml import xml_tree_units
from anonyma.models import (
    Document,
    Issue,
    ReplacementPlan,
    WriteContext,
    Writer,
    apply_replacements,
)
from anonyma.registry import register_writer


@register_writer
class JsonWriter(Writer):
    name = "json"
    formats = frozenset({"json"})

    def write(
        self,
        document: Document,
        plan: ReplacementPlan,
        destination: Path,
        ctx: WriteContext,
    ) -> list[Issue]:
        text, detected = decode_bytes(document.source.read_bytes())
        encoding: TextEncoding = document.properties.get("encoding", detected)
        out: list[str] = []
        cursor = 0
        for i, token in enumerate(tokenize_json(text)):
            reps = plan.for_segment(f"j{i}")
            if not reps:
                continue
            out.append(text[cursor : token.start])
            out.append(
                encode_json_string(apply_replacements(token.value, reps), token.ascii_escaped)
            )
            cursor = token.end
        out.append(text[cursor:])
        destination.write_bytes(encoding.encode("".join(out)))
        return []


def _is_cdata(unit: Unit) -> bool:
    el = unit.anchor
    if unit.kind != "text" or unit.slot != "text" or not isinstance(el.tag, str):
        return False
    raw = etree.tostring(el, with_tail=False, encoding="unicode")
    start = raw.find(">") + 1
    return raw[start:].startswith("<![CDATA[")


@register_writer
class XmlWriter(Writer):
    name = "xml"
    formats = frozenset({"xml"})

    def write(
        self,
        document: Document,
        plan: ReplacementPlan,
        destination: Path,
        ctx: WriteContext,
    ) -> list[Issue]:
        data = document.source.read_bytes()
        tree = parse_xml(data)
        units = xml_tree_units(tree)
        cdata: list[Unit] = []
        for i, unit in enumerate(units):
            reps = plan.for_segment(f"x{i}")
            if not reps:
                continue
            if _is_cdata(unit):
                cdata.append(unit)
            apply_unit(unit, reps)
        for unit in cdata:
            unit.anchor.text = etree.CDATA(unit.anchor.text or "")
        bom = codecs.BOM_UTF8 if data.startswith(codecs.BOM_UTF8) else b""
        has_declaration = data[len(bom) :].lstrip().startswith(b"<?xml")
        info = tree.docinfo
        encoding = info.encoding or "UTF-8"
        if info.standalone:
            body = etree.tostring(tree, xml_declaration=True, encoding=encoding, standalone=True)
        else:
            body = etree.tostring(tree, xml_declaration=has_declaration, encoding=encoding)
        destination.write_bytes(bom + (body if isinstance(body, bytes) else body.encode()))
        return []
