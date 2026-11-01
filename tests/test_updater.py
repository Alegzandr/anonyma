from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest

from anonyma import __version__, netguard, updater


def _payload(version: str, names: list[str], **extra: Any) -> dict[str, Any]:
    return {
        "tag_name": f"v{version}",
        "html_url": f"https://github.com/{updater.REPOSITORY}/releases/tag/v{version}",
        "assets": [
            {
                "name": name,
                "browser_download_url": f"https://github.com/dl/{name}",
                "size": 10,
                "digest": "sha256:" + "ab" * 32,
            }
            for name in names
        ],
        **extra,
    }


def test_versions() -> None:
    assert updater.parse_version("v0.10.2") == (0, 10, 2)
    assert updater.parse_version("0.2.0-rc1") == ()
    assert updater.is_newer("0.2.0", "0.1.0")
    assert updater.is_newer("0.10.0", "0.9.9")
    assert updater.is_newer("1.0.1", "1.0")
    assert not updater.is_newer("0.1.0", "0.1.0")
    assert not updater.is_newer("0.1", "0.1.0")
    assert not updater.is_newer("nimporte", "0.1.0")


def test_parse_release_picks_the_platform_archive() -> None:
    tag = "windows-x86_64"
    names = [
        f"Anonyma-9.0.0-{tag}.zip",
        f"Anonyma-9.0.0-{tag}-sans-libreoffice.zip",
        "Anonyma-9.0.0-linux-x86_64.zip",
    ]
    release = updater.parse_release(_payload("9.0.0", names), current="0.1.0", tag=tag)
    assert release is not None
    assert release.version == "9.0.0"
    assert release.asset_name == f"Anonyma-9.0.0-{tag}.zip"
    assert release.sha256 == "ab" * 32
    light = updater.parse_release(
        _payload("9.0.0", names), current="0.1.0", tag=tag, with_libreoffice=False
    )
    assert light is not None and light.asset_name.endswith("-sans-libreoffice.zip")
    # Fallback on the other variant when only one exists.
    only_full = updater.parse_release(
        _payload("9.0.0", names[:1]), current="0.1.0", tag=tag, with_libreoffice=False
    )
    assert only_full is not None and only_full.asset_name == names[0]


@pytest.mark.parametrize(
    "payload",
    [
        _payload("0.1.0", [f"Anonyma-0.1.0-{updater.platform_tag()}.zip"]),  # not newer
        _payload("9.0.0", ["Anonyma-9.0.0-autre-os.zip"]),  # no archive for us
        _payload("9.0.0", [f"Anonyma-9.0.0-{updater.platform_tag()}.zip"], prerelease=True),
    ],
)
def test_parse_release_ignores(payload: dict[str, Any]) -> None:
    assert updater.parse_release(payload, current="0.1.0") is None


def test_parse_release_requires_a_digest() -> None:
    payload = _payload("9.0.0", [f"Anonyma-9.0.0-{updater.platform_tag()}.zip"])
    payload["assets"][0]["digest"] = None
    assert updater.parse_release(payload, current="0.1.0") is None


def test_no_self_update_from_sources() -> None:
    assert not updater.can_self_update()


def test_check_is_blocked_without_the_network_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """The check opens netguard's window itself; outside of it nothing goes out."""
    netguard.install()
    calls: list[str] = []

    class Client:
        def __enter__(self) -> Client:
            calls.append("open")
            assert netguard._allowed > 0
            return self

        def __exit__(self, *_a: object) -> None:
            pass

        def get(self, _url: str) -> Any:
            raise OSError("hors ligne")

    monkeypatch.setattr(updater, "_client", lambda _t: Client())
    with pytest.raises(updater.UpdateError, match="vérifier"):
        updater.check_latest()
    assert calls == ["open"]


def _make_release_zip(path: Path, version: str = "9.0.0") -> bytes:
    exe = updater.gui_executable(Path(updater.APP)).as_posix()
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(exe, f"nouvelle {version}")
        zf.writestr(f"{updater.APP}/_internal/lib.txt", "bibliothèque")
        zf.writestr(f"{updater.APP}/data/", "")
        zf.writestr(f"{updater.APP}/models/", "")
        zf.writestr(f"{updater.APP}/README.md", "lisez-moi")
    return path.read_bytes()


def test_download_checks_the_digest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    body = b"contenu de l'archive"

    class Response:
        headers = {"content-length": str(len(body))}

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_a: object) -> None:
            pass

        def raise_for_status(self) -> None:
            pass

        def iter_bytes(self, _size: int) -> list[bytes]:
            return [body[:5], body[5:]]

    class Client:
        def __enter__(self) -> Client:
            return self

        def __exit__(self, *_a: object) -> None:
            pass

        def stream(self, _method: str, _url: str) -> Response:
            return Response()

    monkeypatch.setattr(updater, "_client", lambda _t: Client())
    good = updater.Release(
        "9.0.0", "", "a.zip", "https://x/a.zip", len(body), hashlib.sha256(body).hexdigest()
    )
    seen: list[tuple[int, int]] = []
    target = updater.download(good, tmp_path / "dl", lambda d, t: seen.append((d, t)))
    assert target.read_bytes() == body
    assert seen[-1] == (len(body), len(body))

    bad = updater.Release("9.0.0", "", "b.zip", "https://x/b.zip", len(body), "00" * 32)
    with pytest.raises(updater.UpdateError, match="SHA-256"):
        updater.download(bad, tmp_path / "dl")
    assert not list((tmp_path / "dl").glob("b.zip*"))

    with pytest.raises(updater.UpdateCancelledError):
        updater.download(good, tmp_path / "dl2", cancel=lambda: True)


