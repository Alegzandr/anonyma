"""docx / xlsx / pptx specifics plugged into the OOXML engine."""

from __future__ import annotations

import posixpath
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from lxml import etree

from anonyma.extractors._ooxml import (
    ANONYMOUS,
    Flavor,
    Package,
    office_attr_filter,
)
from anonyma.extractors._xml import (
    attr_items,
    is_element,
    local_name,
    namespace,
    remove_keep_tail,
    unwrap,
)
from anonyma.extractors._xmlscan import ScanSpec, Unit, redistribute
from anonyma.fieldhints import header_hints, with_column_hint
from anonyma.models import (
    Issue,
    Replacement,
    SegmentContext,
    SegmentKind,
    apply_replacements,
)

Element = etree._Element

W_NS = frozenset(
    {
        "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
        "http://purl.oclc.org/ooxml/wordprocessingml/main",
    }
)
A_NS = frozenset(
    {
        "http://schemas.openxmlformats.org/drawingml/2006/main",
        "http://purl.oclc.org/ooxml/drawingml/main",
    }
)
M_NS = frozenset(
    {
        "http://schemas.openxmlformats.org/officeDocument/2006/math",
        "http://purl.oclc.org/ooxml/officeDocument/math",
    }
)
S_NS = frozenset(
    {
        "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
        "http://purl.oclc.org/ooxml/spreadsheetml/main",
    }
)
R_ID_NS = frozenset(
    {
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
        "http://purl.oclc.org/ooxml/officeDocument/relationships",
    }
)


def _tags(namespaces: Iterable[str], *locals_: str) -> frozenset[str]:
    return frozenset(f"{{{ns}}}{local}" for ns in namespaces for local in locals_)


PARA_TAGS = _tags(W_NS, "p") | _tags(A_NS, "p") | _tags(S_NS, "si", "is", "text")
PIECE_TAGS = (
    _tags(W_NS, "t", "delText", "instrText", "delInstrText")
    | _tags(A_NS, "t")
    | _tags(M_NS, "t")
    | _tags(S_NS, "t")
)
VIRTUAL: dict[str, str] = {
    **dict.fromkeys(_tags(W_NS, "tab", "ptab"), "\t"),
    **dict.fromkeys(_tags(W_NS, "br", "cr"), "\n"),
    **dict.fromkeys(_tags(W_NS, "noBreakHyphen"), "-"),
    **dict.fromkeys(_tags(A_NS, "br"), "\n"),
}
SKIP_SUBTREE = _tags(W_NS, "binData", "fldData")


def stream_of(el: Element, para: Element) -> str:
    local = local_name(el.tag)
    if local in ("delText", "delInstrText"):
        return "deleted"
    node = el.getparent()
    while node is not None and node is not para:
        node_local = local_name(node.tag)
        if node_local in ("del", "moveFrom") and namespace(node.tag) in W_NS:
            return "deleted"
        if node_local == "rPh":
            return "phonetic"
        node = node.getparent()
    if local == "instrText":
        return "field"
    return ""


SPEC = ScanSpec(
    para_tags=PARA_TAGS,
    piece_tags=PIECE_TAGS,
    virtual=VIRTUAL,
    stream_of=stream_of,
    skip_subtree=SKIP_SUBTREE,
    attr_filter=office_attr_filter,
)


def has_ancestor(el: Element, *locals_: str) -> bool:
    node = el.getparent()
    while node is not None:
        if local_name(node.tag) in locals_:
            return True
        node = node.getparent()
    return False


def ancestor(el: Element, local: str) -> Element | None:
    node: Element | None = el
    while node is not None:
        if local_name(node.tag) == local:
            return node
        node = node.getparent()
    return None


def _attr_context(unit: Unit, kind_label: str) -> SegmentContext:
    first = local_name(unit.attrs[0][1]) if unit.attrs else "attribut"
    return SegmentContext(SegmentKind.ATTRIBUTE, label=f"{kind_label} ({first})")


