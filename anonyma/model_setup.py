"""Automatic model management: the application decides which models this machine
needs, installs them, replaces outdated ones and removes what is no longer used.

The user never picks a model. ``wanted_models`` derives the set from the hardware
(``hardware.recommend_profile`` / ``recommend_llm``) and from the engines this build
ships and that load on this machine (no language model is fetched where
llama-cpp-python is missing or refused). ``plan`` compares it with ``models/``:

* a wanted model missing, or installed with files that no longer match the catalogue
  (new revision after an update of Anonyma, altered file), is (re)installed;
* an installed model the machine no longer needs (another size of language model, a
  model dropped from the catalogue) is removed, once every wanted model is in place,
  so detection never loses a capability on the way.

Network access only happens in ``ModelManager.download`` (inside
``netguard.allow_network()``); a failure leaves the installed models untouched and the
next start tries again (partial downloads resume).
"""

from __future__ import annotations

import functools
import importlib
import importlib.util
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from anonyma import hardware, model_catalog
from anonyma.logs import get_logger
from anonyma.model_catalog import ModelKind, ModelSpec
from anonyma.model_manager import (
    DownloadCancelledError,
    DownloadProgress,
    ModelError,
    ModelManager,
)
from anonyma.models import Profile

log = get_logger("model_setup")

#: Engine (detector name) each model kind runs on.
_ENGINE_OF_KIND = {ModelKind.GLINER: "gliner", ModelKind.LLM: "llm"}
_ENGINE_MODULES = {"gliner": ("onnxruntime", "tokenizers"), "llm": ("llama_cpp",)}

#: (model, bytes done, bytes total) -> None
SetupProgress = Callable[[ModelSpec, int, int], None]


#: Engines loaded for real before being reported available: their native library may
#: be shipped yet fail to load on this machine (missing instruction set, runtime).
_LOADED_ENGINES = frozenset({"llm"})


def engine_shipped(detector: str) -> bool:
    """Whether the libraries of a model-based detector are present in this build."""
    modules = _ENGINE_MODULES.get(detector, ())
    return all(importlib.util.find_spec(m) is not None for m in modules)


@functools.cache
def engine_available(detector: str) -> bool:
    """Whether this build ships the library a model-based detector runs on (a model
    may be installed while its engine is not), and, for a native engine, whether it
    loads on this machine: no model is fetched for an engine that cannot run."""
    if not engine_shipped(detector):
        return False
    if detector not in _LOADED_ENGINES:
        return True
    try:
        for module in _ENGINE_MODULES[detector]:
            importlib.import_module(module)
    except Exception as exc:  # native library refused: OSError, RuntimeError...
        log.warning("engine %s does not load: %s", detector, type(exc).__name__)
        return False
    return True


def wanted_models(info: hardware.HardwareInfo) -> list[ModelSpec]:
    """Models this machine should have, smallest first (the quickest gain first)."""
    profile = hardware.recommend_profile(info)
    wanted: list[ModelSpec] = []
    ocr = model_catalog.get_spec("ocr-latin-ppocrv5")
    if ocr is not None:
        wanted.append(ocr)
    if profile.rank >= Profile.STANDARD.rank and engine_available("gliner"):
        gliner = model_catalog.get_spec(model_catalog.DEFAULT_GLINER)
        if gliner is not None:
            wanted.append(gliner)
    if profile is Profile.FULL and engine_available("llm"):
        llm_id = hardware.recommend_llm(info)
        llm = model_catalog.get_spec(llm_id) if llm_id else None
        if llm is not None:
            wanted.append(llm)
    return sorted(wanted, key=lambda s: sum(f.size for f in s.files))


@dataclass(frozen=True, slots=True)
class SetupPlan:
    #: Every model the machine uses (installed or not), smallest first.
    wanted: tuple[ModelSpec, ...]
    #: Wanted models to download: missing, or outdated / altered.
    install: tuple[ModelSpec, ...]
    #: Folder names under ``models/`` to remove once ``install`` is done.
    obsolete: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return not self.install and not self.obsolete

    @property
    def download_bytes(self) -> int:
        return sum(f.size for spec in self.install for f in spec.files)


def plan(manager: ModelManager, info: hardware.HardwareInfo | None = None) -> SetupPlan:
    info = info or hardware.detect_hardware()
    wanted = wanted_models(info)
    install = tuple(spec for spec in wanted if not manager.status(spec.id).verified)
    wanted_ids = {spec.id for spec in wanted}
    obsolete = tuple(name for name in manager.installed_folders() if name not in wanted_ids)
    return SetupPlan(tuple(wanted), install, obsolete)


@dataclass(slots=True)
class SetupResult:
    installed: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    #: French, value-free messages (download refused, network down, folder in use...).
    errors: list[str] = field(default_factory=list)
    cancelled: bool = False

    @property
    def ok(self) -> bool:
        return not self.errors and not self.cancelled


def _bind(progress: SetupProgress | None, spec: ModelSpec) -> DownloadProgress | None:
    if progress is None:
        return None
    return lambda done, total: progress(spec, done, total)


def run(
    manager: ModelManager,
    setup: SetupPlan,
    *,
    download: bool = True,
    progress: SetupProgress | None = None,
    cancel: Callable[[], bool] | None = None,
) -> SetupResult:
    """Install ``setup.install`` (when ``download``), then remove ``setup.obsolete``.

    Obsolete models are removed only when every wanted model is installed: a machine
    keeps its previous language model until the new one is ready.
    """
    result = SetupResult()
    manager.clean_staging(setup.wanted)
    missing = False
    for spec in setup.install if download else ():
        try:
            manager.download(spec.id, progress=_bind(progress, spec), cancel=cancel)
        except DownloadCancelledError:
            result.cancelled = True
            return result
        except ModelError as exc:
            log.warning("automatic install of %s failed: %s", spec.id, type(exc).__name__)
            result.errors.append(str(exc))
            missing = True
            continue
        result.installed.append(spec.id)
    if not download and setup.install:
        missing = True
    if missing:
        return result
    for name in setup.obsolete:
        try:
            manager.remove_folder(name)
        except ModelError as exc:  # in use: removed at a later start
            log.warning("obsolete model %s kept: %s", name, type(exc).__name__)
            result.errors.append(str(exc))
            continue
        result.removed.append(name)
    return result


def import_from(manager: ModelManager, setup: SetupPlan, sources: list[Path]) -> SetupResult:
    """Offline install: take each model ``setup`` still needs from ``sources`` (another
    computer's ``models`` folder, a USB key...), matched by SHA-256, then remove the
    obsolete ones when nothing is missing any more. ``errors`` names what was not found."""
    result = SetupResult()
    missing: list[str] = []
    for spec in setup.install:
        try:
            manager.import_files(spec.id, sources)
        except ModelError as exc:
            log.info("offline import of %s: %s", spec.id, type(exc).__name__)
            missing.append(spec.title)
            continue
        result.installed.append(spec.id)
    if missing:
        result.errors.append("Fichiers introuvables dans ce dossier pour : " + ", ".join(missing))
        return result
    for name in setup.obsolete:
        try:
            manager.remove_folder(name)
        except ModelError as exc:
            result.errors.append(str(exc))
            continue
        result.removed.append(name)
    return result


__all__ = [
    "SetupPlan",
    "SetupProgress",
    "SetupResult",
    "engine_available",
    "engine_shipped",
    "import_from",
    "plan",
    "run",
    "wanted_models",
]
