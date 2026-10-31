"""Build the portable Anonyma folder with PyInstaller, smoke-test it and zip it.

Usage (from the project root)::

    uv run python packaging/build.py              # build + smoke test + zip
    uv run python packaging/build.py --no-zip     # keep only dist/Anonyma/
    uv run python packaging/build.py --no-smoke   # skip the smoke test

Result: ``dist/Anonyma/`` (portable folder) and
``dist/Anonyma-<version>-<os>-<arch>.zip``.

Portable folder layout::

    Anonyma/
      Anonyma.exe | Anonyma (Linux) | Anonyma.app (macOS)   GUI
      anonyma-cli.exe | anonyma-cli                            CLI (macOS: shell wrapper)
      _internal/                                               shared libraries (not macOS)
      models/   data/                                          empty, filled at runtime
      LICENSES.md   README.md
"""

from __future__ import annotations

import argparse
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGING = ROOT / "packaging"
DIST = ROOT / "dist"
WORK = ROOT / "build"
APP = "Anonyma"
CLI = "anonyma-cli"

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


def cli_path(folder: Path) -> Path:
    if sys.platform == "win32":
        return folder / f"{CLI}.exe"
    if sys.platform == "darwin":
        return folder / f"{APP}.app" / "Contents" / "MacOS" / CLI
    return folder / CLI


def reset_runtime_dirs(folder: Path) -> None:
    for name in ("models", "data"):
        shutil.rmtree(folder / name, ignore_errors=True)
        (folder / name).mkdir()


def smoke_test(folder: Path) -> None:
    """Run the frozen CLI: list formats, then anonymize a txt file with 2 workers."""
    exe = cli_path(folder)
    env = {k: v for k, v in os.environ.items() if k != "ANONYMA_HOME"}
    env["PYTHONIOENCODING"] = "utf-8"
    with tempfile.TemporaryDirectory(prefix="anonyma-smoke-") as tmp:
        work = Path(tmp)
        source = work / "entree"
        source.mkdir()
        for i in range(3):
            (source / f"lettre{i}.txt").write_text(
                "Courrier de Zéphyrin Quintavalle <zq@exemple.test>.\n", encoding="utf-8"
            )
        terms = work / "liste.csv"
        terms.write_text(
            "valeur;type\nZéphyrin Quintavalle;PERSONNE\nzq@exemple.test;EMAIL\n",
            encoding="utf-8",
        )
        output = work / "sortie"
        report = work / "rapport.html"
        run([str(exe), "--version"], env=env)
        run([str(exe), "formats"], env=env)
        run([str(exe), "materiel"], env=env)
        run(
            [
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
            ],
            env=env,
        )
        texts = [p.read_text(encoding="utf-8") for p in sorted(output.rglob("*.txt"))]
        if len(texts) != 3 or any("Quintavalle" in t or "[PERSONNE_001]" not in t for t in texts):
            raise SystemExit("Test de fumée en échec : sortie inattendue.")
        if not report.is_file():
            raise SystemExit("Test de fumée en échec : rapport absent.")
    tmp_left = folder / "data" / "tmp"
    if tmp_left.is_dir() and any(tmp_left.iterdir()):
        raise SystemExit("Test de fumée en échec : fichiers temporaires non supprimés.")
    print("Test de fumée réussi.", flush=True)


def zip_folder(folder: Path) -> Path:
    archive = DIST / f"{APP}-{version()}-{platform_tag()}.zip"
    archive.unlink(missing_ok=True)
    if sys.platform == "darwin":
        # ditto keeps symlinks, permissions and extended attributes of the bundle.
        run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(folder), str(archive)])
        return archive
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in sorted(folder.rglob("*")):
            arcname = Path(APP) / path.relative_to(folder)
            if path.is_dir():
                info = zipfile.ZipInfo(arcname.as_posix() + "/")
                info.external_attr = (0o40755 << 16) | 0x10
                zf.writestr(info, "")
            else:
                zf.write(path, arcname.as_posix())
    return archive


def main() -> None:
    parser = argparse.ArgumentParser(description="Construit le dossier portable d'Anonyma.")
    parser.add_argument("--no-zip", action="store_true", help="ne pas créer l'archive zip")
    parser.add_argument("--no-smoke", action="store_true", help="ne pas lancer le test de fumée")
    parser.add_argument("--no-licenses", action="store_true", help="ne pas régénérer LICENSES.md")
    parser.add_argument("--clean", action="store_true", help="vider le cache de PyInstaller")
    args = parser.parse_args()

    if not args.no_licenses:
        generate_licenses()
    pyinstaller(args.clean)
    folder = assemble()
    if not args.no_smoke:
        smoke_test(folder)
    reset_runtime_dirs(folder)
    print(f"Dossier portable : {folder}", flush=True)
    if not args.no_zip:
        print(f"Archive : {zip_folder(folder)}", flush=True)


if __name__ == "__main__":
    main()
