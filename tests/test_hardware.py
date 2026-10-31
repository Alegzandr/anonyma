from __future__ import annotations

import pytest

from anonyma import hardware, model_catalog
from anonyma.config import Config
from anonyma.hardware import GIB, HardwareInfo
from anonyma.model_catalog import ModelFile, ModelKind, ModelSpec
from anonyma.models import Profile


def _llm(model_id: str, ram_gib: int) -> ModelSpec:
    return ModelSpec(
        id=model_id,
        kind=ModelKind.LLM,
        repo_id=f"exemple/{model_id}",
        revision="0" * 40,
        files=(ModelFile("model.gguf", "ab" * 32, 10),),
        license="apache-2.0",
        title=model_id,
        min_ram=ram_gib * GIB,
    )


@pytest.fixture
def fake_catalog(monkeypatch: pytest.MonkeyPatch) -> list[ModelSpec]:
    specs = [
        _llm("llm-moyen", 12),
        _llm("llm-petit", 6),
        _llm("llm-grand", 24),
        ModelSpec(
            "gliner-x", ModelKind.GLINER, "exemple/g", "0" * 40, (), "apache-2.0", "g", min_ram=1
        ),
    ]
    monkeypatch.setattr(model_catalog, "CATALOG", specs)
    return specs


def test_detect_hardware_is_sane() -> None:
    info = hardware.detect_hardware(refresh=True)
    assert info.cpu_count >= 1
    assert info.ram_bytes > 0
    assert info.providers[-1] == "CPUExecutionProvider"
    assert hardware.detect_hardware() is info  # cached


def test_sort_providers() -> None:
    order = hardware.sort_providers(
        ["CPUExecutionProvider", "AzureExecutionProvider", "DmlExecutionProvider"]
    )
    assert order == ("DmlExecutionProvider", "AzureExecutionProvider", "CPUExecutionProvider")
    assert hardware.sort_providers([]) == ("CPUExecutionProvider",)


@pytest.mark.parametrize(
    ("ram_gib", "expected"),
    [(4, None), (8, "llm-petit"), (16, "llm-moyen"), (64, "llm-grand")],
)
def test_recommend_llm_by_ram(fake_catalog: list[ModelSpec], ram_gib: int, expected: str) -> None:
    assert hardware.recommend_llm(HardwareInfo(ram_bytes=ram_gib * GIB, cpu_count=8)) == expected


def test_recommend_llm_empty_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(model_catalog, "CATALOG", [])
    assert hardware.recommend_llm(HardwareInfo(ram_bytes=64 * GIB)) is None


def test_recommend_profile(fake_catalog: list[ModelSpec]) -> None:
    assert hardware.recommend_profile(HardwareInfo()) is Profile.LIGHT
    assert hardware.recommend_profile(HardwareInfo(ram_bytes=4 * GIB, cpu_count=8)) is Profile.LIGHT
    assert (
        hardware.recommend_profile(HardwareInfo(ram_bytes=8 * GIB, cpu_count=8)) is Profile.STANDARD
    )
    assert (
        hardware.recommend_profile(HardwareInfo(ram_bytes=32 * GIB, cpu_count=4))
        is Profile.STANDARD
    )
    big = HardwareInfo(ram_bytes=32 * GIB, cpu_count=16)
    assert hardware.recommend_profile(big) is Profile.FULL


def test_full_requires_an_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(model_catalog, "CATALOG", [])
    info = HardwareInfo(ram_bytes=64 * GIB, cpu_count=32)
    assert hardware.recommend_profile(info) is Profile.STANDARD


def test_forced_profile_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hardware, "detect_hardware", lambda **_: HardwareInfo(ram_bytes=GIB))
    assert Config().effective_profile() is Profile.LIGHT
    assert Config(profile=Profile.FULL).effective_profile() is Profile.FULL


def test_summary_is_french(fake_catalog: list[ModelSpec]) -> None:
    lines = dict(hardware.summary(HardwareInfo(ram_bytes=16 * GIB, cpu_count=8)))
    assert lines["Mémoire vive"] == "16,0 Go"
    assert lines["Modèle de langage recommandé"] == "llm-moyen"
    assert hardware.format_bytes(0) == "inconnue"
    assert hardware.format_bytes(512) == "512 o"
