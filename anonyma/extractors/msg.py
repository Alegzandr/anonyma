"""Outlook .msg files, read natively from the compound file ([MS-OXMSG]).

Every string property of every property object (message, recipients,
attachments, embedded messages at any depth, named properties included) is a
segment; the plain body is chunked, the HTML body goes through the HTML tokenizer,
the transport headers give one segment per header. By-value attachments are
children (named after their file name). The writer (``writers/msg.py``) rewrites
the same compound file; see :mod:`anonyma.extractors._msgfile` for the policy on
binary and identity-bearing properties.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path, PurePosixPath

from anonyma.codec import chunk_text
from anonyma.extractors._cfb import read_cfb
from anonyma.extractors._html import HtmlSource, build_segments, extension_for
from anonyma.extractors._mime import DROPPED_HEADERS, decode_header_value
from anonyma.extractors._msgfile import (
    ATTACH_BY_VALUE,
    ATTACH_EMBEDDED_MSG,
    ATTACH_OLE,
    PR_ATTACH_DATA,
    PR_ATTACH_FILENAME,
    PR_ATTACH_LONG_FILENAME,
    PR_ATTACH_MIME_TAG,
    PR_DISPLAY_NAME,
    PT_BINARY,
    STRING_TYPES,
    MsgTree,
    PropObject,
    TextItem,
    decode_string,
    rtf_only,
    split_headers,
    text_items,
)
from anonyma.models import (
    Document,
    ExtractionContext,
    Extractor,
    Segment,
    SegmentContext,
    Severity,
)
from anonyma.registry import register_extractor

logging.getLogger("extract_msg").setLevel(logging.CRITICAL)

_RTF_TOKEN = re.compile(r"\\([a-zA-Z]+)(-?\d+)? ?|\\'([0-9a-fA-F]{2})|\\(.)")


def _patch_embedded_messages() -> None:
    """extract-msg 0.58 opens embedded messages through a ``prefixList`` attribute
    that its ``MSGFile`` no longer has: every .msg with an attached message failed
    to open there. Provide it (path components of the current prefix)."""
    from extract_msg.msg_classes import MSGFile

    if not hasattr(MSGFile, "prefixList"):
        MSGFile.prefixList = property(  # type: ignore[attr-defined]
            lambda self: list(getattr(self.prefix, "parts", ()) or ())
        )


_patch_embedded_messages()


def rtf_to_text(rtf: bytes) -> str:
    """Very small RTF to text conversion (fallback when no other body exists)."""
    text = rtf.decode("latin-1")
    out: list[str] = []
    skip_depth: list[int] = []
    depth = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "{":
            depth += 1
            if text.startswith("{\\*", i):
                skip_depth.append(depth)
            i += 1
        elif ch == "}":
            if skip_depth and skip_depth[-1] == depth:
                skip_depth.pop()
            depth -= 1
            i += 1
        elif ch == "\\":
            match = _RTF_TOKEN.match(text, i)
            if not match:
                i += 1
                continue
            i = match.end()
            if skip_depth:
                continue
            word, arg, hexa, sym = match.groups()
            if hexa:
                out.append(bytes([int(hexa, 16)]).decode("cp1252", "replace"))
            elif sym:
                out.append(sym if sym in "\\{}" else "")
            elif word in ("par", "line", "row"):
                out.append("\n")
            elif word == "tab":
                out.append("\t")
            elif word == "u" and arg:
                out.append(chr(int(arg) % 65536))
                if i < n and text[i] == "?":
                    i += 1
            elif word in ("fonttbl", "colortbl", "stylesheet", "info", "pict"):
                skip_depth.append(depth)
        else:
            if not skip_depth and ch not in "\r\n":
                out.append(ch)
            i += 1
    return "".join(out)


def item_segments(item: TextItem) -> list[Segment]:
    """Segments of one text item (ids derived from the stream path)."""
    sid = item.segment_id
    if item.kind == "html":
        return build_segments(HtmlSource(item.text), f"{sid}|h", item.label)
    if item.kind in ("body", "rtf"):
        return [
            Segment(
                id=f"{sid}|c{i}",
                text=chunk,
                locator=("chunk", offset),
                context=SegmentContext(item.segment_kind, label=item.label),
            )
            for i, (offset, chunk) in enumerate(chunk_text(item.text))
            if chunk.strip()
        ]
    if item.kind == "headers":
        segments = []
        for i, (name, value) in enumerate(split_headers(item.text)[1]):
            text = decode_header_value(value)
            if name.lower() in DROPPED_HEADERS or not text.strip():
                continue
            segments.append(
                Segment(
                    id=f"{sid}|t{i}",
                    text=text,
                    locator=("header", i),
                    context=SegmentContext(item.segment_kind, label=f"en-tête {name[:60]}"),
                )
            )
        return segments
    if not item.text.strip():
        return []
    return [
        Segment(
            id=sid,
            text=item.text,
            locator=("string",),
            context=SegmentContext(item.segment_kind, label=item.label),
        )
    ]


def attachment_name(tree: MsgTree, obj: PropObject, index: int) -> str:
    for pid in (PR_ATTACH_LONG_FILENAME, PR_ATTACH_FILENAME, PR_DISPLAY_NAME):
        for ptype in STRING_TYPES:
            data = obj.stream(pid, ptype)
            if data:
                name = decode_string(data, ptype, tree.codec).replace("\\", "/")
                base = PurePosixPath(name.rstrip("/")).name.strip()
                if base:
                    return base
    mime_data = obj.stream(PR_ATTACH_MIME_TAG, 0x001F) or obj.stream(PR_ATTACH_MIME_TAG, 0x001E)
    mime = decode_string(mime_data, 0x001F, tree.codec) if mime_data else ""
    return f"piece-jointe-{index}{extension_for(mime)}"


@register_extractor
class MsgExtractor(Extractor):
    name = "msg"
    extensions = {".msg": "msg"}

    def extract(self, source: Path, document: Document, ctx: ExtractionContext) -> None:
        _, entries = read_cfb(
            source, lambda size: ctx.budget.consume(size, depth=ctx.depth, files=0)
        )
        tree = MsgTree(entries)
        if tree.root is None:
            raise ValueError("pas un message Outlook")
        rtf_bodies = set()
        for item in text_items(tree):
            document.segments.extend(item_segments(item))
            if item.kind == "rtf":
                rtf_bodies.add(item.owner)
        if any(
            o.kind == "message" and rtf_only(o) and o.path not in rtf_bodies for o in tree.objects
        ):  # the writer always drops the compressed RTF
            document.add_issue(
                Severity.WARNING,
                "msg.rtf_unreadable",
                "Corps de message RTF illisible : il est supprimé.",
            )
        children: dict[str, str] = {}
        for index, obj in enumerate(tree.attachments(), start=1):
            ctx.check_cancelled()
            method = obj.attach_method
            if method == ATTACH_EMBEDDED_MSG:
                continue  # its storage is a property object of its own
            if method == ATTACH_OLE:
                document.add_issue(
                    Severity.WARNING,
                    "msg.ole_attachment",
                    "Objet OLE incorporé : il ne peut pas être anonymisé et est supprimé.",
                )
                continue
            data = obj.stream(PR_ATTACH_DATA, PT_BINARY)
            if method != ATTACH_BY_VALUE or data is None:
                continue  # references only (file paths, URLs): exposed as strings
            # Its bytes were charged when the compound file was read.
            ctx.budget.consume(0, depth=ctx.depth + 1)
            child = ctx.extract_child(data, attachment_name(tree, obj, index), document)
            children["/".join(obj.path)] = child.id
        document.properties["msg_children"] = children
