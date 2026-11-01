from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import time
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


def _run_script(tmp_path: Path, root: Path, staged: Path, pid: int) -> int:
    """Run the platform's installer script in the foreground; its exit code."""
    updates = root / "data" / "maj"
    entries = updater.replaced_entries(staged)
    if sys.platform == "win32":
        if shutil.which("powershell.exe") is None:
            pytest.skip("powershell.exe absent")
        script = tmp_path / "installer.ps1"
        text = updater.powershell_script(
            root, staged, entries, pid, updates, version="9.0.0", relaunch=False
        )
        script.write_text(text, encoding="utf-8-sig")
        command = [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
        ]
    else:
        script = tmp_path / "installer.sh"
        text = updater.shell_script(
            root, staged, entries, pid, updates, version="9.0.0", relaunch=False
        )
        script.write_text(text, encoding="utf-8")
        command = ["/bin/sh", str(script)]
    return subprocess.run(command, timeout=120, check=False).returncode


def _check_log(root: Path) -> str:
    text = updater.install_log_path(root).read_text(encoding="utf-8")
    # Value-free: step and entry names, never the installation path.
    assert str(root) not in text and root.name not in text
    return text


def test_installer_script_replaces_the_program(tmp_path: Path) -> None:
    root, staged = _fake_install(tmp_path)
    assert _run_script(tmp_path, root, staged, _dead_pid()) == 0
    _check_installed(root)
    state = updater.read_state(root)
    assert state is not None and (state.status, state.detail) == ("ok", "9.0.0")
    log = _check_log(root)
    assert "replacing _internal" in log and "update installed" in log


def test_installer_gives_up_while_the_application_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(updater, "WAIT_STEPS", 2)
    root, staged = _fake_install(tmp_path)
    assert _run_script(tmp_path, root, staged, os.getpid()) == 1  # this process lives on
    assert (root / "_internal" / "ancienne.txt").read_text() == "ancienne"  # untouched
    state = updater.read_state(root)
    assert state is not None and (state.status, state.detail) == ("echec", "attente")
    assert "still running" in _check_log(root)


def test_detached_installer_really_runs(tmp_path: Path) -> None:
    """The real launch (creation flags, detached session, handshake): the installer
    must outlive the call and replace the program. Started with DETACHED_PROCESS,
    PowerShell 5.1 exited without running its script."""
    if sys.platform == "win32" and shutil.which("powershell.exe") is None:
        pytest.skip("powershell.exe absent")
    root, staged = _fake_install(tmp_path)
    updater.launch_installer(staged, root, _dead_pid(), version="9.0.0", relaunch=False)
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        state = updater.read_state(root)
        if state is not None and state.status != updater.STATE_STARTED:
            break
        time.sleep(0.2)
    assert state is not None and (state.status, state.detail) == ("ok", "9.0.0")
    time.sleep(0.5)  # the installer removes data/maj right after writing its state
    _check_installed(root)
    assert "restart skipped" in _check_log(root)


class _FakePopen:
    """Records the launch; writes the installer's first state like the real one."""

    calls: list[dict[str, Any]] = []
    refuse_breakaway = False
    starts = True

    def __init__(self, command: list[str], **kwargs: Any) -> None:
        flags = int(kwargs.get("creationflags", 0))
        if self.refuse_breakaway and flags & updater.CREATE_BREAKAWAY_FROM_JOB:
            raise PermissionError("accès refusé")
        type(self).calls.append({"command": command, **kwargs})
        self.killed = False
        if self.starts:
            root = Path(kwargs["cwd"])
            updater.state_path(root).write_text("demarre 9.0.0", encoding="utf-8")

    def poll(self) -> int | None:
        return None if self.starts else 1

    def kill(self) -> None:
        self.killed = True


@pytest.fixture
def fake_popen(monkeypatch: pytest.MonkeyPatch) -> type[_FakePopen]:
    monkeypatch.setattr(_FakePopen, "calls", [])
    monkeypatch.setattr(updater.subprocess, "Popen", _FakePopen)
    return _FakePopen


def test_windows_launch_breaks_away_from_the_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_popen: type[_FakePopen]
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    root, staged = _fake_install(tmp_path)
    updater.launch_installer(staged, root, 4242, version="9.0.0")
    (call,) = fake_popen.calls
    flags = call["creationflags"]
    assert flags & updater.CREATE_BREAKAWAY_FROM_JOB
    assert flags & updater.CREATE_NO_WINDOW
    assert not flags & updater.DETACHED_PROCESS  # PowerShell would exit at once
    assert call["command"][0] == "powershell.exe"
    script = Path(call["command"][-1])
    assert script == root / "data" / "maj" / "installer.ps1"
    assert call["cwd"] == str(root)
    text = script.read_text(encoding="utf-8-sig")
    assert f"$exe = '{root / 'Anonyma.exe'}'" in text  # quoted: the path has spaces
    assert "Start-Process -FilePath $exe -WorkingDirectory $root" in text
    assert "Get-Process -Id 4242" in text

    # A job that forbids breaking away refuses the flag: started inside it instead.
    monkeypatch.setattr(fake_popen, "refuse_breakaway", True)
    fake_popen.calls.clear()
    updater.launch_installer(staged, root, 4242, version="9.0.0")
    (call,) = fake_popen.calls
    assert not call["creationflags"] & updater.CREATE_BREAKAWAY_FROM_JOB


@pytest.mark.parametrize("system", ["linux", "darwin"])
def test_posix_launch_starts_a_new_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_popen: type[_FakePopen], system: str
) -> None:
    root, staged = _fake_install(tmp_path)
    monkeypatch.setattr(sys, "platform", system)
    updater.launch_installer(staged, root, 4242, version="9.0.0")
    (call,) = fake_popen.calls
    assert call["start_new_session"] is True
    assert call["command"][0] == "/bin/sh"
    text = Path(call["command"][1]).read_text(encoding="utf-8")
    assert "kill -0 4242" in text
    if system == "darwin":
        assert 'open "$root/Anonyma.app"' in text
    else:
        assert 'nohup "$root/Anonyma"' in text


def test_launch_fails_visibly_when_the_installer_does_not_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_popen: type[_FakePopen]
) -> None:
    root, staged = _fake_install(tmp_path)
    monkeypatch.setattr(fake_popen, "starts", False)
    with pytest.raises(updater.UpdateError, match="arrêté avant de commencer"):
        updater.launch_installer(staged, root, 4242, version="9.0.0", timeout=5)


def test_install_state_is_read_once(tmp_path: Path) -> None:
    root = tmp_path / "Anonyma"
    (root / "data").mkdir(parents=True)
    assert updater.take_state(root) is None
    updater.state_path(root).write_text("ok 9.0.0", encoding="utf-8")
    state = updater.take_state(root)
    assert state is not None and state.status == "ok" and state.detail == "9.0.0"
    assert not state.running
    assert updater.take_state(root) is None  # consumed
    # A fresh "started" state: the installer may still run, keep it.
    updater.state_path(root).write_text("demarre 9.0.0", encoding="utf-8")
    state = updater.take_state(root)
    assert state is not None and state.running
    assert updater.state_path(root).exists()
    updater.state_path(root).write_text("n'importe quoi", encoding="utf-8")
    assert updater.read_state(root) is None


def test_current_version_is_parsable() -> None:
    assert updater.parse_version(__version__)
