"""Model catalogue, download (resumable, hash-checked, license-checked), import.

Phase 0 stub, owned by the infrastructure workstream. Contract used by detectors:
``ModelManager().path(model_id)`` returns the local folder/file of an installed
model or None.
"""

from __future__ import annotations

from pathlib import Path

from anonyma import paths


class ModelManager:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or paths.models_dir()

    def path(self, model_id: str) -> Path | None:
        candidate = self.root / model_id.replace("/", "--")
        return candidate if candidate.exists() else None

    def is_installed(self, model_id: str) -> bool:
        return self.path(model_id) is not None
