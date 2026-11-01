"""Logging setup and helpers that keep sensitive values out of logs.

Every process (CLI, GUI, worker processes of a batch) logs to ``data/logs`` only:
nothing reaches the user's terminal, whatever the library that logs. Records are
value-free (types, codes, fingerprints), in English.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import logging.handlers
import secrets
from pathlib import Path
from typing import override

from anonyma import paths

# Per-process random key: fingerprints correlate within a run but cannot be
# reversed or matched across runs.
_KEY = secrets.token_bytes(32)
LOG_NAME = "anonyma.log"
_FORMAT = "%(asctime)s %(levelname)s %(process)d %(name)s: %(message)s"


def fingerprint(value: str) -> str:
    """Short irreversible tag for a sensitive value, safe to log."""
    digest = hmac.new(_KEY, value.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"#{digest[:10]}"


class _QuietRotatingHandler(logging.handlers.RotatingFileHandler):
    """Main process: rotating log file. A failed write or rotation (the file held by
    a worker process on Windows) loses the record instead of printing a traceback
    on the user's terminal."""

    @override
    def handleError(self, record: logging.LogRecord) -> None:
        pass


class _AppendHandler(logging.Handler):
    """Worker processes: append each record to the shared log file and close it at
    once, so that the main process can still rotate it. Never rotates, never raises."""

    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = path

    @override
    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record) + "\n"
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(line)
        except Exception:
            pass


def setup_logging(level: int = logging.INFO, *, worker: bool = False) -> None:
    """Send this process's records to ``data/logs`` (idempotent).

    ``worker``: a worker process of a batch, which shares the log file of the main
    process. Records of other libraries never reach ``stderr`` (Python's last-resort
    handler prints them otherwise): the root logger gets a null handler."""
    paths.ensure_dirs()
    root = logging.getLogger("anonyma")
    if not any(isinstance(h, logging.NullHandler) for h in logging.root.handlers):
        logging.root.addHandler(logging.NullHandler())
    if root.handlers and not worker:
        return
    if worker:
        # A worker only ever appends: never keep a rotating handler it may have inherited.
        for existing in list(root.handlers):
            root.removeHandler(existing)
    root.setLevel(level)
    path = paths.logs_dir() / LOG_NAME
    handler: logging.Handler
    if worker:
        handler = _AppendHandler(path)
    else:
        handler = _QuietRotatingHandler(path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter(_FORMAT))
    root.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"anonyma.{name}" if not name.startswith("anonyma") else name)
