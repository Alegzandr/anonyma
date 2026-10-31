"""Plugin registry for extractors, writers and detectors.

Adding a format or a detector means adding one module to ``anonyma/extractors``,
``anonyma/writers`` or ``anonyma/detectors`` and decorating its class. Modules are
imported automatically by :func:`load_plugins`; a module whose optional
dependency is missing is recorded as unavailable instead of crashing.
"""

from __future__ import annotations

import importlib
import pkgutil
import threading
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar

from anonyma.logs import get_logger
from anonyma.models import Detector, Extractor, Profile, Writer

if TYPE_CHECKING:
    from anonyma.config import Config

log = get_logger("registry")

_extractors: dict[str, type[Extractor]] = {}  # format id -> extractor class
_extensions: dict[str, str] = {}  # ".ext" -> format id
_sniffers: list[type[Extractor]] = []
_writers: dict[str, type[Writer]] = {}  # format id -> writer class
_detectors: dict[str, type[Detector]] = {}
#: Plugin module -> reason it could not be imported.
unavailable: dict[str, str] = {}

_loaded = False
_lock = threading.Lock()

E = TypeVar("E", bound=type[Extractor])
W = TypeVar("W", bound=type[Writer])
D = TypeVar("D", bound=type[Detector])

PLUGIN_PACKAGES = ("anonyma.extractors", "anonyma.writers", "anonyma.detectors")


def register_extractor[E: type[Extractor]](cls: E) -> E:
    for ext, fmt in cls.extensions.items():
        _extensions[ext.lower()] = fmt
        _extractors[fmt] = cls
    if cls.sniff.__func__ is not Extractor.sniff.__func__:  # type: ignore[attr-defined]
        _sniffers.append(cls)
    return cls


def register_writer[W: type[Writer]](cls: W) -> W:
    for fmt in cls.formats:
        _writers[fmt] = cls
    return cls


def register_detector[D: type[Detector]](cls: D) -> D:
    _detectors[cls.name] = cls
    return cls


def load_plugins() -> None:
    """Import every plugin module once (idempotent, thread-safe)."""
    global _loaded
    with _lock:
        if _loaded:
            return
        for package_name in PLUGIN_PACKAGES:
            package = importlib.import_module(package_name)
            for info in pkgutil.iter_modules(package.__path__):
                if info.name.startswith("_"):
                    continue
                module = f"{package_name}.{info.name}"
                try:
                    importlib.import_module(module)
                except ImportError as exc:
                    unavailable[module] = f"dépendance manquante : {exc.name or exc}"
                    log.warning("plugin %s unavailable: %s", module, exc)
        _loaded = True


def detect_format(path: Path, head: bytes | None = None) -> str | None:
    """Format id of ``path``: magic-byte sniffing first, then the extension."""
    load_plugins()
    if head is None:
        try:
            with path.open("rb") as fh:
                head = fh.read(8192)
        except OSError:
            head = b""
    for cls in _sniffers:
        fmt = cls.sniff(head)
        if fmt:
            return fmt
    return _extensions.get(path.suffix.lower())


def extractor_for(fmt: str) -> Extractor | None:
    load_plugins()
    cls = _extractors.get(fmt)
    return cls() if cls else None


def writer_for(fmt: str) -> Writer | None:
    load_plugins()
    cls = _writers.get(fmt)
    return cls() if cls else None


def supported_formats() -> dict[str, tuple[bool, bool]]:
    """Format id -> (has extractor, has writer)."""
    load_plugins()
    formats = set(_extractors) | set(_writers)
    return {f: (f in _extractors, f in _writers) for f in sorted(formats)}


def supported_extensions() -> dict[str, str]:
    load_plugins()
    return dict(_extensions)


def detector_classes() -> dict[str, type[Detector]]:
    load_plugins()
    return dict(_detectors)


def missing_detectors(config: Config, profile: Profile) -> list[str]:
    """Detectors ``profile`` enables whose dependency or model is missing (sorted names)."""
    load_plugins()
    return sorted(
        name
        for name, cls in _detectors.items()
        if cls.min_profile.rank <= profile.rank and not cls.is_available(config)
    )


def usable_profile(config: Config, profile: Profile) -> Profile:
    """The most thorough profile up to ``profile`` that installed models can serve.

    A profile above LIGHT is usable when at least one detector it adds is available
    (GLiNER for STANDARD, the LLM for FULL). Running FULL without its LLM would only
    cost speed (a single worker) and claim a detection that does not happen.
    """
    load_plugins()
    for candidate in (Profile.FULL, Profile.STANDARD):
        if candidate.rank > profile.rank:
            continue
        added = [cls for cls in _detectors.values() if cls.min_profile is candidate]
        if any(cls.is_available(config) for cls in added):
            return candidate
    return Profile.LIGHT


def build_detectors(config: Config, profile: Profile | None = None) -> list[Detector]:
    """Instantiate every detector enabled for ``profile`` and available."""
    load_plugins()
    profile = profile or config.effective_profile()
    chain: list[Detector] = []
    for name, cls in sorted(_detectors.items()):
        if cls.min_profile.rank > profile.rank:
            continue
        if not cls.is_available(config):
            log.warning("detector %s unavailable, skipped", name)
            continue
        chain.append(cls(config))
    return chain
