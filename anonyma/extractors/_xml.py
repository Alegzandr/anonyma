"""Safe XML parsing shared by the Office/ODF/XML plugins.

Every parser refuses external entities, DTD loading and network access, and
keeps comments, processing instructions and CDATA so that writers can round-trip
documents.

A DOCTYPE is refused (:class:`XmlError`, the part fails closed) unless the caller
reads standalone XML files (``doctype=True``): then every reference to an internal
entity is replaced by its text (bounded: :data:`MAX_ENTITY_TEXT`), so that it is
exposed and replaced like any other text, references to external entities are
dropped (never resolved), and the writer drops the internal subset
(:func:`drop_doctype`): entity values and attribute defaults never reach the output.
"""

from __future__ import annotations

import re
from typing import Any, cast

from lxml import etree

#: Strict parser: no entity expansion, no DTD, no network.
_STRICT = etree.XMLParser(
    resolve_entities=False,
    no_network=True,
    load_dtd=False,
    dtd_validation=False,
    huge_tree=False,
    remove_blank_text=False,
    remove_comments=False,
    remove_pis=False,
    strip_cdata=False,
)

#: Recovering parser for the sloppy XML some producers emit (Excel VML).
_RECOVER = etree.XMLParser(
    resolve_entities=False,
    no_network=True,
    load_dtd=False,
    dtd_validation=False,
    huge_tree=False,
    recover=True,
    remove_comments=False,
    remove_pis=False,
    strip_cdata=False,
)
#: Characters internal entities may insert in one document (entity bombs fail).
MAX_ENTITY_TEXT = 1_000_000
_MAX_ENTITY_DEPTH = 16
_REFERENCE = re.compile(r"&(#[0-9]+|#x[0-9A-Fa-f]+|[A-Za-z_:][\w.:-]*);")
_PREDEFINED = {"lt": "<", "gt": ">", "amp": "&", "quot": '"', "apos": "'"}


class XmlError(ValueError):
    """Raised when an XML part cannot be parsed."""


def parse_xml(data: bytes, *, recover: bool = False, doctype: bool = False) -> etree._ElementTree:
    """Parse ``data`` safely; raise :class:`XmlError` (value-free message) on failure.

    ``doctype``: accept a DOCTYPE and expand its internal entities (standalone XML
    files); otherwise a DOCTYPE is an error (Office and ODF parts never have one)."""
    try:
        root = etree.fromstring(data, _RECOVER if recover and not doctype else _STRICT)
    except etree.XMLSyntaxError as exc:
        raise XmlError("XML invalide") from exc
    if root is None:
        raise XmlError("XML vide")
    tree = root.getroottree()
    info = _docinfo(tree)
    if info.doctype or info.internalDTD is not None:
        if not doctype:
            raise XmlError("DTD refusée")
        _expand_entities(tree)
    return tree


def _expand_entities(tree: etree._ElementTree) -> None:
    """Replace every entity reference by its text (internal entities) or nothing
    (external or unknown ones), in text and in attribute values."""
    dtd = _docinfo(tree).internalDTD
    decls: dict[str, Any] = {e.name: e for e in dtd.iterentities()} if dtd is not None else {}
    values: dict[str, str] = {}
    budget = [MAX_ENTITY_TEXT]

    def charge(size: int) -> None:
        budget[0] -= size
        if budget[0] < 0:
            raise XmlError("entités trop volumineuses")

    def expand(name: str, stack: tuple[str, ...]) -> str:
        if name in values:
            return values[name]
        decl = decls.get(name)
        if decl is None or decl.system_url or decl.content is None:
            return ""
        if name in stack or len(stack) >= _MAX_ENTITY_DEPTH:
            raise XmlError("entités récursives")
        parts: list[str] = []
        content = str(decl.content)
        cursor = 0
        for match in _REFERENCE.finditer(content):
            parts.append(content[cursor : match.start()])
            ref = match.group(1)
            if ref.startswith("#"):
                code = int(ref[2:], 16) if ref[1:2] == "x" else int(ref[1:])
                parts.append(chr(code) if 0 < code <= 0x10FFFF else "")
            else:
                parts.append(_PREDEFINED.get(ref) or expand(ref, (*stack, name)))
            charge(len(parts[-1]))
            cursor = match.end()
        parts.append(content[cursor:])
        values[name] = "".join(parts)
        return values[name]

    root = tree.getroot()
    for el in root.iter():
        if is_element(el):
            # libxml2 keeps entity references inside attributes: store plain values.
            for key, value in el.attrib.items():
                el.set(key, value)
    for node in list(root.iter(etree.Entity)):
        text = expand(str(cast(Any, node).name), ())
        charge(len(text))
        parent = node.getparent()
        if parent is None:
            continue
        joined = text + (node.tail or "")
        previous = node.getprevious()
        if previous is not None:
            previous.tail = (previous.tail or "") + joined
        else:
            parent.text = (parent.text or "") + joined
        parent.remove(node)


