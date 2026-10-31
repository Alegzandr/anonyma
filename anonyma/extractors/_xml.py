"""Safe XML parsing shared by the Office/ODF/XML plugins.

Every parser refuses external entities, DTD loading and network access, and
keeps comments, processing instructions and CDATA so that writers can round-trip
documents.
"""

from __future__ import annotations

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


class XmlError(ValueError):
    """Raised when an XML part cannot be parsed."""


def parse_xml(data: bytes, *, recover: bool = False) -> etree._ElementTree:
    """Parse ``data`` safely; raise :class:`XmlError` (value-free message) on failure."""
    try:
        root = etree.fromstring(data, _RECOVER if recover else _STRICT)
    except etree.XMLSyntaxError as exc:
        raise XmlError("XML invalide") from exc
    if root is None:
        raise XmlError("XML vide")
    return root.getroottree()


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
