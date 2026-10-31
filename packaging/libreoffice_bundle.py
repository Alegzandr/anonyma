"""Fetch and unpack the official LibreOffice build bundled in the portable folder.

Build time only: the application never downloads anything. The version is pinned
and every installer is checked against the SHA-256 published by The Document
Foundation (``*.mirrorlist`` pages of downloadarchive.documentfoundation.org).

Bundled layout (``anonyma/libreoffice.py`` looks there first)::

    <portable>/libreoffice/program/soffice.exe                      Windows
    <portable>/libreoffice/program/soffice                          Linux
    <portable>/libreoffice/LibreOffice.app/Contents/MacOS/soffice   macOS

Usage::

    uv run python packaging/libreoffice_bundle.py DESTINATION   # e.g. dist/Anonyma/libreoffice
"""

from __future__ import annotations

import argparse
import hashlib
import io
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "build" / "libreoffice-cache"

#: LibreOffice 26.2.6 ("still" branch); 26.2.6.3 is the build published as 26.2.6.
VERSION = "26.2.6.3"
BASE_URL = f"https://downloadarchive.documentfoundation.org/libreoffice/old/{VERSION}/"


@dataclass(frozen=True, slots=True)
class Installer:
    path: str
    sha256: str
    size: int

    @property
    def url(self) -> str:
        return BASE_URL + self.path

    @property
    def filename(self) -> str:
        return self.path.rsplit("/", 1)[-1]


INSTALLERS: dict[tuple[str, str], Installer] = {
    ("win32", "x86_64"): Installer(
        f"win/x86_64/LibreOffice_{VERSION}_Win_x86-64.msi",
        "f9877032fd908beb9c0ddf06df4af5c2e85f419c42e14876c4cce5aae5fb2660",
        373_252_096,
    ),
    ("darwin", "arm64"): Installer(
        f"mac/aarch64/LibreOffice_{VERSION}_MacOS_aarch64.dmg",
        "94bb3248df074c225490a8a6d1d9dc87c7d6783dbb7a8e9f0d0c3d94348552af",
        297_798_926,
    ),
    ("darwin", "x86_64"): Installer(
        f"mac/x86_64/LibreOffice_{VERSION}_MacOS_x86-64.dmg",
        "135b8a95b8133396d54bf8e726dbc0066145efa0d785963fc3d4592acbfcfe5b",
        308_308_875,
    ),
    ("linux", "x86_64"): Installer(
        f"deb/x86_64/LibreOffice_{VERSION}_Linux_x86-64_deb.tar.gz",
        "fd0e8f8f2408dd2e5b90286e60f3f97cf566ba441cd48cfc5bcc68067303e0bc",
        217_285_735,
    ),
    ("linux", "arm64"): Installer(
        f"deb/aarch64/LibreOffice_{VERSION}_Linux_aarch64_deb.tar.gz",
        "f8e8b1d30abde0d530d727ce1b26909ccbedc3d76bcf75d93b3dd5fcd5b8d278",
        207_165_540,
    ),
}


def current_platform() -> tuple[str, str]:
    system = "linux" if sys.platform.startswith("linux") else sys.platform
    machine = platform.machine().lower()
    machine = {"amd64": "x86_64", "x64": "x86_64", "aarch64": "arm64"}.get(machine, machine)
    return system, machine


def installer_for(system: str, machine: str) -> Installer:
    try:
        return INSTALLERS[(system, machine)]
    except KeyError:
        raise SystemExit(f"Pas de LibreOffice épinglé pour {system}/{machine}.") from None


def soffice_relative(system: str) -> Path:
    if system == "win32":
        return Path("program") / "soffice.exe"
    if system == "darwin":
        return Path("LibreOffice.app") / "Contents" / "MacOS" / "soffice"
    return Path("program") / "soffice"


