"""Hardware detection and profile recommendation.

Everything here is cheap (stdlib, ``ctypes``, the Windows registry, at most one
short ``nvidia-smi`` call on Linux) and never touches the network. Results are
cached for the process lifetime: call ``detect_hardware(refresh=True)`` to
re-probe.

ONNX Runtime packaging (see ``pyproject.toml``): ``onnxruntime-directml`` on
Windows (DirectML + CPU), ``onnxruntime`` elsewhere (CoreML + CPU on macOS, CPU on
Linux). ``providers`` lists what the installed wheel actually offers, best first,
so detectors can pass it straight to ``InferenceSession(providers=...)``.
"""

from __future__ import annotations

import ctypes
import functools
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from anonyma.logs import get_logger
from anonyma.models import Profile

log = get_logger("hardware")

GIB = 1024**3

#: Below this much RAM, only the LIGHT profile (regex and rules) is recommended.
STANDARD_MIN_RAM = 6 * GIB
#: The FULL profile (local LLM) needs at least this much RAM...
FULL_MIN_RAM = 16 * GIB
#: ... and this many logical CPUs (LLM inference on CPU is slow otherwise).
FULL_MIN_CPUS = 8

#: Preferred onnxruntime execution providers, best first. Unlisted providers
#: keep their onnxruntime order, before the CPU provider which always comes last.
PROVIDER_PREFERENCE = (
    "CUDAExecutionProvider",
    "DmlExecutionProvider",
    "CoreMLExecutionProvider",
    "ROCMExecutionProvider",
    "OpenVINOExecutionProvider",
)
CPU_PROVIDER = "CPUExecutionProvider"
#: Providers that mean "hardware acceleration is available".
ACCELERATED_PROVIDERS = frozenset(PROVIDER_PREFERENCE)

PROVIDER_LABELS = {
    "CUDAExecutionProvider": "NVIDIA CUDA",
    "DmlExecutionProvider": "DirectML",
    "CoreMLExecutionProvider": "Core ML",
    "ROCMExecutionProvider": "AMD ROCm",
    "OpenVINOExecutionProvider": "Intel OpenVINO",
    "CPUExecutionProvider": "Processeur (CPU)",
}


@dataclass(frozen=True, slots=True)
class HardwareInfo:
    ram_bytes: int = 0
    cpu_count: int = 1
    #: onnxruntime execution providers available, best first.
    providers: tuple[str, ...] = ("CPUExecutionProvider",)
    gpu_name: str = ""
    vram_bytes: int = 0
    notes: tuple[str, ...] = field(default_factory=tuple)
    #: RAM currently free (0 when unknown); informative only.
    available_ram_bytes: int = 0
    os_name: str = ""
    machine: str = ""
    onnxruntime_version: str = ""


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def detect_hardware(*, refresh: bool = False) -> HardwareInfo:
    """Probe the machine (cached; ``refresh=True`` probes again)."""
    if refresh:
        _detect.cache_clear()
    return _detect()


@functools.cache
def _detect() -> HardwareInfo:
    notes: list[str] = []
    total, available = _memory()
    if not total:
        notes.append("Mémoire vive non détectée.")
    providers, ort_version = _onnx_providers()
    if not ort_version:
        notes.append("ONNX Runtime absent : modèles d'IA indisponibles.")
    elif any(p in ACCELERATED_PROVIDERS for p in providers):
        labels = ", ".join(PROVIDER_LABELS.get(p, p) for p in providers if p != CPU_PROVIDER)
        notes.append(f"Accélération matérielle disponible : {labels}.")
    else:
        notes.append("Pas d'accélération matérielle : calculs sur le processeur.")
    gpu_name, vram = "", 0
    try:
        gpu_name, vram = _gpu()
    except Exception as exc:  # never fail on a best-effort probe
        log.info("GPU probe failed: %s", type(exc).__name__)
    if sys.platform == "darwin" and platform.machine() == "arm64":
        notes.append("Mémoire unifiée Apple Silicon : la mémoire graphique est la mémoire vive.")
    return HardwareInfo(
        ram_bytes=total,
        cpu_count=_cpu_count(),
        providers=providers,
        gpu_name=gpu_name,
        vram_bytes=vram,
        notes=tuple(notes),
        available_ram_bytes=available,
        os_name=platform.system(),
        machine=platform.machine(),
        onnxruntime_version=ort_version,
    )


