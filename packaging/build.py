"""Build the portable Anonyma folder with PyInstaller, smoke-test it and zip it.

Usage (from the project root)::

    uv run python packaging/build.py                     # full build (LibreOffice bundled)
    uv run python packaging/build.py --sans-libreoffice  # light build
    uv run python packaging/build.py --no-zip --no-smoke

Result: ``dist/Anonyma/`` (portable folder) and
``dist/Anonyma-<version>-<os>-<arch>[-sans-libreoffice].zip``.

Portable folder layout::

    Anonyma/
      Anonyma.exe | Anonyma (Linux) | Anonyma.app (macOS)   GUI
      anonyma-cli.exe | anonyma-cli                            CLI (macOS: shell wrapper)
      _internal/                                               shared libraries (not macOS)
      libreoffice/                                             bundled LibreOffice (MPL-2.0)
      models/   data/                                          empty, filled at runtime
      LICENSES.md   README.md

LibreOffice is downloaded at build time only (pinned version, SHA-256 checked, see
``packaging/libreoffice_bundle.py``); the application never downloads it.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parent.parent
PACKAGING = ROOT / "packaging"
DIST = ROOT / "dist"
WORK = ROOT / "build"
FIXTURES = ROOT / "tests" / "fixtures" / "contract"
APP = "Anonyma"
CLI = "anonyma-cli"
LIBREOFFICE_DIR = "libreoffice"

CANARY_PERSON = "Zéphyrin Quintavalle"
CANARY_EMAIL = "zephyrin.quintavalle@exemple.test"
#: Contract fixtures anonymized by the frozen CLI (real OCR for the PNG).
SMOKE_FIXTURES = (
    "rapport.docx",
    "tableau.xlsx",
    "rapport.pdf",
    "courriel.eml",
    "message.msg",
    "archive.zip",
    "scan.png",
    "note.txt",
)
#: Documented output shape changes: a PST becomes a folder of .eml (no extension).
#: Every other format, .msg included, keeps its original extension.
CONVERTED = {".pst": ""}
#: Report statuses meaning "verified clean" (uncertain entities masked and flagged).
CLEAN_STATUSES = frozenset({"anonymise", "a_revoir"})  # status codes of the JSON report
#: Legacy formats, round-tripped through the bundled LibreOffice.
LEGACY_FIXTURES = (
    ("rapport.doc", "rapport.docx", "doc:MS Word 97"),
    ("tableau.xls", None, None),
    ("presentation.ppt", "presentation.pptx", "ppt:MS PowerPoint 97"),
)

MAC_CLI_WRAPPER = """#!/bin/sh
# Console version of Anonyma (the executable lives inside the application bundle).
exec "$(cd "$(dirname "$0")" && pwd)/Anonyma.app/Contents/MacOS/anonyma-cli" "$@"
"""


def version() -> str:
    init = (ROOT / "anonyma" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r"""^__version__\s*=\s*["']([^"']+)["']""", init, re.MULTILINE)
    return match.group(1) if match else "0"


def platform_tag() -> str:
    system = {"win32": "windows", "darwin": "macos"}.get(sys.platform, sys.platform)
    machine = platform.machine().lower().replace("amd64", "x86_64").replace("aarch64", "arm64")
    return f"{system}-{machine}"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"_anonyma_{name}", PACKAGING / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(module)
    return module


def run(cmd: list[str], **kwargs: object) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, cwd=ROOT, **kwargs)  # type: ignore[call-overload]


def generate_licenses() -> None:
    run([sys.executable, str(PACKAGING / "gen_licenses.py"), "--output", str(ROOT / "LICENSES.md")])


def pyinstaller(clean: bool) -> None:
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        str(PACKAGING / "anonyma.spec"),
        "--noconfirm",
        "--distpath",
        str(DIST),
        "--workpath",
        str(WORK),
        "--log-level",
        "WARN",
    ]
    if clean:
        cmd.append("--clean")
    run(cmd)


def assemble() -> Path:
    """Turn PyInstaller's output into the portable folder."""
    folder = DIST / APP
    if sys.platform == "darwin":
        bundle = DIST / f"{APP}.app"
        if not bundle.is_dir():
            raise SystemExit("Anonyma.app est introuvable : la construction a échoué.")
        shutil.rmtree(folder, ignore_errors=True)  # COLLECT copy, already inside the .app
        folder.mkdir(parents=True)
        shutil.move(str(bundle), folder / bundle.name)
        wrapper = folder / CLI
        wrapper.write_text(MAC_CLI_WRAPPER, encoding="utf-8", newline="\n")
        wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    elif not folder.is_dir():
        raise SystemExit("Le dossier portable est introuvable : la construction a échoué.")
    for name in ("models", "data"):
        (folder / name).mkdir(exist_ok=True)
    for doc in ("LICENSES.md", "README.md"):
        if (ROOT / doc).is_file():
            shutil.copy2(ROOT / doc, folder / doc)
    return folder