# -- download ------------------------------------------------------------------


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while block := fh.read(8 * 1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def fetch(installer: Installer, cache: Path = CACHE) -> Path:
    """Download (resumable) into ``cache`` and verify the pinned SHA-256."""
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / installer.filename
    if target.is_file() and target.stat().st_size == installer.size:
        if sha256_of(target) == installer.sha256:
            return target
        target.unlink()
    part = target.with_name(target.name + ".part")
    for _attempt in range(5):
        start = part.stat().st_size if part.is_file() else 0
        if start >= installer.size:
            break
        request = urllib.request.Request(installer.url, headers={"User-Agent": "anonyma-build"})
        if start:
            request.add_header("Range", f"bytes={start}-")
        print(f"Téléchargement de {installer.url} (à partir de {start} octets)", flush=True)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                mode = "ab" if start and response.status == 206 else "wb"
                with part.open(mode) as fh:
                    shutil.copyfileobj(response, fh, 1024 * 1024)
        except OSError as exc:
            print(f"Interruption ({type(exc).__name__}), reprise...", flush=True)
            continue
    if not part.is_file() or part.stat().st_size != installer.size:
        raise SystemExit("Téléchargement de LibreOffice incomplet.")
    if sha256_of(part) != installer.sha256:
        part.unlink()
        raise SystemExit("Empreinte SHA-256 de LibreOffice incorrecte : fichier supprimé.")
    part.replace(target)
    return target


# -- unpacking -------------------------------------------------------------------


def _unpack_windows(msi: Path, destination: Path, scratch: Path) -> None:
    """Administrative install: extracts the files without installing anything."""
    target = scratch / "admin"
    log = scratch / "msiexec.log"
    subprocess.run(
        ["msiexec", "/a", str(msi), "/qn", f"TARGETDIR={target}", "/l*", str(log)], check=True
    )
    programs = sorted(target.rglob("program/soffice.exe"))
    if not programs:
        raise SystemExit("soffice.exe introuvable après l'extraction du MSI.")
    install_root = programs[0].parent.parent
    program = install_root / "program"
    # The MSVC runtime is deployed by merge modules into "System64" (x64) and
    # "System" (x86) folders of the administrative image: copy the x64 one next to
    # soffice.exe so that machines without the Visual C++ redistributable run it.
    runtime = next((p for p in target.rglob("System64") if p.is_dir()), None)
    if runtime is not None:
        for dll in runtime.glob("*.dll"):
            if not (program / dll.name).exists():
                shutil.copy2(dll, program / dll.name)
    # Fonts normally installed system-wide: LibreOffice also loads share/fonts/truetype.
    fonts = install_root / "Fonts"
    if fonts.is_dir():
        truetype = install_root / "share" / "fonts" / "truetype"
        truetype.mkdir(parents=True, exist_ok=True)
        for font in fonts.iterdir():
            shutil.move(str(font), truetype / font.name)
    # Not needed for headless conversions (spelling dictionaries, wiki and solver
    # extensions, the copy of the MSI, the per-system runtime folders, offline help).
    for pattern in ("share/extensions/dict-*", "share/extensions/nlpsolver",
                    "share/extensions/wiki-publisher", "*.msi", "System", "System64", "Fonts",
                    "help"):  # fmt: skip
        for item in install_root.glob(pattern):
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()
    _keep_languages(install_root)
    shutil.move(str(install_root), destination)


#: UI languages kept from the multilingual Windows installer (en-US is built in).
KEPT_LANGUAGES = {"fr", "en-US"}


def _keep_languages(install_root: Path) -> None:
    """Drop the UI translations and language registries of other languages."""
    for folder in (install_root / "program" / "resource").glob("*"):
        if folder.is_dir() and folder.name.replace("_", "-") not in KEPT_LANGUAGES:
            shutil.rmtree(folder)
    registry = install_root / "share" / "registry"
    for folder, prefix in (("res", "registry_"), ("res", "fcfg_langpack_"), (".", "Langpack-")):
        for xcd in (registry / folder).glob(f"{prefix}*.xcd"):
            if xcd.stem.removeprefix(prefix).replace("_", "-") not in KEPT_LANGUAGES:
                xcd.unlink()


def _unpack_macos(dmg: Path, destination: Path, scratch: Path) -> None:
    mount = scratch / "mnt"
    mount.mkdir()
    subprocess.run(
        ["hdiutil", "attach", "-nobrowse", "-readonly", "-noautoopen", "-mountpoint", str(mount),
         str(dmg)],
        check=True,
    )  # fmt: skip
    try:
        destination.mkdir(parents=True)
        subprocess.run(
            ["ditto", str(mount / "LibreOffice.app"), str(destination / "LibreOffice.app")],
            check=True,
        )
    finally:
        subprocess.run(["hdiutil", "detach", str(mount), "-force"], check=False)
    # Drop the quarantine attribute the download may have propagated.
    subprocess.run(["xattr", "-dr", "com.apple.quarantine", str(destination)], check=False)


def _ar_members(data: bytes) -> dict[str, bytes]:
    """Members of a Debian ``ar`` archive (no external tool needed)."""
    if not data.startswith(b"!<arch>\n"):
        raise SystemExit("Paquet Debian invalide.")
    members: dict[str, bytes] = {}
    offset = 8
    while offset + 60 <= len(data):
        header = data[offset : offset + 60]
        name = header[:16].decode().strip().rstrip("/")
        size = int(header[48:58].decode().strip())
        offset += 60
        members[name] = data[offset : offset + size]
        offset += size + (size % 2)
    return members


def _unpack_linux(archive: Path, destination: Path, scratch: Path) -> None:
    tree = scratch / "tree"
    tree.mkdir()
    with tarfile.open(archive) as outer:
        debs = [m for m in outer.getmembers() if m.isfile() and m.name.endswith(".deb")]
        for member in debs:
            if "-dict-" in member.name or "debian-menus" in member.name:
                continue
            handle = outer.extractfile(member)
            assert handle is not None
            members = _ar_members(handle.read())
            payload = next((v for k, v in members.items() if k.startswith("data.tar")), None)
            if payload is None:
                continue
            with tarfile.open(fileobj=io.BytesIO(payload)) as inner:
                inner.extractall(tree, filter="tar")
    roots = sorted((tree / "opt").glob("libreoffice*"))
    if not roots or not (roots[0] / "program" / "soffice").exists():
        raise SystemExit("soffice introuvable après l'extraction des paquets Debian.")
    shutil.move(str(roots[0]), destination)


def unpack(installer_file: Path, destination: Path, system: str) -> Path:
    """Unpack into ``destination`` (replaced) and return the soffice path."""
    if destination.exists():
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    (ROOT / "build").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="anonyma-lo-", dir=ROOT / "build") as tmp:
        scratch = Path(tmp)
        if system == "win32":
            _unpack_windows(installer_file, destination, scratch)
        elif system == "darwin":
            _unpack_macos(installer_file, destination, scratch)
        else:
            _unpack_linux(installer_file, destination, scratch)
    soffice = destination / soffice_relative(system)
    if not soffice.exists():
        raise SystemExit(f"Exécutable LibreOffice absent : {soffice}")
    return soffice


def check(soffice: Path) -> str:
    """``soffice --version`` with a throw-away profile; returns the version line."""
    console = soffice.with_suffix(".com")  # Windows: soffice.exe has no console output
    executable = console if console.is_file() else soffice
    with tempfile.TemporaryDirectory(prefix="anonyma-lo-profile-") as tmp:
        profile = Path(tmp).resolve().as_uri()
        result = subprocess.run(
            [str(executable), f"-env:UserInstallation={profile}", "--headless", "--version"],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    line = (result.stdout or "").strip().splitlines()
    if result.returncode != 0 or not line:
        raise SystemExit("LibreOffice embarqué ne démarre pas.")
    return line[0]


def bundle(destination: Path) -> Path:
    system, machine = current_platform()
    installer = installer_for(system, machine)
    soffice = unpack(fetch(installer), destination, system)
    print(f"LibreOffice embarqué : {check(soffice)}", flush=True)
    return soffice


def main() -> None:
    parser = argparse.ArgumentParser(description="Embarque LibreOffice dans un dossier.")
    parser.add_argument("destination", type=Path)
    print(bundle(parser.parse_args().destination.resolve()))


if __name__ == "__main__":
    main()
