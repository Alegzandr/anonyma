"""Minimal Unicode PST writer, only used to generate synthetic contract fixtures.

Implements just enough of [MS-PST] for libpff/pypff to read folders, messages,
recipients and attachments back:

* NDB layer: 564-byte Unicode header, one AMap and one PMap page, data blocks
  (no encryption, ``NDB_CRYPT_NONE``), XBLOCKs for large values, SLBLOCKs for
  subnodes, node (NBT) and block (BBT) B-trees with intermediate levels.
* LTP layer: single-block heap-on-node, BTH, property contexts (0xBC) and table
  contexts (0x7C) with 4-byte cells only.
* Messaging layer: message store, root folder (0x122) with hierarchy, contents and
  associated contents tables, folders, messages with recipient and attachment
  tables, attachment objects.

Never used at runtime and not a general-purpose PST writer (small files only:
everything must fit in the first allocation map, i.e. about 240 KiB).
"""

from __future__ import annotations

import struct
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field

PT_LONG = 0x0003
PT_BOOLEAN = 0x000B
PT_SYSTIME = 0x0040
PT_UNICODE = 0x001F
PT_BINARY = 0x0102

NID_TYPE_NORMAL_FOLDER = 0x02
NID_TYPE_NORMAL_MESSAGE = 0x04
NID_TYPE_ATTACHMENT = 0x05
NID_TYPE_LTP = 0x1F
NID_MESSAGE_STORE = 0x21
NID_ROOT_FOLDER = 0x122
NID_RECIPIENT_TABLE = 0x692
NID_ATTACHMENT_TABLE = 0x671
HIERARCHY = 0x0D - NID_TYPE_NORMAL_FOLDER  # table NIDs relative to the folder NID
CONTENTS = 0x0E - NID_TYPE_NORMAL_FOLDER
ASSOCIATED = 0x0F - NID_TYPE_NORMAL_FOLDER

PAGE_SIZE = 512
BLOCK_MAX_DATA = 8192 - 16
HEAP_VALUE_MAX = 3580
HEAP_BUDGET = 7600
AMAP_OFFSET = 0x4400
PMAP_OFFSET = 0x4600
FIRST_DATA_OFFSET = 0x4800
AMAP_COVERAGE = 496 * 8 * 64
FILETIME_2026 = 134117136000000000  # 2026-01-01T00:00:00Z

Value = int | str | bytes
Props = dict[int, tuple[int, Value]]  # property id -> (type, value)
SubnodeEntry = tuple[int, int, int]  # (nid, bid data, bid sub)


@dataclass
class PstAttachment:
    filename: str
    data: bytes
    mime_type: str = "application/octet-stream"


@dataclass
class PstRecipient:
    display_name: str
    smtp_address: str
    recipient_type: int = 1  # 1 = To, 2 = Cc, 3 = Bcc


@dataclass
class PstMessage:
    subject: str
    sender_name: str
    sender_email: str
    body: str
    html_body: str | None = None
    transport_headers: str | None = None
    display_to: str = ""
    recipients: list[PstRecipient] = field(default_factory=list)
    attachments: list[PstAttachment] = field(default_factory=list)


@dataclass
class PstFolder:
    name: str
    messages: list[PstMessage] = field(default_factory=list)
    folders: list[PstFolder] = field(default_factory=list)


def crc(data: bytes) -> int:
    """[MS-PST] CRC: CRC-32 table update with initial value 0, no final xor."""
    return ~zlib.crc32(data, 0xFFFFFFFF) & 0xFFFFFFFF


def signature(ib: int, bid: int) -> int:
    value = ib ^ bid
    return ((value >> 16) ^ value) & 0xFFFF


# ---------------------------------------------------------------------------
# NDB layer: blocks and nodes
# ---------------------------------------------------------------------------


@dataclass
class _Node:
    nid: int
    bid_data: int
    bid_sub: int
    parent: int