def _cpu_count() -> int:
    process_cpu_count = getattr(os, "process_cpu_count", None)  # Python 3.13+
    count = process_cpu_count() if process_cpu_count else os.cpu_count()
    return max(1, count or 1)


def _memory() -> tuple[int, int]:
    """(total, available) physical memory in bytes; 0 when unknown."""
    try:
        if sys.platform == "win32":
            return _memory_windows()
        if sys.platform == "darwin":
            return _sysctl_int("hw.memsize"), 0
        return _memory_linux()
    except Exception as exc:
        log.info("memory probe failed: %s", type(exc).__name__)
        return 0, 0


def _memory_windows() -> tuple[int, int]:
    class MemoryStatusEx(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    status = MemoryStatusEx()
    status.dwLength = ctypes.sizeof(MemoryStatusEx)
    windll = getattr(ctypes, "windll", None)
    if windll is None or not windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return 0, 0
    return int(status.ullTotalPhys), int(status.ullAvailPhys)


def _memory_linux() -> tuple[int, int]:
    total = available = 0
    meminfo = Path("/proc/meminfo")
    if meminfo.is_file():
        for line in meminfo.read_text(encoding="ascii", errors="replace").splitlines():
            key, _, rest = line.partition(":")
            parts = rest.split()
            if not parts or not parts[0].isdigit():
                continue
            if key == "MemTotal":
                total = int(parts[0]) * 1024
            elif key == "MemAvailable":
                available = int(parts[0]) * 1024
    if not total and hasattr(os, "sysconf"):
        total = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    return total, available


def _sysctl_raw(name: str) -> bytes:
    """``sysctlbyname`` through libc (macOS); empty bytes on failure."""
    libc = ctypes.CDLL(None)
    size = ctypes.c_size_t(0)
    if libc.sysctlbyname(name.encode(), None, ctypes.byref(size), None, 0) != 0:
        return b""
    buffer = ctypes.create_string_buffer(size.value)
    if libc.sysctlbyname(name.encode(), buffer, ctypes.byref(size), None, 0) != 0:
        return b""
    return buffer.raw[: size.value]


def _sysctl_int(name: str) -> int:
    raw = _sysctl_raw(name)
    return int.from_bytes(raw, sys.byteorder) if raw else 0


def _sysctl_str(name: str) -> str:
    return _sysctl_raw(name).split(b"\0", 1)[0].decode("utf-8", errors="replace").strip()


def sort_providers(providers: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    """Order execution providers best first, CPU last (always present)."""
    rank = {name: i for i, name in enumerate(PROVIDER_PREFERENCE)}
    others = [p for p in providers if p != CPU_PROVIDER]
    others.sort(key=lambda p: rank.get(p, len(rank)))
    return (*dict.fromkeys(others), CPU_PROVIDER)


def _onnx_providers() -> tuple[tuple[str, ...], str]:
    try:
        import onnxruntime
    except (ImportError, OSError) as exc:
        log.info("onnxruntime unavailable: %s", type(exc).__name__)
        return (CPU_PROVIDER,), ""
    try:
        available = list(onnxruntime.get_available_providers())
    except Exception as exc:
        log.info("onnxruntime provider query failed: %s", type(exc).__name__)
        available = []
    return sort_providers(available), str(getattr(onnxruntime, "__version__", "?"))


# -- GPU (best effort, cheap) ---------------------------------------------------

_IGNORED_ADAPTERS = ("microsoft basic", "remote display", "virtual", "parsec", "citrix")


def _gpu() -> tuple[str, int]:
    if sys.platform == "win32":
        return _gpu_windows()
    if sys.platform == "darwin":
        if platform.machine() == "arm64":
            return _sysctl_str("machdep.cpu.brand_string"), 0
        return "", 0
    return _gpu_linux()


def _gpu_windows() -> tuple[str, int]:
    """Largest display adapter from the registry (name, dedicated memory)."""
    # A platform block (not an early return): mypy then skips it on other systems,
    # where winreg has no attributes.
    if sys.platform == "win32":
        import winreg

        def value(key: winreg.HKEYType, name: str) -> object:
            try:
                return winreg.QueryValueEx(key, name)[0]
            except OSError:
                return None

        def as_int(raw: object) -> int:
            if isinstance(raw, int):
                return raw
            if isinstance(raw, bytes) and raw:
                return int.from_bytes(raw[:8], "little")
            return 0

        base = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
        best: tuple[str, int] = ("", 0)
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base) as root:
            index = 0
            while True:
                try:
                    sub = winreg.EnumKey(root, index)
                except OSError:
                    break
                index += 1
                if not sub.isdigit():
                    continue
                try:
                    with winreg.OpenKey(root, sub) as key:
                        name = str(value(key, "DriverDesc") or "")
                        vram = as_int(value(key, "HardwareInformation.qwMemorySize")) or as_int(
                            value(key, "HardwareInformation.MemorySize")
                        )
                except OSError:
                    continue
                if not name or any(word in name.lower() for word in _IGNORED_ADAPTERS):
                    continue
                if vram > best[1] or not best[0]:
                    best = (name, vram)
        return best
    return "", 0


def _gpu_linux() -> tuple[str, int]:
    smi = shutil.which("nvidia-smi")
    if smi:
        try:
            out = subprocess.run(
                [smi, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                timeout=3,
                check=True,
            ).stdout
            first = out.strip().splitlines()[0]
            name, _, mib = first.rpartition(",")
            return name.strip(), int(float(mib.strip())) * 1024**2
        except (OSError, subprocess.SubprocessError, ValueError, IndexError):
            pass
    for card in sorted(Path("/sys/class/drm").glob("card[0-9]*")):
        vram_file = card / "device" / "mem_info_vram_total"  # amdgpu
        if vram_file.is_file():
            try:
                return "GPU AMD", int(vram_file.read_text().strip())
            except (OSError, ValueError):
                continue
    return "", 0


# ---------------------------------------------------------------------------
# Recommendations
# ---------------------------------------------------------------------------


def recommend_llm(info: HardwareInfo) -> str | None:
    """Catalogue id of the LLM suited to this machine (FULL profile), or None.

    The largest catalogue LLM whose ``min_ram`` fits in the machine's RAM (entries
    sorted by ``min_ram``, so this works whatever ids the catalogue uses).
    """
    from anonyma.model_catalog import CATALOG, ModelKind

    llms = sorted((m for m in CATALOG if m.kind is ModelKind.LLM), key=lambda m: m.min_ram)
    fitting = [m for m in llms if info.ram_bytes and m.min_ram <= info.ram_bytes]
    return fitting[-1].id if fitting else None


def recommend_profile(info: HardwareInfo) -> Profile:
    """LIGHT below ``STANDARD_MIN_RAM`` (or unknown RAM); FULL with enough RAM and
    CPUs and a catalogue LLM that fits; STANDARD otherwise."""
    if info.ram_bytes < STANDARD_MIN_RAM:
        return Profile.LIGHT
    if (
        info.ram_bytes >= FULL_MIN_RAM
        and info.cpu_count >= FULL_MIN_CPUS
        and recommend_llm(info) is not None
    ):
        return Profile.FULL
    return Profile.STANDARD


PROFILE_LABELS = {
    Profile.LIGHT: "Léger (expressions régulières et règles)",
    Profile.STANDARD: "Standard (+ GLiNER)",
    Profile.FULL: "Complet (+ modèle de langage local)",
}


def format_bytes(n: int) -> str:
    """Human-readable size in French (``"15,8 Go"``)."""
    if n <= 0:
        return "inconnue"
    value = float(n)
    for unit in ("o", "Ko", "Mo", "Go", "To"):
        if value < 1024 or unit == "To":
            text = f"{value:.0f}" if unit in ("o", "Ko") else f"{value:.1f}"
            return f"{text.replace('.', ',')} {unit}"
        value /= 1024
    return f"{n} o"  # pragma: no cover


def summary(info: HardwareInfo) -> list[tuple[str, str]]:
    """French ``(label, value)`` lines describing the machine (CLI, GUI)."""
    llm = recommend_llm(info)
    lines = [
        ("Système", f"{info.os_name} {info.machine}".strip() or "inconnu"),
        ("Mémoire vive", format_bytes(info.ram_bytes)),
        ("Processeurs logiques", str(info.cpu_count)),
        ("Carte graphique", info.gpu_name or "non détectée"),
    ]
    if info.vram_bytes:
        lines.append(("Mémoire graphique", format_bytes(info.vram_bytes)))
    lines += [
        ("ONNX Runtime", info.onnxruntime_version or "absent"),
        ("Fournisseurs d'exécution", ", ".join(PROVIDER_LABELS.get(p, p) for p in info.providers)),
        ("Profil recommandé", PROFILE_LABELS[recommend_profile(info)]),
        ("Modèle de langage recommandé", llm or "aucun"),
    ]
    return lines