@pytest.mark.skipif(sys.platform == "darwin", reason="macOS unpacks with ditto")
def test_extract_and_entries(tmp_path: Path) -> None:
    archive = tmp_path / "maj.zip"
    _make_release_zip(archive)
    folder = updater.extract(archive, tmp_path / "extrait")
    assert updater.gui_executable(folder).read_text() == "nouvelle 9.0.0"
    entries = updater.replaced_entries(folder)
    assert "data" not in entries and "models" not in entries
    assert "_internal" in entries and "README.md" in entries


@pytest.mark.parametrize("name", ["../evil.txt", "/abs.txt", "Autre/x.txt", "Anonyma/../../x"])
def test_extract_rejects_unsafe_members(tmp_path: Path, name: str) -> None:
    if sys.platform == "darwin":
        pytest.skip("macOS unpacks with ditto")
    archive = tmp_path / "maj.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr(name, "x")
    with pytest.raises(updater.UpdateError):
        updater.extract(archive, tmp_path / "extrait")


def test_cleanup_restores_an_interrupted_install(tmp_path: Path) -> None:
    root = tmp_path / "Anonyma"
    (root / "data" / "maj").mkdir(parents=True)
    (root / f"_internal{updater.BACKUP_SUFFIX}").mkdir()  # nothing replaced it: restore
    (root / "README.md").write_text("nouveau")
    (root / f"README.md{updater.BACKUP_SUFFIX}").write_text("ancien")  # replaced: drop
    updater.cleanup(root)
    assert (root / "_internal").is_dir()
    assert (root / "README.md").read_text() == "nouveau"
    assert not list(root.glob(f"*{updater.BACKUP_SUFFIX}"))
    assert not (root / "data" / "maj").exists()


def _dead_pid() -> int:
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    return process.pid


def _fake_install(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "installation avec é"
    (root / "_internal").mkdir(parents=True)
    (root / "_internal" / "ancienne.txt").write_text("ancienne")
    (root / "README.md").write_text("ancien")
    (root / "data").mkdir()
    (root / "data" / "config.json").write_text("{}")
    (root / "models").mkdir()
    (root / "models" / "modele.onnx").write_text("poids")
    (root / "outil-perso.txt").write_text("à garder")
    archive = tmp_path / "maj.zip"
    _make_release_zip(archive)
    updates = root / "data" / "maj"
    staged = updater.extract(archive, updates / "nouvelle-version")
    return root, staged


def _check_installed(root: Path) -> None:
    assert updater.gui_executable(root).read_text() == "nouvelle 9.0.0"
    assert (root / "_internal" / "lib.txt").is_file()
    assert not (root / "_internal" / "ancienne.txt").exists()
    assert (root / "README.md").read_text() == "lisez-moi"
    assert (root / "data" / "config.json").read_text() == "{}"
    assert (root / "models" / "modele.onnx").read_text() == "poids"
    assert (root / "outil-perso.txt").read_text() == "à garder"
    assert not list(root.glob(f"*{updater.BACKUP_SUFFIX}"))
    assert not (root / "data" / "maj").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell installer")
def test_powershell_installer_replaces_the_program(tmp_path: Path) -> None:
    if shutil.which("powershell.exe") is None:
        pytest.skip("powershell.exe absent")
    root, staged = _fake_install(tmp_path)
    updates = root / "data" / "maj"
    entries = updater.replaced_entries(staged)
    script = tmp_path / "installer.ps1"
    text = updater.powershell_script(root, staged, entries, _dead_pid(), updates, relaunch=False)
    script.write_text(text, encoding="utf-8-sig")
    subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
        ],
        check=True,
        timeout=120,
    )
    _check_installed(root)


@pytest.mark.skipif(sys.platform == "win32", reason="sh installer")
def test_shell_installer_replaces_the_program(tmp_path: Path) -> None:
    root, staged = _fake_install(tmp_path)
    updates = root / "data" / "maj"
    entries = updater.replaced_entries(staged)
    script = tmp_path / "installer.sh"
    text = updater.shell_script(root, staged, entries, _dead_pid(), updates, relaunch=False)
    script.write_text(text, encoding="utf-8")
    subprocess.run(["/bin/sh", str(script)], check=True, timeout=120)
    _check_installed(root)


def test_current_version_is_parsable() -> None:
    assert updater.parse_version(__version__)