class _Ndb:
    def __init__(self) -> None:
        self.blocks: list[tuple[int, bytes]] = []
        self.nodes: list[_Node] = []
        self._counter = 0

    def block(self, data: bytes, internal: bool = False) -> int:
        assert len(data) <= BLOCK_MAX_DATA
        self._counter += 1
        bid = (self._counter << 2) | (0x2 if internal else 0)
        self.blocks.append((bid, data))
        return bid

    def data_tree(self, data: bytes) -> int:
        """One data block, or an XBLOCK over several data blocks."""
        if len(data) <= BLOCK_MAX_DATA:
            return self.block(data)
        step = BLOCK_MAX_DATA
        bids = [self.block(data[i : i + step]) for i in range(0, len(data), step)]
        xblock = struct.pack("<BBHI", 0x01, 0x01, len(bids), len(data))
        xblock += b"".join(struct.pack("<Q", bid) for bid in bids)
        return self.block(xblock, internal=True)

    def subnode_block(self, entries: list[SubnodeEntry]) -> int:
        """SLBLOCK listing ``entries``; 0 when there is none."""
        if not entries:
            return 0
        data = struct.pack("<BBHI", 0x02, 0x00, len(entries), 0)
        data += b"".join(struct.pack("<QQQ", *entry) for entry in sorted(entries))
        return self.block(data, internal=True)

    def node(self, nid: int, bid_data: int, bid_sub: int, parent: int) -> None:
        self.nodes.append(_Node(nid, bid_data, bid_sub, parent))


# ---------------------------------------------------------------------------
# LTP layer: heap, BTH, property and table contexts
# ---------------------------------------------------------------------------


class _Heap:
    """Single-block heap-on-node; large values become subnodes of the owner node."""

    def __init__(self, ndb: _Ndb, client: int, subnodes: list[SubnodeEntry]) -> None:
        self.ndb = ndb
        self.client = client
        self.subnodes = subnodes
        self.allocations: list[bytes] = []

    def alloc(self, data: bytes) -> int:
        self.allocations.append(data)
        return len(self.allocations) << 5  # HID: index in bits 5-15, block 0

    def _size(self) -> int:
        used = sum(len(a) + len(a) % 2 for a in self.allocations)
        return 12 + used + 2 * len(self.allocations) + 6

    def value(self, data: bytes) -> int:
        """HNID of variable-size data: heap allocation, or subnode when large."""
        if not data:
            return 0
        if len(data) <= HEAP_VALUE_MAX and self._size() + len(data) + 4 <= HEAP_BUDGET:
            return self.alloc(data)
        nid = ((0x100 + len(self.subnodes)) << 5) | NID_TYPE_LTP
        self.subnodes.append((nid, self.ndb.data_tree(data), 0))
        return nid

    def build(self, root: int) -> int:
        body = bytearray()
        offsets = []
        for data in self.allocations:
            offsets.append(12 + len(body))
            body += data + b"\0" * (len(data) % 2)
        end = 12 + len(body)
        page_map = struct.pack("<HH", len(self.allocations), 0)
        page_map += b"".join(struct.pack("<H", o) for o in [*offsets, end])
        header = struct.pack("<HBBI4x", end, 0xEC, self.client, root)
        return self.ndb.block(header + bytes(body) + page_map)


def _bth(heap: _Heap, key_size: int, entry_size: int, records: list[tuple[bytes, bytes]]) -> int:
    records = sorted(records, key=lambda r: int.from_bytes(r[0], "little"))
    root = heap.alloc(b"".join(k + v for k, v in records)) if records else 0
    return heap.alloc(struct.pack("<BBBBI", 0xB5, key_size, entry_size, 0, root))


