"""Logging setup and helpers that keep sensitive values out of logs."""

from __future__ import annotations

import hashlib
import hmac
import logging
import logging.handlers
import secrets

from anonyma import paths

# Per-process random key: fingerprints correlate within a run but cannot be
# reversed or matched across runs.
_KEY = secrets.token_bytes(32)


def fingerprint(value: str) -> str:
    """Short irreversible tag for a sensitive value, safe to log."""
    digest = hmac.new(_KEY, value.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"#{digest[:10]}"


def setup_logging(level: int = logging.INFO) -> None:
    paths.ensure_dirs()
    root = logging.getLogger("anonyma")
    if root.handlers:
        return
    root.setLevel(level)
    handler = logging.handlers.RotatingFileHandler(
        paths.logs_dir() / "anonyma.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"anonyma.{name}" if not name.startswith("anonyma") else name)
