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
    sys.modules[name] = module  # dataclasses resolve annotations through sys.modules
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
    for module in ("rapidocr_onnxruntime", "pymupdf", "extract_msg", "pillow_heif", "py7zr"):
        assert f'"{module}"' in text


# -- bundled LibreOffice --------------------------------------------------------------


def _fake_soffice(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return path


def test_bundled_libreoffice_wins_over_path(
    portable_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from anonyma import libreoffice
    from anonyma.config import Config

    system_copy = _fake_soffice(tmp_path / "systeme" / "soffice")
    monkeypatch.setattr(libreoffice.shutil, "which", lambda name: str(system_copy))
    assert libreoffice.find_soffice(Config()) == system_copy
    if sys.platform == "darwin":
        rel = Path("libreoffice/LibreOffice.app/Contents/MacOS/soffice")
    elif sys.platform == "win32":
        rel = Path("libreoffice/program/soffice.exe")
    else:
        rel = Path("libreoffice/program/soffice")
    bundled = _fake_soffice(portable_home / rel)
    assert libreoffice.find_soffice(Config()) == bundled
    # An explicit setting still wins; disabling LibreOffice disables everything.
    assert libreoffice.find_soffice(Config(libreoffice_path=str(system_copy))) == system_copy
    assert libreoffice.find_soffice(Config(use_libreoffice=False)) is None


def test_libreoffice_profile_is_short_and_inside_batch(portable_home: Path) -> None:
    from anonyma import libreoffice

    batch = paths.tmp_dir() / "20260101-000000-abcdef"
    deep = batch / "w-12345678" / ("verify-" + "a" * 32) / ("b" * 32 + "-lo")
    profile = libreoffice._profile_dir(deep)
    assert profile.parent == batch.resolve()
    assert profile.name.startswith("lo-")
    outside = portable_home.parent / "ailleurs" / "w"
    assert libreoffice._profile_dir(outside) == outside.parent / "libreoffice-profile"


def test_libreoffice_bundle_pins() -> None:
    lo = _load("libreoffice_bundle")
    for (system, _machine), installer in lo.INSTALLERS.items():
        assert len(installer.sha256) == 64 and installer.size > 100_000_000
        assert installer.url.startswith("https://downloadarchive.documentfoundation.org/")
        assert lo.VERSION in installer.filename
        assert lo.soffice_relative(system).name.startswith("soffice")
    assert lo.installer_for("win32", "x86_64").filename.endswith(".msi")
    with pytest.raises(SystemExit):
        lo.installer_for("plan9", "mips")


def test_ar_parser() -> None:
    lo = _load("libreoffice_bundle")

    def member(name: str, data: bytes) -> bytes:
        header = f"{name + '/':<16}{0:<12}{0:<6}{0:<6}{'100644':<8}{len(data):<10}`\n"
        return header.encode() + data + (b"\n" if len(data) % 2 else b"")

    archive = b"!<arch>\n" + member("debian-binary", b"2.0\n") + member("data.tar.xz", b"abc")
    assert lo._ar_members(archive) == {"debian-binary": b"2.0\n", "data.tar.xz": b"abc"}
    with pytest.raises(SystemExit):
        lo._ar_members(b"not an ar archive")


def test_fetch_verifies_hash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import hashlib
    import io

    lo = _load("libreoffice_bundle")
    payload = b"x" * 1000
    good = lo.Installer("win/x86_64/fake.msi", hashlib.sha256(payload).hexdigest(), len(payload))

    class Response(io.BytesIO):
        status = 200

    monkeypatch.setattr(lo.urllib.request, "urlopen", lambda *a, **k: Response(payload))
    assert lo.fetch(good, tmp_path).read_bytes() == payload
    assert lo.fetch(good, tmp_path).name == "fake.msi"  # cached, verified again
    bad = lo.Installer("win/x86_64/bad.msi", "0" * 64, len(payload))
    with pytest.raises(SystemExit, match="SHA-256"):
        lo.fetch(bad, tmp_path)
    assert not (tmp_path / "bad.msi").exists()
