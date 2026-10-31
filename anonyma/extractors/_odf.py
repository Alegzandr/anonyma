"""OpenDocument engine shared by the odt, ods and odp plugins.

Same principle as the OOXML engine: a deterministic scan of every XML entry
(``content.xml``, ``styles.xml`` with headers/footers and master pages,
embedded ``Object N/`` sub-documents, macros...) gives units addressed by
``(entry, unit index)``.

Writer policy: tracked changes are accepted (``text:tracked-changes`` holds the
deleted text: it is removed), annotations are kept but anonymized and their
author/date removed, ``meta.xml`` is emptied, the thumbnail and printer settings
are removed, embedded pictures/objects are replaced by their anonymized output
or a blank placeholder.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Mapping
from pathlib import Path

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
from anonyma.extractors._ooxml import group_replacements, source_path
from anonyma.extractors._xml import (
    XmlError,
    is_element,
    local_name,
    namespace,
    parse_xml,
    remove_keep_tail,
    serialize,
)
from anonyma.extractors._xmlscan import ScanSpec, Unit, apply_unit, scan
from anonyma.extractors._zip import ZipPackage, read_package, write_package
from anonyma.models import (
    Document,
    ExtractionContext,
    Issue,
    ReplacementPlan,
    Segment,
    SegmentContext,
    SegmentKind,
    Severity,
    WriteContext,
)

Element = etree._Element

OFFICE = "urn:oasis:names:tc:opendocument:xmlns:office:1.0"
TEXT = "urn:oasis:names:tc:opendocument:xmlns:text:1.0"
TABLE = "urn:oasis:names:tc:opendocument:xmlns:table:1.0"
DRAW = "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"
PRESENTATION = "urn:oasis:names:tc:opendocument:xmlns:presentation:1.0"
STYLE = "urn:oasis:names:tc:opendocument:xmlns:style:1.0"
META = "urn:oasis:names:tc:opendocument:xmlns:meta:1.0"
CONFIG = "urn:oasis:names:tc:opendocument:xmlns:config:1.0"
MANIFEST = "urn:oasis:names:tc:opendocument:xmlns:manifest:1.0"
DC = "http://purl.org/dc/elements/1.1/"
XLINK = "http://www.w3.org/1999/xlink"
FO = "urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0"
SVG = "urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0"
CALCEXT = "urn:org:documentfoundation:names:experimental:calc:xmlns:calcext:1.0"

MIMETYPE = "mimetype"
MANIFEST_PART = "META-INF/manifest.xml"

MIMETYPES = {
    "application/vnd.oasis.opendocument.text": "odt",
    "application/vnd.oasis.opendocument.text-template": "odt",
    "application/vnd.oasis.opendocument.spreadsheet": "ods",
    "application/vnd.oasis.opendocument.spreadsheet-template": "ods",
    "application/vnd.oasis.opendocument.presentation": "odp",
    "application/vnd.oasis.opendocument.presentation-template": "odp",
}


def sniff_odf(head: bytes) -> str | None:
    """ODF packages start with an uncompressed ``mimetype`` entry."""
    if len(head) < 30 or not head.startswith(b"PK"):
        return None
    name_len = int.from_bytes(head[26:28], "little")
    extra_len = int.from_bytes(head[28:30], "little")
    if head[30 : 30 + name_len] != b"mimetype":
        return None
    data = head[30 + name_len + extra_len : 30 + name_len + extra_len + 100]
    for mimetype in sorted(MIMETYPES, key=len, reverse=True):
        if data.startswith(mimetype.encode()):
            return MIMETYPES[mimetype]
    return None


def _q(ns: str, local: str) -> str:
    return f"{{{ns}}}{local}"


def _spaces(el: Element) -> str:
    try:
        count = int(el.get(_q(TEXT, "c"), "1"))
    except ValueError:
        count = 1
    return " " * max(1, min(count, 10_000))


EXCLUDED_NS = frozenset({DRAW, SVG, OFFICE, DC, META, TABLE, PRESENTATION, STYLE})

#: Attributes always exposed (local names).
ODF_TEXT_ATTRS = frozenset(
    {
        "title",
        "string-value",
        "formula",
        "name",
        "condition",
        "base-cell-address",
        "cell-range-address",
        "target-range-address",
        "source-cell-range",
        "description",
        "display-name",
        "label",
        "current-value",
        "help-message",
        "initials",
        "author",
    }
)
INTERNAL_HREF = ("#", "./Object", "Object ", "Pictures/", "./Pictures/", "ObjectReplacements/")
_DIGITS = re.compile(r"\d")
_TWO_WORDS = re.compile(r"[^\W\d_]{2,}\s+[^\W\d_]{2,}")
_PATHISH = re.compile(r"(?:[A-Za-z]:\\|\\\\|/Users/|/home/)")
MIN_NUMBER_DIGITS = 6


def odf_attr_filter(el: Element, name: str, value: str) -> bool:
    ns, local = namespace(name), local_name(name)
    if ns in (FO, SVG) or local.endswith("style-name") or local.endswith("font-family"):
        return False
    if local in ("font-name", "family", "value-type", "date-value", "time-value"):
        return False
    if "rsid" in local:
        return False
    if local == "href":
        return not value.startswith(INTERNAL_HREF)
    if local == "value":
        return len(_DIGITS.findall(value)) >= MIN_NUMBER_DIGITS or not _is_number(value)
    if local in ODF_TEXT_ATTRS:
        return True
    return "@" in value or bool(_TWO_WORDS.search(value)) or bool(_PATHISH.search(value))


def _is_number(value: str) -> bool:
    try:
        float(value)
    except ValueError:
        return False
    return True


class _NamespaceExclusion(frozenset[str]):
    """Exclusion set that also matches every element of a foreign namespace
    (frames, annotations, shapes...): they interrupt the enclosing paragraph and
    are handled by the generic fallback."""

    def __contains__(self, tag: object) -> bool:
        if isinstance(tag, str) and namespace(tag) in EXCLUDED_NS:
            return True
        return super().__contains__(tag)


def _build_spec() -> ScanSpec:
    exclude = _NamespaceExclusion(
        {
            _q(TEXT, "note"),
            _q(TEXT, "tracked-changes"),
            _q(TEXT, "ruby-text"),
        }
    )
    return ScanSpec(
        para_tags=frozenset({_q(TEXT, "p"), _q(TEXT, "h")}),
        mixed=True,
        virtual={
            _q(TEXT, "s"): _spaces,
            _q(TEXT, "tab"): "\t",
            _q(TEXT, "line-break"): "\n",
            _q(TEXT, "soft-page-break"): "",
        },
        exclude=exclude,
        skip_subtree=frozenset({_q(OFFICE, "binary-data")}),
        attr_filter=odf_attr_filter,
    )


SPEC = _build_spec()
META_SPEC = ScanSpec(para_tags=frozenset(), attr_filter=odf_attr_filter)


class OdfInfo:
    """Labels computed once per package (cell coordinates, slide numbers)."""

    def __init__(self) -> None:
        self.cells: dict[Element, str] = {}
        self.pages: dict[Element, int] = {}
        self.tables: dict[Element, int] = {}


def _column_letters(index: int) -> str:
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _int_attr(el: Element, name: str) -> int:
    try:
        return max(1, int(el.get(name, "1")))
    except ValueError:
        return 1


def prepare(root: Element, fmt: str) -> OdfInfo:
    info = OdfInfo()
    if fmt == "ods":
        table_index = 0
        for table in root.iter(_q(TABLE, "table")):
            table_index += 1
            info.tables[table] = table_index
            row = 0
            for row_el in table.iter(_q(TABLE, "table-row")):
                col = 0
                for cell in row_el:
                    if local_name(cell.tag) not in ("table-cell", "covered-table-cell"):
                        continue
                    info.cells[cell] = f"feuille {table_index}!{_column_letters(col)}{row + 1}"
                    col += _int_attr(cell, _q(TABLE, "number-columns-repeated"))
                row += _int_attr(row_el, _q(TABLE, "number-rows-repeated"))
    if fmt == "odp":
        for n, page in enumerate(root.iter(_q(DRAW, "page")), start=1):
            info.pages[page] = n
    return info


def _ancestors(el: Element) -> list[Element]:
    out: list[Element] = []
    node = el.getparent()
    while node is not None:
        out.append(node)
        node = node.getparent()
    return out


def context_for(entry: str, unit: Unit, info: OdfInfo, n: int) -> SegmentContext:
    if posixpath.basename(entry) == "meta.xml":
        return SegmentContext(SegmentKind.METADATA, label=local_name(unit.anchor.tag))
    chain = [unit.anchor, *_ancestors(unit.anchor)]
    locals_ = [local_name(e.tag) for e in chain]
    if unit.kind == "attr":
        attr = local_name(unit.attrs[0][1]) if unit.attrs else "attribut"
        if "table" in locals_ and attr == "name" and locals_[0] == "table":
            return SegmentContext(SegmentKind.ATTRIBUTE, label="nom de feuille")
        return SegmentContext(SegmentKind.ATTRIBUTE, label=f"attribut ({attr})")
    if "annotation" in locals_:
        if locals_[0] in ("creator", "date", "creator-initials"):
            return SegmentContext(SegmentKind.METADATA, label="auteur de commentaire")
        return SegmentContext(SegmentKind.COMMENT, label="commentaire")
    if "tracked-changes" in locals_ or "change-info" in locals_:
        return SegmentContext(SegmentKind.COMMENT, label="modification suivie")
    if "note" in locals_:
        return SegmentContext(SegmentKind.NOTE, label="note")
    for el in chain:
        if el in info.cells:
            return SegmentContext(SegmentKind.CELL, label=info.cells[el])
        if el in info.pages:
            number = info.pages[el]
            if "notes" in locals_:
                return SegmentContext(
                    SegmentKind.NOTE, label=f"notes diapositive {number}", page=number - 1
                )
            return SegmentContext(SegmentKind.BODY, label=f"diapositive {number}", page=number - 1)
    for local in locals_:
        if local.startswith("header"):
            return SegmentContext(SegmentKind.HEADER, label="en-tête")
        if local.startswith("footer"):
            return SegmentContext(SegmentKind.HEADER, label="pied de page")
    if "table-cell" in locals_:
        return SegmentContext(SegmentKind.CELL, label=f"tableau, bloc {n}")
    if "text-box" in locals_ or "frame" in locals_:
        return SegmentContext(SegmentKind.BODY, label=f"zone de texte, bloc {n}")
    base = posixpath.basename(entry)
    if base == "content.xml" and "/" not in entry:
        return SegmentContext(SegmentKind.BODY, label=f"corps, bloc {n}")
    if base == "styles.xml":
        return SegmentContext(SegmentKind.HIDDEN, label=f"styles, bloc {n}")
    if entry.startswith("Object "):
        return SegmentContext(SegmentKind.BODY, label=f"objet intégré, bloc {n}")
    return SegmentContext(SegmentKind.HIDDEN, label=f"{base}, bloc {n}")


def is_xml_entry(name: str) -> bool:
    return suffix(name) in (".xml", ".rdf")


DROPPED_PREFIXES = ("Thumbnails/", "layout-cache")


def _is_dropped(name: str) -> bool:
    return name.startswith(DROPPED_PREFIXES)


def _units(entry: str, root: Element) -> list[Unit]:
    if posixpath.basename(entry) == "meta.xml":
        return scan(root, META_SPEC)
    return scan(root, SPEC)


def _encrypted(package: ZipPackage) -> bool:
    manifest = package.entries.get(MANIFEST_PART)
    return manifest is not None and b"encryption-data" in manifest


def _main_info(trees: Mapping[str, etree._ElementTree], fmt: str) -> OdfInfo:
    tree = trees.get("content.xml")
    return prepare(tree.getroot(), fmt) if tree is not None else OdfInfo()


def extract(source: Path, document: Document, ctx: ExtractionContext, fmt: str) -> None:
    package = read_package(source, ctx.budget, ctx.depth)
    if _encrypted(package):
        document.add_issue(
            Severity.ERROR,
            "extract.encrypted",
            "Document protégé par mot de passe (chiffré) : impossible à lire.",
        )
        return
    trees: dict[str, etree._ElementTree] = {}
    unreadable = False
    for name in package.names:
        if is_xml_entry(name) and name != MANIFEST_PART:
            try:
                trees[name] = parse_xml(package.entries[name])
            except XmlError:
                unreadable = True
    info = _main_info(trees, fmt)
    children: dict[str, str] = {}
    seq = 0
    for name in package.names:
        ctx.check_cancelled()
        if name in (MIMETYPE, MANIFEST_PART) or _is_dropped(name):
            continue
        if name in trees:
            units = _units(name, trees[name].getroot())
            for index, unit in enumerate(units):
                document.segments.append(
                    Segment(
                        id=f"s{seq}",
                        text=unit.text,
                        locator=(name, index),
                        context=context_for(name, unit, info, index + 1),
                    )
                )
                seq += 1
        elif is_xml_entry(name):
            continue
        else:
            data = package.entries[name]
            if is_placeholder(data) or name.startswith("Fonts/"):
                continue
            child = ctx.extract_child(data, posixpath.basename(name), document)
            children[child.id] = name
    if unreadable:
        document.add_issue(
            Severity.ERROR,
            "office.xml",
            "Une partie XML du document est illisible : contenu non vérifiable.",
        )
    document.properties["odf_children"] = children


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

CONFIG_DROPPED = frozenset(
    {
        "PrinterName",
        "PrinterSetup",
        "PrinterPaperFromSetup",
        "CurrentDatabaseDataSource",
        "CurrentDatabaseCommand",
        "CurrentDatabaseCommandType",
    }
)
_INVALID_SHEET = re.compile(r"[\[\]*?:/\\]")


def _minimal_meta(tree: etree._ElementTree) -> bytes:
    version = tree.getroot().get(_q(OFFICE, "version"), "1.2")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<office:document-meta xmlns:office="{OFFICE}" xmlns:meta="{META}" '
        f'xmlns:dc="{DC}" office:version="{version}"><office:meta/></office:document-meta>'
    ).encode()


def purge(root: Element) -> None:
    for el in list(root.iter()):
        if not is_element(el):
            continue
        ns, local = namespace(el.tag), local_name(el.tag)
        if (
            (ns in (TEXT, TABLE) and local == "tracked-changes")
            or (ns == TEXT and local in ("change", "change-start", "change-end"))
            or (ns == OFFICE and local == "change-info")
        ):
            remove_keep_tail(el)
        elif ns in (DC, META) and local in ("creator", "date", "creator-initials"):
            parent = el.getparent()
            if parent is not None and local_name(parent.tag) == "annotation":
                remove_keep_tail(el)
        elif (
            ns == CONFIG
            and local == "config-item"
            and el.get(_q(CONFIG, "name")) in (CONFIG_DROPPED)
        ):
            remove_keep_tail(el)
        for name in list(el.attrib):
            if "rsid" in local_name(name):
                del el.attrib[name]
        value = el.get(_q(OFFICE, "value"))
        if value is not None and not _is_number(value):
            # A number replaced by a substitute becomes a text cell.
            del el.attrib[_q(OFFICE, "value")]
            el.set(_q(OFFICE, "value-type"), "string")
            if el.get(_q(CALCEXT, "value-type")) is not None:
                el.set(_q(CALCEXT, "value-type"), "string")


def sanitize_sheets(trees: Mapping[str, etree._ElementTree]) -> None:
    """Sheet names cannot hold ``[]*?:/\\``; references are renamed accordingly."""
    tree = trees.get("content.xml")
    if tree is None:
        return
    renames: dict[str, str] = {}
    used: set[str] = set()
    for table in tree.getroot().iter(_q(TABLE, "table")):
        name = table.get(_q(TABLE, "name"), "")
        clean = _INVALID_SHEET.sub("_", name.replace("[", "(").replace("]", ")")) or "Feuille"
        base, i = clean, 2
        while clean.casefold() in used:
            clean = f"{base} ({i})"
            i += 1
        used.add(clean.casefold())
        if clean != name:
            table.set(_q(TABLE, "name"), clean)
            renames[name] = clean
    if not renames:
        return
    for entry_tree in trees.values():
        for el in entry_tree.getroot().iter():
            if not is_element(el):
                continue
            for attr, value in list(el.attrib.items()):
                text = str(value)
                local = local_name(str(attr))
                if local not in (
                    "formula",
                    "base-cell-address",
                    "cell-range-address",
                    "target-range-address",
                    "source-cell-range",
                    "condition",
                    "href",
                ):
                    continue
                for old, new in renames.items():
                    quoted_old = "'" + old.replace("'", "''") + "'."
                    quoted_new = "'" + new.replace("'", "''") + "'."
                    text = text.replace(quoted_old, quoted_new)
                    text = re.sub(
                        r"(?<=[\[$:.;\s])" + re.escape(old) + r"\.",
                        quoted_new.replace("\\", "\\\\"),
                        text,
                    )
                el.set(attr, text)


def write(
    document: Document,
    plan: ReplacementPlan,
    destination: Path,
    ctx: WriteContext,
    fmt: str,
) -> list[Issue]:
    package = read_package(source_path(document), None)
    trees: dict[str, etree._ElementTree] = {}
    for name in package.names:
        if is_xml_entry(name) and name != MANIFEST_PART:
            trees[name] = parse_xml(package.entries[name])
    by_entry = group_replacements(document, plan)
    for name, tree in trees.items():
        units = _units(name, tree.getroot())
        for index, reps in by_entry.get(name, {}).items():
            apply_unit(units[index], reps)
    for name, tree in trees.items():
        if posixpath.basename(name) != "meta.xml":
            purge(tree.getroot())
    if fmt == "ods":
        sanitize_sheets(trees)
    manifest = (
        parse_xml(package.entries[MANIFEST_PART]) if MANIFEST_PART in package.entries else None
    )
    for name in package.names:
        if _is_dropped(name):
            del package.entries[name]
            if manifest is not None:
                _manifest_remove(manifest.getroot(), name)
    issues = _replace_children(package, trees, manifest, document, ctx)
    for name, tree in trees.items():
        if name in package.entries:
            package.entries[name] = (
                _minimal_meta(tree) if posixpath.basename(name) == "meta.xml" else serialize(tree)
            )
    if manifest is not None:
        package.entries[MANIFEST_PART] = serialize(manifest)
    write_package(package, destination, first=MIMETYPE, stored={MIMETYPE})
    return issues


def _manifest_remove(root: Element, name: str) -> None:
    for entry in list(root):
        if entry.get(_q(MANIFEST, "full-path")) == name:
            root.remove(entry)


def _replace_children(
    package: ZipPackage,
    trees: Mapping[str, etree._ElementTree],
    manifest: etree._ElementTree | None,
    document: Document,
    ctx: WriteContext,
) -> list[Issue]:
    children: Mapping[str, str] = document.properties.get("odf_children", {})
    blanked = 0
    for child in document.children:
        name = children.get(child.id)
        if name is None or name not in package.entries:
            continue
        output = ctx.child_outputs.get(child.id)
        new_ext: str | None
        if output is not None and output.path is not None:
            data, new_ext = output.path.read_bytes(), output.path.suffix.lower() or None
        else:
            blanked += 1
            if is_image_name(name):
                data, new_ext = PLACEHOLDER_PNG, ".png"
            else:
                data, new_ext = b"", None
        package.entries[name] = data
        if new_ext and new_ext != suffix(name):
            new_name = name[: len(name) - len(suffix(name))] + new_ext
            if new_name not in package.entries:
                _rename_entry(package, trees, manifest, name, new_name)
    if blanked:
        return [Issue(Severity.WARNING, BLANKED_CODE, blanked_message(blanked))]
    return []


def _rename_entry(
    package: ZipPackage,
    trees: Mapping[str, etree._ElementTree],
    manifest: etree._ElementTree | None,
    old: str,
    new: str,
) -> None:
    package.entries = {(new if k == old else k): v for k, v in package.entries.items()}
    for tree in trees.values():
        for el in tree.getroot().iter():
            if not is_element(el):
                continue
            href = el.get(_q(XLINK, "href"))
            if href in (old, "./" + old):
                el.set(_q(XLINK, "href"), href.replace(old, new))
    if manifest is not None:
        for entry in manifest.getroot():
            if entry.get(_q(MANIFEST, "full-path")) == old:
                entry.set(_q(MANIFEST, "full-path"), new)
                entry.set(
                    _q(MANIFEST, "media-type"),
                    CONTENT_TYPES.get(suffix(new), "application/octet-stream"),
                )
