"""Helpers shared by archive-like containers (zip, 7z, pst output)."""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import IO

from anonyma import registry
from anonyma.models import (
    FILENAME_SEGMENT_ID,
    ArchiveBombError,
    Document,
    DocumentStatus,
    ExtractionContext,
    ReplacementPlan,
    Segment,
    SegmentContext,
    SegmentKind,
    Severity,
)
from anonyma.textutils import safe_filename

CHUNK = 1 << 16
#: Fixed timestamp written in rebuilt archives (original dates are purged).
EPOCH_1980 = (1980, 1, 1, 0, 0, 0)
EPOCH_1980_TS = 315532800


def clean_entry_name(raw: str) -> str | None:
    """Logical, zip-slip-proof entry path: ``/`` separators, no drive, no ``..``."""
    name = raw.replace("\\", "/")
    if len(name) > 1 and name[1] == ":":
        name = name[2:]
    parts = [p for p in name.split("/") if p not in ("", ".", "..")]
    return "/".join(parts) if parts else None


def copy_limited(src: IO[bytes], destination: Path, limit: int) -> int:
    """Stream ``src`` to ``destination``; never write more than ``limit`` bytes."""
    total = 0
    with destination.open("wb") as dst:
        while True:
            chunk = src.read(CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise ArchiveBombError("taille réelle supérieure à la taille déclarée")
            dst.write(chunk)
    return total


def failed_child(
    parent: Document, ctx: ExtractionContext, name: str, code: str, message: str
) -> Document:
    """Register a child that could not be read (encrypted, unsupported method...).

    Its original bytes are never available to writers: the container omits it.
    """
    fmt = registry.detect_format(Path(PurePosixPath(name).name), head=b"") or "inconnu"
    child = Document(
        source=parent.source,
        name=name,
        format=fmt,
        depth=ctx.depth + 1,
        parent_id=parent.id,
        status=DocumentStatus.FAILED,
    )
    child.add_issue(Severity.ERROR, code, message)
    child.segments.append(
        Segment(
            id=FILENAME_SEGMENT_ID,
            text=name,
            locator=(),
            context=SegmentContext(SegmentKind.FILENAME, label="nom de fichier"),
        )
    )
    parent.children.append(child)
    return child


def empty_directories(directories: Iterable[str], files: Iterable[str]) -> list[str]:
    """Directories that hold no file (the others are implied by file paths)."""
    used: set[str] = set()
    for name in files:
        parts = name.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            used.add("/".join(parts[:i]))
    result = []
    for directory in sorted(set(directories)):
        if directory in used or any(other.startswith(directory + "/") for other in directories):
            continue
        result.append(directory)
    return result


def directory_segments(document: Document, directories: list[str]) -> None:
    for i, directory in enumerate(directories):
        document.segments.append(
            Segment(
                id=f"dir{i}",
                text=directory,
                locator=("dir", i),
                context=SegmentContext(SegmentKind.FILENAME, label="nom de dossier"),
            )
        )


def anonymized_directories(document: Document, plan: ReplacementPlan) -> list[str]:
    names = []
    for segment in document.segments:
        if segment.locator[:1] == ("dir",):
            raw = plan.apply(segment).replace("\\", "/")
            parts = [safe_filename(p) for p in raw.split("/") if p not in ("", ".", "..")]
            if parts:
                names.append("/".join(parts))
    return names


class UniqueNames:
    """Avoid duplicate entry names (several originals may share a substitute)."""

    def __init__(self) -> None:
        self._used: set[str] = set()

    def __call__(self, name: str) -> str:
        candidate = name
        path = PurePosixPath(name)
        i = 2
        while candidate.casefold() in self._used:
            candidate = str(path.with_name(f"{path.stem} ({i}){path.suffix}"))
            i += 1
        self._used.add(candidate.casefold())
        return candidate


def neutral_timestamp(path: Path) -> None:
    """Give a written child a neutral modification time before archiving it."""
    with contextlib.suppress(OSError):
        os.utime(path, (EPOCH_1980_TS, EPOCH_1980_TS))
