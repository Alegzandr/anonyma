"""Generate LICENSES.md from the installed runtime distributions and the model catalogue.

Usage::

    uv run python packaging/gen_licenses.py [--output LICENSES.md]

Only the runtime dependency closure of the ``anonyma`` distribution is listed
(development tools are excluded), as resolved for the current platform: run it on
each target OS (the CI build job does) since some dependencies are
platform-specific (``onnxruntime-directml`` on Windows, ``onnxruntime`` elsewhere).
"""

from __future__ import annotations

import argparse
import platform
import re
import sys
from datetime import date
from importlib import metadata
from pathlib import Path

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

WARNING = """\
> [!CAUTION]
> **USAGE INTERNE UNIQUEMENT — NE PAS DISTRIBUER**
>
> Anonyma embarque **PyMuPDF (AGPL-3.0)** et **extract-msg (GPL-3.0)**. Ces licences
> copyleft imposent, en cas de distribution du logiciel à des tiers (et, pour
> l'AGPL, de mise à disposition par le réseau), de fournir l'intégralité du code
> source de l'ensemble sous la même licence.
>
> Anonyma est donc réservé à un **usage interne, non distribué**, au sein de
> l'organisation qui l'exploite. Ne le redistribuez pas, ne le vendez pas et ne le
> proposez pas en service à des tiers sans avoir remplacé ces composants ou obtenu
> les licences commerciales correspondantes (Artifex propose une licence
> commerciale de PyMuPDF). D'autres composants embarqués relèvent aussi de la
> GPL (x265 via pillow-heif, pcodedmp via extract-msg) : voir plus bas.
"""

COPYLEFT = re.compile(r"\bA?GPL|Affero|GNU General Public", re.IGNORECASE)
WEAK_COPYLEFT = re.compile(r"\bLGPL|Lesser General Public|\bMPL\b|Mozilla Public", re.IGNORECASE)

OTHER_COMPONENTS = [
    ("Python", "PSF-2.0", "https://docs.python.org/3/license.html"),
    (
        "Qt 6 (via PySide6)",
        "LGPL-3.0 — bibliothèques livrées séparément dans le dossier, remplaçables",
        "https://www.qt.io/licensing/",
    ),
    (
        "Chargeur PyInstaller",
        "GPL-2.0 avec exception explicite autorisant la diffusion d'applications",
        "https://pyinstaller.org/en/stable/license.html",
    ),
    (
        "DirectML (Windows, via onnxruntime-directml)",
        "MIT",
        "https://github.com/microsoft/DirectML",
    ),
    (
        "LibreOffice (embarqué dans `libreoffice/`)",
        "MPL-2.0 ; composants tiers : voir `libreoffice/LICENSE.html`, `NOTICE`, "
        "`CREDITS.fodt` et `readmes/` (macOS : `LibreOffice.app/Contents/Resources/`)",
        "https://www.libreoffice.org/about-us/licenses/",
    ),
    (
        "Modèles OCR PP-OCR (inclus dans rapidocr-onnxruntime)",
        "Apache-2.0",
        "https://github.com/PaddlePaddle/PaddleOCR",
    ),
]

#: Copyleft code reached through dependencies whose own license looks permissive.
BUNDLED_NOTES = """- **pillow-heif** (BSD-3-Clause) embarque dans ses roues binaires **libheif**
  (LGPL-3.0), **libde265** (LGPL-3.0) et l'encodeur **x265 (GPL-2.0)**.
- **extract-msg** (GPL-3.0) dépend de **RTFDE** (LGPL-3.0), qui dépend d'**oletools**
  (BSD), qui dépend de **pcodedmp (GPL-3.0)** : du code GPL-3.0 est donc embarqué même
  si l'on ne regarde que les licences permissives de la chaîne.
- **PyMuPDF** (AGPL-3.0) embarque **MuPDF** (AGPL-3.0, Artifex).
- **libpff-python** (LGPL-3.0) et **PySide6 / Qt** (LGPL-3.0) sont livrés sous forme de
  bibliothèques distinctes, remplaçables dans le dossier portable.
- **LibreOffice** (MPL-2.0) est livré tel que publié par The Document Foundation
  (version épinglée, empreinte SHA-256 vérifiée à la construction), allégé des
  dictionnaires et des langues d'interface autres que le français et l'anglais
  (Windows) ; ses avis de licence tiers sont conservés dans le dossier `libreoffice/`.
"""


def _license_of(dist: metadata.Distribution) -> str:
    meta = dist.metadata
    expression = meta.get("License-Expression")
    if expression:
        return str(expression).strip()
    raw = (meta.get("License") or "").strip()
    classifiers = [
        c.split("::")[-1].strip()
        for c in meta.get_all("Classifier") or []
        if c.startswith("License ::") and c.split("::")[-1].strip() != "OSI Approved"
    ]
    if raw and "\n" not in raw and len(raw) <= 80 and raw.upper() != "UNKNOWN":
        return raw
    if classifiers:
        return " ; ".join(dict.fromkeys(classifiers))
    if raw:
        first = raw.splitlines()[0].strip()
        return (first[:77] + "...") if len(first) > 80 else first
    return "inconnue"


