"""Portable paths: everything Anonyma writes lives under the application folder."""

from __future__ import annotations

import contextlib
import os
import shutil
import stat
import sys
import tempfile
import time
import warnings
from pathlib import Path
from typing import IO

#: Largest image decoded (pixels): an A1 sheet scanned at 400 dpi. Beyond, decoding
#: fails (``DecompressionBombError``/``Warning`` both raise) instead of exhausting
#: the memory of a worker.
MAX_IMAGE_PIXELS = 100_000_000


def app_root() -> Path:
    """Folder holding the executable (frozen) or the project root (source).

    ``ANONYMA_HOME`` overrides it (tests, custom portable locations).
    """
    override = os.environ.get("ANONYMA_HOME")
    if override:
        return Path(override).resolve()
    if getattr(sys, "frozen", False):
        executable = Path(sys.executable).resolve()
        if sys.platform == "darwin":
            # Anonyma.app/Contents/MacOS/<exe>: never write inside the bundle (it
            # breaks its signature and may be read-only); the portable folder is
            # the one that contains the .app.
            for parent in executable.parents:
                if parent.suffix == ".app":
                    return parent.parent
        return executable.parent
    return Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    return app_root() / "data"


def tmp_dir() -> Path:
    return data_dir() / "tmp"


def logs_dir() -> Path:
    return data_dir() / "logs"


def models_dir() -> Path:
    return app_root() / "models"


def config_path() -> Path:
    return data_dir() / "config.json"


def ensure_dirs() -> None:
    for d in (data_dir(), tmp_dir(), logs_dir(), models_dir()):
        d.mkdir(parents=True, exist_ok=True)


def configure_environment() -> None:
    """Redirect every library cache/temp location into the portable folder.

    Must be called by each entry point (CLI, GUI, worker processes) before heavy
    imports.
    """
    ensure_dirs()
    tmp = str(tmp_dir())
    for var in ("TMPDIR", "TEMP", "TMP"):
        os.environ[var] = tmp
    tempfile.tempdir = tmp
    models = str(models_dir())
    os.environ["HF_HOME"] = str(models_dir() / "hf")
    os.environ["HF_HUB_CACHE"] = str(models_dir() / "hf" / "hub")
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    os.environ["HF_HUB_OFFLINE"] = "1"  # model_manager lifts it explicitly
    os.environ["DO_NOT_TRACK"] = "1"
    os.environ["XDG_CACHE_HOME"] = str(data_dir() / "cache")
    os.environ.setdefault("ANONYMA_MODELS", models)
    sys.dont_write_bytecode = True
    limit_images()


def limit_images() -> None:
    """Cap the size of the images Pillow decodes (every process)."""
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    warnings.simplefilter("error", Image.DecompressionBombWarning)


#: Lock file of a temporary folder in use (:func:`claim`).
LOCK_NAME = ".verrou"
#: Unclaimed temporary entries younger than this are left alone (being created).
STALE_SECONDS = 3600.0
#: Folders claimed by this process: their open, locked lock file.
_claims: dict[Path, IO[bytes]] = {}


def _lock(handle: IO[bytes]) -> bool:
    """Lock ``handle`` exclusively without waiting; False when another process holds it."""
    try:
        if sys.platform == "win32":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(handle: IO[bytes]) -> None:
    with contextlib.suppress(OSError):
        if sys.platform == "win32":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    with contextlib.suppress(OSError):
        handle.close()


def claim(folder: Path) -> None:
    """Create ``folder`` (a batch temporary folder) and mark it in use by this process
    until :func:`release` or the process ends: :func:`clean_tmp` of another instance
    never removes it."""
    folder.mkdir(parents=True, exist_ok=True)
    handle = (folder / LOCK_NAME).open("a+b")
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()
    if not _lock(handle):
        handle.close()
        return
    _claims[folder.resolve()] = handle


def release(folder: Path) -> None:
    handle = _claims.pop(folder.resolve(), None)
    if handle is not None:
        _unlock(handle)


def _in_use(folder: Path) -> bool:
    """True when a live process holds the lock of ``folder`` (or it has no lock yet
    and was just created)."""
    lock = folder / LOCK_NAME
    if not lock.exists():
        return _recent(folder)
    if folder.resolve() in _claims:
        return True
    try:
        handle = lock.open("r+b")
    except OSError:
        return True
    if not _lock(handle):
        handle.close()
        return True
    _unlock(handle)  # its owner is gone
    return False


def _recent(path: Path) -> bool:
    try:
        return time.time() - path.stat().st_mtime < STALE_SECONDS
    except OSError:
        return False


def _clear_read_only(function: object, path: str, _exc: BaseException) -> None:
    """``shutil.rmtree`` error handler: on Windows a read-only file or folder (LibreOffice
    marks its « Documents » folder so) cannot be removed until the flag is cleared."""
    with contextlib.suppress(OSError):
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
        if os.path.isdir(path) and not os.path.islink(path):
            os.rmdir(path)
        else:
            os.unlink(path)


def rmtree(path: Path) -> None:
    """``shutil.rmtree`` that also removes read-only entries and never raises."""
    with contextlib.suppress(OSError):
        shutil.rmtree(path, onexc=_clear_read_only)


#: Seconds a removal keeps retrying while files are still held: on Windows,
#: ``soffice.exe`` returns while its ``soffice.bin`` child still holds the profile.
REMOVE_RETRY_SECONDS = 10.0


def remove_tree(path: Path, retry_seconds: float = REMOVE_RETRY_SECONDS) -> bool:
    """Remove the folder ``path``, retrying while some of its files are still held by
    a process that is exiting. True when nothing is left."""
    deadline = time.monotonic() + retry_seconds
    while True:
        rmtree(path)
        if not path.exists():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.25)


def clean_tmp() -> None:
    """Remove the temporary files left behind (called at startup): batch folders no
    live process holds, and other entries untouched for :data:`STALE_SECONDS`. A
    batch running in another instance of Anonyma is never touched."""
    root = tmp_dir()
    if root.exists():
        for child in root.iterdir():
            if child.is_dir():
                if not _in_use(child):
                    rmtree(child)
            elif not _recent(child):
                with contextlib.suppress(OSError):
                    child.unlink()
    root.mkdir(parents=True, exist_ok=True)
