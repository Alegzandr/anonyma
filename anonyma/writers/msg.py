"""Outlook .msg writer: the compound file is rewritten in place of a copy.

Every storage/stream is copied except what the policy of
:mod:`anonyma.extractors._msgfile` drops; string properties, bodies, transport
headers and attachment data are replaced by their anonymized versions and the
sizes in each ``__properties_version1.0`` stream are updated.
"""

from __future__ import annotations

import re
import struct
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath

from anonyma.codec import chunk_text
from anonyma.extractors._cfb import CfbEntry, Entry, read_cfb, write_cfb
from anonyma.extractors._html import HtmlSource, plan_edits
from anonyma.extractors._mailheaders import (
    apply_tracked,
    content_id_key,
    new_content_id,
    safe_address,
    safe_header,
    safe_message_ids,
    thread_depth,
)
from anonyma.extractors._mime import (
    DROPPED_HEADERS,
    cid_values,
    decode_header_value,
    encode_header_text,
    fold_header,
)
from anonyma.extractors._msgfile import (
    ATTACH_BY_VALUE,
    ATTACH_OLE,
    ATTACH_PATHS,
    MV_STRING_TYPES,
    PR_ATTACH_DATA,
    PR_ATTACH_EXTENSION,
    PR_ATTACH_FILENAME,
    PR_ATTACH_LONG_FILENAME,
    PR_ATTACH_SIZE,
    PR_BODY,
    PR_DISPLAY_NAME,
    PR_HTML,
    PR_NATIVE_BODY_INFO,
    PR_RTF_COMPRESSED,
    PR_RTF_IN_SYNC,
    PROPS,
    PT_BINARY,
    PT_LONG,
    PT_MV_BINARY,
    PT_UNICODE,
    STRING_TYPES,
    MsgTree,
    PropEntry,
    PropObject,
    TextItem,
    build_props,
    decode_string,
    encode_string,
    html_codec,
    parse_name,
    split_headers,
    string_size,
    text_items,
)
from anonyma.extractors.html import decode_html
from anonyma.models import (
    ChildOutput,
    Document,
    Issue,
    ReplacementPlan,
    Segment,
    WriteContext,
    Writer,
)
from anonyma.registry import register_writer

#: Attachment string properties replaced by the anonymized child file name.
_NAME_PROPS = (PR_ATTACH_LONG_FILENAME, PR_ATTACH_FILENAME, PR_DISPLAY_NAME)
#: PR_ATTACH_CONTENT_ID: regenerated, the ``cid:`` URLs of the HTML body follow.
PR_ATTACH_CONTENT_ID = 0x3712
#: Address properties (sender, on behalf of, recipients, received by): a bare
#: ``addr-spec`` that mail clients parse, kept valid.
_ADDRESS_PROPS = frozenset(
    {0x0C1F, 0x0065, 0x5D01, 0x5D02, 0x3003, 0x39FE, 0x0076, 0x0078, 0x5D07, 0x5D08}
)
#: PR_INTERNET_MESSAGE_ID, PR_IN_REPLY_TO_ID, PR_INTERNET_REFERENCES.
_MESSAGE_ID_PROPS = frozenset({0x1035, 0x1042, 0x1039})


def _new_text(
    item: TextItem,
    segments: Sequence[Segment],
    plan: ReplacementPlan,
    content_ids: Mapping[str, str] | None = None,
) -> str:
    if item.kind == "html":
        source = HtmlSource(item.text)
        edits = plan_edits(source, segments, plan)
        values = cid_values(source, content_ids or {})
        for unit in values:
            edits.pop(unit, None)
        return source.rewrite(edits, values)
    if item.kind in ("body", "rtf"):
        by_offset = {int(s.locator[1]): s for s in segments}
        return "".join(
            plan.apply(by_offset[offset]) if offset in by_offset else chunk
            for offset, chunk in chunk_text(item.text)
        )
    if item.kind == "headers":
        by_index = {int(s.locator[1]): s for s in segments}
        prefix, headers = split_headers(item.text)
        lines = [prefix + "\r\n"] if prefix else []
        depth = thread_depth((name, decode_header_value(raw)) for name, raw in headers)
        for i, (name, raw) in enumerate(headers):
            segment = by_index.get(i)
            if name.lower() in DROPPED_HEADERS:
                continue
            replacements = plan.for_segment(segment.id) if segment is not None else []
            if segment is not None and replacements:
                new = safe_header(name, segment.text, replacements, depth)
                lines.append(fold_header(name, encode_header_text(new), "\r\n"))
            else:
                lines.append(f"{name}: {re.sub(r'\r?\n', '\r\n', raw.strip('\r\n'))}\r\n")
        return "".join(lines) + "\r\n"
    if segments:
        return plan.apply(segments[0])
    return item.text


