"""Outlook data files (.pst/.ost) through pypff (libpff-python, LGPL-3.0+).

Every message of the folder tree is converted to an eml child named
``<folder>/<sub folder>/message-0001.eml``; the writer produces a folder tree of
the anonymized eml files (``directory_output``) that verification re-reads
like any folder. Folder names are part of the entry names, hence anonymized.
Non-mail items (contacts, appointments...) keep every string property in their
plain body. Orphan/deleted items recoverable from free space are not exported.
"""

from __future__ import annotations

import contextlib
from datetime import datetime
from email.utils import format_datetime
from pathlib import Path
from typing import Any

import pypff

from anonyma.extractors._html import extension_for
from anonyma.extractors._mime import (
    DROPPED_HEADERS,
    BuiltAttachment,
    build_eml,
    decode_header_value,
)
from anonyma.extractors.html import decode_html
from anonyma.extractors.msg import rtf_to_text
from anonyma.models import Document, ExtractionContext, Extractor, Severity
from anonyma.registry import register_extractor

PST_MAGIC = b"!BDN"
_STRING_TYPES = (0x001F, 0x001E)
#: Properties mapped to headers/bodies (not repeated in the annex of non-mail items).
_MAPPED = frozenset(
    {
        0x0037, 0x0E1D, 0x1000, 0x1013, 0x1009, 0x007D, 0x0C1A, 0x5D01, 0x0E04, 0x0E03,
        0x0E02, 0x1035, 0x1042, 0x1039, 0x0070, 0x001A, 0x0042, 0x0065, 0x0064, 0x0C1F,
        0x0C1E, 0x5D02, 0x0075, 0x0076, 0x0077, 0x0078, 0x3FFA, 0x3FF8, 0x0E1F, 0x3001,
    }
)  # fmt: skip
_RECIPIENT_HEADERS = {1: "To", 2: "Cc", 3: "Bcc"}
_ATTACH_EMBEDDED_MSG = 5


def _call(obj: Any, name: str, *args: Any) -> Any:
    try:
        return getattr(obj, name)(*args)
    except Exception:
        return None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)
    text = text.replace("\x00", "").strip()
    return text or None


def properties(item: Any) -> dict[int, Any]:
    """First value of every property of ``item`` (strings decoded, binaries raw)."""
    found: dict[int, Any] = {}
    count = _call(item, "get_number_of_record_sets") or 0
    for i in range(count):
        record_set = _call(item, "get_record_set", i)
        if record_set is None:
            continue
        for j in range(_call(record_set, "get_number_of_entries") or 0):
            entry = _call(record_set, "get_entry", j)
            if entry is None:
                continue
            tag = _call(entry, "get_entry_type")
            if not isinstance(tag, int) or tag in found:
                continue
            value_type = _call(entry, "get_value_type")
            if value_type in _STRING_TYPES:
                found[tag] = _text(_call(entry, "get_data_as_string"))
            elif value_type == 0x0003:
                found[tag] = _call(entry, "get_data_as_integer")
            elif value_type == 0x0040:
                found[tag] = _call(entry, "get_data_as_datetime")
            else:
                found[tag] = _call(entry, "get_data")
    return found


def _recipient_sets(message: Any) -> list[dict[int, Any]]:
    recipients = _call(message, "get_recipients")
    if recipients is None:
        recipients = getattr(message, "recipients", None)
    if recipients is None:
        return []
    return [p for p in [properties_of_set(rs) for rs in _record_sets(recipients)] if p]


def _record_sets(item: Any) -> list[Any]:
    count = _call(item, "get_number_of_record_sets") or 0
    return [rs for i in range(count) if (rs := _call(item, "get_record_set", i)) is not None]


def properties_of_set(record_set: Any) -> dict[int, Any]:
    found: dict[int, Any] = {}
    for j in range(_call(record_set, "get_number_of_entries") or 0):
        entry = _call(record_set, "get_entry", j)
        tag = _call(entry, "get_entry_type") if entry is not None else None
        if not isinstance(tag, int) or tag in found:
            continue
        value_type = _call(entry, "get_value_type")
        if value_type in _STRING_TYPES:
            found[tag] = _text(_call(entry, "get_data_as_string"))
        elif value_type == 0x0003:
            found[tag] = _call(entry, "get_data_as_integer")
    return found


def _address(name: str | None, email: str | None) -> str | None:
    if name and email and name != email:
        return f'"{name}" <{email}>'
    return email or name