def bundle_libreoffice(folder: Path) -> Path:
    """Unpack the pinned LibreOffice into ``<folder>/libreoffice`` (cached download)."""
    return Path(_load("libreoffice_bundle").bundle(folder / LIBREOFFICE_DIR))


def cli_path(folder: Path) -> Path:
    if sys.platform == "win32":
        return folder / f"{CLI}.exe"
    if sys.platform == "darwin":
        return folder / f"{APP}.app" / "Contents" / "MacOS" / CLI
    return folder / CLI


def gui_path(folder: Path) -> Path:
    if sys.platform == "win32":
        return folder / f"{APP}.exe"
    if sys.platform == "darwin":
        return folder / f"{APP}.app" / "Contents" / "MacOS" / APP
    return folder / APP


def reset_runtime_dirs(folder: Path) -> None:
    for name in ("models", "data"):
        shutil.rmtree(folder / name, ignore_errors=True)
        (folder / name).mkdir()


def _frozen_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in {"ANONYMA_HOME", "VIRTUAL_ENV"}}
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _legacy_inputs(folder: Path, target: Path) -> list[str]:
    """Copy (or create with the bundled soffice) the doc/xls/ppt fixtures."""
    from anonyma import libreoffice

    names: list[str] = []
    system = "linux" if sys.platform.startswith("linux") else sys.platform
    soffice = folder / LIBREOFFICE_DIR / _load("libreoffice_bundle").soffice_relative(system)
    for name, modern, filter_spec in LEGACY_FIXTURES:
        fixture = FIXTURES / name
        if fixture.is_file():
            shutil.copy2(fixture, target / name)
        elif modern and filter_spec:
            with tempfile.TemporaryDirectory(prefix="lo-") as tmp:
                converted = libreoffice.convert(
                    FIXTURES / modern, filter_spec, Path(tmp) / "w", soffice
                )
                shutil.copy2(converted, target / name)
        else:
            continue
        names.append(name)
    return names


def smoke_test(folder: Path, with_libreoffice: bool) -> None:
    """Frozen CLI: version, formats, hardware, then a batch of contract fixtures with
    2 worker processes; every file must come out ANONYMIZED with its extension."""
    exe = cli_path(folder)
    env = _frozen_env()
    appdata_lo = Path(os.environ.get("APPDATA", "~")).expanduser() / "LibreOffice"
    appdata_existed = appdata_lo.exists()
    with tempfile.TemporaryDirectory(prefix="anonyma-smoke-") as tmp:
        work = Path(tmp)
        source = work / "entree"
        source.mkdir()
        expected = []
        for name in SMOKE_FIXTURES:
            if (FIXTURES / name).is_file():
                shutil.copy2(FIXTURES / name, source / name)
                expected.append(name)
        if with_libreoffice:
            expected += _legacy_inputs(folder, source)
        terms = work / "liste.csv"
        terms.write_text(
            f"valeur;type\n{CANARY_PERSON};PERSONNE\n{CANARY_EMAIL};EMAIL\n", encoding="utf-8"
        )
        output = work / "sortie"
        report = work / "rapport.json"
        run([str(exe), "--version"], env=env)
        run([str(exe), "formats"], env=env)
        run([str(exe), "materiel"], env=env)
        command = [
            str(exe),
            "run",
            str(source),
            "--sortie",
            str(output),
            "--profil",
            "leger",
            "--liste-metier",
            str(terms),
            "--processus",
            "2",
            "--rapport",
            str(report),
            "--silencieux",
        ]
        print("+", " ".join(command), flush=True)
        completed = subprocess.run(command, env=env, cwd=ROOT, check=False)
        data = json.loads(report.read_text(encoding="utf-8")) if report.is_file() else {}
        files = {Path(f["fichier"]).name: f for f in data.get("fichiers", [])}
        problems = []
        for name in expected:
            suffix = Path(name).suffix
            entry = files.get(Path(name).stem + CONVERTED.get(suffix, suffix))
            if entry is None:
                problems.append(f"{name} : absent du rapport (nom ou extension modifiés)")
            elif entry["statut"] not in CLEAN_STATUSES:
                problems.append(f"{name} : {entry['statut']}")
        for path in output.rglob("*"):
            if path.is_file() and "uintavalle" in path.name.casefold():
                problems.append(f"nom non anonymisé : {path.name}")
        if completed.returncode != 0 or problems:
            for failure in data.get("echecs", []):
                print(f"  {failure['fichier']} : {failure['problemes']}", flush=True)
            raise SystemExit(
                "Test de fumée en échec (code "
                f"{completed.returncode}) : " + " ; ".join(problems or ["voir ci-dessus"])
            )
        print(f"{len(expected)} fichiers anonymisés : {', '.join(expected)}", flush=True)
    tmp_left = folder / "data" / "tmp"
    # Files only: on Windows the exiting soffice.bin recreates empty folders (its
    # « Documents ») in its throw-away profile after the batch folder is removed; they
    # hold nothing and the next start removes them.
    left = sorted(p.relative_to(tmp_left).as_posix() for p in tmp_left.rglob("*") if p.is_file())
    if left:
        # Batch folders, LibreOffice profiles and staged copies have neutral names.
        raise SystemExit(
            "Test de fumée en échec : fichiers temporaires non supprimés : " + ", ".join(left[:20])
        )
    if sys.platform == "win32" and not appdata_existed and appdata_lo.exists():
        raise SystemExit("Test de fumée en échec : LibreOffice a écrit dans %APPDATA%.")
    print("Test de fumée réussi.", flush=True)