def _url_of(dist: metadata.Distribution) -> str:
    meta = dist.metadata
    homepage = meta.get("Home-page")
    if homepage and homepage.upper() != "UNKNOWN":
        return str(homepage).strip()
    urls = [str(u) for u in meta.get_all("Project-URL") or []]
    preferred = ("homepage", "home", "source", "repository", "code", "documentation")
    for key in preferred:
        for entry in urls:
            label, _, url = entry.partition(",")
            if label.strip().lower() == key:
                return url.strip()
    return urls[0].partition(",")[2].strip() if urls else ""


#: Optional extras the portable build ships (the language model engine).
SHIPPED_EXTRAS = frozenset({"llm"})


def runtime_distributions(root: str = "anonyma") -> list[metadata.Distribution]:
    """Runtime dependency closure of ``root`` and of the extras the portable build
    ships (markers evaluated for this platform; an extra not installed is skipped)."""
    environment = default_environment()
    seen: dict[str, metadata.Distribution] = {}
    pending: list[tuple[str, frozenset[str]]] = [(root, SHIPPED_EXTRAS)]
    while pending:
        name, extras = pending.pop()
        key = canonicalize_name(name)
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            continue
        first_visit = key not in seen
        seen[key] = dist
        if not first_visit and not extras:
            continue
        for raw in dist.requires or []:
            req = Requirement(raw)
            if req.marker is not None:
                envs = [{**environment, "extra": e} for e in (extras or {""})]
                if not any(req.marker.evaluate(env) for env in envs):  # type: ignore[arg-type]
                    continue
            child = canonicalize_name(req.name)
            if child not in seen or req.extras:
                pending.append((req.name, frozenset(req.extras)))
    seen.pop(canonicalize_name(root), None)
    return sorted(seen.values(), key=lambda d: canonicalize_name(d.metadata["Name"]))


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def model_rows() -> list[str]:
    try:
        from anonyma.model_catalog import CATALOG
    except ImportError:
        return []
    return [
        f"| {_cell(m.id)} | {_cell(m.title)} | [{m.repo_id}](https://huggingface.co/{m.repo_id}) "
        f"| `{m.revision[:12]}` | {_cell(m.license)} |"
        for m in CATALOG
    ]


def render() -> str:
    dists = runtime_distributions()
    rows: list[str] = []
    copyleft: list[str] = []
    weak: list[str] = []
    for dist in dists:
        name = str(dist.metadata["Name"])
        lic = _license_of(dist)
        url = _url_of(dist)
        rows.append(f"| {_cell(name)} | {dist.version} | {_cell(lic)} | {url} |")
        if COPYLEFT.search(lic) and not WEAK_COPYLEFT.search(lic):
            copyleft.append(f"- **{name}** {dist.version} : {lic}")
        elif WEAK_COPYLEFT.search(lic):
            weak.append(f"- {name} {dist.version} : {lic}")
    models = model_rows()
    py = platform.python_version()
    lines = [
        "# Licences des composants d'Anonyma",
        "",
        WARNING,
        f"Fichier généré le {date.today().isoformat()} par `packaging/gen_licenses.py` "
        f"({platform.system()} {platform.machine()}, Python {py}). Les dépendances "
        "propres à un système (ONNX Runtime DirectML sous Windows, par exemple) "
        "varient d'une plateforme à l'autre : chaque archive portable contient la liste "
        "générée pour son système.",
        "",
        "## Licences copyleft fortes détectées",
        "",
        *(copyleft or ["- Aucune parmi les composants installés lors de la génération."]),
        "",
        "Dans tous les cas, PyMuPDF (AGPL-3.0) et extract-msg (GPL-3.0) font partie des "
        "composants prévus par Anonyma : l'avertissement ci-dessus s'applique.",
        "",
        "## Code copyleft embarqué indirectement",
        "",
        BUNDLED_NOTES,
        "## Licences copyleft faibles (bibliothèques)",
        "",
        *(weak or ["- Aucune."]),
        "",
        "## Modèles d'IA du catalogue",
        "",
        "Les modèles ne sont pas inclus dans l'archive : ils sont téléchargés à la "
        "demande (ou importés hors ligne) après vérification en direct de leur licence "
        "sur Hugging Face. Les licences non commerciales sont refusées.",
        "",
    ]
    if models:
        lines += [
            "| Identifiant | Titre | Dépôt | Révision | Licence |",
            "|---|---|---|---|---|",
            *models,
        ]
    else:
        lines.append("Aucun modèle dans le catalogue au moment de la génération.")
    lines += [
        "",
        "## Composants Python",
        "",
        "| Composant | Version | Licence | Adresse |",
        "|---|---|---|---|",
        *rows,
        "",
        "## Autres composants",
        "",
        "| Composant | Licence | Adresse |",
        "|---|---|---|",
        *(f"| {name} | {lic} | {url} |" for name, lic, url in OTHER_COMPONENTS),
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Génère LICENSES.md.")
    parser.add_argument("--output", type=Path, default=ROOT / "LICENSES.md")
    args = parser.parse_args()
    args.output.write_text(render(), encoding="utf-8", newline="\n")
    print(f"Écrit : {args.output}")


if __name__ == "__main__":
    main()
