"""Zip archives: every entry is a child; comments, extra fields and dates are purged.

Anti-bomb: the expansion budget is charged with the declared size and ratio before
each entry is materialised, and actual sizes are enforced while streaming.
Encrypted entries become FAILED children (omitted from the output).
"""

from __future__ import annotations

import zipfile
from concurrent.futures import CancelledError
from pathlib import Path

from anonyma.extractors._archive import (
    clean_entry_name,
    copy_limited,
    directory_segments,
    empty_directories,
    failed_child,
)
from anonyma.extractors._empty import empty_file
from anonyma.models import ArchiveBombError, Document, ExtractionContext, Extractor
from anonyma.registry import register_extractor
from anonyma.textutils import safe_suffix

_UTF8_FLAG = 0x800
_ENCRYPTED_FLAG = 0x1


def entry_name(info: zipfile.ZipInfo) -> str:
    """Entry name; legacy (non-UTF-8 flagged) names are tried as UTF-8 then cp850."""
    if info.flag_bits & _UTF8_FLAG:
        return info.filename
    try:
        raw = info.filename.encode("cp437")
    except UnicodeEncodeError:
        return info.filename
    for codec in ("utf-8", "cp850"):
        try:
            return raw.decode(codec)
        except UnicodeDecodeError:
            continue
    return info.filename


@register_extractor
class ZipExtractor(Extractor):
    name = "zip"
    extensions = {".zip": "zip"}

    def extract(self, source: Path, document: Document, ctx: ExtractionContext) -> None:
        if empty_file(source, document):
            return
        directories: list[str] = []
        files: list[str] = []
        with zipfile.ZipFile(source) as archive:
            for info in archive.infolist():
                ctx.check_cancelled()
                name = clean_entry_name(entry_name(info))
                if name is None:
                    continue
                if info.is_dir():
                    directories.append(name)
                    continue
                files.append(name)
                if info.flag_bits & _ENCRYPTED_FLAG:
                    failed_child(
                        document,
                        ctx,
                        name,
                        "archive.encrypted",
                        "Élément chiffré par mot de passe : il ne peut pas être anonymisé.",
                    )
                    continue
                ctx.budget.consume(
                    info.file_size, compressed=info.compress_size, depth=ctx.depth + 1
                )
                target = ctx.temp_path(safe_suffix(name))
                try:
                    with archive.open(info) as src:
                        copy_limited(src, target, info.file_size)
                except (ArchiveBombError, CancelledError):
                    raise
                except (zipfile.BadZipFile, NotImplementedError, RuntimeError, OSError) as exc:
                    target.unlink(missing_ok=True)
                    failed_child(
                        document,
                        ctx,
                        name,
                        "archive.entry_unreadable",
                        f"Élément illisible ({type(exc).__name__}) : il ne peut pas être "
                        "anonymisé.",
                    )
                    continue
                ctx.extract_child(target, name, document)
        directory_segments(document, empty_directories(directories, files))
