"""Outlook .msg ([MS-OXMSG]) property model shared by the msg extractor and writer.

A .msg is a compound file; every *property object* (the message, each recipient,
each attachment, each embedded message) is a storage holding a
``__properties_version1.0`` stream (fixed-size values and sizes of the variable
ones) and one ``__substg1.0_PPPPTTTT`` stream per variable-size property.

:func:`text_items` lists, deterministically, every piece of text the extractor
exposes and the writer rewrites: string properties (Unicode and 8-bit, single and
multi-valued, named properties included), the plain body (chunked), the HTML body
(binary or string, through the shared HTML tokenizer), the transport headers (one
item per header) and, for messages whose only body is compressed RTF, a plain body
derived from the RTF.

Writer policy (identity-bearing values that cannot be anonymized are removed):

* every binary property is dropped (entry ids, one-off entry ids embedding SMTP
  addresses or X500 DNs, search/record keys, conversation index, change keys,
  rendering icons...) except the HTML body and attachment data, which are
  rewritten;
* the compressed RTF body is dropped (plain/HTML bodies remain; when RTF was the
  only body, an anonymized plain body replaces it) together with
  ``PR_RTF_IN_SYNC``; ``PR_NATIVE_BODY_INFO`` is updated;
* string properties holding an X500/Exchange DN (``/o=...``) are dropped;
* attachment local paths (``PR_ATTACH_PATHNAME``/``PR_ATTACH_LONG_PATHNAME``) are
  dropped; file names come from the anonymized child; OLE-object attachments and
  attachments whose child could not be anonymized are removed;
* the named-property map (``__nameid_version1.0``) is copied as is (property
  names and GUIDs, no values).
"""

from __future__ import annotations

import re
import struct
from collections.abc import Iterator
from dataclasses import dataclass, field
from email.parser import HeaderParser
from email.policy import compat32

from anonyma.extractors._cfb import CfbEntry, Entry
from anonyma.models import SegmentKind

PROPS = "__properties_version1.0"
NAMEID = "__nameid_version1.0"
EMBEDDED = "__substg1.0_3701000D"
RECIP_PREFIX = "__recip_version1.0_#"
ATTACH_PREFIX = "__attach_version1.0_#"
SUBSTG = re.compile(r"^__substg1\.0_([0-9A-F]{4})([0-9A-F]{4})(?:-([0-9A-F]{8}))?$", re.I)
HEADERS_PREFIX = "Microsoft Mail Internet Headers Version 2.0"

PT_LONG = 0x0003
PT_STRING8 = 0x001E
PT_UNICODE = 0x001F
PT_BINARY = 0x0102
PT_MV_STRING8 = 0x101E
PT_MV_UNICODE = 0x101F
PT_MV_BINARY = 0x1102
STRING_TYPES = (PT_UNICODE, PT_STRING8)
MV_STRING_TYPES = (PT_MV_UNICODE, PT_MV_STRING8)

PR_BODY = 0x1000
PR_HTML = 0x1013
PR_RTF_COMPRESSED = 0x1009
PR_RTF_IN_SYNC = 0x0E1F
PR_NATIVE_BODY_INFO = 0x1016
PR_TRANSPORT_HEADERS = 0x007D
PR_MESSAGE_CODEPAGE = 0x3FFD
PR_INTERNET_CPID = 0x3FDE
PR_ATTACH_METHOD = 0x3705
PR_ATTACH_DATA = 0x3701
PR_ATTACH_SIZE = 0x0E20
PR_ATTACH_FILENAME = 0x3704
PR_ATTACH_LONG_FILENAME = 0x3707
PR_DISPLAY_NAME = 0x3001
PR_ATTACH_EXTENSION = 0x3703
PR_ATTACH_MIME_TAG = 0x370E
ATTACH_PATHS = (0x3708, 0x370D)  # PR_ATTACH_PATHNAME, PR_ATTACH_LONG_PATHNAME
ATTACH_BY_VALUE = 1
ATTACH_EMBEDDED_MSG = 5
ATTACH_OLE = 6

LABELS = {
    0x0037: "objet",
    0x0E1D: "objet normalisé",
    0x0070: "sujet de conversation",
    0x0C1A: "nom de l'expéditeur",
    0x0C1F: "adresse de l'expéditeur",
    0x5D01: "adresse SMTP de l'expéditeur",
    0x0042: "nom (de la part de)",
    0x0065: "adresse (de la part de)",
    0x5D02: "adresse SMTP (de la part de)",
    0x0E04: "À (affichage)",
    0x0E03: "Cc (affichage)",
    0x0E02: "Cci (affichage)",
    0x3001: "nom affiché",
    0x3003: "adresse",
    0x39FE: "adresse SMTP",
    0x3707: "nom de fichier",
    0x3704: "nom de fichier court",
    0x1000: "corps du message",
    0x1013: "corps HTML : ",
    0x007D: "en-tête",
    0x1035: "Message-ID",
    0x1042: "In-Reply-To",
    0x1039: "References",
}
HEADER_TAGS = frozenset(
    {0x0037, 0x0E1D, 0x0070, 0x0C1A, 0x0C1F, 0x5D01, 0x0042, 0x0065, 0x5D02, 0x0E04, 0x0E03,
     0x0E02, 0x3001, 0x3003, 0x39FE, 0x1035, 0x1042, 0x1039}
)  # fmt: skip