def _docinfo(tree: etree._ElementTree) -> Any:
    """``tree.docinfo``, untyped: the stubs lack ``doctype`` and ``system_url``."""
    return cast(Any, tree.docinfo)


def doctype_ids(tree: etree._ElementTree) -> str:
    """Public and system identifiers of the DOCTYPE (``""`` when none)."""
    info = _docinfo(tree)
    return " ".join(i for i in (info.public_id, info.system_url) if i)


def drop_doctype(tree: etree._ElementTree, *, keep_ids: bool) -> None:
    """Remove the internal subset of the DOCTYPE (entity values, attribute defaults):
    nothing of the DTD reaches the output (references were expanded on parsing).
    ``keep_ids``: keep a DOCTYPE with the public and system identifiers only."""
    info = _docinfo(tree)
    if not info.doctype and info.internalDTD is None:
        return
    public_id, system_url = info.public_id, info.system_url
    info.clear()
    if keep_ids and (public_id or system_url):
        if public_id:
            info.public_id = public_id
        info.system_url = system_url


def serialize(tree: etree._ElementTree) -> bytes:
    """Serialize with an XML declaration, keeping the original encoding/standalone."""
    info = tree.docinfo
    encoding = info.encoding or "UTF-8"
    standalone = info.standalone
    if not standalone:
        data = etree.tostring(tree, xml_declaration=True, encoding=encoding)
    else:
        data = etree.tostring(tree, xml_declaration=True, encoding=encoding, standalone=standalone)
    return data if isinstance(data, bytes) else data.encode(encoding)


def attr_items(el: etree._Element) -> list[tuple[str, str]]:
    """Attributes of ``el`` as ``(clark name, value)`` strings."""
    return [(str(k), str(v)) for k, v in el.attrib.items()]


def qn(ns: str, local: str) -> str:
    """Clark notation ``{ns}local``."""
    return f"{{{ns}}}{local}"


def local_name(tag: object) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def namespace(tag: object) -> str:
    if not isinstance(tag, str) or not tag.startswith("{"):
        return ""
    return tag[1:].split("}", 1)[0]


def is_element(node: object) -> bool:
    """True for real elements (not comments, PIs or entity references)."""
    return isinstance(node, etree._Element) and isinstance(node.tag, str)


def unwrap(el: etree._Element) -> None:
    """Replace ``el`` by its children, keeping text and tails in place."""
    parent = el.getparent()
    if parent is None:
        return
    index = parent.index(el)
    text = el.text or ""
    tail = el.tail or ""
    children = list(el)
    if text:
        if index == 0:
            parent.text = (parent.text or "") + text
        else:
            prev = parent[index - 1]
            prev.tail = (prev.tail or "") + text
    for offset, child in enumerate(children):
        parent.insert(index + offset, child)
    last_index = index + len(children)
    if tail:
        if children:
            last = children[-1]
            last.tail = (last.tail or "") + tail
        elif last_index == 0:
            parent.text = (parent.text or "") + tail
        else:
            prev = parent[last_index - 1]
            prev.tail = (prev.tail or "") + tail
    parent.remove(el)


def remove_keep_tail(el: etree._Element) -> None:
    """Remove ``el`` and its subtree, keeping its tail text."""
    parent = el.getparent()
    if parent is None:
        return
    tail = el.tail
    if tail:
        prev = el.getprevious()
        if prev is not None:
            prev.tail = (prev.tail or "") + tail
        else:
            parent.text = (parent.text or "") + tail
    parent.remove(el)
