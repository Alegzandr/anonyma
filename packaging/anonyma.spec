# PyInstaller spec: one portable "onedir" folder holding two executables that
# share the same libraries:
#
#   Anonyma[.exe]       windowed GUI   (anonyma.app:main)
#   anonyma-cli[.exe]   console CLI    (anonyma.cli:main)
#   _internal/          shared Python runtime, libraries and data
#
# macOS: the folder is additionally wrapped in Anonyma.app (both executables in
# Contents/MacOS); packaging/build.py then moves the .app into the portable
# folder, next to models/ and data/ (paths.app_root() resolves to the folder that
# contains the .app, never to the inside of the bundle).
#
# Build with:  uv run python packaging/build.py   (or: uv run pyinstaller packaging/anonyma.spec)
# ruff: noqa

import importlib.util
import re
import sys
from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
    copy_metadata,
)

ROOT = Path(SPECPATH).resolve().parent  # noqa: F821 (SPECPATH is injected by PyInstaller)
sys.path.insert(0, str(ROOT))

APP_NAME = "Anonyma"
CLI_NAME = "anonyma-cli"
# Generated from anonyma/brand.py by packaging/gen_icons.py.
ICONS = ROOT / "packaging" / "icons"
EXE_ICON = str(ICONS / "anonyma.ico") if sys.platform == "win32" else None