@dataclass(slots=True)
class PropEntry:
    pid: int
    ptype: int
    flags: int
    value: bytes  # 8 bytes: fixed value, or (size, reserved) for variable types

    @property
    def int_value(self) -> int:
        return int(struct.unpack_from("<i", self.value)[0])

    def to_bytes(self) -> bytes:
        head = struct.pack("<HHI", self.ptype, self.pid, self.flags)
        return head + self.value[:8].ljust(8, b"\0")


def header_size(path: Entry) -> int:
    if not path:
        return 32
    return 24 if path[-1] == EMBEDDED else 8


def parse_props(data: bytes, size: int) -> tuple[bytes, list[PropEntry]]:
    header = data[:size]
    entries = []
    for offset in range(size, len(data) - 15, 16):
        ptype, pid, flags = struct.unpack_from("<HHI", data, offset)
        entries.append(PropEntry(pid, ptype, flags, data[offset + 8 : offset + 16]))
    return header, entries


def build_props(header: bytes, entries: list[PropEntry]) -> bytes:
    return header + b"".join(e.to_bytes() for e in entries)


def stream_name(pid: int, ptype: int, index: int | None = None) -> str:
    base = f"__substg1.0_{pid:04X}{ptype:04X}"
    return base if index is None else f"{base}-{index:08X}"


def parse_name(name: str) -> tuple[int, int, int | None] | None:
    match = SUBSTG.match(name)
    if not match:
        return None
    index = int(match.group(3), 16) if match.group(3) else None
    return int(match.group(1), 16), int(match.group(2), 16), index


def is_x500(text: str) -> bool:
    return text.strip().lower().startswith("/o=")


@dataclass(slots=True)
class PropObject:
    """A storage holding a property stream."""

    path: Entry
    kind: str  # "message" | "recipient" | "attachment"
    header: bytes
    entries: list[PropEntry]
    streams: dict[str, CfbEntry] = field(default_factory=dict)

    def fixed(self, pid: int, ptype: int = PT_LONG) -> int | None:
        for entry in self.entries:
            if entry.pid == pid and entry.ptype == ptype:
                return entry.int_value
        return None

    def stream(self, pid: int, ptype: int, index: int | None = None) -> bytes | None:
        entry = self.streams.get(stream_name(pid, ptype, index).upper())
        return entry.data if entry is not None else None

    @property
    def attach_method(self) -> int:
        return self.fixed(PR_ATTACH_METHOD) or ATTACH_BY_VALUE

    @property
    def label(self) -> str:
        if self.kind == "recipient":
            return f"destinataire {int(self.path[-1].rsplit('#', 1)[1], 16) + 1} : "
        if self.kind == "attachment":
            return f"pièce jointe {int(self.path[-1].rsplit('#', 1)[1], 16) + 1} : "
        return "message joint : " if self.path else ""


class MsgTree:
    """Every property object of a .msg compound file."""

    def __init__(self, entries: list[CfbEntry]) -> None:
        self.entries = entries
        self.by_path = {e.path: e for e in entries}
        self.objects: list[PropObject] = []
        for entry in entries:
            if entry.storage or entry.path[-1] != PROPS:
                continue
            owner = entry.path[:-1]
            if owner and owner[0] == NAMEID:
                continue
            if owner and owner[-1].startswith(RECIP_PREFIX):
                kind = "recipient"
            elif owner and owner[-1].startswith(ATTACH_PREFIX):
                kind = "attachment"
            else:
                kind = "message"
            header, props = parse_props(entry.data or b"", header_size(owner))
            self.objects.append(PropObject(owner, kind, header, props))
        owners = {o.path: o for o in self.objects}
        for entry in entries:
            if not entry.storage and entry.path[:-1] in owners and parse_name(entry.path[-1]):
                owners[entry.path[:-1]].streams[entry.path[-1].upper()] = entry
        self.root = owners.get(())
        self.codec = _codepage(self.root)

    def attachments(self) -> list[PropObject]:
        return [o for o in self.objects if o.kind == "attachment"]


def _codepage(root: PropObject | None) -> str:
    for pid in (PR_MESSAGE_CODEPAGE, PR_INTERNET_CPID):
        value = root.fixed(pid) if root is not None else None
        if value:
            name = "utf-8" if value == 65001 else f"cp{value}"
            try:
                "".encode(name)
                return name
            except LookupError:
                continue
    return "cp1252"


