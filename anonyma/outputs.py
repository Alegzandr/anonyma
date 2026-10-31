"""What Anonyma wrote into each output folder, so that a new run can replace it.

For every output folder, ``data/sorties/<key>.json`` lists the files a batch placed
there or in its quarantine, with their size and modification time. ``anonyma run
--remplacer`` deletes those files, and only those, when they are unchanged: a file
added, renamed or modified by someone else is never touched. The list holds the
anonymized output paths only, in the application folder (never in the deliverable).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from anonyma import paths
from anonyma.logs import get_logger

log = get_logger("outputs")

MANIFEST_FOLDER = "sorties"


@dataclass(frozen=True, slots=True)
class Cleanup:
    """Outcome of :func:`remove_previous`."""

    removed: int = 0
    #: Recorded files left in place: modified or replaced since they were written.
    kept: int = 0


def _key(output_dir: Path) -> str:
    text = str(Path(output_dir).resolve())
    if os.name == "nt":
        text = text.casefold()
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def manifest_path(output_dir: Path) -> Path:
    return paths.data_dir() / MANIFEST_FOLDER / f"{_key(output_dir)}.json"


def _roots(output_dir: Path) -> list[Path]:
    from anonyma.pipeline import quarantine_dir

    output_dir = Path(output_dir).resolve()
    return [output_dir, quarantine_dir(output_dir)]


def _load(output_dir: Path) -> dict[str, dict[str, int]]:
    try:
        data = json.loads(manifest_path(output_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    files = data.get("fichiers") if isinstance(data, dict) else None
    if not isinstance(files, dict):
        return {}
    return {
        str(path): {"taille": int(stamp["taille"]), "date": int(stamp["date"])}
        for path, stamp in files.items()
        if isinstance(stamp, dict) and {"taille", "date"} <= stamp.keys()
    }


def _save(output_dir: Path, files: dict[str, dict[str, int]]) -> None:
    path = manifest_path(output_dir)
    if not files:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {"dossier": str(Path(output_dir).resolve()), "fichiers": files}
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(temp, path)


def _stamp(path: Path) -> dict[str, int]:
    info = path.stat()
    return {"taille": info.st_size, "date": info.st_mtime_ns}


def _files_of(path: Path) -> Iterable[Path]:
    if path.is_dir():
        yield from (p for p in sorted(path.rglob("*")) if p.is_file() and not p.is_symlink())
    elif path.is_file():
        yield path


def record(output_dir: Path, outputs: Iterable[Path]) -> None:
    """Add ``outputs`` (files or folders placed by a batch) to the folder's record.
    Best effort: a failure is logged, never raised."""
    try:
        files = {p: s for p, s in _load(output_dir).items() if Path(p).exists()}
        roots = _roots(output_dir)
        for output in outputs:
            for file in _files_of(Path(output)):
                resolved = file.resolve()
                if any(resolved.is_relative_to(root) for root in roots):
                    files[str(resolved)] = _stamp(resolved)
        _save(output_dir, files)
    except OSError as exc:
        log.warning("output record not saved: %s", type(exc).__name__)


def remove_previous(output_dir: Path) -> Cleanup:
    """Delete the recorded, unchanged files of earlier batches in ``output_dir`` and
    its quarantine, then the folders this leaves empty. Nothing else is touched."""
    roots = _roots(output_dir)
    removed = kept = 0
    remaining: dict[str, dict[str, int]] = {}
    emptied: set[Path] = set()
    for raw, stamp in _load(output_dir).items():
        path = Path(raw)
        inside = any(path.is_relative_to(root) and path != root for root in roots)
        try:
            if not inside or path.is_symlink() or not path.is_file():
                continue
            if _stamp(path) != stamp:
                kept += 1
                remaining[raw] = stamp
                continue
            path.unlink()
        except OSError:
            kept += 1
            remaining[raw] = stamp
            continue
        removed += 1
        emptied.add(path.parent)
    for folder in sorted(emptied, key=lambda p: len(p.parts), reverse=True):
        _remove_empty(folder, roots)
    with contextlib.suppress(OSError):
        _save(output_dir, remaining)
    return Cleanup(removed, kept)


def _remove_empty(folder: Path, roots: list[Path]) -> None:
    """Remove ``folder`` and its parents while empty, never a root itself."""
    while any(folder.is_relative_to(root) and folder != root for root in roots):
        try:
            folder.rmdir()  # only succeeds when empty
        except OSError:
            return
        folder = folder.parent
    for root in roots[1:]:  # an emptied quarantine folder goes too
        if folder == root:
            with contextlib.suppress(OSError):
                root.rmdir()


def holds_files(output_dir: Path) -> bool:
    """Whether ``output_dir`` exists and already holds something."""
    folder = Path(output_dir)
    try:
        return folder.is_dir() and any(folder.iterdir())
    except OSError:
        return False


__all__ = ["Cleanup", "holds_files", "manifest_path", "record", "remove_previous"]
