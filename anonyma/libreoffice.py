"""LibreOffice headless: detection and format conversion (legacy doc/xls/ppt).

Every conversion runs with a throw-away user profile located inside the batch
temporary folder (``-env:UserInstallation``) so that nothing is written outside
the portable folder; the profile disables update checks, telemetry and macros.
Conversions are bounded by a timeout and the whole process tree is killed when
it expires.
"""

from __future__ import annotations

import contextlib
import glob
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

from anonyma import paths
from anonyma.config import Config
from anonyma.logs import get_logger

log = get_logger("libreoffice")

#: Seconds allowed for one conversion.
DEFAULT_TIMEOUT = 180.0

_PROFILE_SETTINGS = """<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry"
 xmlns:xs="http://www.w3.org/2001/XMLSchema"
 xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<item oor:path="/org.openoffice.Office.Jobs/Jobs/org.openoffice.Office.Jobs:Job['UpdateCheck']/Arguments"><prop oor:name="AutoCheckEnabled" oor:op="fuse"><value>false</value></prop></item>
<item oor:path="/org.openoffice.Office.Common/Misc"><prop oor:name="CollectUsageInformation" oor:op="fuse"><value>false</value></prop></item>
<item oor:path="/org.openoffice.Office.Common/Misc"><prop oor:name="ShowTipOfTheDay" oor:op="fuse"><value>false</value></prop></item>
<item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop></item>
<item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="DisableMacrosExecution" oor:op="fuse"><value>true</value></prop></item>
<item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="RemovePersonalInformationOnSaving" oor:op="fuse"><value>true</value></prop></item>
<item oor:path="/org.openoffice.Office.Common/Save/Document"><prop oor:name="CreateBackup" oor:op="fuse"><value>false</value></prop></item>
<item oor:path="/org.openoffice.Office.Common/Save/Document"><prop oor:name="AutoSave" oor:op="fuse"><value>false</value></prop></item>
</oor:items>
"""  # noqa: E501


class ConversionError(RuntimeError):
    """A LibreOffice conversion failed (message is value-free)."""


def _executable_names() -> tuple[str, ...]:
    return ("soffice.exe", "soffice.com") if sys.platform == "win32" else ("soffice", "libreoffice")


def standard_locations() -> list[Path]:
    """Usual install locations, portable copies next to the application first."""
    root = paths.app_root()
    found: list[Path] = [
        root / "LibreOffice" / "program" / "soffice.exe",
        root / "LibreOffice" / "program" / "soffice",
        root / "LibreOfficePortable" / "App" / "libreoffice" / "program" / "soffice.exe",
    ]
    if sys.platform == "win32":
        for var in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)", "LOCALAPPDATA"):
            base = os.environ.get(var)
            if not base:
                continue
            prefix = Path(base) / "Programs" if var == "LOCALAPPDATA" else Path(base)
            found.extend(
                Path(p) / "program" / "soffice.exe" for p in glob.glob(str(prefix / "LibreOffice*"))
            )
    elif sys.platform == "darwin":
        for app in ("/Applications", str(Path.home() / "Applications")):
            found.append(Path(app) / "LibreOffice.app" / "Contents" / "MacOS" / "soffice")
    else:
        found.extend(
            Path(p)
            for p in (
                "/usr/bin/soffice",
                "/usr/bin/libreoffice",
                "/usr/local/bin/soffice",
                "/usr/lib/libreoffice/program/soffice",
                "/usr/lib64/libreoffice/program/soffice",
                "/snap/bin/libreoffice",
            )
        )
        found.extend(Path(p) for p in glob.glob("/opt/libreoffice*/program/soffice"))
    return found


def _from_setting(value: str) -> Path | None:
    path = Path(value).expanduser()
    if path.is_file():
        return path
    if path.is_dir():
        for sub in ("", "program", "Contents/MacOS", "App/libreoffice/program"):
            for name in _executable_names():
                candidate = path / sub / name
                if candidate.is_file():
                    return candidate
    return None


def find_soffice(config: Config) -> Path | None:
    """LibreOffice executable, or None when disabled or not installed.

    Order: ``config.libreoffice_path`` (file or install folder), ``PATH``, then
    the standard locations of Windows, macOS and Linux.
    """
    if not config.use_libreoffice:
        return None
    if config.libreoffice_path:
        configured = _from_setting(config.libreoffice_path)
        if configured is not None:
            return configured
    for name in _executable_names():
        which = shutil.which(name)
        if which:
            return Path(which)
    for candidate in standard_locations():
        if candidate.is_file():
            return candidate
    return None


def _seed_profile(profile: Path) -> None:
    user = profile / "user"
    user.mkdir(parents=True, exist_ok=True)
    settings = user / "registrymodifications.xcu"
    if not settings.exists():
        settings.write_text(_PROFILE_SETTINGS, encoding="utf-8")


def _kill_tree(process: subprocess.Popen[bytes]) -> None:
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True,
                timeout=30,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    with contextlib.suppress(OSError):
        process.kill()


def convert(
    source: Path,
    target: str,
    work_dir: Path,
    soffice: Path,
    *,
    timeout: float = DEFAULT_TIMEOUT,
) -> Path:
    """Convert ``source`` with the LibreOffice filter spec ``target``
    (e.g. ``"docx:MS Word 2007 XML"``) into ``work_dir``; return the new file.

    The source is copied under a neutral name first, so that LibreOffice never
    touches the original nor sees its (sensitive) name.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    in_dir = work_dir / "in"
    out_dir = work_dir / "out"
    profile = work_dir.parent / "libreoffice-profile"
    in_dir.mkdir(exist_ok=True)
    out_dir.mkdir(exist_ok=True)
    _seed_profile(profile)
    staged = in_dir / f"document{source.suffix.lower()}"
    shutil.copyfile(source, staged)
    command = [
        str(soffice),
        f"-env:UserInstallation={profile.resolve().as_uri()}",
        "--headless",
        "--invisible",
        "--norestore",
        "--nologo",
        "--nodefault",
        "--nofirststartwizard",
        "--nolockcheck",
        "--convert-to",
        target,
        "--outdir",
        str(out_dir),
        str(staged),
    ]
    env = dict(os.environ)
    env["HOME"] = str(profile)  # some builds still look at $HOME
    kwargs: dict[str, object] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        kwargs["start_new_session"] = True
    process = subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        env=env,
        **kwargs,  # type: ignore[call-overload]
    )
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _kill_tree(process)
        raise ConversionError("délai de conversion LibreOffice dépassé") from exc
    extension = target.split(":", 1)[0]
    result = out_dir / f"{staged.stem}.{extension}"
    if process.returncode != 0 or not result.is_file() or result.stat().st_size == 0:
        log.warning("LibreOffice conversion failed (exit code %s)", process.returncode)
        raise ConversionError("échec de la conversion LibreOffice")
    return result