def _drop_with_rels(pkg: Package, part: str, el: Element) -> None:
    """Remove ``el`` and the relationships its r:id attributes point to."""
    ids = {
        value
        for node in el.iter()
        if is_element(node)
        for name, value in attr_items(node)
        if namespace(name) in R_ID_NS
    }
    remove_keep_tail(el)
    for rel_id in ids:
        pkg.remove_relationship(part, rel_id)


def _main_part(pkg: Package) -> str:
    for rel in pkg.relationships(""):
        if rel.type.endswith("/officeDocument") and not rel.external:
            return rel.target
    return ""


def _related(pkg: Package, part: str, type_suffix: str) -> list[str]:
    return [
        r.target for r in pkg.relationships(part) if not r.external and r.type.endswith(type_suffix)
    ]


# ---------------------------------------------------------------------------
# docx
# ---------------------------------------------------------------------------

CHANGE_TAGS = frozenset(
    {
        "rPrChange",
        "pPrChange",
        "sectPrChange",
        "tblPrChange",
        "tblGridChange",
        "tcPrChange",
        "trPrChange",
        "tblPrExChange",
        "numberingChange",
        "moveFromRangeStart",
        "moveFromRangeEnd",
        "moveToRangeStart",
        "moveToRangeEnd",
        "customXmlInsRangeStart",
        "customXmlInsRangeEnd",
        "customXmlDelRangeStart",
        "customXmlDelRangeEnd",
        "customXmlMoveFromRangeStart",
        "customXmlMoveFromRangeEnd",
        "customXmlMoveToRangeStart",
        "customXmlMoveToRangeEnd",
        "cellIns",
        "cellDel",
        "cellMerge",
    }
)
MARKER_PARENTS = frozenset({"rPr", "tcPr", "trPr", "numPr", "pPr"})


def _docx_role(part: str) -> tuple[SegmentKind, str]:
    base = posixpath.basename(part)
    if part.startswith("word/glossary/"):
        return SegmentKind.HIDDEN, "insertion automatique"
    if base == "document.xml":
        return SegmentKind.BODY, "corps"
    if base.startswith("header"):
        return SegmentKind.HEADER, "en-tête"
    if base.startswith("footer"):
        return SegmentKind.HEADER, "pied de page"
    if base.startswith("footnotes"):
        return SegmentKind.NOTE, "note de bas de page"
    if base.startswith("endnotes"):
        return SegmentKind.NOTE, "note de fin"
    if base.startswith("comments"):
        return SegmentKind.COMMENT, "commentaire"
    if "/charts/" in part:
        return SegmentKind.BODY, "graphique"
    if "/diagrams/" in part:
        return SegmentKind.BODY, "SmartArt"
    if base.startswith("people"):
        return SegmentKind.METADATA, "auteurs"
    if part.startswith("customXml/"):
        return SegmentKind.HIDDEN, "XML personnalisé"
    if base == "settings.xml":
        return SegmentKind.HIDDEN, "paramètres"
    return SegmentKind.HIDDEN, base


class DocxFlavor(Flavor):
    name = "docx"
    drop_patterns = (*Flavor.drop_patterns, "word/people.xml")

    def spec(self, part: str) -> ScanSpec:
        return SPEC

    def context(self, part: str, unit: Unit, info: Mapping[str, Any], n: int) -> SegmentContext:
        kind, label = _docx_role(part)
        if unit.kind == "attr":
            return _attr_context(unit, label)
        if unit.kind == "para" and kind is SegmentKind.BODY:
            if has_ancestor(unit.anchor, "txbxContent", "txBody"):
                label = "zone de texte"
            elif has_ancestor(unit.anchor, "tc"):
                kind, label = SegmentKind.CELL, "tableau"
        return SegmentContext(kind, label=f"{label}, bloc {n}")

    def purge(self, part: str, root: Element, pkg: Package, info: Mapping[str, Any]) -> None:
        purge_revisions(root)
        for el in root.iter():
            if not is_element(el):
                continue
            for name in list(el.attrib):
                if namespace(name) in W_NS and local_name(name).startswith("rsid"):
                    del el.attrib[name]
            if namespace(el.tag) in W_NS and local_name(el.tag) == "comment":
                _anonymize_author(el, W_NS)
        if namespace(root.tag) in W_NS and local_name(root.tag) == "settings":
            for el in list(root):
                if local_name(el.tag) in ("rsids", "docVars", "attachedTemplate", "mailMerge"):
                    _drop_with_rels(pkg, part, el)