def gui_smoke_test(folder: Path, seconds: float = 15.0) -> None:
    """The GUI executable must start and keep running (offscreen Qt platform)."""
    env = _frozen_env()
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    process = subprocess.Popen([str(gui_path(folder))], env=env, cwd=ROOT)
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and process.poll() is None:
        time.sleep(0.5)
    code = process.poll()
    if code is None:
        process.kill()
        process.wait(timeout=30)
        print("Interface graphique démarrée.", flush=True)
        return
    raise SystemExit(f"L'interface graphique s'est arrêtée au démarrage (code {code}).")


def zip_folder(folder: Path, suffix: str = "") -> Path:
    archive = DIST / f"{APP}-{version()}-{platform_tag()}{suffix}.zip"
    archive.unlink(missing_ok=True)
    if sys.platform == "darwin":
        # ditto keeps symlinks, permissions and extended attributes of the bundles.
        run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(folder), str(archive)])
        return archive
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in sorted(folder.rglob("*")):
            arcname = Path(APP) / path.relative_to(folder)
            if path.is_symlink() and sys.platform != "win32":
                info = zipfile.ZipInfo(arcname.as_posix())
                info.external_attr = 0o120777 << 16
                zf.writestr(info, os.readlink(path))
            elif path.is_dir():
                info = zipfile.ZipInfo(arcname.as_posix() + "/")
                info.external_attr = (0o40755 << 16) | 0x10
                zf.writestr(info, "")
            else:
                zf.write(path, arcname.as_posix())
    return archive


def main() -> None:
    parser = argparse.ArgumentParser(description="Construit le dossier portable d'Anonyma.")
    parser.add_argument(
        "--sans-libreoffice", action="store_true", help="ne pas embarquer LibreOffice"
    )
    parser.add_argument("--no-zip", action="store_true", help="ne pas créer l'archive zip")
    parser.add_argument("--no-smoke", action="store_true", help="ne pas lancer les tests de fumée")
    parser.add_argument("--no-licenses", action="store_true", help="ne pas régénérer LICENSES.md")
    parser.add_argument("--clean", action="store_true", help="vider le cache de PyInstaller")
    args = parser.parse_args()

    if not args.no_licenses:
        generate_licenses()
    pyinstaller(args.clean)
    folder = assemble()
    with_lo = not args.sans_libreoffice
    if with_lo:
        bundle_libreoffice(folder)
    if not args.no_smoke:
        smoke_test(folder, with_lo)
        reset_runtime_dirs(folder)
        gui_smoke_test(folder)
    reset_runtime_dirs(folder)
    size = sum(p.stat().st_size for p in folder.rglob("*") if p.is_file() and not p.is_symlink())
    print(f"Dossier portable : {folder} ({size / 1024**2:.0f} Mo)", flush=True)
    if not args.no_zip:
        archive = zip_folder(folder, "" if with_lo else "-sans-libreoffice")
        print(f"Archive : {archive} ({archive.stat().st_size / 1024**2:.0f} Mo)", flush=True)


if __name__ == "__main__":
    main()
