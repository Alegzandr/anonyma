"""Hardware detection and profile recommendation.

Phase 0 stub, owned by the infrastructure workstream. Contract: ``detect_hardware``
and ``recommend_profile`` keep these signatures.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from anonyma.models import Profile


@dataclass(frozen=True, slots=True)
class HardwareInfo:
    ram_bytes: int = 0
    cpu_count: int = 1
    #: onnxruntime execution providers available, best first.
    providers: tuple[str, ...] = ("CPUExecutionProvider",)
    gpu_name: str = ""
    vram_bytes: int = 0
    notes: tuple[str, ...] = field(default_factory=tuple)


def detect_hardware() -> HardwareInfo:
    return HardwareInfo(cpu_count=os.cpu_count() or 1)


def recommend_profile(info: HardwareInfo) -> Profile:
    return Profile.LIGHT


def recommend_llm(info: HardwareInfo) -> str | None:
    """Catalogue id of the LLM suited to this machine (FULL profile), or None."""
    return None