def have(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


# -- hidden imports ------------------------------------------------------------
# Plugins are discovered with pkgutil at runtime: PyInstaller cannot see them.
hiddenimports = collect_submodules("anonyma")
for package in ("anonyma.extractors", "anonyma.writers", "anonyma.detectors"):
    hiddenimports += collect_submodules(package)

datas = collect_data_files("anonyma")  # anonyma/data/** and any package data
binaries = []

# Qt's own French texts (standard buttons, context menus): loaded by anonyma.app at
# start-up from PySide6/translations. Listed explicitly so that the file is shipped
# whatever the PyInstaller Qt hooks decide to collect.
import PySide6  # noqa: E402

QT_TRANSLATIONS = Path(PySide6.__file__).resolve().parent / "translations"
for name in ("qtbase_fr.qm",):
    if (QT_TRANSLATIONS / name).is_file():
        datas.append((str(QT_TRANSLATIONS / name), "PySide6/translations"))

# Faker loads its locale providers dynamically (the contrib hook also collects
# them; listed explicitly so that fr_FR never goes missing).
LOCALE_MODULE = re.compile(r"^[a-z]{2,3}_[A-Z]{2}$")
KEPT_LOCALES = {"fr_FR", "en_US"}


def wanted_provider(name: str) -> bool:
    last = name.rsplit(".", 1)[-1]
    return not LOCALE_MODULE.match(last) or last in KEPT_LOCALES


if have("faker"):
    hiddenimports += collect_submodules("faker.providers", filter=wanted_provider)

# Optional heavy dependencies added by other workstreams: collected only when
# installed, so the spec works at every stage of the project.
OPTIONAL = {
    # module: (submodules, data files, dynamic libs, metadata distribution)
    # OCR: PP-OCR models (*.onnx) and config.yaml live inside the package.
    "rapidocr_onnxruntime": (True, True, False, "rapidocr-onnxruntime"),
    "rapidocr": (True, True, False, "rapidocr"),
    "onnxruntime": (False, False, True, None),
    "cv2": (False, False, False, None),  # contrib hook (opencv-python-headless)
    "pyclipper": (False, False, True, None),
    "shapely": (False, False, False, None),  # contrib hook (GEOS libs)
    # Images.
    "pillow_heif": (True, True, True, "pillow-heif"),
    # PDF (PyMuPDF, AGPL-3.0).
    "pymupdf": (True, True, True, "pymupdf"),  # (the deprecated ``fitz`` shim is not used)
    # Mails: extract-msg pulls RTFDE (lark grammars), oletools, compressed_rtf, ebcdic.
    "extract_msg": (True, True, False, "extract-msg"),
    "RTFDE": (True, True, False, "RTFDE"),
    "lark": (True, True, False, "lark"),
    "oletools": (True, True, False, "oletools"),
    "compressed_rtf": (True, False, False, None),
    "ebcdic": (True, False, False, None),
    "tzlocal": (True, False, False, None),
    "bs4": (False, False, False, None),
    "pypff": (False, False, False, "libpff-python"),
    # Archives: py7zr loads its codecs lazily.
    "py7zr": (True, False, False, "py7zr"),
    "pyzstd": (True, False, False, None),
    "pyppmd": (True, False, False, None),
    "pybcj": (True, False, False, None),
    "inflate64": (True, False, False, None),
    "multivolumefile": (True, False, False, None),
    "brotli": (False, False, False, None),
    # Office / data formats.
    "lxml": (False, False, False, None),  # PyInstaller hooks
    "xlrd": (True, False, False, None),
    "olefile": (True, False, False, None),
    # Future LLM detector.
    "llama_cpp": (True, True, True, None),
    "tokenizers": (False, False, True, None),
}
for module, (subs, data, dylibs, dist) in OPTIONAL.items():
    if not have(module):
        continue
    if subs:
        hiddenimports += collect_submodules(module)
    if data:
        datas += collect_data_files(module)
    if dylibs:
        binaries += collect_dynamic_libs(module)
    if dist:
        try:
            datas += copy_metadata(dist)
        except Exception:  # metadata not found under that name
            pass

EXCLUDES = [
    # Forbidden or useless in the frozen application.
    "torch",
    "tensorflow",
    "jax",
    "tkinter",
    "matplotlib",
    "IPython",
    "notebook",
    "pytest",
    "mypy",
    "ruff",
    "PyInstaller",
    # onnxruntime only needs sympy for its offline tools (shape inference).
    "sympy",
    "mpmath",
    # Downloads use plain HTTP Range requests (model_manager), not Xet storage.
    "hf_xet",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DRender",
    "PySide6.QtQuick3D",
    "PySide6.QtMultimedia",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtBluetooth",
    "PySide6.QtNfc",
    "PySide6.QtPositioning",
    "PySide6.QtLocation",
    "PySide6.QtSensors",
    "PySide6.QtSerialPort",
    "PySide6.QtWebSockets",
    "PySide6.QtHttpServer",
    # The application never opens a socket through Qt (updates and downloads use httpx2).
    "PySide6.QtNetwork",
]

common = dict(
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=sorted(set(hiddenimports)),
    hookspath=[],
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
)

gui_analysis = Analysis([str(ROOT / "packaging" / "launch_gui.py")], **common)  # noqa: F821
cli_analysis = Analysis([str(ROOT / "packaging" / "launch_cli.py")], **common)  # noqa: F821

# -- size: files collected by the hooks but never used ----------------------------------
# Matched against the destination path inside _internal/, "/" separators, lower case.
UNUSED_FILES = [
    re.compile(pattern)
    for pattern in (
        # Mesa software OpenGL fallback: the GUI is plain Qt Widgets (no OpenGL).
        r"(^|/)opengl32sw\.dll$",
        # OpenCV's FFmpeg video back end: OCR only decodes still images.
        r"(^|/)opencv_videoio_ffmpeg[^/]*$",
        # AVIF codec of Pillow: AVIF is not a supported format.
        r"^pil/_avif\.",
        # MuPDF C++ headers and import libraries (for building extensions only).
        r"^pymupdf/mupdf-devel/",
        # Qt network stack and its plugins (QtNetwork is excluded above).
        r"(^|/)(lib)?qt6network[^/]*$",
        r"^pyside6/plugins/(tls|networkinformation)/",
    )
]
QT_TRANSLATION = re.compile(r"^pyside6/translations/(?!qtbase_fr\.qm$)")


def wanted(entry: tuple[str, ...]) -> bool:
    dest = entry[0].replace("\\", "/").lower()
    return not QT_TRANSLATION.match(dest) and not any(p.search(dest) for p in UNUSED_FILES)


for analysis in (gui_analysis, cli_analysis):
    analysis.binaries = [e for e in analysis.binaries if wanted(e)]
    analysis.datas = [e for e in analysis.datas if wanted(e)]

gui_pyz = PYZ(gui_analysis.pure)  # noqa: F821
cli_pyz = PYZ(cli_analysis.pure)  # noqa: F821

gui_exe = EXE(  # noqa: F821
    gui_pyz,
    gui_analysis.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    icon=EXE_ICON,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    upx=False,
    codesign_identity=None,
    entitlements_file=None,
)
cli_exe = EXE(  # noqa: F821
    cli_pyz,
    cli_analysis.scripts,
    [],
    exclude_binaries=True,
    name=CLI_NAME,
    icon=EXE_ICON,
    console=True,
    upx=False,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(  # noqa: F821
    gui_exe,
    cli_exe,
    gui_analysis.binaries,
    gui_analysis.datas,
    cli_analysis.binaries,
    cli_analysis.datas,
    strip=False,
    upx=False,
    name=APP_NAME,
)

if sys.platform == "darwin":
    from anonyma import __version__

    app = BUNDLE(  # noqa: F821
        coll,
        name=f"{APP_NAME}.app",
        icon=str(ICONS / "anonyma.icns"),
        bundle_identifier="fr.anonyma.desktop",
        version=__version__,
        info_plist={
            "CFBundleName": APP_NAME,
            "CFBundleDisplayName": APP_NAME,
            "CFBundleShortVersionString": __version__,
            "NSHighResolutionCapable": True,
            "LSApplicationCategoryType": "public.app-category.productivity",
            "NSHumanReadableCopyright": "Usage interne uniquement (voir LICENSES.md)",
        },
    )
