"""Outlook .msg files (extract-msg), converted to an equivalent RFC 5322 message.

The .msg is turned into an eml (headers block, sender, recipients with display
names and SMTP addresses, subject, dates, ids, plain/HTML bodies, attachments,
embedded messages as ``.msg`` attachments) stored in the batch temporary folder;
that eml is then extracted and written by the shared MIME code, so the output is
an ``.eml`` file. Outlook-only properties of mail items are purged; for other item
classes (contacts, appointments, tasks...) every string property is appended to
the plain body so that nothing they hold is lost silently.
"""

from __future__ import annotations

import contextlib
import logging
import re
from pathlib import Path
from typing import Any

import extract_msg

from anonyma.extractors._html import extension_for
from anonyma.extractors._mime import (
    DROPPED_HEADERS,
    BuiltAttachment,
    build_eml,
    decode_header_value,
    extract_mime,
)
from anonyma.extractors.html import decode_html
from anonyma.models import Document, ExtractionContext, Extractor, Severity
from anonyma.registry import register_extractor

logging.getLogger("extract_msg").setLevel(logging.CRITICAL)


def _patch_embedded_messages() -> None:
    """extract-msg 0.58 opens embedded messages through a ``prefixList`` attribute
    that its ``MSGFile`` no longer has: every .msg with an attached message failed
    to open. Provide it (path components of the current prefix)."""
    from extract_msg.msg_classes import MSGFile

    if not hasattr(MSGFile, "prefixList"):
        MSGFile.prefixList = property(  # type: ignore[attr-defined]
            lambda self: list(getattr(self.prefix, "parts", ()) or ())
        )


_patch_embedded_messages()

#: Property tags already mapped to the eml (not repeated in the annex).
_MAPPED_TAGS = frozenset(
    {
        "0037", "0E1D", "1000", "1013", "1009", "007D", "0C1A", "5D01", "0E04", "0E03",
        "0E02", "1035", "1042", "1039", "0070", "001A", "0042", "0065", "0064", "0C1F",
        "0C1E", "5D02", "0075", "0076", "0077", "0078", "3FFA", "3FF8", "0E1F",
    }
)  # fmt: skip
_STRING_STREAM = re.compile(r"^__substg1\.0_([0-9A-F]{4})(001F|001E)$", re.I)


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
            match = re.match(r"\\([a-zA-Z]+)(-?\d+)? ?|\\'([0-9a-fA-F]{2})|\\(.)", text[i:])
            if not match:
                i += 1
                continue
            i += match.end()
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


def _string_props(msg: Any) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    try:
        entries = msg.listDir(streams=True, storages=False)
    except Exception:
        return found
    for entry in entries:
        parts = list(entry)
        if len(parts) != 1:
            continue
        match = _STRING_STREAM.match(str(parts[0]))
        if not match or match.group(1).upper() in _MAPPED_TAGS:
            continue
        value = msg.getStringStream(f"__substg1.0_{match.group(1)}")
        if isinstance(value, str) and value.strip():
            found.append((match.group(1).upper(), value))
    return sorted(found)


def msg_to_eml(path: Path, document: Document) -> bytes:
    """Convert a .msg file into eml bytes (see module docstring)."""
    msg = extract_msg.openMsg(str(path), strict=False, delayAttachments=False)
    try:
        return _convert(msg, document)
    finally:
        msg.close()