def decode_string(data: bytes, ptype: int, codec: str) -> str:
    if ptype in (PT_UNICODE, PT_MV_UNICODE):
        return data.decode("utf-16-le", "replace").rstrip("\x00")
    return data.decode(codec, "replace").rstrip("\x00")


def encode_string(text: str, ptype: int, codec: str) -> bytes:
    if ptype in (PT_UNICODE, PT_MV_UNICODE):
        return text.encode("utf-16-le")
    return text.encode(codec, "replace")


def string_size(data: bytes, ptype: int) -> int:
    """Size recorded in the property stream (terminating null included)."""
    return len(data) + (2 if ptype in (PT_UNICODE, PT_MV_UNICODE) else 1)


def html_codec(obj: PropObject) -> str | None:
    cpid = obj.fixed(PR_INTERNET_CPID)
    if not cpid:
        return None
    name = "utf-8" if cpid == 65001 else f"cp{cpid}"
    try:
        "".encode(name)
    except LookupError:
        return None
    return name


def split_headers(text: str) -> tuple[str, list[tuple[str, str]]]:
    """Transport headers: (prefix line, [(name, raw value)])."""
    prefix = ""
    body = text
    if body.startswith(HEADERS_PREFIX):
        prefix = HEADERS_PREFIX
        body = body[len(HEADERS_PREFIX) :].lstrip("\r\n")
    parsed = HeaderParser(policy=compat32).parsestr(body)
    return prefix, [(name, str(value)) for name, value in parsed.items()]


@dataclass(frozen=True, slots=True)
class TextItem:
    """A text the extractor exposes and the writer rewrites."""

    owner: Entry
    stream: str  # stream name (may not exist yet: RTF-derived body)
    pid: int
    ptype: int
    kind: str  # "string" | "body" | "html" | "headers" | "rtf"
    text: str
    label: str

    @property
    def segment_id(self) -> str:
        return "/".join((*self.owner, self.stream))

    @property
    def segment_kind(self) -> SegmentKind:
        if self.kind in ("body", "rtf", "html"):
            return SegmentKind.BODY
        if self.kind == "headers" or self.pid in HEADER_TAGS:
            return SegmentKind.HEADER
        return SegmentKind.METADATA


def text_items(tree: MsgTree) -> Iterator[TextItem]:
    for obj in tree.objects:
        prefix = obj.label
        for name in sorted(obj.streams):
            parsed = parse_name(name)
            assert parsed is not None
            pid, ptype, index = parsed
            data = obj.streams[name].data or b""
            label = prefix + LABELS.get(pid, f"propriété {pid:04X}")
            if pid == PR_HTML and ptype in (PT_BINARY, *STRING_TYPES):
                if ptype == PT_BINARY:
                    from anonyma.extractors.html import decode_html

                    codec = html_codec(obj)
                    text = data.decode(codec, "replace") if codec else decode_html(data)[0]
                else:
                    text = decode_string(data, ptype, tree.codec)
                yield TextItem(obj.path, name, pid, ptype, "html", text, prefix + "corps HTML : ")
                continue
            string_type = ptype in STRING_TYPES or (ptype in MV_STRING_TYPES and index is not None)
            if not string_type:
                continue
            text = decode_string(data, ptype, tree.codec)
            if is_x500(text):
                continue
            if pid == PR_BODY and index is None:
                yield TextItem(obj.path, name, pid, ptype, "body", text, label)
            elif pid == PR_TRANSPORT_HEADERS and index is None:
                yield TextItem(obj.path, name, pid, ptype, "headers", text, label)
            else:
                yield TextItem(obj.path, name, pid, ptype, "string", text, label)
        if obj.kind == "message" and _rtf_only(obj):
            rtf_text = rtf_body(obj)
            if rtf_text is not None:
                name = stream_name(PR_BODY, PT_UNICODE)
                yield TextItem(
                    obj.path, name, PR_BODY, PT_UNICODE, "rtf", rtf_text, prefix + "corps"
                )


def _rtf_only(obj: PropObject) -> bool:
    has_rtf = obj.stream(PR_RTF_COMPRESSED, PT_BINARY) is not None
    has_body = any(
        obj.stream(pid, ptype) is not None
        for pid in (PR_BODY, PR_HTML)
        for ptype in (PT_UNICODE, PT_STRING8, PT_BINARY)
    )
    return has_rtf and not has_body


def rtf_body(obj: PropObject) -> str | None:
    """Plain text of the compressed RTF body (only body of the message)."""
    import compressed_rtf  # type: ignore[import-untyped]

    from anonyma.extractors.msg import rtf_to_text

    data = obj.stream(PR_RTF_COMPRESSED, PT_BINARY)
    if not data:
        return None
    try:
        rtf = compressed_rtf.decompress(data)
    except Exception:
        return None
    return rtf_to_text(rtf)
