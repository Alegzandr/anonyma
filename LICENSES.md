# Licences des composants d'Anonyma

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

Fichier généré le 2026-10-31 par `packaging/gen_licenses.py` (Windows AMD64, Python 3.12.10). Les dépendances propres à un système (ONNX Runtime DirectML sous Windows, par exemple) varient d'une plateforme à l'autre : chaque archive portable contient la liste générée pour son système.

## Licences copyleft fortes détectées

- **extract-msg** 0.58.0 : GPL
- **pcodedmp** 1.2.6 : GPL
- **pymupdf** 1.28.2 : Dual Licensed - GNU AFFERO GPL 3.0 or Artifex Commercial License

Dans tous les cas, PyMuPDF (AGPL-3.0) et extract-msg (GPL-3.0) font partie des composants prévus par Anonyma : l'avertissement ci-dessus s'applique.

## Code copyleft embarqué indirectement

- **pillow-heif** (BSD-3-Clause) embarque dans ses roues binaires **libheif**
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

## Licences copyleft faibles (bibliothèques)

- inflate64 1.0.4 : LGPL-2.1-or-later
- libpff-python 20260926 : LGPL-3.0-or-later
- multivolumefile 0.2.3 : LGPL-2.1+
- py7zr 1.1.4 : LGPL-2.1-or-later
- pybcj 1.0.8 : LGPL-2.1-or-later
- pyppmd 1.3.1 : LGPL-2.1-or-later
- PySide6_Essentials 6.12.0 : LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only
- RTFDE 0.1.2.2 : GNU Lesser General Public License v3 (LGPLv3)
- shiboken6 6.12.0 : LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only
- tqdm 4.70.1 : MPL-2.0 AND MIT

## Modèles d'IA du catalogue

Les modèles ne sont pas inclus dans l'archive : ils sont téléchargés à la demande (ou importés hors ligne) après vérification en direct de leur licence sur Hugging Face. Les licences non commerciales sont refusées.

Aucun modèle dans le catalogue au moment de la génération.

## Composants Python