def _anonymize_author(el: Element, namespaces: frozenset[str]) -> None:
    for name in list(el.attrib):
        if namespace(name) not in namespaces:
            continue
        local = local_name(name)
        if local == "author":
            el.set(name, ANONYMOUS)
        elif local == "initials":
            el.set(name, "A")
        elif local == "date":
            del el.attrib[name]


def purge_revisions(root: Element) -> None:
    """Accept every tracked change: keep insertions, drop deletions and history."""
    inserted: list[Element] = []
    deleted: list[Element] = []
    dropped: list[Element] = []
    for el in root.iter():
        if not is_element(el) or namespace(el.tag) not in W_NS:
            continue
        local = local_name(el.tag)
        if local in ("ins", "moveTo"):
            inserted.append(el)
        elif local in ("del", "moveFrom"):
            deleted.append(el)
        elif local in CHANGE_TAGS:
            dropped.append(el)
    for el in dropped:
        remove_keep_tail(el)
    for el in deleted:
        parent = el.getparent()
        if parent is None:
            continue
        parent_local = local_name(parent.tag)
        if parent_local == "trPr":
            row = parent.getparent()
            table = row.getparent() if row is not None else None
            if row is not None and table is not None:
                table.remove(row)
        elif parent_local in MARKER_PARENTS:
            parent.remove(el)
        else:
            remove_keep_tail(el)
    for el in inserted:
        parent = el.getparent()
        if parent is None:
            continue
        if local_name(parent.tag) in MARKER_PARENTS:
            parent.remove(el)
        else:
            unwrap(el)


# ---------------------------------------------------------------------------
# xlsx
# ---------------------------------------------------------------------------

FORMULA_TAGS = frozenset({"f", "formula", "formula1", "formula2", "definedName"})
_DIGITS = re.compile(r"\d")
_CELL_REF = re.compile(r"^([A-Z]{1,3})(\d+)$")


def _si_text(si: Element) -> str:
    """Visible text of a shared string (runs joined, phonetic hints left out)."""
    return "".join(
        t.text or ""
        for t in si.iter()
        if is_element(t) and local_name(t.tag) == "t" and ancestor(t, "rPh") is None
    )


def _column_hints(root: Element, shared: Sequence[str]) -> tuple[int, dict[str, str]]:
    """(header row number, column letters -> field hint) of a worksheet."""
    rows: dict[int, dict[int, str]] = {}
    letters: dict[int, str] = {}
    for c in root.iter():
        if not is_element(c) or local_name(c.tag) != "c":
            continue
        m = _CELL_REF.match(c.get("r", ""))
        if m is None:
            continue
        row = int(m.group(2))
        if rows and row > min(rows):
            break  # only the first non-empty row is a header candidate
        text = ""
        cell_type = c.get("t", "n")
        for child in c.iter():
            if not is_element(child):
                continue
            local = local_name(child.tag)
            if local == "v" and child.text:
                if cell_type == "s" and child.text.strip().isdigit():
                    index = int(child.text)
                    text = shared[index] if index < len(shared) else ""
                elif cell_type in ("str", "inlineStr"):
                    text = child.text
            elif local == "t" and cell_type == "inlineStr" and child.text:
                text += child.text
        if text.strip():
            col = _column_index(m.group(1))
            letters[col] = m.group(1)
            rows.setdefault(row, {})[col] = text
    if not rows:
        return 0, {}
    hints = header_hints(rows)
    return min(rows), {letters[c]: h for c, h in hints.items()}


