"""Compound File Binary (OLE2) read/write helpers.

Reading uses ``olefile``; writing uses the CFB v3 writer shipped with extract-msg
(``extract_msg.ole_writer.OleWriter``: 512-byte sectors, FAT/DIFAT, mini stream
and mini FAT below 4096 bytes, sorted directory tree), so no hand-rolled sector
allocator is needed. Files written here are validated by re-reading them with
olefile and extract-msg (tests). Directory timestamps and state bits are not
copied (they are purged); CLSIDs are kept.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

import olefile
from extract_msg.ole_writer import OleWriter

Entry = tuple[str, ...]


@dataclass(slots=True)
class CfbEntry:
    path: Entry
    storage: bool
    clsid: bytes = b""
    data: bytes | None = field(default=None, repr=False)


def _clsid_bytes(text: str) -> bytes:
    if not text:
        return b""
    try:
        return uuid.UUID(text).bytes_le
    except ValueError:
        return b""


def read_cfb(
    source: Path, charge: Callable[[int], None] | None = None
) -> tuple[bytes, list[CfbEntry]]:
    """Root CLSID and every storage/stream (parents before children). ``charge``
    receives each stream's declared size before it is read (expansion budget:
    sector chains may be shared, so streams can add up to more than the file)."""
    with olefile.OleFileIO(str(source)) as ole:
        root_clsid = _clsid_bytes(ole.root.clsid)
        entries: list[CfbEntry] = []
        for parts in sorted(ole.listdir(streams=True, storages=True)):
            path = tuple(parts)
            kind = ole.get_type(list(path))
            clsid = _clsid_bytes(ole.getclsid(list(path)))
            if kind == olefile.STGTY_STORAGE:
                entries.append(CfbEntry(path, True, clsid))
            elif kind == olefile.STGTY_STREAM:
                if charge is not None:
                    charge(ole.get_size(list(path)))
                with ole.openstream(list(path)) as stream:
                    entries.append(CfbEntry(path, False, clsid, stream.read()))
    return root_clsid, entries


def write_cfb(root_clsid: bytes, entries: Iterable[CfbEntry], destination: Path) -> None:
    writer = OleWriter(root_clsid) if len(root_clsid) == 16 else OleWriter()
    for entry in sorted(entries, key=lambda e: e.path):
        clsid = entry.clsid if len(entry.clsid) == 16 else None
        data = None if entry.storage else (entry.data or b"")
        writer.addEntry(list(entry.path), data, entry.storage, clsid=clsid)
    writer.write(str(destination))
