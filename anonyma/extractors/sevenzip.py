"""7z archives (py7zr): every entry is a child, metadata purged on output.

Anti-bomb: the budget is charged for every entry (declared size, ratio computed
from the packed size share) before decompression, which then streams into
size-limited temporary files. Encrypted archives (any encrypted block) make every
entry a FAILED child; encrypted headers make the whole archive fail.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import py7zr
from py7zr.exceptions import PasswordRequired
from py7zr.helpers import get_sanitized_output_path
from py7zr.io import Py7zIO, WriterFactory

from anonyma.extractors._archive import (
    clean_entry_name,
    directory_segments,
    empty_directories,
    failed_child,
)
from anonyma.extractors._empty import empty_file
from anonyma.models import (
    ArchiveBombError,
    Document,
    ExtractionContext,
    Extractor,
    Severity,
)
from anonyma.registry import register_extractor
from anonyma.textutils import safe_suffix

SEVEN_ZIP_MAGIC = b"7z\xbc\xaf\x27\x1c"


class _LimitedFile(Py7zIO):
    """Writes one entry to a temporary file, refusing more than ``limit`` bytes."""

    def __init__(self, path: Path, limit: int) -> None:
        self.path = path
        self.limit = limit
        self.written = 0
        self._fh: io.BufferedWriter | None = path.open("wb")

    def write(self, s: bytes | bytearray) -> int:
        self.written += len(s)
        if self.written > self.limit:
            self.close()
            raise ArchiveBombError("taille réelle supérieure à la taille déclarée")
        assert self._fh is not None
        return self._fh.write(s)

    def read(self, size: int | None = None) -> bytes:
        return b""

    def seek(self, offset: int, whence: int = 0) -> int:
        return 0

    def flush(self) -> None:
        if self._fh is not None:
            self._fh.flush()

    def size(self) -> int:
        return self.written

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


class _Factory(WriterFactory):
    def __init__(self, targets: dict[str, tuple[Path, int]]) -> None:
        self.targets = targets
        self.products: dict[str, _LimitedFile] = {}

    def create(self, filename: str) -> Py7zIO:
        path, limit = self.targets.get(filename, (Path(), -1))
        if limit < 0:
            raise ArchiveBombError("élément inattendu dans l'archive")
        product = _LimitedFile(path, limit)
        self.products[filename] = product
        return product


@register_extractor
class SevenZipExtractor(Extractor):
    name = "7z"
    extensions = {".7z": "7z"}

    @classmethod
    def sniff(cls, head: bytes) -> str | None:
        return "7z" if head.startswith(SEVEN_ZIP_MAGIC) else None

    def extract(self, source: Path, document: Document, ctx: ExtractionContext) -> None:
        if empty_file(source, document):
            return
        try:
            archive = py7zr.SevenZipFile(source, "r")
        except PasswordRequired:
            document.add_issue(
                Severity.ERROR,
                "archive.encrypted",
                "Archive chiffrée par mot de passe : elle ne peut pas être anonymisée.",
            )
            return
        with archive:
            self._extract(archive, source, document, ctx)

    def _extract(
        self, archive: Any, source: Path, document: Document, ctx: ExtractionContext
    ) -> None:
        entries = archive.list()
        packed = max(source.stat().st_size, 1)
        unpacked = max(sum(e.uncompressed or 0 for e in entries if not e.is_directory), 1)
        encrypted = archive.needs_password()
        directories: list[str] = []
        files: list[tuple[str, str, int]] = []  # (logical name, factory key, size)
        seen: dict[str, int] = {}
        for entry in entries:
            name = clean_entry_name(entry.filename)
            if entry.filename not in seen:
                outname = entry.filename
                seen[entry.filename] = 0
            else:
                outname = f"{entry.filename}_{seen[entry.filename]}"
                seen[entry.filename] += 1
            if name is None:
                continue
            if entry.is_directory:
                directories.append(name)
                continue
            key = get_sanitized_output_path(outname, None).as_posix()
            files.append((name, key, int(entry.uncompressed or 0)))
        directory_segments(document, empty_directories(directories, [f[0] for f in files]))
        if encrypted:
            for name, _, _ in files:
                failed_child(
                    document,
                    ctx,
                    name,
                    "archive.encrypted",
                    "Élément chiffré par mot de passe : il ne peut pas être anonymisé.",
                )
            return

        targets: dict[str, tuple[Path, int]] = {}
        for name, key, size in files:
            ctx.check_cancelled()
            share = max(1, round(size * packed / unpacked))
            ctx.budget.consume(size, compressed=share, depth=ctx.depth + 1)
            targets[key] = (ctx.temp_path(safe_suffix(name)), size)
        if targets:
            factory = _Factory(targets)
            try:
                archive.extractall(factory=factory)
            finally:
                for product in factory.products.values():
                    product.close()
        for name, key, _ in files:
            ctx.check_cancelled()
            path, _ = targets[key]
            if not path.exists():
                path.write_bytes(b"")  # empty entries have no stream
            ctx.extract_child(path, name, document)
