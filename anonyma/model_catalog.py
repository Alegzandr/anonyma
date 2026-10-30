"""Catalogue of downloadable models.

Contract between the detection workstream (fills ``CATALOG``) and the
infrastructure workstream (``model_manager`` downloads/verifies what is listed).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class ModelKind(StrEnum):
    GLINER = "gliner"
    LLM = "llm"
    OCR = "ocr"


@dataclass(frozen=True, slots=True)
class ModelFile:
    #: Path inside the Hugging Face repository.
    filename: str
    #: Expected SHA-256 (hex). Empty only while the entry is being authored.
    sha256: str = ""
    size: int = 0


@dataclass(frozen=True, slots=True)
class ModelSpec:
    #: Stable local id, e.g. "gliner-multi-pii"; also the folder name in models/.
    id: str
    kind: ModelKind
    repo_id: str
    #: Pinned commit hash of the repository.
    revision: str
    files: tuple[ModelFile, ...]
    #: License declared on Hugging Face (SPDX-like, lower case), checked again at
    #: download time against the live repository metadata.
    license: str
    title: str  # French, shown in the GUI
    description: str = ""  # French
    #: Minimum RAM (bytes) recommended to run it.
    min_ram: int = 0
    extra: dict[str, str] = field(default_factory=dict)


CATALOG: list[ModelSpec] = []


def get_spec(model_id: str) -> ModelSpec | None:
    return next((m for m in CATALOG if m.id == model_id), None)