| Composant | Version | Licence | Adresse |
|---|---|---|---|
| annotated-doc | 0.0.5 | MIT | https://github.com/fastapi/annotated-doc |
| anyio | 4.15.1 | MIT | https://anyio.readthedocs.io/en/latest/ |
| backports.zstd | 1.8.0 | PSF-2.0 | https://github.com/rogdham/backports.zstd |
| beautifulsoup4 | 4.15.0 | MIT License | https://www.crummy.com/software/BeautifulSoup/bs4/ |
| brotli | 1.2.1 | MIT | https://github.com/google/brotli |
| cffi | 2.1.1 | MIT-0 | https://cffi.readthedocs.io/ |
| click | 8.5.0 | BSD-3-Clause | https://github.com/pallets/click/ |
| colorama | 0.4.6 | BSD License | https://github.com/tartley/colorama |
| colorclass | 2.2.2 | MIT | https://github.com/matthewdeanmartin/colorclass |
| compressed-rtf | 1.0.7 | MIT | https://github.com/delimitry/compressed_rtf |
| cryptography | 50.0.2 | Apache-2.0 OR BSD-3-Clause | https://github.com/pyca/cryptography |
| easygui | 0.98.3 | BSD | https://github.com/robertlugg/easygui |
| ebcdic | 2.0.1 | BSD-2-Clause | https://pypi.python.org/pypi/ebcdic |
| extract-msg | 0.58.0 | GPL | https://github.com/TeamMsgExtractor/msg-extractor |
| Faker | 40.44.0 | MIT License | https://github.com/joke2k/faker |
| filelock | 4.2.5 | MIT | https://github.com/tox-dev/filelock |
| flatbuffers | 25.12.19 | Apache 2.0 | https://google.github.io/flatbuffers/ |
| fsspec | 2026.9.0 | BSD-3-Clause | https://github.com/fsspec/filesystem_spec |
| h11 | 0.16.0 | MIT | https://github.com/python-hyper/h11 |
| hf-xet | 1.8.0 | Apache-2.0 | https://github.com/huggingface/xet-core |
| httpcore2 | 2.16.0 | BSD-3-Clause | https://github.com/pydantic/httpx2 |
| httpx2 | 2.16.0 | BSD-3-Clause | https://github.com/pydantic/httpx2 |
| huggingface_hub | 2.4.0 | Apache-2.0 | https://github.com/huggingface/huggingface_hub |
| idna | 3.20 | BSD-3-Clause | https://github.com/kjd/idna |
| inflate64 | 1.0.4 | LGPL-2.1-or-later | https://inflate64.readthedocs.io/ |
| lark | 1.3.1 | MIT | https://github.com/lark-parser/lark |
| libpff-python | 20260926 | LGPL-3.0-or-later | https://github.com/libyal/libpff |
| lxml | 6.1.3 | BSD-3-Clause | https://lxml.de/ |
| markdown-it-py | 4.2.0 | MIT License | https://github.com/executablebooks/markdown-it-py |
| mdurl | 0.1.2 | MIT License | https://github.com/executablebooks/mdurl |
| mpmath | 1.3.0 | BSD | http://mpmath.org/ |
| msoffcrypto-tool | 6.0.0 | MIT | https://github.com/nolze/msoffcrypto-tool |
| multivolumefile | 0.2.3 | LGPL-2.1+ | https://github.com/miurahr/multivolume |
| numpy | 2.5.4 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | https://numpy.org |
| olefile | 0.47 | BSD | https://www.decalage.info/python/olefileio |
| oletools | 0.60.2 | BSD | https://github.com/decalage2/oletools |
| onnxruntime-directml | 1.24.4 | MIT License | https://onnxruntime.ai |
| opencv-python-headless | 5.0.0.93 | Apache 2.0 | https://github.com/opencv/opencv-python |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause | https://github.com/pypa/packaging |
| pcodedmp | 1.2.6 | GPL | https://github.com/bontchev/pcodedmp |
| pillow | 13.0.0 | MIT-CMU | https://python-pillow.org |
| pillow_heif | 1.8.1 | BSD-3-Clause | https://github.com/bigcat88/pillow_heif |
| protobuf | 7.36.2 | 3-Clause BSD License | https://developers.google.com/protocol-buffers/ |
| psutil | 7.2.2 | BSD-3-Clause | https://github.com/giampaolo/psutil |
| py7zr | 1.1.4 | LGPL-2.1-or-later | https://py7zr.readthedocs.io/ |
| pybcj | 1.0.8 | LGPL-2.1-or-later | https://pypi.org/project/pybcj |
| pyclipper | 1.4.0 | MIT | https://github.com/fonttools/pyclipper |
| pycparser | 3.12 | BSD-3-Clause | https://github.com/eliben/pycparser |
| pycryptodomex | 3.24.1 | BSD, Public Domain | https://www.pycryptodome.org |
| Pygments | 2.21.0 | BSD-2-Clause | https://pygments.org |
| pymupdf | 1.28.2 | Dual Licensed - GNU AFFERO GPL 3.0 or Artifex Commercial License | https://github.com/pymupdf/pymupdf |
| pyparsing | 3.3.3 | MIT | https://github.com/pyparsing/pyparsing/ |
| pyppmd | 1.3.1 | LGPL-2.1-or-later | https://pyppmd.readthedocs.io/ |
| PySide6_Essentials | 6.12.0 | LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only | https://pyside.org |
| PyYAML | 6.0.3 | MIT | https://pyyaml.org/ |
| rapidocr-onnxruntime | 1.4.4 | Apache-2.0 | https://github.com/RapidAI/RapidOCR |
| red-black-tree-mod | 1.22 | MIT | http://stromberg.dnsalias.org/~strombrg/red-black-tree-mod/ |
| rich | 15.0.0 | MIT | https://github.com/Textualize/rich |
| RTFDE | 0.1.2.2 | GNU Lesser General Public License v3 (LGPLv3) | https://github.com/seamustuohy/RTFDE |
| shapely | 2.2.0 | BSD-3-Clause | https://github.com/shapely/shapely |
| shellingham | 1.5.4 | ISC License | https://github.com/sarugaku/shellingham |
| shiboken6 | 6.12.0 | LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only | https://pyside.org |
| six | 1.17.0 | MIT | https://github.com/benjaminp/six |
| soupsieve | 3.0.4 | MIT | https://github.com/facelessuser/soupsieve |
| sympy | 1.14.0 | BSD | https://sympy.org |
| texttable | 1.7.2 | MIT | https://github.com/foutaise/texttable/ |
| tqdm | 4.70.1 | MPL-2.0 AND MIT | https://tqdm.github.io |
| truststore | 0.10.5 | MIT | https://github.com/sethmlarson/truststore |
| typer | 0.27.3 | MIT | https://github.com/fastapi/typer |
| typing_extensions | 4.16.0 | PSF-2.0 | https://github.com/python/typing_extensions |
| tzdata | 2026.6 | Apache-2.0 | https://github.com/python/tzdata |
| tzlocal | 5.4.5 | MIT | https://github.com/regebro/tzlocal |
| win_unicode_console | 0.5 | MIT | https://github.com/Drekin/win-unicode-console |
| xlrd | 2.0.2 | BSD | http://www.python-excel.org/ |

## Autres composants

| Composant | Licence | Adresse |
|---|---|---|
| Python | PSF-2.0 | https://docs.python.org/3/license.html |
| Qt 6 (via PySide6) | LGPL-3.0 — bibliothèques livrées séparément dans le dossier, remplaçables | https://www.qt.io/licensing/ |
| Chargeur PyInstaller | GPL-2.0 avec exception explicite autorisant la diffusion d'applications | https://pyinstaller.org/en/stable/license.html |
| DirectML (Windows, via onnxruntime-directml) | MIT | https://github.com/microsoft/DirectML |
| LibreOffice (embarqué dans `libreoffice/`) | MPL-2.0 ; composants tiers : voir `libreoffice/LICENSE.html`, `NOTICE`, `CREDITS.fodt` et `readmes/` (macOS : `LibreOffice.app/Contents/Resources/`) | https://www.libreoffice.org/about-us/licenses/ |
| Modèles OCR PP-OCR (inclus dans rapidocr-onnxruntime) | Apache-2.0 | https://github.com/PaddlePaddle/PaddleOCR |