def message_to_eml(message: Any, document: Document, depth: int = 0) -> bytes:
    """Convert a pypff message (or any message-like item) into eml bytes."""
    props = properties(message)
    headers: list[tuple[str, str]] = []
    seen: set[str] = set()
    transport = _text(_call(message, "get_transport_headers")) or _text(props.get(0x007D))
    if transport:
        from email.parser import HeaderParser
        from email.policy import compat32

        for name, value in HeaderParser(policy=compat32).parsestr(transport).items():
            lname = name.lower()
            if lname in DROPPED_HEADERS or lname.startswith("content-") or lname == "mime-version":
                continue
            headers.append((name, decode_header_value(str(value))))
            seen.add(lname)

    def add(name: str, value: Any) -> None:
        text = _text(value)
        if text and name.lower() not in seen:
            headers.append((name, text))
            seen.add(name.lower())

    sender_email = props.get(0x5D01) or props.get(0x0C1F) or props.get(0x0065)
    add(
        "From",
        _address(_text(_call(message, "get_sender_name")) or props.get(0x0C1A), sender_email),
    )
    by_kind: dict[str, list[str]] = {}
    for recipient in _recipient_sets(message):
        address = _address(recipient.get(0x3001), recipient.get(0x39FE) or recipient.get(0x3003))
        kind = _RECIPIENT_HEADERS.get(int(recipient.get(0x0C15) or 1), "To")
        if address:
            by_kind.setdefault(kind, []).append(address)
    for kind in ("To", "Cc", "Bcc"):
        add(kind, ", ".join(by_kind.get(kind, [])))
    add("To", props.get(0x0E04))
    add("Cc", props.get(0x0E03))
    add("Bcc", props.get(0x0E02))
    add("Subject", _call(message, "get_subject") or props.get(0x0037))
    submitted = _call(message, "get_client_submit_time") or _call(message, "get_delivery_time")
    if isinstance(submitted, datetime):
        with contextlib.suppress(TypeError, ValueError):
            add("Date", format_datetime(submitted))
    add("Message-ID", props.get(0x1035))
    add("In-Reply-To", props.get(0x1042))
    add("References", props.get(0x1039))
    add("Thread-Topic", _call(message, "get_conversation_topic") or props.get(0x0070))
    add("X-Sent-Representing", props.get(0x0042))
    add("X-Message-Class", props.get(0x001A))

    plain = _text_body(_call(message, "get_plain_text_body"))
    html_raw = _call(message, "get_html_body")
    html_body = (
        decode_html(html_raw)[0] if isinstance(html_raw, bytes) and html_raw.strip() else None
    )
    if not plain and html_body is None:
        rtf = _call(message, "get_rtf_body")
        if isinstance(rtf, bytes) and rtf:
            plain = rtf_to_text(rtf)
    message_class = str(props.get(0x001A) or "").lower()
    if message_class and not message_class.startswith(("ipm.note", "report")):
        annex = "\n".join(
            f"Propriété {tag:04X} : {value}"
            for tag, value in sorted(props.items())
            if tag not in _MAPPED and tag < 0x8000 and isinstance(value, str) and value
        )
        if annex:
            plain = f"{plain or ''}\n\n--- Propriétés Outlook ---\n{annex}\n"

    attachments: list[BuiltAttachment] = []
    for i in range(_call(message, "get_number_of_attachments") or 0):
        built = _attachment(_call(message, "get_attachment", i), i + 1, document, depth)
        if built is not None:
            attachments.append(built)
    return build_eml(headers, plain, html_body, attachments)


def _text_body(value: Any) -> str | None:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace").rstrip("\x00") or None
    return _text(value)


def _attachment(att: Any, index: int, document: Document, depth: int) -> BuiltAttachment | None:
    if att is None:
        return None
    props = properties(att)
    name = (
        _text(_call(att, "get_long_filename"))
        or props.get(0x3707)
        or props.get(0x3704)
        or props.get(0x3001)
    )
    if props.get(0x3705) == _ATTACH_EMBEDDED_MSG or (
        not (_call(att, "get_size") or 0) and (_call(att, "get_number_of_sub_items") or 0)
    ):
        embedded = _call(att, "get_sub_item", 0)
        if embedded is not None and depth < 32:
            try:
                data = message_to_eml(embedded, document, depth + 1)
            except Exception:
                data = None
            if data is not None:
                base = (name or f"message-joint-{index}").rsplit(".", 1)[0]
                return BuiltAttachment(f"{base}.eml", data, "message/rfc822")
        document.add_issue(
            Severity.WARNING, "pst.attachment_unreadable", "Message joint illisible : il est omis."
        )
        return None
    size = _call(att, "get_size") or 0
    data = _call(att, "read_buffer", size) if size else b""
    if not isinstance(data, bytes):
        document.add_issue(
            Severity.WARNING,
            "pst.attachment_unreadable",
            "Pièce jointe illisible : elle est omise.",
        )
        return None
    mime = props.get(0x370E) or "application/octet-stream"
    cid = props.get(0x3712)
    return BuiltAttachment(
        name or f"piece-jointe-{index}{extension_for(mime)}",
        data,
        mime,
        content_id=cid,
        inline=bool(cid),
    )


@register_extractor
class PstExtractor(Extractor):
    name = "pst"
    extensions = {".pst": "pst", ".ost": "pst"}

    @classmethod
    def sniff(cls, head: bytes) -> str | None:
        return "pst" if head.startswith(PST_MAGIC) and head[8:10] == b"SM" else None

    def extract(self, source: Path, document: Document, ctx: ExtractionContext) -> None:
        pst = pypff.file()
        pst.open(str(source))
        try:
            root = pst.get_root_folder()
            counter = [0]
            self._folder(root, "", document, ctx, counter)
        finally:
            pst.close()

    def _folder(
        self, folder: Any, path: str, document: Document, ctx: ExtractionContext, counter: list[int]
    ) -> None:
        for i in range(_call(folder, "get_number_of_sub_messages") or 0):
            ctx.check_cancelled()
            message = _call(folder, "get_sub_message", i)
            if message is None:
                document.add_issue(
                    Severity.ERROR,
                    "pst.item_unreadable",
                    "Un élément de la boîte aux lettres est illisible.",
                )
                continue
            data = message_to_eml(message, document)
            counter[0] += 1
            ctx.budget.consume(len(data), depth=ctx.depth + 1)
            ctx.extract_child(data, f"{path}message-{counter[0]:04d}.eml", document)
        for i in range(_call(folder, "get_number_of_sub_folders") or 0):
            sub = _call(folder, "get_sub_folder", i)
            if sub is None:
                continue
            name = (_text(_call(sub, "get_name")) or f"dossier-{i + 1}").replace("/", "_")
            self._folder(sub, f"{path}{name}/", document, ctx, counter)
