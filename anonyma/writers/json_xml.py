"""JSON and XML writers.

JSON: replacements are spliced into the original text (formatting, key order and
escaping style kept); a replaced number becomes a string.

XML: the tree is re-serialized in its encoding; CDATA sections stay CDATA. The XML
declaration is copied byte for byte (quotes, encoding name casing) and so is the
whitespace around the top-level nodes (processing instructions, comments, DOCTYPE,
root element, trailing newline); CRLF line ends are restored. Internal entities were
expanded on reading; the internal subset of the DOCTYPE is dropped (only its
public/system identifiers are kept, unless they held a value).
"""

from __future__ import annotations

import codecs
import re
from pathlib import Path
from typing import Any, cast

from lxml import etree

from anonyma.codec import TextEncoding, decode_bytes
from anonyma.extractors._tabular import encode_json_string, tokenize_json
from anonyma.extractors._xml import drop_doctype, parse_xml
from anonyma.extractors._xmlscan import Unit, apply_unit
from anonyma.extractors.json_xml import DOCTYPE_SEGMENT, xml_tree_units
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


#: One top-level construct of an XML document before its root element.
_PROLOG_ITEM = re.compile(
    r"\s+|<\?.*?\?>|<!--.*?-->|<!DOCTYPE(?:[^\[>\"']|\"[^\"]*\"|'[^']*'|\[.*?\])*>",
    re.S,
)
_DECLARATION = re.compile(r"<\?xml\s")


def _epilog_items(text: str) -> tuple[int, list[str]]:
    """Start of the epilog (after the root element) and its items, in order."""
    end = len(text)
    items: list[str] = []
    while end:
        stripped = text[:end].rstrip()
        if len(stripped) < end:
            items.append(text[len(stripped) : end])
            end = len(stripped)
            continue
        opener = "<?" if stripped.endswith("?>") else "<!--" if stripped.endswith("-->") else ""
        start = text.rfind(opener, 0, end) if opener else -1
        if start < 0:
            break
        items.append(text[start:end])
        end = start
    return end, items[::-1]


def _same_layout(data: bytes, bom: bytes, tree: etree._ElementTree, encoding: str) -> bytes | None:
    """The anonymized ``tree`` serialized with the original declaration and top-level
    whitespace; None when the layout cannot be mapped (the caller falls back to a
    plain serialization)."""
    try:
        codec = codecs.lookup(encoding).name
    except LookupError:
        return None
    if "<?".encode(codec, "replace") != b"<?":
        return None  # not ASCII-compatible (UTF-16...): plain serialization
    text = data[len(bom) :].decode(codec, "replace")
    prolog: list[str] = []
    pos = 0
    while (match := _PROLOG_ITEM.match(text, pos)) is not None and match.end() > pos:
        prolog.append(match.group(0))
        pos = match.end()
    _, epilog = _epilog_items(text)
    root = tree.getroot()
    before = list(root.itersiblings(preceding=True))[::-1]
    after = list(root.itersiblings())

    def nodes(items: list[str]) -> int:
        return sum(
            1
            for i in items
            if i.startswith(("<?", "<!--")) and not (i is prolog[0] and _DECLARATION.match(i))
        )

    if nodes(prolog) != len(before) or nodes(epilog) != len(after):
        return None

    # Parsing turned CRLF into LF: a CRLF document gets its line ends back (a parser
    # reads both the same way; attribute newlines are serialized as references).
    crlf = text.count("\r\n") * 2 > text.count("\n")

    def node(element: etree._Element) -> bytes:
        raw = etree.tostring(element, encoding=encoding, xml_declaration=False, with_tail=False)
        out = raw if isinstance(raw, bytes) else raw.encode(codec)
        return out.replace(b"\n", b"\r\n") if crlf else out

    out = bytearray(bom)
    pending = iter(before)
    for index, item in enumerate(prolog):
        if index == 0 and _DECLARATION.match(item):
            out += item.encode(codec)  # declaration: no value, copied as written
        elif item.startswith("<!DOCTYPE"):
            doctype = str(cast(Any, tree.docinfo).doctype or "")  # untyped in the stubs
            out += doctype.encode(codec)
        elif item.startswith(("<?", "<!--")):
            out += node(next(pending))
        else:
            out += item.encode(codec)
    out += node(root)
    pending = iter(after)
    for item in epilog:
        out += node(next(pending)) if item.startswith(("<?", "<!--")) else item.encode(codec)
    return bytes(out)


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
        tree = parse_xml(data, doctype=True)
        drop_doctype(tree, keep_ids=not plan.for_segment(DOCTYPE_SEGMENT))
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
        same = _same_layout(data, bom, tree, encoding)
        if same is not None:
            destination.write_bytes(same)
            return []
        if info.standalone:
            body = etree.tostring(tree, xml_declaration=True, encoding=encoding, standalone=True)
        else:
            body = etree.tostring(tree, xml_declaration=has_declaration, encoding=encoding)
        destination.write_bytes(bom + (body if isinstance(body, bytes) else body.encode()))
        return []