def _cell(heap: _Heap, ptype: int, value: Value) -> int | None:
    """4-byte cell / PC value: inline integer or HNID (None when empty)."""
    if ptype in (PT_LONG, PT_BOOLEAN):
        assert isinstance(value, int)
        return value & 0xFFFFFFFF
    if ptype == PT_UNICODE:
        assert isinstance(value, str)
        data = value.encode("utf-16-le")
    elif ptype == PT_SYSTIME:
        assert isinstance(value, int)
        data = struct.pack("<Q", value)
    else:
        assert isinstance(value, bytes)
        data = value
    return heap.value(data) or None


#: Builds a heap for a node: (ndb, subnodes of the node) -> bid of the heap block.
Builder = Callable[[_Ndb, list[SubnodeEntry]], int]


def property_context(props: Props) -> Builder:
    def build(ndb: _Ndb, subnodes: list[SubnodeEntry]) -> int:
        heap = _Heap(ndb, 0xBC, subnodes)
        records = []
        for prop_id, (ptype, value) in sorted(props.items()):
            stored = _cell(heap, ptype, value)
            if stored is not None:
                records.append((struct.pack("<H", prop_id), struct.pack("<HI", ptype, stored)))
        return heap.build(_bth(heap, 2, 6, records))

    return build


def table_context(columns: list[tuple[int, int]], rows: list[dict[int, Value]]) -> Builder:
    """Table with 4-byte cells. ``columns``: (property id, type); every row holds the
    row id (0x67F2)."""
    cols = [(0x67F2, PT_LONG), (0x67F3, PT_LONG)]
    cols += [c for c in columns if c[0] not in (0x67F2, 0x67F3)]

    def build(ndb: _Ndb, subnodes: list[SubnodeEntry]) -> int:
        heap = _Heap(ndb, 0x7C, subnodes)
        width = 4 * len(cols)
        row_size = width + (len(cols) + 7) // 8
        matrix = bytearray()
        index = []
        for row_index, row in enumerate(rows):
            cells = bytearray(row_size)
            for i, (prop_id, ptype) in enumerate(cols):
                value = row.get(prop_id, 0 if prop_id == 0x67F3 else None)
                stored = None if value is None else _cell(heap, ptype, value)
                if stored is None:
                    continue
                struct.pack_into("<I", cells, 4 * i, stored)
                cells[width + i // 8] |= 0x80 >> (i % 8)
            matrix += cells
            row_id = row[0x67F2]
            assert isinstance(row_id, int)
            index.append((struct.pack("<I", row_id), struct.pack("<I", row_index)))
        row_index_hid = _bth(heap, 4, 4, index)
        rows_hnid = heap.value(bytes(matrix))
        info = struct.pack(
            "<BB4HIII", 0x7C, len(cols), width, width, width, row_size, row_index_hid,
            rows_hnid, 0,
        )  # fmt: skip
        for prop_id, ptype, offset, bit in sorted(
            (prop_id, ptype, 4 * i, i) for i, (prop_id, ptype) in enumerate(cols)
        ):
            info += struct.pack("<IHBB", (prop_id << 16) | ptype, offset, 4, bit)
        return heap.build(heap.alloc(info))

    return build


def _top_node(
    ndb: _Ndb, nid: int, parent: int, build: Builder, children: list[SubnodeEntry] | None = None
) -> None:
    subnodes = list(children or [])
    bid = build(ndb, subnodes)
    ndb.node(nid, bid, ndb.subnode_block(subnodes), parent)


def _sub_node(ndb: _Ndb, nid: int, build: Builder, owner: list[SubnodeEntry]) -> None:
    own: list[SubnodeEntry] = []
    bid = build(ndb, own)
    owner.append((nid, bid, ndb.subnode_block(own)))


# ---------------------------------------------------------------------------
# Messaging layer
# ---------------------------------------------------------------------------


class _Ids:
    def __init__(self) -> None:
        self.folder = 0x400
        self.message = 0x400

    def next_folder(self) -> int:
        self.folder += 1
        return (self.folder << 5) | NID_TYPE_NORMAL_FOLDER

    def next_message(self) -> int:
        self.message += 1
        return (self.message << 5) | NID_TYPE_NORMAL_MESSAGE


def _message(ndb: _Ndb, nid: int, parent: int, msg: PstMessage) -> None:
    subnodes: list[SubnodeEntry] = []
    recipient_rows: list[dict[int, Value]] = [
        {
            0x67F2: i,
            0x0C15: r.recipient_type,
            0x3001: r.display_name,
            0x3002: "SMTP",
            0x3003: r.smtp_address,
            0x39FE: r.smtp_address,
            0x5FF6: r.display_name,
            0x0FFE: 6,
            0x3900: 0,
        }
        for i, r in enumerate(msg.recipients)
    ]
    recipient_columns = [
        (0x0C15, PT_LONG), (0x0FFE, PT_LONG), (0x3001, PT_UNICODE), (0x3002, PT_UNICODE),
        (0x3003, PT_UNICODE), (0x3900, PT_LONG), (0x39FE, PT_UNICODE), (0x5FF6, PT_UNICODE),
    ]  # fmt: skip
    _sub_node(ndb, NID_RECIPIENT_TABLE, table_context(recipient_columns, recipient_rows), subnodes)
    if msg.attachments:
        rows: list[dict[int, Value]] = []
        for i, att in enumerate(msg.attachments):
            att_nid = ((0x400 + i) << 5) | NID_TYPE_ATTACHMENT
            props: Props = {
                0x0E20: (PT_LONG, len(att.data)),
                0x0FFE: (PT_LONG, 7),
                0x3001: (PT_UNICODE, att.filename),
                0x3701: (PT_BINARY, att.data),
                0x3704: (PT_UNICODE, att.filename),
                0x3705: (PT_LONG, 1),
                0x3707: (PT_UNICODE, att.filename),
                0x370B: (PT_LONG, -1),
                0x370E: (PT_UNICODE, att.mime_type),
            }
            _sub_node(ndb, att_nid, property_context(props), subnodes)
            rows.append(
                {0x67F2: att_nid, 0x0E20: len(att.data), 0x3704: att.filename,
                 0x3705: 1, 0x3707: att.filename, 0x370B: -1}
            )  # fmt: skip
        columns = [
            (0x0E20, PT_LONG), (0x3704, PT_UNICODE), (0x3705, PT_LONG),
            (0x3707, PT_UNICODE), (0x370B, PT_LONG),
        ]  # fmt: skip
        _sub_node(ndb, NID_ATTACHMENT_TABLE, table_context(columns, rows), subnodes)
    props = {
        0x001A: (PT_UNICODE, "IPM.Note"),
        0x0037: (PT_UNICODE, msg.subject),
        0x0039: (PT_SYSTIME, FILETIME_2026),
        0x0042: (PT_UNICODE, msg.sender_name),
        0x0064: (PT_UNICODE, "SMTP"),
        0x0065: (PT_UNICODE, msg.sender_email),
        0x0070: (PT_UNICODE, msg.subject),
        0x0C1A: (PT_UNICODE, msg.sender_name),
        0x0C1E: (PT_UNICODE, "SMTP"),
        0x0C1F: (PT_UNICODE, msg.sender_email),
        0x0E04: (PT_UNICODE, msg.display_to),
        0x0E06: (PT_SYSTIME, FILETIME_2026),
        0x0E07: (PT_LONG, 0x1 | (0x10 if msg.attachments else 0)),
        0x0E1B: (PT_BOOLEAN, 1 if msg.attachments else 0),
        0x1000: (PT_UNICODE, msg.body),
        0x5D01: (PT_UNICODE, msg.sender_email),
    }
    if msg.html_body is not None:
        props[0x1013] = (PT_BINARY, msg.html_body.encode("utf-8"))
    if msg.transport_headers is not None:
        props[0x007D] = (PT_UNICODE, msg.transport_headers)
    _top_node(ndb, nid, parent, property_context(props), subnodes)


def _folder(ndb: _Ndb, ids: _Ids, nid: int, parent: int, folder: PstFolder) -> None:
    sub_nids = [ids.next_folder() for _ in folder.folders]
    message_nids = [ids.next_message() for _ in folder.messages]
    props: Props = {
        0x3001: (PT_UNICODE, folder.name),
        0x3602: (PT_LONG, len(folder.messages)),
        0x3603: (PT_LONG, 0),
        0x360A: (PT_BOOLEAN, 1 if folder.folders else 0),
        0x3613: (PT_UNICODE, "IPF.Note"),
    }
    _top_node(ndb, nid, parent, property_context(props))
    hierarchy_rows: list[dict[int, Value]] = [
        {0x67F2: n, 0x3001: f.name, 0x3602: len(f.messages)}
        for n, f in zip(sub_nids, folder.folders, strict=True)
    ]
    hierarchy_columns = [(0x3001, PT_UNICODE), (0x3602, PT_LONG)]
    _top_node(ndb, nid + HIERARCHY, 0, table_context(hierarchy_columns, hierarchy_rows))
    contents_rows: list[dict[int, Value]] = [
        {0x67F2: n, 0x001A: "IPM.Note", 0x0037: m.subject, 0x0042: m.sender_name}
        for n, m in zip(message_nids, folder.messages, strict=True)
    ]
    contents_columns = [(0x001A, PT_UNICODE), (0x0037, PT_UNICODE), (0x0042, PT_UNICODE)]
    _top_node(ndb, nid + CONTENTS, 0, table_context(contents_columns, contents_rows))
    _top_node(ndb, nid + ASSOCIATED, 0, table_context([], []))
    for sub_nid, sub in zip(sub_nids, folder.folders, strict=True):
        _folder(ndb, ids, sub_nid, nid, sub)
    for message_nid, message in zip(message_nids, folder.messages, strict=True):
        _message(ndb, message_nid, nid, message)


# ---------------------------------------------------------------------------
# File assembly
# ---------------------------------------------------------------------------


def _page(
    entries: bytes,
    count: int,
    max_count: int,
    entry_size: int,
    level: int,
    ptype: int,
    ib: int,
    bid: int,
) -> bytes:
    body = entries.ljust(488, b"\0") + struct.pack("<BBBB4x", count, max_count, entry_size, level)
    return body + struct.pack("<BBHIQ", ptype, ptype, signature(ib, bid), crc(body), bid)


class _Pages:
    def __init__(self, start: int) -> None:
        self.offset = start
        self.data = bytearray()
        self.bid = 0x10000

    def tree(
        self, entries: list[tuple[int, bytes]], entry_size: int, ptype: int
    ) -> tuple[int, int]:
        """Write a B-tree over sorted ``(key, leaf entry)``; returns the root BREF."""
        max_count = 488 // entry_size
        level = 0
        nodes = [(e[0], e[1]) for e in entries]
        size = entry_size
        while True:
            chunks = [nodes[i : i + max_count] for i in range(0, len(nodes), max_count)] or [[]]
            refs = []
            for chunk in chunks:
                self.bid += 4
                ib = self.offset + len(self.data)
                raw = b"".join(e for _, e in chunk)
                self.data += _page(raw, len(chunk), max_count, size, level, ptype, ib, self.bid)
                refs.append((chunk[0][0] if chunk else 0, self.bid, ib))
            if len(refs) == 1:
                return refs[0][1], refs[0][2]
            level += 1
            size = 24
            max_count = 488 // 24
            nodes = [(key, struct.pack("<QQQ", key, bid, ib)) for key, bid, ib in refs]


def _amap(used_until: int) -> bytes:
    slots = -(-(used_until - AMAP_OFFSET) // 64)
    bits = bytearray(496)
    for i in range(slots):
        bits[i // 8] |= 0x80 >> (i % 8)
    body = bytes(bits)
    return body + struct.pack("<BBHIQ", 0x84, 0x84, 0, crc(body), AMAP_OFFSET)


def _pmap() -> bytes:
    body = b"\xff" * 496
    return body + struct.pack("<BBHIQ", 0x83, 0x83, 0, crc(body), PMAP_OFFSET)


def pst_bytes(top_folders: list[PstFolder], store_name: str = "Fichier de données") -> bytes:
    ndb = _Ndb()
    store_props: Props = {
        0x0FF9: (PT_BINARY, bytes(range(16))),
        0x3001: (PT_UNICODE, store_name),
        0x6633: (PT_BOOLEAN, 1),
    }
    _top_node(ndb, NID_MESSAGE_STORE, 0, property_context(store_props))
    ids = _Ids()
    _folder(ndb, ids, NID_ROOT_FOLDER, NID_ROOT_FOLDER, PstFolder("", folders=top_folders))

    # Data blocks, 64-byte aligned, trailer at the end of each block.
    data = bytearray()
    bbt: list[tuple[int, bytes]] = []
    for bid, payload in ndb.blocks:
        ib = FIRST_DATA_OFFSET + len(data)
        total = -(-(len(payload) + 16) // 64) * 64
        trailer = struct.pack("<HHIQ", len(payload), signature(ib, bid), crc(payload), bid)
        data += payload.ljust(total - 16, b"\0") + trailer
        bbt.append((bid, struct.pack("<QQHH4x", bid, ib, len(payload), 2)))
    pages_start = FIRST_DATA_OFFSET + -(-len(data) // PAGE_SIZE) * PAGE_SIZE
    data = data.ljust(pages_start - FIRST_DATA_OFFSET, b"\0")
    pages = _Pages(pages_start)
    nbt = sorted(
        (n.nid, struct.pack("<QQQI4x", n.nid, n.bid_data, n.bid_sub, n.parent)) for n in ndb.nodes
    )
    nbt_root = pages.tree(nbt, 32, 0x81)
    bbt_root = pages.tree(sorted(bbt), 24, 0x80)
    eof = pages_start + len(pages.data)
    assert eof - AMAP_OFFSET <= AMAP_COVERAGE, "fixture too large for one allocation map"

    header = bytearray(564)
    header[0:4] = b"!BDN"
    struct.pack_into("<HHHBB", header, 8, 0x4D53, 23, 19, 1, 1)
    struct.pack_into("<QI", header, 32, pages.bid + 4, 1)
    nid_counters = [0x400] * 32
    nid_counters[NID_TYPE_NORMAL_FOLDER] = ids.folder + 1
    nid_counters[NID_TYPE_NORMAL_MESSAGE] = ids.message + 1
    struct.pack_into("<32I", header, 44, *nid_counters)
    amap_free = AMAP_COVERAGE - -(-(eof - AMAP_OFFSET) // 64) * 64
    struct.pack_into(
        "<IQQQQQQQQBBH", header, 180, 0, eof, AMAP_OFFSET, amap_free, 0,
        nbt_root[0], nbt_root[1], bbt_root[0], bbt_root[1], 0x02, 0, 0,
    )  # fmt: skip
    header[256:512] = b"\xff" * 256
    header[512] = 0x80  # bSentinel
    header[513] = 0x00  # bCryptMethod: NDB_CRYPT_NONE
    struct.pack_into("<Q", header, 516, (len(ndb.blocks) + 1) << 2)
    struct.pack_into("<I", header, 4, crc(bytes(header[8 : 8 + 471])))
    struct.pack_into("<I", header, 524, crc(bytes(header[8 : 8 + 516])))

    out = bytearray(header).ljust(AMAP_OFFSET, b"\0")
    out += _amap(eof) + _pmap() + data + pages.data
    assert len(out) == eof
    return bytes(out)
