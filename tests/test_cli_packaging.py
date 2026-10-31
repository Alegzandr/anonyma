"""Packaging helpers: frozen app root, license generator, spec sanity."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

from anonyma import paths

ROOT = Path(__file__).resolve().parent.parent


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "packaging" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def frozen(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.delenv("ANONYMA_HOME", raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    return monkeypatch


def test_app_root_frozen_folder(frozen: pytest.MonkeyPatch, tmp_path: Path) -> None:
    exe = tmp_path / "Anonyma" / "anonyma-cli.exe"
    frozen.setattr(sys, "executable", str(exe))
    frozen.setattr(sys, "platform", "win32")
    assert paths.app_root() == (tmp_path / "Anonyma").resolve()


def test_app_root_frozen_macos_bundle(frozen: pytest.MonkeyPatch, tmp_path: Path) -> None:
    exe = tmp_path / "Anonyma" / "Anonyma.app" / "Contents" / "MacOS" / "anonyma-cli"
    frozen.setattr(sys, "executable", str(exe))
    frozen.setattr(sys, "platform", "darwin")
    assert paths.app_root() == (tmp_path / "Anonyma").resolve()
    assert paths.models_dir() == (tmp_path / "Anonyma" / "models").resolve()


def test_app_root_override_wins(frozen: pytest.MonkeyPatch, tmp_path: Path) -> None:
    frozen.setenv("ANONYMA_HOME", str(tmp_path / "ailleurs"))
    assert paths.app_root() == (tmp_path / "ailleurs").resolve()


def test_gen_licenses_runtime_closure() -> None:
    gen = _load("gen_licenses")
    names = {gen.canonicalize_name(d.metadata["Name"]) for d in gen.runtime_distributions()}
    assert {"typer", "cryptography", "faker", "huggingface-hub", "rich"} <= names
    assert "pytest" not in names and "ruff" not in names and "pyinstaller" not in names
    text = gen.render()
    assert "USAGE INTERNE UNIQUEMENT" in text
    assert "PyMuPDF (AGPL-3.0)" in text and "extract-msg (GPL-3.0)" in text
    assert "## Modèles d'IA du catalogue" in text


def test_gen_licenses_lists_catalog_models(monkeypatch: pytest.MonkeyPatch) -> None:
    from anonyma import model_catalog
    from anonyma.model_catalog import ModelKind, ModelSpec

    spec = ModelSpec("llm-x", ModelKind.LLM, "exemple/llm-x", "a" * 40, (), "apache-2.0", "LLM X")
    monkeypatch.setattr(model_catalog, "CATALOG", [spec])
    assert "exemple/llm-x" in "\n".join(_load("gen_licenses").model_rows())


def test_spec_declares_both_executables() -> None:
    text = (ROOT / "packaging" / "anonyma.spec").read_text(encoding="utf-8")
    for needle in (
        'APP_NAME = "Anonyma"',
        'CLI_NAME = "anonyma-cli"',
        'collect_submodules("anonyma")',
        '"anonyma.extractors", "anonyma.writers", "anonyma.detectors"',
        "console=False",
        "console=True",
        "BUNDLE(",
    ):
        assert needle in text
    assert "anonyma.app" in (ROOT / "packaging" / "launch_gui.py").read_text(encoding="utf-8")
    assert "anonyma.cli" in (ROOT / "packaging" / "launch_cli.py").read_text(encoding="utf-8")
