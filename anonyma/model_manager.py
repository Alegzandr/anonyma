"""Model download (resumable, hash-checked, license-checked) and offline import.

Phase 0: API contract with a minimal local implementation, owned by the
infrastructure workstream. The GUI and detectors rely on these signatures.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from anonyma import paths
from anonyma.model_catalog import CATALOG, ModelSpec, get_spec

#: (bytes done, bytes total) -> None
DownloadProgress = Callable[[int, int], None]


class ModelError(Exception):
    """Base error; ``str(exc)`` is a French user-facing message."""


class LicenseRefusedError(ModelError):
    pass


class IntegrityError(ModelError):
    pass


@dataclass(frozen=True, slots=True)
class LicenseCheck:
    allowed: bool
    license: str
    reason: str  # French


@dataclass(frozen=True, slots=True)
class ModelStatus:
    spec: ModelSpec
    installed: bool
    path: Path | None
    #: All files present with the expected SHA-256.
    verified: bool
    size_on_disk: int = 0


class ModelManager:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or paths.models_dir()

    def catalog(self) -> list[ModelSpec]:
        return list(CATALOG)

    def path(self, model_id: str) -> Path | None:
        """Local folder of an installed model (files at their repo-relative paths)."""
        candidate = self.root / model_id
        return candidate if candidate.is_dir() else None

    def is_installed(self, model_id: str) -> bool:
        return self.path(model_id) is not None

    def status(self, model_id: str) -> ModelStatus:
        spec = get_spec(model_id)
        if spec is None:
            raise ModelError(f"Modèle inconnu : {model_id}")
        path = self.path(model_id)
        return ModelStatus(spec, path is not None, path, verified=False)

    def statuses(self) -> list[ModelStatus]:
        return [self.status(m.id) for m in CATALOG]

    def check_license(self, model_id: str) -> LicenseCheck:
        """Read the live license on Hugging Face (network, explicit user action)."""
        raise NotImplementedError

    def download(
        self,
        model_id: str,
        progress: DownloadProgress | None = None,
        cancel: Callable[[], bool] | None = None,
    ) -> Path:
        """Check license, download with resume, verify SHA-256. Raises ModelError."""
        raise NotImplementedError

    def import_files(self, model_id: str, files: Sequence[Path]) -> Path:
        """Offline install from user-provided files (or a folder); verifies SHA-256."""
        raise NotImplementedError

    def remove(self, model_id: str) -> None:
        raise NotImplementedError