def _column_index(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n


def _cell_label(sheet: int, ref: str, hints: tuple[int, dict[str, str]] | None) -> str:
    label = f"feuille {sheet}!{ref}"
    m = _CELL_REF.match(ref)
    if hints is None or m is None or int(m.group(2)) <= hints[0]:
        return label
    return with_column_hint(label, hints[1].get(m.group(1)))


_INVALID_SHEET = re.compile(r"[:\\/?*]")
_INVALID_NAME = re.compile(r"[^\w.]")
MIN_NUMBER_DIGITS = 6


def _xlsx_role(part: str) -> tuple[SegmentKind, str]:
    base = posixpath.basename(part)
    if "/charts/" in part:
        return SegmentKind.BODY, "graphique"
    if "/drawings/" in part:
        return SegmentKind.BODY, "dessin"
    if "/diagrams/" in part:
        return SegmentKind.BODY, "SmartArt"
    if "/pivot" in part:
        return SegmentKind.CELL, "tableau croisé dynamique"
    if "/tables/" in part:
        return SegmentKind.ATTRIBUTE, "tableau"
    if "/externalLinks/" in part:
        return SegmentKind.HIDDEN, "liaison externe"
    if "/revisions/" in part:
        return SegmentKind.COMMENT, "historique des révisions"
    if "/threadedComments/" in part:
        return SegmentKind.COMMENT, "commentaire"
    if "/persons/" in part:
        return SegmentKind.METADATA, "auteurs"
    if base == "workbook.xml":
        return SegmentKind.HIDDEN, "classeur"
    if part.startswith("customXml/"):
        return SegmentKind.HIDDEN, "XML personnalisé"
    return SegmentKind.HIDDEN, base


class XlsxFlavor(Flavor):
    name = "xlsx"
    drop_patterns = (*Flavor.drop_patterns, "xl/revisions/")

    def prepare(self, pkg: Package) -> dict[str, Any]:
        workbook = _main_part(pkg)
        sheets: dict[str, int] = {}
        if workbook in pkg.zip.entries:
            rels = {r.id: r.target for r in pkg.relationships(workbook) if not r.external}
            n = 0
            for el in pkg.root(workbook).iter():
                if is_element(el) and local_name(el.tag) == "sheet":
                    n += 1
                    rid = next((v for k, v in attr_items(el) if namespace(k) in R_ID_NS), None)
                    if rid and rid in rels:
                        sheets[rels[rid]] = n
        comments: dict[str, int] = {}
        shared_labels: dict[int, str] = {}
        shared_index: dict[Element, int] = {}
        shared_texts: list[str] = []
        for target in _related(pkg, workbook, "/sharedStrings"):
            if target in pkg.zip.entries:
                items = [el for el in pkg.root(target) if local_name(el.tag) == "si"]
                shared_index = {el: i for i, el in enumerate(items)}
                shared_texts = [_si_text(el) for el in items]
        column_hints: dict[str, tuple[int, dict[str, str]]] = {}
        for sheet in sheets:
            if sheet in pkg.zip.entries:
                column_hints[sheet] = _column_hints(pkg.root(sheet), shared_texts)
        for sheet, n in sheets.items():
            for target in _related(pkg, sheet, "/comments") + _related(
                pkg, sheet, "/threadedComment"
            ):
                comments[target] = n
            if sheet not in pkg.zip.entries:
                continue
            for c in pkg.root(sheet).iter():
                if not is_element(c) or local_name(c.tag) != "c" or c.get("t") != "s":
                    continue
                for v in c:
                    if local_name(v.tag) == "v" and v.text and v.text.strip().isdigit():
                        ref = c.get("r", "")
                        label = _cell_label(n, ref, column_hints.get(sheet))
                        shared_labels.setdefault(int(v.text), label)
        return {
            "workbook": workbook,
            "sheets": sheets,
            "comments": comments,
            "shared_labels": shared_labels,
            "shared_index": shared_index,
            "column_hints": column_hints,
        }

    def spec(self, part: str) -> ScanSpec:
        return SPEC

    def pre_units(
        self, part: str, root: Element, info: Mapping[str, Any]
    ) -> tuple[list[Unit], set[tuple[Element, str]]]:
        units: list[Unit] = []
        claimed: set[tuple[Element, str]] = set()
        if part not in info["sheets"]:
            return units, claimed
        for c in root.iter():
            if not is_element(c) or namespace(c.tag) not in S_NS:
                continue
            if local_name(c.tag) in HEADER_FOOTER_TAGS:
                claimed.add((c, "text"))
                if c.text and c.text.strip():
                    pieces = split_header_footer(c.text)
                    text = "".join(display for display, _raw, _editable in pieces)
                    units.append(Unit("hf", text, c, payload=pieces))
                continue
            if local_name(c.tag) != "c":
                continue
            ref = c.get("r", "")
            cell_type = c.get("t", "n")
            for child in c:
                local = local_name(child.tag)
                if local not in ("f", "v"):
                    continue
                claimed.add((child, "text"))
                text = child.text or ""
                if not text.strip():
                    continue
                if local == "f":
                    units.append(Unit("text", text, child, slot="text", payload=("f", ref)))
                elif cell_type in ("str", "d"):
                    units.append(Unit("text", text, child, slot="text", payload=("v", ref)))
                elif cell_type == "n" and len(_DIGITS.findall(text)) >= MIN_NUMBER_DIGITS:
                    units.append(Unit("cellnum", text, c, payload=("n", ref, child)))
        return units, claimed

    def context(self, part: str, unit: Unit, info: Mapping[str, Any], n: int) -> SegmentContext:
        sheets: Mapping[str, int] = info["sheets"]
        if part in sheets:
            sheet = sheets[part]
            hints = info["column_hints"].get(part)
            if isinstance(unit.payload, tuple):
                ref = unit.payload[1]
                if unit.payload[0] == "f":
                    label = f"feuille {sheet}!{ref} (formule)"
                else:
                    label = _cell_label(sheet, ref, hints)
                return SegmentContext(SegmentKind.CELL, label=label, page=sheet - 1)
            if unit.kind == "attr":
                return _attr_context(unit, f"feuille {sheet}")
            cell = ancestor(unit.anchor, "c")
            if cell is not None:
                return SegmentContext(
                    SegmentKind.CELL,
                    label=_cell_label(sheet, cell.get("r", ""), hints),
                    page=sheet - 1,
                )
            local = local_name(unit.anchor.tag)
            if "Header" in local or "Footer" in local:
                return SegmentContext(
                    SegmentKind.HEADER, label=f"en-tête/pied de page feuille {sheet}"
                )
            return SegmentContext(SegmentKind.HIDDEN, label=f"feuille {sheet} ({local})")
        if part in info["comments"]:
            sheet = info["comments"][part]
            if local_name(unit.anchor.tag) == "author":
                return SegmentContext(SegmentKind.METADATA, label="auteur de commentaire")
            comment = ancestor(unit.anchor, "comment")
            if comment is None:
                comment = ancestor(unit.anchor, "threadedComment")
            ref = comment.get("ref", "") if comment is not None else ""
            return SegmentContext(
                SegmentKind.COMMENT, label=f"commentaire feuille {sheet}!{ref}", page=sheet - 1
            )
        if local_name(unit.anchor.tag) == "si" and unit.anchor in info["shared_index"]:
            index = info["shared_index"][unit.anchor]
            return SegmentContext(
                SegmentKind.CELL, label=info["shared_labels"].get(index, "chaîne partagée")
            )
        kind, label = _xlsx_role(part)
        if unit.kind == "attr":
            if local_name(unit.anchor.tag) == "sheet":
                return SegmentContext(SegmentKind.ATTRIBUTE, label="nom de feuille")
            return _attr_context(unit, label)
        if local_name(unit.anchor.tag) == "definedName":
            return SegmentContext(SegmentKind.HIDDEN, label="nom défini")
        return SegmentContext(kind, label=f"{label}, bloc {n}")

    def apply_custom(self, unit: Unit, replacements: Sequence[Replacement]) -> None:
        if unit.kind == "hf":
            pieces: list[tuple[str, str, bool]] = unit.payload
            new = redistribute([(d, editable) for d, _raw, editable in pieces], replacements)
            unit.anchor.text = "".join(
                text.replace("&", "&&") if editable else raw
                for text, (_d, raw, editable) in zip(new, pieces, strict=True)
            )
            return
        if unit.kind != "cellnum":
            super().apply_custom(unit, replacements)
            return
        cell = unit.anchor
        value: Element = unit.payload[2]
        new_text = apply_replacements(unit.text, replacements)
        ns = namespace(cell.tag)
        inline = etree.Element(f"{{{ns}}}is")
        etree.SubElement(inline, f"{{{ns}}}t").text = new_text
        cell.insert(cell.index(value), inline)
        for child in list(cell):
            if local_name(child.tag) in ("f", "v"):
                cell.remove(child)
        cell.set("t", "inlineStr")

    def purge(self, part: str, root: Element, pkg: Package, info: Mapping[str, Any]) -> None:
        root_local = local_name(root.tag)
        for el in list(root.iter()):
            if not is_element(el):
                continue
            local = local_name(el.tag)
            if local == "absPath":
                remove_keep_tail(el)
            elif local == "fileSharing":
                if "userName" in el.attrib:
                    del el.attrib["userName"]
            elif local == "author" and root_local == "comments":
                el.text = ANONYMOUS
            elif local == "person" and root_local == "personList":
                el.set("displayName", ANONYMOUS)
                if "userId" in el.attrib:
                    el.set("userId", "")
                if "providerId" in el.attrib:
                    el.set("providerId", "None")

    def finish(self, pkg: Package, info: Mapping[str, Any]) -> list[Issue]:
        workbook = info["workbook"]
        if workbook not in pkg.trees:
            return []
        renames: dict[str, str] = {}
        used: set[str] = set()
        for el in pkg.root(workbook).iter():
            if not is_element(el):
                continue
            local = local_name(el.tag)
            if local == "sheet":
                name = el.get("name", "")
                new = sanitize_sheet_name(name, used)
                if new != name:
                    el.set("name", new)
                    renames[name] = new
            elif local == "definedName" and el.get("name"):
                el.set("name", sanitize_defined_name(el.get("name", "")))
        for part, tree in pkg.trees.items():
            if "/tables/" in part:
                table = tree.getroot()
                for attr in ("name", "displayName"):
                    if table.get(attr):
                        table.set(attr, sanitize_defined_name(table.get(attr, "")))
            if not renames:
                continue
            for el in tree.getroot().iter():
                if not is_element(el):
                    continue
                if local_name(el.tag) in FORMULA_TAGS and el.text:
                    el.text = rename_sheet_refs(el.text, renames)
                if el.get("location"):
                    el.set("location", rename_sheet_refs(el.get("location", ""), renames))
        return []


HEADER_FOOTER_TAGS = frozenset(
    {"oddHeader", "oddFooter", "evenHeader", "evenFooter", "firstHeader", "firstFooter"}
)
_HF_CODE = re.compile(r'&&|&"[^"]*"|&K[0-9A-Fa-f]{6}|&\d+|&.', re.DOTALL)


def split_header_footer(text: str) -> list[tuple[str, str, bool]]:
    """Split an Excel header/footer into ``(display, raw, editable)`` pieces.

    Format codes (``&L``, ``&"Arial,Bold"``, ``&12``...) become a virtual line
    break so that detectors never see them glued to the text.
    """
    pieces: list[tuple[str, str, bool]] = []
    cursor = 0
    for match in _HF_CODE.finditer(text):
        if match.start() > cursor:
            chunk = text[cursor : match.start()]
            pieces.append((chunk, chunk, True))
        code = match.group()
        pieces.append(("&", code, True) if code == "&&" else ("\n", code, False))
        cursor = match.end()
    if cursor < len(text):
        pieces.append((text[cursor:], text[cursor:], True))
    return pieces


def sanitize_sheet_name(name: str, used: set[str]) -> str:
    """Excel sheet names: no ``[]:*?/\\``, at most 31 characters, unique."""
    clean = _INVALID_SHEET.sub("_", name.replace("[", "(").replace("]", ")")).strip("'")
    clean = clean[:31] or "Feuille"
    base, i = clean, 2
    while clean.casefold() in used:
        tail = f" ({i})"
        clean = base[: 31 - len(tail)] + tail
        i += 1
    used.add(clean.casefold())
    return clean


def sanitize_defined_name(name: str) -> str:
    clean = _INVALID_NAME.sub("_", name)
    if not clean or not (clean[0].isalpha() or clean[0] in "_\\"):
        clean = f"_{clean}"
    return clean[:255]


def rename_sheet_refs(text: str, renames: Mapping[str, str]) -> str:
    for old, new in renames.items():
        quoted_new = "'" + new.replace("'", "''") + "'!"
        text = text.replace("'" + old.replace("'", "''") + "'!", quoted_new)
        text = re.sub(r"(?<![\w'.])" + re.escape(old) + "!", quoted_new.replace("\\", "\\\\"), text)
    return text


# ---------------------------------------------------------------------------
# pptx
# ---------------------------------------------------------------------------


def _pptx_role(part: str) -> tuple[SegmentKind, str]:
    base = posixpath.basename(part)
    if "/slideLayouts/" in part:
        return SegmentKind.HIDDEN, "disposition"
    if "/slideMasters/" in part:
        return SegmentKind.HIDDEN, "masque des diapositives"
    if "/notesMasters/" in part:
        return SegmentKind.HIDDEN, "masque des notes"
    if "/handoutMasters/" in part:
        return SegmentKind.HIDDEN, "masque du document"
    if "/comments/" in part:
        return SegmentKind.COMMENT, "commentaire"
    if base in ("commentAuthors.xml", "authors.xml"):
        return SegmentKind.METADATA, "auteurs de commentaires"
    if "/charts/" in part:
        return SegmentKind.BODY, "graphique"
    if "/diagrams/" in part:
        return SegmentKind.BODY, "SmartArt"
    if "/tags/" in part:
        return SegmentKind.HIDDEN, "balise"
    if part.startswith("customXml/"):
        return SegmentKind.HIDDEN, "XML personnalisé"
    return SegmentKind.HIDDEN, base


class PptxFlavor(Flavor):
    name = "pptx"

    def prepare(self, pkg: Package) -> dict[str, Any]:
        presentation = _main_part(pkg)
        slides: dict[str, int] = {}
        if presentation in pkg.zip.entries:
            rels = {r.id: r.target for r in pkg.relationships(presentation) if not r.external}
            n = 0
            for el in pkg.root(presentation).iter():
                if is_element(el) and local_name(el.tag) == "sldId":
                    n += 1
                    rid = next((v for k, v in attr_items(el) if namespace(k) in R_ID_NS), None)
                    if rid and rid in rels:
                        slides[rels[rid]] = n
        notes: dict[str, int] = {}
        for part in pkg.names:
            if part.startswith("ppt/notesSlides/") and part.endswith(".xml"):
                for target in _related(pkg, part, "/slide"):
                    if target in slides:
                        notes[part] = slides[target]
        return {"slides": slides, "notes": notes}

    def spec(self, part: str) -> ScanSpec:
        return SPEC

    def context(self, part: str, unit: Unit, info: Mapping[str, Any], n: int) -> SegmentContext:
        slides: Mapping[str, int] = info["slides"]
        if part in slides or part in info["notes"]:
            number = slides[part] if part in slides else info["notes"][part]
            is_notes = part not in slides
            label = f"notes diapositive {number}" if is_notes else f"diapositive {number}"
            if unit.kind == "attr":
                ctx = _attr_context(unit, label)
                return SegmentContext(ctx.kind, ctx.label, page=number - 1)
            kind = SegmentKind.NOTE if is_notes else SegmentKind.BODY
            if not is_notes and has_ancestor(unit.anchor, "tc"):
                kind = SegmentKind.CELL
            return SegmentContext(kind, label=label, page=number - 1)
        kind, label = _pptx_role(part)
        if unit.kind == "attr":
            return _attr_context(unit, label)
        return SegmentContext(kind, label=f"{label}, bloc {n}")

    def purge(self, part: str, root: Element, pkg: Package, info: Mapping[str, Any]) -> None:
        for el in root.iter():
            if not is_element(el):
                continue
            local = local_name(el.tag)
            if local == "cmAuthor" or (local == "author" and local_name(root.tag) == "authorLst"):
                el.set("name", ANONYMOUS)
                if "initials" in el.attrib:
                    el.set("initials", "A")
                if "userId" in el.attrib:
                    el.set("userId", "")
                if "providerId" in el.attrib:
                    el.set("providerId", "None")
