"""Zip packages (OOXML, ODF) read under the expansion budget, and rewritten."""

from __future__ import annotations

import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from anonyma.models import ArchiveBombError, ExpansionBudget

_CHUNK = 1 << 20


@dataclass(slots=True)
class ZipPackage:
    """In-memory copy of a zip package; entry order is preserved."""

    entries: dict[str, bytes] = field(default_factory=dict)
    #: Entries originally stored without compression (ODF ``mimetype``).
    stored: set[str] = field(default_factory=set)

    @property
    def names(self) -> list[str]:
        return list(self.entries)


def read_package(path: Path, budget: ExpansionBudget | None, depth: int = 0) -> ZipPackage:
    """Read every entry of the zip at ``path``.

    Each entry is charged to ``budget`` before being decompressed, and the actual
    decompressed size is capped to the declared one (lying headers are refused).
    """
    package = ZipPackage()
    with zipfile.ZipFile(path) as zf:
        infos = zf.infolist()
        for info in infos:
            if info.is_dir():
                continue
            if info.flag_bits & 0x1:
                raise PermissionError("entrée chiffrée")
            if budget is not None:
                budget.consume(info.file_size, compressed=info.compress_size or None, depth=depth)
            package.entries[_normalise(info.filename)] = _read_capped(zf, info)
            if info.compress_type == zipfile.ZIP_STORED:
                package.stored.add(_normalise(info.filename))
    return package


def _normalise(name: str) -> str:
    return name.replace("\\", "/")


def _read_capped(zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    limit = info.file_size
    chunks: list[bytes] = []
    total = 0
    with zf.open(info) as fh:
        while True:
            chunk = fh.read(_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise ArchiveBombError("taille décompressée supérieure à la taille déclarée")
            chunks.append(chunk)
    return b"".join(chunks)


def write_package(
    package: ZipPackage,
    destination: Path,
    *,
    first: str | None = None,
    stored: set[str] | frozenset[str] = frozenset(),
) -> None:
    """Write ``package`` to ``destination``; ``first`` is written first (and
    entries in ``stored`` uncompressed), as required by ODF's ``mimetype``."""
    names = list(package.entries)
    if first is not None and first in package.entries:
        names.remove(first)
        names.insert(0, first)
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in names:
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED if name in stored else zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, package.entries[name])


def is_zip(head: bytes) -> bool:
    return head.startswith(b"PK\x03\x04")
