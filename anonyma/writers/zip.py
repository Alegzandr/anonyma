"""Zip writer: rebuilt from anonymized children, no comment, no extra field, fixed dates."""

from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

from anonyma.extractors._archive import EPOCH_1980, UniqueNames, anonymized_directories
from anonyma.models import Document, Issue, ReplacementPlan, WriteContext, Writer
from anonyma.registry import register_writer


def write_zip(entries: list[tuple[str, Path]], directories: list[str], destination: Path) -> None:
    unique = UniqueNames()
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, path in entries:
            info = zipfile.ZipInfo(unique(name), EPOCH_1980)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            large = path.stat().st_size >= zipfile.ZIP64_LIMIT
            with path.open("rb") as src, archive.open(info, "w", force_zip64=large) as dst:
                shutil.copyfileobj(src, dst, 1 << 16)
        for directory in directories:
            info = zipfile.ZipInfo(unique(directory.rstrip("/") + "/"), EPOCH_1980)
            info.external_attr = (0o40755 << 16) | 0x10
            archive.writestr(info, b"")


@register_writer
class ZipWriter(Writer):
    name = "zip"
    formats = frozenset({"zip"})

    def write(
        self,
        document: Document,
        plan: ReplacementPlan,
        destination: Path,
        ctx: WriteContext,
    ) -> list[Issue]:
        entries: list[tuple[str, Path]] = []
        for child in document.children:
            output = ctx.child_outputs.get(child.id)
            if output is not None and output.path is not None:
                entries.append((output.name, output.path))
        write_zip(entries, anonymized_directories(document, plan), destination)
        return []