def _convert(msg: Any, document: Document) -> bytes:
    headers: list[tuple[str, str]] = []
    seen: set[str] = set()
    header_text = getattr(msg, "headerText", None)
    if header_text:
        from email.parser import HeaderParser
        from email.policy import compat32

        if header_text.startswith("Microsoft Mail Internet Headers Version 2.0"):
            header_text = header_text[43:].lstrip()
        parsed = HeaderParser(policy=compat32).parsestr(header_text)
        for name, value in parsed.items():
            lname = name.lower()
            if lname in DROPPED_HEADERS or lname.startswith("content-") or lname == "mime-version":
                continue
            headers.append((name, decode_header_value(str(value))))
            seen.add(lname)

    def add(name: str, value: object) -> None:
        if value is None or name.lower() in seen:
            return
        text = str(value).strip()
        if text:
            headers.append((name, text))
            seen.add(name.lower())

    add("From", getattr(msg, "sender", None))
    add("To", getattr(msg, "to", None))
    add("Cc", getattr(msg, "cc", None))
    add("Bcc", getattr(msg, "bcc", None))
    add("Subject", getattr(msg, "subject", None))
    date = getattr(msg, "date", None)
    if date is not None:
        from email.utils import format_datetime

        with contextlib.suppress(TypeError, ValueError):
            add("Date", format_datetime(date))
    add("Message-ID", getattr(msg, "messageId", None))
    add("In-Reply-To", getattr(msg, "inReplyTo", None))
    add("References", msg.getStringStream("__substg1.0_1039"))
    add("Thread-Topic", msg.getStringStream("__substg1.0_0070"))
    add("X-Sent-Representing", msg.getStringStream("__substg1.0_0042"))
    add("X-Message-Class", getattr(msg, "classType", None))

    plain = getattr(msg, "body", None)
    html_body: str | None = None
    try:
        raw_html = msg.getStream("__substg1.0_10130102")
        if raw_html is None and getattr(msg, "rtfBody", None):
            raw_html = msg.htmlBody  # de-encapsulated from RTF when possible
    except Exception:
        raw_html = None
    if isinstance(raw_html, bytes) and raw_html.strip():
        html_body = decode_html(raw_html)[0]
    if not plain and html_body is None:
        rtf = getattr(msg, "rtfBody", None)
        if rtf:
            plain = rtf_to_text(rtf)
    class_type = str(getattr(msg, "classType", "") or "").lower()
    if not class_type.startswith(("ipm.note", "report")):
        annex = "\n".join(f"Propriété {tag} : {value}" for tag, value in _string_props(msg))
        if annex:
            plain = f"{plain or ''}\n\n--- Propriétés Outlook ---\n{annex}\n"

    attachments: list[BuiltAttachment] = []
    for i, att in enumerate(getattr(msg, "attachments", []) or [], start=1):
        built = _attachment(att, i, document)
        if built is not None:
            attachments.append(built)
    return build_eml(headers, plain if plain else None, html_body, attachments)


def _attachment(att: Any, index: int, document: Document) -> BuiltAttachment | None:
    from extract_msg.enums import AttachmentType

    kind = getattr(att, "type", None)
    name = getattr(att, "longFilename", None) or getattr(att, "shortFilename", None)
    cid = getattr(att, "contentId", None)
    hidden = bool(getattr(att, "hidden", False))
    if kind in (AttachmentType.MSG, AttachmentType.SIGNED_EMBEDDED):
        try:
            data = att.data.exportBytes(allowBadEmbed=True)
        except Exception:
            document.add_issue(
                Severity.WARNING,
                "msg.attachment_unreadable",
                "Message joint illisible : il est omis.",
            )
            return None
        base = (name or f"message-joint-{index}").rsplit(".", 1)[0]
        return BuiltAttachment(f"{base}.msg", data, "application/vnd.ms-outlook")
    data = getattr(att, "data", None)
    if kind not in (AttachmentType.DATA, AttachmentType.SIGNED) or not isinstance(data, bytes):
        document.add_issue(
            Severity.WARNING,
            "msg.attachment_unsupported",
            "Pièce jointe d'un type non pris en charge (lien, objet OLE...) : elle est omise.",
        )
        return None
    mimetype = getattr(att, "mimetype", None) or "application/octet-stream"
    if not name:
        name = f"piece-jointe-{index}{extension_for(mimetype)}"
    return BuiltAttachment(name, data, mimetype, content_id=cid, inline=hidden and bool(cid))


@register_extractor
class MsgExtractor(Extractor):
    name = "msg"
    extensions = {".msg": "msg"}

    def extract(self, source: Path, document: Document, ctx: ExtractionContext) -> None:
        data = msg_to_eml(source, document)
        converted = ctx.temp_path(".eml")
        converted.write_bytes(data)
        document.properties["eml_path"] = converted
        extract_mime(data, document, ctx)
