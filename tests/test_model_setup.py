from __future__ import annotations

import json
from pathlib import Path

import pytest

from anonyma import hardware, model_catalog, model_setup
from anonyma.model_catalog import ModelKind, ModelSpec
from anonyma.model_manager import MANIFEST, ModelManager
from anonyma.models import Profile
from tests.test_model_manager import FakeApi, FakeTransport, _spec

GIB = 1024**3


def _catalog_entry(model_id: str, kind: ModelKind, min_ram: int = 0) -> ModelSpec:
    base = _spec(model_id)
    return ModelSpec(
        id=model_id,
        kind=kind,
        repo_id=base.repo_id,
        revision=base.revision,
        files=base.files,
        license=base.license,
        title=f"Modèle {model_id}",
        min_ram=min_ram,
    )


@pytest.fixture
def catalog(monkeypatch: pytest.MonkeyPatch) -> list[ModelSpec]:
    specs = [
        _catalog_entry("ocr-latin-ppocrv5", ModelKind.OCR),
        _catalog_entry("gliner-multi-pii", ModelKind.GLINER, 6 * GIB),
        _catalog_entry("llm-petit", ModelKind.LLM, 8 * GIB),
        _catalog_entry("llm-grand", ModelKind.LLM, 24 * GIB),
    ]
    monkeypatch.setattr(model_catalog, "CATALOG", specs)
    monkeypatch.setattr(model_setup, "engine_available", lambda _name: True)
    return specs


def _info(ram_gib: int, cpus: int = 16) -> hardware.HardwareInfo:
    return hardware.HardwareInfo(ram_bytes=ram_gib * GIB, cpu_count=cpus)


def _manager(tmp_path: Path) -> ModelManager:
    return ModelManager(tmp_path / "models", api=FakeApi(), transport=FakeTransport())


def _ids(specs: tuple[ModelSpec, ...] | list[ModelSpec]) -> set[str]:
    return {s.id for s in specs}


def test_wanted_follows_the_hardware(catalog: list[ModelSpec]) -> None:
    assert _ids(model_setup.wanted_models(_info(4))) == {"ocr-latin-ppocrv5"}
    assert _ids(model_setup.wanted_models(_info(8, cpus=4))) == {
        "ocr-latin-ppocrv5",
        "gliner-multi-pii",
    }
    assert hardware.recommend_profile(_info(32)) is Profile.FULL
    assert _ids(model_setup.wanted_models(_info(32))) == {
        "ocr-latin-ppocrv5",
        "gliner-multi-pii",
        "llm-grand",
    }
    assert _ids(model_setup.wanted_models(_info(16))) == {
        "ocr-latin-ppocrv5",
        "gliner-multi-pii",
        "llm-petit",
    }


def test_no_language_model_without_its_engine(
    catalog: list[ModelSpec], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(model_setup, "engine_available", lambda name: name != "llm")
    assert "llm-grand" not in _ids(model_setup.wanted_models(_info(32)))


def test_install_then_replace_and_remove_obsolete(catalog: list[ModelSpec], tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    first = model_setup.plan(manager, _info(16))
    assert _ids(first.install) == {"ocr-latin-ppocrv5", "gliner-multi-pii", "llm-petit"}
    result = model_setup.run(manager, first)
    assert result.ok and set(result.installed) == _ids(first.install)
    assert model_setup.plan(manager, _info(16)).ready

    # More memory: the larger language model comes in, the smaller one goes away.
    second = model_setup.plan(manager, _info(32))
    assert _ids(second.install) == {"llm-grand"}
    assert second.obsolete == ("llm-petit",)
    result = model_setup.run(manager, second)
    assert result.removed == ["llm-petit"]
    assert manager.path("llm-petit") is None
    assert model_setup.plan(manager, _info(32)).ready


def test_outdated_model_is_reinstalled(catalog: list[ModelSpec], tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    model_setup.run(manager, model_setup.plan(manager, _info(4)))
    folder = manager.path("ocr-latin-ppocrv5")
    assert folder is not None
    # A file that no longer matches the catalogue (new revision, altered copy).
    target = next(p for p in folder.rglob("*.onnx"))
    target.write_bytes(b"ancienne version")
    setup = model_setup.plan(manager, _info(4))
    assert _ids(setup.install) == {"ocr-latin-ppocrv5"}
    assert model_setup.run(manager, setup).ok
    assert model_setup.plan(manager, _info(4)).ready


def test_model_dropped_from_catalogue_is_removed(catalog: list[ModelSpec], tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    model_setup.run(manager, model_setup.plan(manager, _info(4)))
    dropped = manager.root / "ancien-modele"
    dropped.mkdir()
    (dropped / MANIFEST).write_text(json.dumps({"id": "ancien-modele"}), encoding="utf-8")
    foreign = manager.root / "dossier-utilisateur"
    foreign.mkdir()
    setup = model_setup.plan(manager, _info(4))
    assert setup.obsolete == ("ancien-modele",)
    model_setup.run(manager, setup)
    assert not dropped.exists()
    assert foreign.is_dir()  # not a model folder: never touched


def test_failed_download_keeps_the_old_models(catalog: list[ModelSpec], tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    model_setup.run(manager, model_setup.plan(manager, _info(16)))
    broken = ModelManager(manager.root, api=FakeApi(), transport=FakeTransport(fail_after=10))
    setup = model_setup.plan(broken, _info(32))
    result = model_setup.run(broken, setup)
    assert result.errors and not result.removed
    assert broken.path("llm-petit") is not None


def test_without_download_only_cleans_up(catalog: list[ModelSpec], tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    setup = model_setup.plan(manager, _info(4))
    result = model_setup.run(manager, setup, download=False)
    assert not result.installed
    assert manager.path("ocr-latin-ppocrv5") is None


def test_offline_import_from_another_models_folder(
    catalog: list[ModelSpec], tmp_path: Path
) -> None:
    source = ModelManager(tmp_path / "autre", api=FakeApi(), transport=FakeTransport())
    model_setup.run(source, model_setup.plan(source, _info(8, cpus=4)))
    manager = ModelManager(tmp_path / "models", api=FakeApi(), transport=FakeTransport())
    setup = model_setup.plan(manager, _info(8, cpus=4))
    result = model_setup.import_from(manager, setup, [source.root])
    assert result.ok and set(result.installed) == _ids(setup.install)
    assert model_setup.plan(manager, _info(8, cpus=4)).ready

    missing = model_setup.import_from(
        ModelManager(tmp_path / "vide"), model_setup.plan(manager, _info(32)), [tmp_path / "x"]
    )
    assert missing.errors
