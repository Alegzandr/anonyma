"""OOXML package engine shared by the docx, xlsx and pptx plugins.

The extractor and the writer both walk the original package with the same
deterministic scan (:mod:`anonyma.extractors._xmlscan`), so a segment locator
``(part name, unit index)`` addresses exactly one unit in the writer.

Package-level policy (all flavors):

* every XML part is scanned (body, headers, notes, comments, charts, SmartArt,
  custom XML, settings...); external relationship targets (hyperlinks, linked
  files, attached templates) are exposed as attributes;
* binary parts (images, embedded objects) become children; the writer puts the
  anonymized child back, or a blank placeholder when the child could not be
  anonymized;
* metadata is purged: ``docProps/core.xml`` and ``app.xml`` are emptied,
  ``custom.xml``, thumbnails, printer settings and macros are removed.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from lxml import etree

from anonyma.extractors._media import (
    BLANKED_CODE,
    CONTENT_TYPES,
    PLACEHOLDER_PNG,
    blanked_message,
    is_image_name,
    is_placeholder,
    suffix,
)
from anonyma.extractors._xml import XmlError, local_name, namespace, parse_xml, serialize
from anonyma.extractors._xmlscan import ScanSpec, Unit, apply_unit, scan
from anonyma.extractors._zip import ZipPackage, read_package, write_package
from anonyma.models import (
    ANONYMOUS_AUTHOR,
    FILENAME_SEGMENT_ID,
    Document,
    ExtractionContext,
    Issue,
    Replacement,
    ReplacementPlan,
    Segment,
    SegmentContext,
    SegmentKind,
    Severity,
    WriteContext,
)

Element = etree._Element

CONTENT_TYPES_PART = "[Content_Types].xml"
CORE = "docProps/core.xml"
APP = "docProps/app.xml"
CUSTOM = "docProps/custom.xml"

NS_CT = "http://schemas.openxmlformats.org/package/2006/content-types"
NS_PR = "http://schemas.openxmlformats.org/package/2006/relationships"
#: Relationship id attribute namespaces (transitional and strict).
NS_R = frozenset(
    {
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
        "http://purl.oclc.org/ooxml/officeDocument/relationships",
    }
)
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"

ANONYMOUS = ANONYMOUS_AUTHOR

MIN_CORE = (
    b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
    b'<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/'
    b'core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" '
    b'xmlns:dcterms="http://purl.org/dc/terms/" xmlns:dcmitype="http://purl.org/dc/dcmitype/" '
    b'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"/>'
)
MIN_APP = (
    b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
    b'<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"'
    b' xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
    b"<DocSecurity>0</DocSecurity></Properties>"
)

ENCRYPTED_CODE = "extract.encrypted"
ENCRYPTED_MESSAGE = "Document protégé par mot de passe (chiffré) : impossible à lire."
CFB_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


# ---------------------------------------------------------------------------
# Relationship helpers
# ---------------------------------------------------------------------------


def rels_name(part: str) -> str:
    directory, base = posixpath.split(part)
    return posixpath.join(directory, "_rels", f"{base}.rels")


def rels_source(rels: str) -> str:
    """Part owning a ``.rels`` file (``""`` for the package relationships)."""
    directory, base = posixpath.split(rels)
    owner_dir = posixpath.dirname(directory)
    return posixpath.join(owner_dir, base[: -len(".rels")]) if base != ".rels" else ""


def resolve_target(source: str, target: str) -> str:
    target = unquote(target.split("#", 1)[0])
    if target.startswith("/"):
        return posixpath.normpath(target.lstrip("/"))
    return posixpath.normpath(posixpath.join(posixpath.dirname(source), target))


@dataclass(slots=True)
class Relationship:
    element: Element
    id: str
    type: str
    target: str  # resolved part name, or raw target for external ones
    external: bool


# ---------------------------------------------------------------------------
# Package
# ---------------------------------------------------------------------------


class Package:
    """Zip entries plus lazily parsed XML trees."""

    def __init__(self, zip_package: ZipPackage) -> None:
        self.zip = zip_package
        self.trees: dict[str, etree._ElementTree] = {}
        self._defaults: dict[str, str] = {}
        self._overrides: dict[str, str] = {}
        if CONTENT_TYPES_PART in self.zip.entries:
            root = self.tree(CONTENT_TYPES_PART).getroot()
            for el in root:
                if local_name(el.tag) == "Default":
                    self._defaults[(el.get("Extension") or "").lower()] = el.get("ContentType", "")
                elif local_name(el.tag) == "Override":
                    name = (el.get("PartName") or "").lstrip("/")
                    self._overrides[unquote(name)] = el.get("ContentType", "")

    @property
    def names(self) -> list[str]:
        return list(self.zip.entries)

    def content_type(self, part: str) -> str:
        if part in self._overrides:
            return self._overrides[part]
        return self._defaults.get(suffix(part).lstrip("."), "")

    def is_xml(self, part: str) -> bool:
        if suffix(part) in (".xml", ".rels", ".vml"):
            return True
        ctype = self.content_type(part)
        return ctype.endswith("xml")

    def tree(self, part: str) -> etree._ElementTree:
        tree = self.trees.get(part)
        if tree is None:
            data = self.zip.entries[part]
            try:
                tree = parse_xml(data)
            except XmlError:
                if suffix(part) != ".vml":
                    raise
                # Excel writes HTML-style ``<br>`` in VML text boxes.
                fixed = re.sub(rb"<br\s*>", b"<br/>", data)
                try:
                    tree = parse_xml(fixed)
                except XmlError:
                    tree = parse_xml(fixed, recover=True)
            self.trees[part] = tree
        return tree

    def root(self, part: str) -> Element:
        return self.tree(part).getroot()

    def relationships(self, part: str) -> list[Relationship]:
        rels = rels_name(part) if part else "_rels/.rels"
        if rels not in self.zip.entries:
            return []
        out: list[Relationship] = []
        for el in self.root(rels):
            if local_name(el.tag) != "Relationship":
                continue
            external = el.get("TargetMode") == "External"
            target = el.get("Target", "")
            out.append(
                Relationship(
                    el,
                    el.get("Id", ""),
                    el.get("Type", ""),
                    target if external else resolve_target(part, target),
                    external,
                )
            )
        return out

    def rels_files(self) -> list[str]:
        return [n for n in self.zip.entries if n.endswith(".rels")]

    # -- mutations (writer) -------------------------------------------------

    def remove_relationship(self, part: str, rel_id: str) -> None:
        """Remove relationship ``rel_id`` of ``part`` and every r:id attribute using it."""
        for rel in self.relationships(part):
            if rel.id == rel_id:
                parent = rel.element.getparent()
                if parent is not None:
                    parent.remove(rel.element)
        if part and part in self.zip.entries and self.is_xml(part):
            _drop_rid_attributes(self.root(part), {rel_id})

    def remove_part(self, part: str) -> None:
        if part not in self.zip.entries:
            return
        del self.zip.entries[part]
        self.trees.pop(part, None)
        own_rels = rels_name(part)
        if own_rels in self.zip.entries:
            del self.zip.entries[own_rels]
            self.trees.pop(own_rels, None)
        for rels in self.rels_files():
            source = rels_source(rels)
            removed: set[str] = set()
            for rel in self.relationships(source):
                if not rel.external and rel.target == part:
                    removed.add(rel.id)
                    parent = rel.element.getparent()
                    if parent is not None:
                        parent.remove(rel.element)
            if removed and source in self.zip.entries and self.is_xml(source):
                _drop_rid_attributes(self.root(source), removed)
        root = self.root(CONTENT_TYPES_PART)
        for el in list(root):
            if (
                local_name(el.tag) == "Override"
                and unquote((el.get("PartName") or "").lstrip("/")) == part
            ):
                root.remove(el)
        self._overrides.pop(part, None)

    def rename_part(self, old: str, new: str) -> None:
        if old == new or old not in self.zip.entries:
            return
        self.zip.entries = {(new if k == old else k): v for k, v in self.zip.entries.items()}
        if old in self.trees:
            self.trees[new] = self.trees.pop(old)
        old_rels, new_rels = rels_name(old), rels_name(new)
        if old_rels in self.zip.entries:
            self.zip.entries = {
                (new_rels if k == old_rels else k): v for k, v in self.zip.entries.items()
            }
            if old_rels in self.trees:
                self.trees[new_rels] = self.trees.pop(old_rels)
        old_base, new_base = posixpath.basename(old), posixpath.basename(new)
        for rels in self.rels_files():
            for rel in self.relationships(rels_source(rels)):
                if rel.external or rel.target != old:
                    continue
                raw = rel.element.get("Target", "")
                if unquote(raw).endswith(old_base):
                    rel.element.set("Target", unquote(raw)[: -len(old_base)] + new_base)
                else:
                    rel.element.set("Target", "/" + new)
        ext = suffix(new)
        ctype = CONTENT_TYPES.get(ext, "application/octet-stream")
        root = self.root(CONTENT_TYPES_PART)
        for el in root:
            if (
                local_name(el.tag) == "Override"
                and unquote((el.get("PartName") or "").lstrip("/")) == old
            ):
                el.set("PartName", "/" + new)
                el.set("ContentType", ctype)
        if old in self._overrides:
            self._overrides[new] = ctype
            del self._overrides[old]
        if ext.lstrip(".") not in self._defaults and new not in self._overrides:
            default = etree.Element(f"{{{NS_CT}}}Default")
            default.set("Extension", ext.lstrip("."))
            default.set("ContentType", ctype)
            root.insert(0, default)
            self._defaults[ext.lstrip(".")] = ctype

    def flush(self) -> None:
        """Serialize every parsed tree back into the entries."""
        for part, tree in self.trees.items():
            if part in self.zip.entries:
                self.zip.entries[part] = serialize(tree)


def _drop_rid_attributes(root: Element, ids: set[str]) -> None:
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        for name in list(el.attrib):
            if namespace(name) in NS_R and el.get(name) in ids:
                del el.attrib[name]


# ---------------------------------------------------------------------------
# Flavors
# ---------------------------------------------------------------------------

#: Attributes always exposed (local names), whatever their namespace.
TEXT_ATTRS = frozenset(
    {
        "descr",
        "title",
        "name",
        "alias",
        "tooltip",
        "display",
        "location",
        "displayText",
        "value",
        "instr",
        "author",
        "initials",
        "userId",
        "displayName",
        "userName",
        "label",
        "caption",
        "prompt",
        "promptTitle",
        "error",
        "errorTitle",
        "text",
        "v",
        "uniqueName",
        "formatCode",
        "url",
        "alt",
        "comment",
        "description",
        "connection",
        "command",
        "sourceFile",
        "odcFile",
        "codeName",
        "fmla",
        "sourceRef",
        "ref3D",
    }
)
#: ``w:val`` is exposed only under these elements (form fields, content controls...).
VAL_PARENTS = frozenset(
    {"alias", "tag", "docVar", "name", "default", "helpText", "statusText", "listEntry", "format"}
)
#: Attributes never exposed (structural).
STOP_ATTRS = frozenset(
    {
        "Ignorable",
        "ProcessContent",
        "PreserveElements",
        "PreserveAttributes",
        "MustUnderstand",
        "Requires",
        "space",
        "type",
        "uri",
        "Extension",
        "ContentType",
        "PartName",
        "Id",
        "Type",
        "TargetMode",
        "Target",
        "style",
        "path",
        "coordsize",
        "coordorigin",
        "adj",
        "connectlocs",
        "textboxrect",
        "equationxml",
        "points",
        "ref",
        "sqref",
        "spans",
        "r",
        "s",
        "t",
        "val",
        "id",
        "paraId",
        "textId",
        "durableId",
        "styleId",
        "clsid",
        "progId",
        "ProgID",
        "spid",
        "spt",
        "fillcolor",
        "strokecolor",
        "ascii",
        "hAnsi",
        "eastAsia",
        "cs",
    }
)

_TWO_WORDS = re.compile(r"[^\W\d_]{2,}\s+[^\W\d_]{2,}")
_PATHISH = re.compile(r"(?:[A-Za-z]:\\|\\\\|/Users/|/home/)")


def office_attr_filter(el: Element, name: str, value: str) -> bool:
    if namespace(name) in NS_R:
        return False
    local = local_name(name)
    if local.startswith("rsid"):
        return False
    if local == "val":
        return local_name(el.tag) in VAL_PARENTS
    if local in TEXT_ATTRS:
        return True
    if local in STOP_ATTRS:
        return False
    return "@" in value or bool(_TWO_WORDS.search(value)) or bool(_PATHISH.search(value))


def rels_attr_filter(el: Element, name: str, value: str) -> bool:
    return name == "Target" and el.get("TargetMode") == "External"


RELS_SPEC = ScanSpec(para_tags=frozenset(), attr_filter=rels_attr_filter)


def metadata_attr_filter(el: Element, name: str, value: str) -> bool:
    return local_name(name) == "name"


META_SPEC = ScanSpec(para_tags=frozenset(), attr_filter=metadata_attr_filter)


def metadata_label(unit: Unit) -> str:
    el: Element | None = unit.anchor
    while el is not None and namespace(el.tag).endswith("docPropsVTypes"):
        el = el.getparent()
    return local_name(el.tag) if el is not None else "propriété"


def preserve_space(el: Element, slot: str) -> None:
    """Mark text nodes whose spaces must be kept (``w:t``, ``s:t``)."""
    if slot != "text" or local_name(el.tag) not in ("t", "delText", "instrText"):
        return
    text = el.text or ""
    if text != text.strip() or "  " in text:
        el.set(XML_SPACE, "preserve")


class Flavor:
    """Format-specific behaviour plugged into the engine."""

    name = "ooxml"
    #: Parts dropped by the writer and never scanned.
    skip_patterns: tuple[str, ...] = (
        "docProps/thumbnail",
        "/printerSettings/",
        "vbaProject.bin",
        "vbaData.xml",
    )
    #: Parts scanned (for detection) but dropped by the writer.
    drop_patterns: tuple[str, ...] = (CUSTOM,)

    def is_skipped(self, part: str) -> bool:
        return any(p in part for p in self.skip_patterns)

    def is_dropped(self, part: str) -> bool:
        return self.is_skipped(part) or any(p in part for p in self.drop_patterns)

    def prepare(self, pkg: Package) -> dict[str, Any]:
        return {}

    def spec(self, part: str) -> ScanSpec:
        raise NotImplementedError

    def pre_units(
        self, part: str, root: Element, info: Mapping[str, Any]
    ) -> tuple[list[Unit], set[tuple[Element, str]]]:
        return [], set()

    def context(self, part: str, unit: Unit, info: Mapping[str, Any], n: int) -> SegmentContext:
        raise NotImplementedError

    def apply_custom(self, unit: Unit, replacements: Sequence[Replacement]) -> None:
        raise ValueError(f"unknown unit kind {unit.kind}")

    def purge(self, part: str, root: Element, pkg: Package, info: Mapping[str, Any]) -> None:
        """Remove personal metadata from one XML part (after replacements)."""

    def finish(self, pkg: Package, info: Mapping[str, Any]) -> list[Issue]:
        """Package-level post-processing (after purge)."""
        return []


def stream_context(unit: Unit) -> SegmentContext | None:
    if unit.stream == "field":
        return SegmentContext(SegmentKind.ATTRIBUTE, label="code de champ")
    if unit.stream == "deleted":
        return SegmentContext(SegmentKind.COMMENT, label="texte supprimé (révision)")
    if unit.stream == "phonetic":
        return SegmentContext(SegmentKind.HIDDEN, label="phonétique")
    return None


def part_units(pkg: Package, flavor: Flavor, part: str, info: Mapping[str, Any]) -> list[Unit]:
    root = pkg.root(part)
    if part.endswith(".rels"):
        return scan(root, RELS_SPEC)
    if part.startswith("docProps/"):
        return scan(root, META_SPEC)
    pre, claimed = flavor.pre_units(part, root, info)
    return scan(root, flavor.spec(part), pre_units=pre, claimed=claimed)


def _context(
    flavor: Flavor, part: str, unit: Unit, info: Mapping[str, Any], n: int
) -> SegmentContext:
    if part.endswith(".rels"):
        return SegmentContext(SegmentKind.ATTRIBUTE, label="lien")
    if part.startswith("docProps/"):
        return SegmentContext(SegmentKind.METADATA, label=metadata_label(unit))
    return stream_context(unit) or flavor.context(part, unit, info, n)


def is_font(part: str) -> bool:
    return "/fonts/" in part or suffix(part) in (".odttf", ".fntdata", ".ttf", ".otf")


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def extract(source: Path, document: Document, ctx: ExtractionContext, flavor: Flavor) -> None:
    with source.open("rb") as fh:
        head = fh.read(8)
    if head == CFB_MAGIC:
        document.add_issue(Severity.ERROR, ENCRYPTED_CODE, ENCRYPTED_MESSAGE)
        return
    pkg = Package(read_package(source, ctx.budget, ctx.depth))
    if CONTENT_TYPES_PART not in pkg.zip.entries:
        raise ValueError("paquet OOXML invalide")
    info = flavor.prepare(pkg)
    children: dict[str, str] = {}
    seq = 0
    unreadable = False
    for part in pkg.names:
        ctx.check_cancelled()
        if part == CONTENT_TYPES_PART or flavor.is_skipped(part):
            continue
        if pkg.is_xml(part):
            try:
                units = part_units(pkg, flavor, part, info)
            except XmlError:
                unreadable = True
                continue
            for index, unit in enumerate(units):
                document.segments.append(
                    Segment(
                        id=f"s{seq}",
                        text=unit.text,
                        locator=(part, index),
                        context=_context(flavor, part, unit, info, index + 1),
                    )
                )
                seq += 1
        elif not is_font(part):
            data = pkg.zip.entries[part]
            if is_placeholder(data):
                continue
            child = ctx.extract_child(data, posixpath.basename(part), document)
            children[child.id] = part
    if unreadable:
        document.add_issue(
            Severity.ERROR,
            "office.xml",
            "Une partie XML du document est illisible : contenu non vérifiable.",
        )
    document.properties["ooxml_children"] = children


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def source_path(document: Document) -> Path:
    """The file holding the package: a LibreOffice conversion for legacy formats."""
    converted = document.properties.get("converted_path")
    return Path(converted) if converted else document.source


def group_replacements(
    document: Document, plan: ReplacementPlan
) -> dict[str, dict[int, list[Replacement]]]:
    by_part: dict[str, dict[int, list[Replacement]]] = {}
    for seg in document.segments:
        if seg.id == FILENAME_SEGMENT_ID:
            continue
        reps = plan.for_segment(seg.id)
        if reps and len(seg.locator) == 2 and isinstance(seg.locator[1], int):
            by_part.setdefault(str(seg.locator[0]), {})[seg.locator[1]] = reps
    return by_part


def replace_children(
    pkg: Package,
    document: Document,
    ctx: WriteContext,
    children: Mapping[str, str],
    rename: Callable[[str, str], None],
) -> list[Issue]:
    """Put anonymized children back; blank the ones that could not be anonymized."""
    blanked = 0
    for child in document.children:
        part = children.get(child.id)
        if part is None or part not in pkg.zip.entries:
            continue
        output = ctx.child_outputs.get(child.id)
        new_ext: str | None
        if output is not None and output.path is not None:
            data = output.path.read_bytes()
            new_ext = output.path.suffix.lower() or None
        else:
            blanked += 1
            if is_image_name(part):
                data, new_ext = PLACEHOLDER_PNG, ".png"
            else:
                data, new_ext = b"", None
        pkg.zip.entries[part] = data
        if new_ext and new_ext != suffix(part):
            new_part = part[: len(part) - len(suffix(part))] + new_ext
            if new_part not in pkg.zip.entries:
                rename(part, new_part)
    if blanked:
        return [Issue(Severity.WARNING, BLANKED_CODE, blanked_message(blanked))]
    return []


def write(
    document: Document,
    plan: ReplacementPlan,
    destination: Path,
    ctx: WriteContext,
    flavor: Flavor,
) -> list[Issue]:
    pkg = Package(read_package(source_path(document), None))
    info = flavor.prepare(pkg)
    by_part = group_replacements(document, plan)
    xml_parts = [
        p
        for p in pkg.names
        if p != CONTENT_TYPES_PART and not flavor.is_skipped(p) and pkg.is_xml(p)
    ]
    for part in xml_parts:
        units = part_units(pkg, flavor, part, info)
        for index, reps in by_part.get(part, {}).items():
            unit = units[index]
            if unit.kind in ("para", "text", "attr"):
                apply_unit(unit, reps, preserve_space)
            else:
                flavor.apply_custom(unit, reps)
    for part in xml_parts:
        if not part.startswith("docProps/") and not part.endswith(".rels"):
            flavor.purge(part, pkg.root(part), pkg, info)
    issues = list(flavor.finish(pkg, info))
    for part in pkg.names:
        if flavor.is_dropped(part):
            pkg.remove_part(part)
    issues.extend(
        replace_children(
            pkg, document, ctx, document.properties.get("ooxml_children", {}), pkg.rename_part
        )
    )
    pkg.flush()
    if CORE in pkg.zip.entries:
        pkg.zip.entries[CORE] = MIN_CORE
    if APP in pkg.zip.entries:
        pkg.zip.entries[APP] = MIN_APP
    first = CONTENT_TYPES_PART if CONTENT_TYPES_PART in pkg.zip.entries else None
    write_package(pkg.zip, destination, first=first)
    return issues
