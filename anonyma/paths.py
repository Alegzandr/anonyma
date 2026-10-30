"""Portable paths: everything Anonyma writes lives under the application folder."""

from __future__ import annotations

import contextlib
import os
import shutil
import sys
import tempfile
from pathlib import Path


def app_root() -> Path:
    """Folder holding the executable (frozen) or the project root (source).

    ``ANONYMA_HOME`` overrides it (tests, custom portable locations).
    """
    override = os.environ.get("ANONYMA_HOME")
    if override:
        return Path(override).resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
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


def clean_tmp() -> None:
    """Remove every leftover temporary file (called at startup and batch end)."""
    root = tmp_dir()
    if root.exists():
        for child in root.iterdir():
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                with contextlib.suppress(OSError):
                    child.unlink()
    root.mkdir(parents=True, exist_ok=True)
