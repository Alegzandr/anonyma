"""Application updates from the GitHub releases of the project.

The only automatic network access of Anonyma: at GUI startup (portable build only,
``Config.check_updates``), one request to the GitHub API asks for the latest
release. Nothing about the documents, the settings or the machine is sent. When the
user accepts, the archive of the current platform is downloaded, checked against
the SHA-256 digest published by GitHub, unpacked into ``data/maj/`` and a small
installer script (PowerShell on Windows, ``sh`` elsewhere) replaces the program
once the application has exited, then starts it again. The installer logs its steps
to ``data/logs/mise-a-jour.log`` and leaves its outcome in ``data/maj-etat.txt``,
which the next start reports to the user. ``data/`` (settings,
encrypted mapping table, logs) and ``models/`` are never touched; the previous
version is restored when a file cannot be replaced.

Every network call runs inside ``netguard.allow_network()``, through the proxy of the
settings or of the system (``anonyma/network.py``).
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import time
import zipfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from anonyma import __version__, netguard, network, paths
from anonyma.errors import network_reason
from anonyma.logs import get_logger

log = get_logger("updater")

REPOSITORY = "Alegzandr/anonyma"
LATEST_RELEASE_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
APP = "Anonyma"
LIBREOFFICE_DIR = "libreoffice"
#: Folders of the portable install that belong to the user, never replaced.
USER_ENTRIES = frozenset({"data", "models"})
#: Suffix of the previous version's entries while the installer runs.
BACKUP_SUFFIX = ".old-maj"
CHECK_TIMEOUT = 8.0
DOWNLOAD_TIMEOUT = 60.0
#: Seconds the application waits for the installer to report that it runs.
STARTUP_TIMEOUT = 30.0
#: Half-second steps the installer waits for the application to exit (5 min).
WAIT_STEPS = 600
#: A "started" state younger than this means the installer may still be at work.
RUNNING_GRACE = 900.0
#: Outcome file of the last installer run (in ``data/``) and its step log (in
#: ``data/logs/``): step and entry names only, never a path or a document name.
STATE_NAME = "maj-etat.txt"
INSTALL_LOG = "mise-a-jour.log"
STATE_STARTED = "demarre"
STATE_OK = "ok"
STATE_FAILED = "echec"
#: Failed steps: the application did not exit, a file could not be replaced (the
#: previous version is back), or the previous version could not be fully restored.
STEP_WAIT = "attente"
STEP_REPLACE = "remplacement"
STEP_RESTORE = "restauration"
#: Windows process creation flags (``subprocess`` defines them on Windows only).
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
DETACHED_PROCESS = 0x00000008
_CHUNK = 1024 * 1024
_ENTRY_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*$")

#: ``progress(done_bytes, total_bytes)``.
Progress = Callable[[int, int], None]


class UpdateError(Exception):
    """Update failure; the message is French and user-facing."""


class UpdateCancelledError(UpdateError):
    pass


@dataclass(frozen=True, slots=True)
class Release:
    version: str
    page_url: str
    asset_name: str
    asset_url: str
    asset_size: int
    sha256: str


# ---------------------------------------------------------------------------
# Versions and platform
# ---------------------------------------------------------------------------


def parse_version(text: str) -> tuple[int, ...]:
    """``"v0.10.2"`` -> ``(0, 10, 2)``; ``()`` when not a plain dotted version."""
    match = re.fullmatch(r"v?(\d+(?:\.\d+)*)", text.strip())
    return tuple(int(p) for p in match.group(1).split(".")) if match else ()


def is_newer(candidate: str, current: str = __version__) -> bool:
    new, old = parse_version(candidate), parse_version(current)
    if not new or not old:
        return False
    width = max(len(new), len(old))
    return new + (0,) * (width - len(new)) > old + (0,) * (width - len(old))


def platform_tag() -> str:
    """Same tag as the release archives (``packaging/build.py``)."""
    system = {"win32": "windows", "darwin": "macos"}.get(sys.platform, sys.platform)
    machine = platform.machine().lower().replace("amd64", "x86_64").replace("aarch64", "arm64")
    return f"{system}-{machine}"


def can_self_update() -> bool:
    """Only the portable build replaces itself (sources are updated with git)."""
    return bool(getattr(sys, "frozen", False)) and not os.environ.get("ANONYMA_HOME")


def install_root() -> Path:
    return paths.app_root()


def update_dir(root: Path | None = None) -> Path:
    """Staging folder of a downloaded update (outside ``data/tmp``, which the
    application empties at startup and after each batch)."""
    return (root or install_root()) / "data" / "maj"


# ---------------------------------------------------------------------------
# Release lookup
# ---------------------------------------------------------------------------


def parse_release(
    payload: Mapping[str, Any],
    *,
    current: str = __version__,
    tag: str | None = None,
    with_libreoffice: bool = True,
) -> Release | None:
    """The update described by a GitHub ``releases/latest`` payload, or None when it
    is not newer or has no archive for this platform."""
    if payload.get("draft") or payload.get("prerelease"):
        return None
    version = str(payload.get("tag_name") or "").strip().removeprefix("v")
    if not is_newer(version, current):
        return None
    tag = tag or platform_tag()
    full = f"{APP}-{version}-{tag}.zip"
    light = f"{APP}-{version}-{tag}-sans-libreoffice.zip"
    preferred = (full, light) if with_libreoffice else (light, full)
    assets = {str(a.get("name")): a for a in payload.get("assets") or () if isinstance(a, Mapping)}
    for name in preferred:
        asset = assets.get(name)
        if asset is None:
            continue
        digest = str(asset.get("digest") or "")
        url = str(asset.get("browser_download_url") or "")
        if not digest.startswith("sha256:") or not url.startswith("https://"):
            continue
        return Release(
            version=version,
            page_url=str(payload.get("html_url") or ""),
            asset_name=name,
            asset_url=url,
            asset_size=int(asset.get("size") or 0),
            sha256=digest.removeprefix("sha256:").lower(),
        )
    return None


def _client(url: str, timeout: float, proxy: str) -> Any:
    return network.client(
        url,
        proxy,
        timeout=timeout,
        headers={"User-Agent": f"Anonyma/{__version__}", "Accept": "application/vnd.github+json"},
    )


def check_latest(timeout: float = CHECK_TIMEOUT, *, proxy: str = "") -> Release | None:
    """Ask GitHub for the latest release. None when up to date; raises UpdateError.

    ``proxy``: ``Config.proxy`` (empty = the system's)."""
    root = install_root()
    try:
        with netguard.allow_network(), _client(LATEST_RELEASE_URL, timeout, proxy) as client:
            response = client.get(LATEST_RELEASE_URL)
            if response.status_code == 404:  # no published release yet
                return None
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:
        log.info("update check failed: %s", type(exc).__name__)
        raise UpdateError(
            f"Impossible de vérifier les mises à jour ({network_reason(exc)})."
        ) from exc
    if not isinstance(payload, Mapping):
        raise UpdateError("Réponse inattendue du service de mises à jour.")
    return parse_release(payload, with_libreoffice=(root / LIBREOFFICE_DIR).is_dir())


# ---------------------------------------------------------------------------
# Download, verification, unpacking
# ---------------------------------------------------------------------------


def download(
    release: Release,
    folder: Path,
    progress: Progress | None = None,
    cancel: Callable[[], bool] | None = None,
    *,
    proxy: str = "",
) -> Path:
    """Download the archive into ``folder`` and check its SHA-256 digest."""
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / release.asset_name
    part = target.with_name(target.name + ".part")
    digest = hashlib.sha256()
    done = 0
    try:
        with (
            netguard.allow_network(),
            _client(release.asset_url, DOWNLOAD_TIMEOUT, proxy) as client,
            client.stream("GET", release.asset_url) as response,
        ):
            response.raise_for_status()
            total = release.asset_size or int(response.headers.get("content-length") or 0)
            with part.open("wb") as fh:
                for chunk in response.iter_bytes(_CHUNK):
                    if cancel is not None and cancel():
                        raise UpdateCancelledError("Mise à jour annulée.")
                    fh.write(chunk)
                    digest.update(chunk)
                    done += len(chunk)
                    if progress is not None:
                        progress(done, total)
    except UpdateError:
        part.unlink(missing_ok=True)
        raise
    except Exception as exc:
        part.unlink(missing_ok=True)
        log.info("update download failed: %s", type(exc).__name__)
        raise UpdateError(
            f"Le téléchargement de la mise à jour a échoué ({network_reason(exc)})."
        ) from exc
    if digest.hexdigest() != release.sha256:
        part.unlink(missing_ok=True)
        raise UpdateError("L'archive téléchargée est corrompue (empreinte SHA-256 différente).")
    os.replace(part, target)
    return target


def _safe_member(name: str) -> PurePosixPath:
    path = PurePosixPath(name.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts or not path.parts or ":" in path.parts[0]:
        raise UpdateError("L'archive de mise à jour contient un chemin invalide.")
    if path.parts[0] != APP:
        raise UpdateError("L'archive de mise à jour n'a pas la structure attendue.")
    return path


def extract(archive: Path, destination: Path) -> Path:
    """Unpack the release archive; returns the ``Anonyma`` folder it contains."""
    shutil.rmtree(destination, ignore_errors=True)
    destination.mkdir(parents=True)
    try:
        if sys.platform == "darwin":
            # Same tool as the build: keeps the bundle's symlinks and attributes.
            subprocess.run(
                ["ditto", "-x", "-k", str(archive), str(destination)],
                check=True,
                capture_output=True,
            )
            for child in destination.iterdir():
                _safe_member(child.name)
        else:
            _extract_zip(archive, destination)
    except UpdateError:
        raise
    except (OSError, zipfile.BadZipFile, subprocess.CalledProcessError) as exc:
        log.info("update extraction failed: %s", type(exc).__name__)
        raise UpdateError("L'archive de mise à jour est illisible.") from exc
    folder = destination / APP
    if not gui_executable(folder).is_file():
        raise UpdateError("L'archive de mise à jour ne contient pas l'application.")
    return folder


def _extract_zip(archive: Path, destination: Path) -> None:
    root = destination.resolve()
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            member = _safe_member(info.filename)
            target = destination.joinpath(*member.parts)
            mode = info.external_attr >> 16
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if stat.S_ISLNK(mode) and sys.platform != "win32":
                link = zf.read(info).decode("utf-8")
                resolved = (target.parent / link).resolve()
                if Path(link).is_absolute() or not resolved.is_relative_to(root):
                    raise UpdateError("L'archive de mise à jour contient un lien invalide.")
                target.symlink_to(link)
                continue
            with zf.open(info) as source, target.open("wb") as fh:
                shutil.copyfileobj(source, fh, _CHUNK)
            if mode & 0o777 and sys.platform != "win32":
                target.chmod(mode & 0o777)


def gui_executable(folder: Path) -> Path:
    if sys.platform == "win32":
        return folder / f"{APP}.exe"
    if sys.platform == "darwin":
        return folder / f"{APP}.app" / "Contents" / "MacOS" / APP
    return folder / APP


def prepare(
    release: Release,
    progress: Progress | None = None,
    cancel: Callable[[], bool] | None = None,
    *,
    proxy: str = "",
) -> Path:
    """Download, verify and unpack ``release``; returns the staged ``Anonyma`` folder."""
    staging = update_dir()
    shutil.rmtree(staging, ignore_errors=True)
    archive = download(release, staging, progress, cancel, proxy=proxy)
    folder = extract(archive, staging / "nouvelle-version")
    archive.unlink(missing_ok=True)
    return folder


# ---------------------------------------------------------------------------
# Installer script (runs once the application has exited)
# ---------------------------------------------------------------------------


def replaced_entries(staged: Path) -> list[str]:
    """Top-level entries of the new version that replace the installed ones."""
    names = sorted(p.name for p in staged.iterdir() if p.name not in USER_ENTRIES)
    for name in names:
        if not _ENTRY_RE.fullmatch(name):
            raise UpdateError("L'archive de mise à jour contient un nom de fichier inattendu.")
    if not names:
        raise UpdateError("L'archive de mise à jour est vide.")
    return names


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _sh_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def powershell_script(
    root: Path,
    staged: Path,
    entries: Iterable[str],
    pid: int,
    updates: Path,
    *,
    version: str = "",
    relaunch: bool = True,
) -> str:
    """Installer for Windows PowerShell 5.1. Every step is appended to
    :func:`install_log_path` (step and entry names only, never a path) and the
    outcome to :func:`state_path`, read by the next start of the application."""
    names = ", ".join(_ps_quote(e) for e in entries)
    exe = root / f"{APP}.exe"
    relaunch_block = (
        """try {
    Start-Process -FilePath $exe -WorkingDirectory $root
    Write-Log 'application restarted'
} catch { Write-Log ('restart failed: ' + $_.Exception.GetType().Name) }"""
        if relaunch
        else "Write-Log 'restart skipped'"
    )
    return f"""# Anonyma update: replaces the program once the application has exited.
$ErrorActionPreference = 'Stop'
$root = {_ps_quote(str(root))}
$staged = {_ps_quote(str(staged))}
$updates = {_ps_quote(str(updates))}
$exe = {_ps_quote(str(exe))}
$logfile = {_ps_quote(str(install_log_path(root)))}
$state = {_ps_quote(str(state_path(root)))}
$version = {_ps_quote(version)}
$entries = @({names})
$suffix = '{BACKUP_SUFFIX}'
function Write-Log([string]$message) {{
    try {{
        $line = (Get-Date -Format 's') + ' ' + $message
        Add-Content -LiteralPath $logfile -Value $line -Encoding UTF8
    }} catch {{}}
}}
function Set-State([string]$value) {{
    try {{ [System.IO.File]::WriteAllText($state, $value) }}
    catch {{ Write-Log ('state not written: ' + $_.Exception.GetType().Name) }}
}}
try {{
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $logfile) | Out-Null
    if ((Get-Item -LiteralPath $logfile -ErrorAction SilentlyContinue).Length -gt 1MB) {{
        Remove-Item -LiteralPath $logfile -Force
    }}
}} catch {{}}
Set-State ('{STATE_STARTED} ' + $version)
Write-Log ('installer started, version ' + $version)
for ($i = 0; $i -lt {WAIT_STEPS}; $i++) {{
    if (-not (Get-Process -Id {pid} -ErrorAction SilentlyContinue)) {{ break }}
    Start-Sleep -Milliseconds 500
}}
if (Get-Process -Id {pid} -ErrorAction SilentlyContinue) {{
    Write-Log 'application still running: update abandoned'
    Set-State '{STATE_FAILED} {STEP_WAIT}'
    exit 1
}}
Write-Log 'application closed'
Start-Sleep -Milliseconds 800
function Move-WithRetry([string]$from, [string]$to) {{
    $last = ''
    for ($i = 0; $i -lt 60; $i++) {{
        try {{ Move-Item -LiteralPath $from -Destination $to -Force; return }}
        catch {{ $last = $_.Exception.GetType().Name; Start-Sleep -Milliseconds 1000 }}
    }}
    throw ('move: ' + $last)
}}
$backed = @()
$installed = @()
$ok = $true
try {{
    foreach ($e in $entries) {{
        Write-Log ('replacing ' + $e)
        $current = Join-Path $root $e
        $old = $current + $suffix
        if (Test-Path -LiteralPath $old) {{ Remove-Item -LiteralPath $old -Recurse -Force }}
        if (Test-Path -LiteralPath $current) {{ Move-WithRetry $current $old; $backed += $e }}
        Move-WithRetry (Join-Path $staged $e) $current
        $installed += $e
    }}
}} catch {{
    $ok = $false
    # Only our own 'move: <type>' message: others may quote a path.
    $detail = $_.Exception.Message
    if ($detail -notlike 'move: *') {{ $detail = $_.Exception.GetType().Name }}
    Write-Log ('replacement failed: ' + $detail)
}}
if ($ok) {{
    foreach ($e in $entries) {{
        $old = (Join-Path $root $e) + $suffix
        try {{ Remove-Item -LiteralPath $old -Recurse -Force }} catch {{}}
    }}
    Set-State ('{STATE_OK} ' + $version)
    Write-Log 'update installed'
    try {{ Remove-Item -LiteralPath $updates -Recurse -Force }} catch {{}}
}} else {{
    $restored = $true
    foreach ($e in $installed) {{
        try {{ Remove-Item -LiteralPath (Join-Path $root $e) -Recurse -Force }} catch {{}}
    }}
    foreach ($e in $backed) {{
        $current = Join-Path $root $e
        try {{ Move-WithRetry ($current + $suffix) $current }} catch {{ $restored = $false }}
    }}
    if ($restored) {{
        Set-State '{STATE_FAILED} {STEP_REPLACE}'
        Write-Log 'previous version restored'
    }} else {{
        Set-State '{STATE_FAILED} {STEP_RESTORE}'
        Write-Log 'previous version not fully restored'
    }}
}}
{relaunch_block}
"""


def shell_script(
    root: Path,
    staged: Path,
    entries: Iterable[str],
    pid: int,
    updates: Path,
    *,
    version: str = "",
    relaunch: bool = True,
) -> str:
    """POSIX ``sh`` installer (macOS, Linux); same steps, log and state as
    :func:`powershell_script`."""
    names = " ".join(entries)  # validated by replaced_entries: no quoting needed
    if not relaunch:
        launch = 'log "restart skipped"'
    elif sys.platform == "darwin":
        launch = (
            f'if open "$root/{APP}.app"; then log "application restarted"; '
            'else log "restart failed"; fi'
        )
    else:
        launch = f'nohup "$root/{APP}" >/dev/null 2>&1 &\nlog "application restarted"'
    return f"""#!/bin/sh
# Anonyma update: replaces the program once the application has exited.
root={_sh_quote(str(root))}
staged={_sh_quote(str(staged))}
updates={_sh_quote(str(updates))}
logfile={_sh_quote(str(install_log_path(root)))}
state={_sh_quote(str(state_path(root)))}
version={_sh_quote(version)}
log() {{ printf '%s %s\\n' "$(date '+%Y-%m-%dT%H:%M:%S')" "$1" >> "$logfile" 2>/dev/null; }}
set_state() {{ printf '%s' "$1" > "$state" 2>/dev/null || log "state not written"; }}
mkdir -p "$(dirname "$logfile")" 2>/dev/null
if [ -f "$logfile" ] &&[ "$(wc -c < "$logfile")" -gt 1048576 ]; then rm -f "$logfile"; fi
set_state "{STATE_STARTED} $version"
log "installer started, version $version"
i=0
while kill -0 {pid} 2>/dev/null; do
    i=$((i + 1))
    if [ "$i" -gt {WAIT_STEPS} ]; then
        log "application still running: update abandoned"
        set_state "{STATE_FAILED} {STEP_WAIT}"
        exit 1
    fi
    sleep 0.5
done
log "application closed"
sleep 1
ok=1
backed=""
installed=""
for e in {names}; do
    log "replacing $e"
    rm -rf "$root/$e{BACKUP_SUFFIX}"
    if [ -e "$root/$e" ] || [ -L "$root/$e" ]; then
        mv "$root/$e" "$root/$e{BACKUP_SUFFIX}" || {{ log "move failed"; ok=0; break; }}
        backed="$backed $e"
    fi
    mv "$staged/$e" "$root/$e" || {{ log "move failed"; ok=0; break; }}
    installed="$installed $e"
done
if [ "$ok" = 1 ]; then
    for e in {names}; do rm -rf "$root/$e{BACKUP_SUFFIX}"; done
    set_state "{STATE_OK} $version"
    log "update installed"
    rm -rf "$updates"
else
    restored=1
    for e in $installed; do rm -rf "$root/$e"; done
    for e in $backed; do mv "$root/$e{BACKUP_SUFFIX}" "$root/$e" || restored=0; done
    if [ "$restored" = 1 ]; then
        set_state "{STATE_FAILED} {STEP_REPLACE}"
        log "previous version restored"
    else
        set_state "{STATE_FAILED} {STEP_RESTORE}"
        log "previous version not fully restored"
    fi
fi
{launch}
"""


def _child_environment() -> dict[str, str]:
    """Environment for the installer and the relaunched application: no
    PyInstaller state inherited from this process."""
    env = dict(os.environ)
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    for name in list(env):
        if name.startswith("_PYI") or name == "_MEIPASS2":
            env.pop(name, None)
    for var in ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH"):
        original = env.pop(f"{var}_ORIG", None)
        if original is not None:
            env[var] = original
        else:
            env.pop(var, None)
    return env


def _start_windows(command: list[str], root: Path) -> subprocess.Popen[bytes]:
    """Start the PowerShell installer so that it outlives this process.

    No ``DETACHED_PROCESS``: PowerShell 5.1 started without a console exits at once
    without running its script. ``CREATE_NO_WINDOW`` gives it a hidden console
    instead. ``CREATE_BREAKAWAY_FROM_JOB`` takes it out of a job object the
    application may run in (a launcher's kill-on-close job would end it together
    with the application); a job that forbids breaking away refuses the flag, and the
    installer then starts inside it."""
    flags = CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    kwargs: dict[str, Any] = {
        "cwd": str(root),  # never the staging folder: Windows locks a cwd
        "env": _child_environment(),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    try:
        return subprocess.Popen(command, creationflags=flags | CREATE_BREAKAWAY_FROM_JOB, **kwargs)
    except OSError as exc:
        log.info("update installer cannot leave the job (%s)", type(exc).__name__)
        return subprocess.Popen(command, creationflags=flags, **kwargs)


def _wait_started(process: subprocess.Popen[bytes], root: Path, timeout: float) -> None:
    """Wait until the installer reports that it runs (its first step writes the
    state file); raise UpdateError when it stops or stays silent."""
    deadline = time.monotonic() + timeout
    while read_state(root) is None:
        code = process.poll()
        if code is not None:
            log.warning("update installer exited before starting (code %s)", code)
            raise UpdateError(
                f"Le programme d'installation de la mise à jour s'est arrêté avant de "
                f"commencer (code {code})."
            )
        if time.monotonic() >= deadline:
            log.warning("update installer silent after %.0f s", timeout)
            with contextlib.suppress(OSError):
                process.kill()
            raise UpdateError("Le programme d'installation de la mise à jour n'a pas démarré.")
        time.sleep(0.1)


def launch_installer(
    staged: Path,
    root: Path | None = None,
    pid: int | None = None,
    *,
    version: str = "",
    relaunch: bool = True,
    timeout: float = STARTUP_TIMEOUT,
) -> None:
    """Start the installer, which waits for this process to exit, replaces the
    program and starts the application again. Returns once the installer reports
    that it runs; raises UpdateError (French message) when it does not."""
    root = root or install_root()
    pid = pid or os.getpid()
    if version and not parse_version(version):
        raise UpdateError("Numéro de version de la mise à jour invalide.")
    entries = replaced_entries(staged)
    folder = update_dir(root)
    folder.mkdir(parents=True, exist_ok=True)
    install_log_path(root).parent.mkdir(parents=True, exist_ok=True)
    state_path(root).unlink(missing_ok=True)
    try:
        if sys.platform == "win32":
            script = folder / "installer.ps1"
            # BOM: Windows PowerShell 5.1 reads BOM-less scripts in the ANSI code page.
            text = powershell_script(
                root, staged, entries, pid, folder, version=version, relaunch=relaunch
            )
            script.write_text(text, encoding="utf-8-sig")
            command = [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-WindowStyle",
                "Hidden",
                "-File",
                str(script),
            ]
            process = _start_windows(command, root)
        else:
            script = folder / "installer.sh"
            text = shell_script(
                root, staged, entries, pid, folder, version=version, relaunch=relaunch
            )
            script.write_text(text, encoding="utf-8")
            script.chmod(0o700)
            process = subprocess.Popen(
                ["/bin/sh", str(script)],
                cwd=str(root),
                env=_child_environment(),
                start_new_session=True,  # survives the application's process group
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
    except OSError as exc:
        log.warning("update installer not started: %s", type(exc).__name__)
        raise UpdateError(
            f"Impossible de lancer l'installation de la mise à jour ({type(exc).__name__})."
        ) from exc
    _wait_started(process, root, timeout)
    log.info("update installer started")


# ---------------------------------------------------------------------------
# Outcome of the last installation (read at the next start)
# ---------------------------------------------------------------------------


def state_path(root: Path | None = None) -> Path:
    """Outcome of the last installer run (outside ``data/maj``, which it deletes)."""
    return (root or install_root()) / "data" / STATE_NAME


def install_log_path(root: Path | None = None) -> Path:
    return (root or install_root()) / "data" / "logs" / INSTALL_LOG


@dataclass(frozen=True, slots=True)
class InstallState:
    """``status`` is ``STATE_STARTED``, ``STATE_OK`` or ``STATE_FAILED``; ``detail``
    the target version (started, ok) or the failed step."""

    status: str
    detail: str
    age: float

    @property
    def running(self) -> bool:
        """The installer is (probably) still at work: leave its files alone."""
        return self.status == STATE_STARTED and self.age < RUNNING_GRACE


def read_state(root: Path | None = None) -> InstallState | None:
    path = state_path(root)
    try:
        text = path.read_text(encoding="utf-8").strip()
        age = max(0.0, time.time() - path.stat().st_mtime)
    except OSError:
        return None
    status, _, detail = text.partition(" ")
    if status not in (STATE_STARTED, STATE_OK, STATE_FAILED):
        return None
    return InstallState(status, detail.strip(), age)


def take_state(root: Path | None = None) -> InstallState | None:
    """The outcome of the last installation, removed once read (kept while the
    installer may still run)."""
    state = read_state(root)
    if state is not None and not state.running:
        with contextlib.suppress(OSError):
            state_path(root).unlink()
    return state


def cleanup(root: Path | None = None) -> None:
    """Remove what a finished or interrupted update left behind (startup)."""
    root = root or install_root()
    shutil.rmtree(update_dir(root), ignore_errors=True)
    with contextlib.suppress(OSError):
        for child in list(root.iterdir()):
            if not child.name.endswith(BACKUP_SUFFIX):
                continue
            original = child.with_name(child.name.removesuffix(BACKUP_SUFFIX))
            if not original.exists() and not original.is_symlink():
                # Interrupted installation: put the previous version back.
                with contextlib.suppress(OSError):
                    child.rename(original)
                continue
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child, ignore_errors=True)
            else:
                with contextlib.suppress(OSError):
                    child.unlink()