class _Rewriter:
    def __init__(
        self,
        tree: MsgTree,
        document: Document,
        plan: ReplacementPlan,
        child_outputs: Mapping[str, ChildOutput],
    ) -> None:
        self.tree = tree
        self.plan = plan
        self.child_outputs = child_outputs
        self.children: dict[str, str] = document.properties.get("msg_children", {})
        self.segments: dict[str, list[Segment]] = {}
        for segment in document.segments:
            base = segment.id.split("|", 1)[0]
            self.segments.setdefault(base, []).append(segment)
        self.dropped: set[Entry] = set()  # storages removed with their subtree
        #: Message object -> original Content-ID (comparable) -> regenerated one.
        self.content_ids: dict[Entry, dict[str, str]] = {}
        for obj in tree.attachments():
            for ptype in STRING_TYPES:
                data = obj.stream(PR_ATTACH_CONTENT_ID, ptype)
                if data is None:
                    continue
                key = content_id_key(decode_string(data, ptype, tree.codec))
                ids = self.content_ids.setdefault(obj.path[:-1], {})
                if key and key not in ids:
                    ids[key] = new_content_id(len(ids) + 1)
        self.data: dict[Entry, bytes | None] = {}  # stream path -> new data (None: drop)
        self.added: list[CfbEntry] = []

    def run(self) -> list[CfbEntry]:
        for obj in self.tree.attachments():
            self._attachment(obj)
        items = {(item.owner, item.stream.upper()): item for item in text_items(self.tree)}
        for obj in self.tree.objects:
            if any(obj.path[: len(d)] == d for d in self.dropped):
                continue
            self._object(obj, items)
        kept = [
            e
            for e in self.tree.entries
            if not any(e.path[: len(d)] == d and len(e.path) >= len(d) for d in self.dropped)
        ]
        out: list[CfbEntry] = []
        for entry in kept:
            if entry.storage:
                out.append(CfbEntry(entry.path, True, entry.clsid))
                continue
            data = self.data.get(entry.path, entry.data)
            if entry.path in self.data and data is None:
                continue
            out.append(CfbEntry(entry.path, False, entry.clsid, data))
        return out + self.added

    # -- attachments -------------------------------------------------------------

    def _attachment(self, obj: PropObject) -> None:
        method = obj.attach_method
        if method == ATTACH_OLE:
            self.dropped.add(obj.path)
            return
        if method != ATTACH_BY_VALUE or obj.stream(PR_ATTACH_DATA, PT_BINARY) is None:
            return
        child_id = self.children.get("/".join(obj.path))
        output = self.child_outputs.get(child_id) if child_id else None
        if output is None or output.path is None:
            self.dropped.add(obj.path)  # never keep original bytes

    def _child_output(self, obj: PropObject) -> ChildOutput | None:
        child_id = self.children.get("/".join(obj.path))
        output = self.child_outputs.get(child_id) if child_id else None
        return output if output is not None and output.path is not None else None

    # -- property objects ----------------------------------------------------------

    def _object(self, obj: PropObject, items: Mapping[tuple[Entry, str], TextItem]) -> None:
        sizes: dict[tuple[int, int], int] = {}
        removed: set[tuple[int, int]] = {(PR_RTF_IN_SYNC, 0x000B)}
        child = self._child_output(obj) if obj.kind == "attachment" else None
        child_name = PurePosixPath(child.name.replace("\\", "/")).name if child else None
        mv_lengths: dict[tuple[int, int], dict[int, int]] = {}
        for name, entry in obj.streams.items():
            parsed = parse_name(entry.path[-1])
            assert parsed is not None
            pid, ptype, index = parsed
            path = entry.path
            if ptype in (PT_BINARY, PT_MV_BINARY) and not (
                pid == PR_HTML or (pid == PR_ATTACH_DATA and obj.kind == "attachment")
            ):
                self.data[path] = None
                removed.add((pid, ptype))
                continue
            if pid == PR_ATTACH_DATA and ptype == PT_BINARY and child is not None:
                assert child.path is not None
                new_data = child.path.read_bytes()
                self.data[path] = new_data
                sizes[(pid, ptype)] = len(new_data)
                continue
            if pid in ATTACH_PATHS and obj.kind == "attachment":
                self.data[path] = None
                removed.add((pid, ptype))
                continue
            is_string = ptype in STRING_TYPES or (ptype in MV_STRING_TYPES and index is not None)
            item = items.get((obj.path, name))
            if item is None and is_string:
                # Strings are never left unexposed except X500/Exchange DNs: drop.
                if index is None:
                    self.data[path] = None
                    removed.add((pid, ptype))
                else:
                    self.data[path] = b""
                    mv_lengths.setdefault((pid, ptype), {})[index] = string_size(b"", ptype)
                continue
            if item is None:
                continue  # GUIDs, multi-valued fixed types, embedded storages...
            segments = self.segments.get(item.segment_id, [])
            if child_name is not None and pid in _NAME_PROPS:
                new = child_name
            elif child_name is not None and pid == PR_ATTACH_EXTENSION:
                new = PurePosixPath(child_name).suffix
            elif obj.kind == "attachment" and pid == PR_ATTACH_CONTENT_ID:
                ids = self.content_ids.get(obj.path[:-1], {})
                new = ids.get(content_id_key(item.text)) or new_content_id(0)
            elif item.kind == "string" and pid in _ADDRESS_PROPS and segments:
                new = safe_address(self.plan.apply(segments[0]))
            elif item.kind == "string" and pid in _MESSAGE_ID_PROPS and segments:
                replacements = self.plan.for_segment(segments[0].id)
                new = segments[0].text
                if replacements:
                    new = safe_message_ids(*apply_tracked(segments[0].text, replacements))
            else:
                new = _new_text(item, segments, self.plan, self.content_ids.get(obj.path))
            if item.kind == "html" and ptype == PT_BINARY:
                codec = html_codec(obj)
                encoding = decode_html(entry.data or b"")[1]
                data = (
                    new.encode(codec, "xmlcharrefreplace")
                    if codec
                    else encoding.bom + new.encode(encoding.encoding, "xmlcharrefreplace")
                )
                self.data[path] = data
                sizes[(pid, ptype)] = len(data)
                continue
            data = encode_string(new, ptype, self.tree.codec)
            self.data[path] = data
            if index is None:
                sizes[(pid, ptype)] = string_size(data, ptype)
            else:
                mv_lengths.setdefault((pid, ptype), {})[index] = string_size(data, ptype)
        for (pid, ptype), lengths in mv_lengths.items():
            length_entry = obj.streams.get(f"__substg1.0_{pid:04X}{ptype:04X}".upper())
            if length_entry is None:
                continue
            table = bytearray(length_entry.data or b"")
            for index, size in lengths.items():
                if 4 * index + 4 <= len(table):
                    struct.pack_into("<I", table, 4 * index, size)
            self.data[length_entry.path] = bytes(table)
        self._rtf_body(obj, items, sizes)
        self._props(obj, sizes, removed, child)

    def _rtf_body(
        self,
        obj: PropObject,
        items: Mapping[tuple[Entry, str], TextItem],
        sizes: dict[tuple[int, int], int],
    ) -> None:
        name = f"__substg1.0_{PR_BODY:04X}{PT_UNICODE:04X}"
        item = items.get((obj.path, name.upper()))
        if item is None or item.kind != "rtf":
            return
        new = _new_text(item, self.segments.get(item.segment_id, []), self.plan)
        data = encode_string(new, PT_UNICODE, self.tree.codec)
        self.added.append(CfbEntry((*obj.path, name), False, b"", data))
        sizes[(PR_BODY, PT_UNICODE)] = string_size(data, PT_UNICODE)

    def _props(
        self,
        obj: PropObject,
        sizes: Mapping[tuple[int, int], int],
        removed: set[tuple[int, int]],
        child: ChildOutput | None,
    ) -> None:
        if obj.stream(PR_RTF_COMPRESSED, PT_BINARY) is not None:
            removed.add((PR_RTF_COMPRESSED, PT_BINARY))
        has_html = obj.stream(PR_HTML, PT_BINARY) is not None
        entries: list[PropEntry] = []
        for entry in obj.entries:
            key = (entry.pid, entry.ptype)
            if key in removed:
                continue
            value = entry.value
            if key in sizes:
                value = struct.pack("<II", sizes[key], 0)
            elif key == (PR_ATTACH_SIZE, PT_LONG) and child is not None and child.path:
                value = struct.pack("<iI", child.path.stat().st_size, 0)
            elif key == (PR_NATIVE_BODY_INFO, PT_LONG):
                value = struct.pack("<iI", 3 if has_html else 1, 0)
            entries.append(PropEntry(entry.pid, entry.ptype, entry.flags, value))
        present = {(e.pid, e.ptype) for e in entries}
        for key, size in sizes.items():
            if key not in present:  # property created (plain body from RTF)
                entries.append(PropEntry(key[0], key[1], 6, struct.pack("<II", size, 0)))
        header = bytearray(obj.header)
        if obj.kind == "message" and len(header) >= 24:
            kept = sum(
                1
                for o in self.tree.attachments()
                if o.path[:-1] == obj.path and o.path not in self.dropped
            )
            struct.pack_into("<I", header, 20, kept)
        self.data[(*obj.path, PROPS)] = build_props(bytes(header), entries)


@register_writer
class MsgWriter(Writer):
    name = "msg"
    formats = frozenset({"msg"})

    def write(
        self,
        document: Document,
        plan: ReplacementPlan,
        destination: Path,
        ctx: WriteContext,
    ) -> list[Issue]:
        root_clsid, entries = read_cfb(document.source)
        tree = MsgTree(entries)
        out = _Rewriter(tree, document, plan, ctx.child_outputs).run()
        write_cfb(root_clsid, out, destination)
        return []
