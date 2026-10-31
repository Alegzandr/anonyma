"""Model download (resumable, hash-checked, license-checked) and offline import.

Layout under ``paths.models_dir()``::

    models/<spec.id>/<repo-relative files>      installed model
    models/<spec.id>/.anonyma-model.json        manifest + verification cache
    models/.staging/<spec.id>-<revision>/       partial downloads (kept for resume)
    models/hf/                                  Hugging Face cache (HF_HOME)

Network access only happens in :meth:`ModelManager.check_license` and
:meth:`ModelManager.download`, both explicit user actions, inside
``netguard.allow_network()``. Downloads go through the Hugging Face session
(``huggingface_hub.get_session``) with HTTP ``Range`` requests so an interrupted
or cancelled download resumes where it stopped, at the pinned revision. Every
file is checked against its SHA-256 before the model folder is swapped in
atomically.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import threading
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from anonyma import model_catalog, netguard, paths
from anonyma.logs import get_logger
from anonyma.model_catalog import ModelSpec

log = get_logger("model_manager")

#: (bytes done, bytes total) -> None
DownloadProgress = Callable[[int, int], None]

MANIFEST = ".anonyma-model.json"
STAGING = ".staging"
_CHUNK = 1024 * 1024
_HASH_BLOCK = 4 * 1024 * 1024
_IN_USE = (
    "Impossible de remplacer ou supprimer le dossier du modèle : il est peut-être utilisé "
    "par un traitement en cours. Réessayez après l'avoir terminé."
)


class ModelError(Exception):
    """Base error; ``str(exc)`` is a French user-facing message."""


class LicenseRefusedError(ModelError):
    pass


class IntegrityError(ModelError):
    pass


class DownloadCancelledError(ModelError):
    """The user cancelled; partial files are kept so the next attempt resumes."""


@dataclass(frozen=True, slots=True)
class LicenseCheck:
    allowed: bool
    license: str
    reason: str  # French


@dataclass(frozen=True, slots=True)
class ModelStatus:
    spec: ModelSpec
    installed: bool
    path: Path | None
    #: All files present with the expected SHA-256.
    verified: bool
    size_on_disk: int = 0


# ---------------------------------------------------------------------------
# License policy
# ---------------------------------------------------------------------------

#: License ids refused outright (non-commercial, research-only, non-production).
REFUSED_LICENSES = frozenset(
    {
        "mrl",  # Mistral Research License
        "mnpl",  # Mistral Non-Production License
        "deepfloyd-if-license",
        "fair-noncommercial-research-license",
        "c-uda",
        "cc-by-nc-4.0",
        "cc-by-nc-sa-4.0",
        "cc-by-nc-nd-4.0",
        "cc-by-nc-2.0",
        "cc-by-nc-3.0",
        "cc-by-nc-sa-2.0",
        "cc-by-nc-sa-3.0",
        "cc-by-nc-nd-3.0",
    }
)
_NON_COMMERCIAL = re.compile(
    r"(^|[-_.])nc($|[-_.])|non-?commercial|research|non-?production|academic|evaluation"
)
#: Licenses that say nothing usable; refused unless allowlisted in the spec.
UNSPECIFIED_LICENSES = frozenset({"other", "unknown", "custom", "proprietary", ""})
#: ``ModelSpec.extra`` key: comma-separated license ids explicitly accepted (e.g.
#: ``"other"`` for a vetted custom license).
ALLOWLIST_KEY = "license_allowlist"


def _normalize_license(value: str) -> str:
    return value.strip().lower().replace(" ", "-")


def is_non_commercial(license_id: str) -> bool:
    lic = _normalize_license(license_id)
    return (
        lic in REFUSED_LICENSES or lic.startswith("cc-by-nc") or bool(_NON_COMMERCIAL.search(lic))
    )


def evaluate_license(spec: ModelSpec, live: Sequence[str]) -> LicenseCheck:
    """Apply the license policy to the licenses read live on Hugging Face."""
    found = sorted({_normalize_license(x) for x in live if x and x.strip()})
    shown = ", ".join(found) or "aucune"
    expected = _normalize_license(spec.license)
    allowlist = {
        _normalize_license(x) for x in spec.extra.get(ALLOWLIST_KEY, "").split(",") if x.strip()
    }
    if not found:
        return LicenseCheck(False, shown, "Aucune licence n'est déclarée sur Hugging Face.")
    for lic in found:
        if is_non_commercial(lic):
            return LicenseCheck(
                False,
                shown,
                f"Licence « {lic} » refusée : usage non commercial ou recherche uniquement.",
            )
    for lic in found:
        if lic in UNSPECIFIED_LICENSES and lic not in allowlist:
            return LicenseCheck(
                False, shown, f"Licence « {lic} » non identifiée : refusée par précaution."
            )
    if found != [expected]:
        return LicenseCheck(
            False,
            shown,
            f"La licence publiée ({shown}) diffère de la licence attendue ({expected}).",
        )
    return LicenseCheck(
        True, shown, f"Licence « {expected} » compatible avec un usage professionnel."
    )


def live_licenses(info: Any) -> list[str]:
    """Licenses declared by a ``huggingface_hub.ModelInfo``: card data ``license``
    (and ``license_name`` for ``other``) plus ``license:*`` tags."""
    found: list[str] = []
    card = getattr(info, "card_data", None) or getattr(info, "cardData", None)
    if card is not None:
        get = card.get if hasattr(card, "get") else lambda k, d=None: getattr(card, k, d)
        value = get("license", None)
        if isinstance(value, str):
            found.append(value)
        elif isinstance(value, list | tuple):
            found.extend(str(v) for v in value)
    for tag in getattr(info, "tags", None) or []:
        if isinstance(tag, str) and tag.startswith("license:"):
            found.append(tag.removeprefix("license:"))
    return found


# ---------------------------------------------------------------------------
# Transport (injectable for tests)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RemoteStream:
    #: Offset the server actually resumed from (0 when it ignored the Range).
    offset: int
    #: Full size of the remote file when known.
    total: int | None
    chunks: Iterator[bytes]


class Transport(Protocol):
    def stream(
        self, spec: ModelSpec, filename: str, start: int
    ) -> contextlib.AbstractContextManager[RemoteStream]:
        """Open ``filename`` of ``spec.repo_id`` at ``spec.revision`` from ``start``."""
        ...


class HubApi(Protocol):
    def model_info(self, repo_id: str, *, revision: str | None = None) -> Any: ...


class HfTransport:
    """HTTP ``Range`` downloads through the Hugging Face session."""

    @contextlib.contextmanager
    def stream(self, spec: ModelSpec, filename: str, start: int) -> Iterator[RemoteStream]:
        from huggingface_hub import get_session, hf_hub_url

        url = hf_hub_url(spec.repo_id, filename, revision=spec.revision)
        # No token is ever sent: only public repositories are supported.
        headers = {"user-agent": "anonyma"}
        if start > 0:
            headers["Range"] = f"bytes={start}-"
        with get_session().stream(
            "GET", url, headers=headers, follow_redirects=True, timeout=60.0
        ) as response:
            if response.status_code == 416:  # nothing left to send
                yield RemoteStream(start, start, iter(()))
                return
            if response.status_code >= 400:
                raise ModelError(
                    f"Téléchargement refusé par Hugging Face (code HTTP {response.status_code})."
                )
            offset = start if response.status_code == 206 else 0
            total: int | None = None
            content_range = response.headers.get("Content-Range", "")
            if "/" in content_range and content_range.rsplit("/", 1)[1].isdigit():
                total = int(content_range.rsplit("/", 1)[1])
            elif (length := response.headers.get("Content-Length", "")).isdigit():
                total = offset + int(length)
            yield RemoteStream(offset, total, response.iter_bytes(_CHUNK))


_hf_lock = threading.Lock()
_hf_online = 0
_hf_saved_offline = True


@contextlib.contextmanager
def _online() -> Iterator[None]:
    """``netguard.allow_network()`` plus lifting huggingface_hub's offline mode,
    which the library reads once at import time (``constants.HF_HUB_OFFLINE``)."""
    global _hf_online, _hf_saved_offline
    with netguard.allow_network():
        from huggingface_hub import constants

        with _hf_lock:
            if _hf_online == 0:
                _hf_saved_offline = constants.HF_HUB_OFFLINE
            _hf_online += 1
            constants.HF_HUB_OFFLINE = False
        try:
            yield
        finally:
            with _hf_lock:
                _hf_online -= 1
                if _hf_online == 0:
                    constants.HF_HUB_OFFLINE = _hf_saved_offline


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------


def sha256_file(path: Path, cancel: Callable[[], bool] | None = None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while block := fh.read(_HASH_BLOCK):
            if cancel and cancel():
                raise DownloadCancelledError("Opération annulée.")
            digest.update(block)
    return digest.hexdigest()


def _safe_relative(filename: str) -> PurePosixPath:
    rel = PurePosixPath(filename.replace("\\", "/"))
    if rel.is_absolute() or ".." in rel.parts or not rel.parts or ":" in rel.parts[0]:
        raise ModelError("Chemin de fichier invalide dans le catalogue.")
    return rel


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class ModelManager:
    def __init__(
        self,
        root: Path | None = None,
        *,
        api: HubApi | None = None,
        transport: Transport | None = None,
    ) -> None:
        self.root = root or paths.models_dir()
        self._api = api
        self._transport = transport or HfTransport()
        self._lock = threading.RLock()

    # -- catalogue / local state ----------------------------------------------

    def catalog(self) -> list[ModelSpec]:
        return list(model_catalog.CATALOG)

    def _spec(self, model_id: str) -> ModelSpec:
        spec = model_catalog.get_spec(model_id)
        if spec is None:
            raise ModelError(f"Modèle inconnu : {model_id}")
        return spec

    def path(self, model_id: str) -> Path | None:
        """Local folder of an installed model (files at their repo-relative paths)."""
        candidate = self.root / model_id
        return candidate if candidate.is_dir() else None

    def is_installed(self, model_id: str) -> bool:
        return self.path(model_id) is not None

    def status(self, model_id: str) -> ModelStatus:
        spec = self._spec(model_id)
        path = self.path(model_id)
        if path is None:
            return ModelStatus(spec, False, None, verified=False)
        verified, size = self._verify_installed(spec, path)
        return ModelStatus(spec, True, path, verified=verified, size_on_disk=size)

    def statuses(self) -> list[ModelStatus]:
        return [self.status(m.id) for m in model_catalog.CATALOG]

    def _verify_installed(self, spec: ModelSpec, folder: Path) -> tuple[bool, int]:
        """Check every expected file; SHA-256 results are cached in the manifest and
        recomputed only when a file's size or mtime changes."""
        manifest = _read_manifest(folder)
        raw_cache = manifest.get("files")
        cache: dict[str, Any] = raw_cache if isinstance(raw_cache, dict) else {}
        verified = bool(spec.files)
        size = 0
        changed = False
        for mf in spec.files:
            target = folder / _safe_relative(mf.filename)
            try:
                st = target.stat()
            except OSError:
                verified = False
                continue
            size += st.st_size
            entry = cache.get(mf.filename)
            stamp = [st.st_size, st.st_mtime_ns]
            if isinstance(entry, dict) and entry.get("stat") == stamp:
                digest = str(entry.get("sha256", ""))
            else:
                digest = sha256_file(target)
                cache[mf.filename] = {"stat": stamp, "sha256": digest}
                changed = True
            if not mf.sha256 or digest != mf.sha256.lower():
                verified = False
        if changed:
            with contextlib.suppress(OSError):
                _write_manifest(folder, {"id": spec.id, **manifest, "files": cache})
        return verified, size

    # -- license -----------------------------------------------------------------

    def _hub(self) -> HubApi:
        if self._api is None:
            from huggingface_hub import HfApi

            self._api = HfApi(token=False, library_name="anonyma")
        return self._api

    def check_license(self, model_id: str) -> LicenseCheck:
        """Read the live license on Hugging Face (network, explicit user action)."""
        spec = self._spec(model_id)
        try:
            with _online():
                info = self._hub().model_info(spec.repo_id, revision=spec.revision)
        except ModelError:
            raise
        except Exception as exc:
            log.warning("license check failed for %s: %s", spec.id, type(exc).__name__)
            raise ModelError(
                "Impossible de lire la licence sur Hugging Face (connexion ou dépôt "
                f"indisponible : {type(exc).__name__})."
            ) from exc
        return evaluate_license(spec, live_licenses(info))

    # -- download ------------------------------------------------------------------

    def download(
        self,
        model_id: str,
        progress: DownloadProgress | None = None,
        cancel: Callable[[], bool] | None = None,
    ) -> Path:
        """Check license, download with resume, verify SHA-256. Raises ModelError.

        Cancellation raises :class:`DownloadCancelledError`; partial files are kept
        under ``models/.staging`` and the next call resumes them.
        """
        spec = self._spec(model_id)
        self._require_hashes(spec)
        check = self.check_license(model_id)
        if not check.allowed:
            raise LicenseRefusedError(check.reason)
        with self._lock:
            staging = self._staging(spec)
            total = sum(mf.size for mf in spec.files)
            done = 0
            for mf in spec.files:
                done = self._fetch_file(
                    spec, mf.filename, mf.size, staging, done, total, progress, cancel
                )
            self._verify_folder(spec, staging, cancel)
            return self._install(spec, staging)

    def _require_hashes(self, spec: ModelSpec) -> None:
        if not spec.files:
            raise IntegrityError(f"Le modèle « {spec.title} » ne déclare aucun fichier.")
        missing = [mf.filename for mf in spec.files if not mf.sha256]
        if missing:
            raise IntegrityError(
                f"Empreinte SHA-256 manquante dans le catalogue pour : {', '.join(missing)}."
            )

    def _staging(self, spec: ModelSpec) -> Path:
        folder = self.root / STAGING / f"{spec.id}-{spec.revision[:12] or 'latest'}"
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def _fetch_file(
        self,
        spec: ModelSpec,
        filename: str,
        expected_size: int,
        staging: Path,
        done_before: int,
        total: int,
        progress: DownloadProgress | None,
        cancel: Callable[[], bool] | None,
    ) -> int:
        """Download one file into ``staging`` (resuming a ``.part``). Returns the
        cumulated byte count for progress reporting."""
        rel = _safe_relative(filename)
        final = staging / rel
        if final.is_file() and (not expected_size or final.stat().st_size == expected_size):
            done = done_before + final.stat().st_size
            if progress:
                progress(done, total)
            return done
        part = final.with_name(final.name + ".part")
        part.parent.mkdir(parents=True, exist_ok=True)
        start = part.stat().st_size if part.is_file() else 0
        if expected_size and start > expected_size:
            part.unlink()
            start = 0
        if cancel and cancel():
            raise DownloadCancelledError("Téléchargement annulé.")
        try:
            with _online(), self._transport.stream(spec, filename, start) as remote:
                if remote.offset not in (0, start):
                    raise ModelError("Reprise du téléchargement impossible : réponse inattendue.")
                mode = "ab" if remote.offset == start and start > 0 else "wb"
                written = remote.offset if mode == "ab" else 0
                with part.open(mode) as fh:
                    for chunk in remote.chunks:
                        if cancel and cancel():
                            raise DownloadCancelledError("Téléchargement annulé.")
                        fh.write(chunk)
                        written += len(chunk)
                        if progress:
                            progress(done_before + written, total)
        except ModelError:
            raise
        except netguard.NetworkBlockedError as exc:
            raise ModelError("Accès réseau bloqué.") from exc
        except Exception as exc:
            log.warning("download failed for %s: %s", spec.id, type(exc).__name__)
            raise ModelError(
                "Le téléchargement a été interrompu ; relancez-le pour le reprendre "
                f"({type(exc).__name__})."
            ) from exc
        if expected_size and written != expected_size:
            if written > expected_size:
                part.unlink(missing_ok=True)
            raise IntegrityError(
                f"Taille inattendue pour « {filename} » : téléchargement incomplet ou corrompu."
            )
        os.replace(part, final)
        return done_before + written

    def _verify_folder(
        self, spec: ModelSpec, folder: Path, cancel: Callable[[], bool] | None = None
    ) -> None:
        for mf in spec.files:
            target = folder / _safe_relative(mf.filename)
            if not target.is_file():
                raise IntegrityError(f"Fichier manquant : {mf.filename}.")
            if sha256_file(target, cancel) != mf.sha256.lower():
                target.unlink(missing_ok=True)
                raise IntegrityError(
                    f"Empreinte SHA-256 incorrecte pour « {mf.filename} » : fichier supprimé."
                )

    def _install(self, spec: ModelSpec, staging: Path) -> Path:
        """Atomically move a verified staging folder to ``models/<id>``."""
        cache: dict[str, Any] = {}
        for mf in spec.files:
            st = (staging / _safe_relative(mf.filename)).stat()
            cache[mf.filename] = {"stat": [st.st_size, st.st_mtime_ns], "sha256": mf.sha256.lower()}
        for leftover in staging.rglob("*.part"):
            leftover.unlink(missing_ok=True)
        _write_manifest(
            staging,
            {"id": spec.id, "repo_id": spec.repo_id, "revision": spec.revision, "files": cache},
        )
        target = self.root / spec.id
        old = self.root / f".{spec.id}.old"
        if old.exists():
            shutil.rmtree(old, ignore_errors=True)
        try:
            if target.exists():
                os.replace(target, old)
            try:
                os.replace(staging, target)
            except OSError:
                if old.exists() and not target.exists():
                    os.replace(old, target)
                raise
        except OSError as exc:
            raise ModelError(_IN_USE) from exc
        shutil.rmtree(old, ignore_errors=True)
        with contextlib.suppress(OSError):
            (self.root / STAGING).rmdir()  # only when empty
        log.info("model %s installed", spec.id)
        return target

    # -- offline import --------------------------------------------------------------

    def import_files(self, model_id: str, files: Sequence[Path]) -> Path:
        """Offline install from user-provided files (or a folder); verifies SHA-256."""
        spec = self._spec(model_id)
        self._require_hashes(spec)
        candidates = _candidates(files)
        if not candidates:
            raise ModelError("Aucun fichier à importer.")
        with self._lock:
            staging = self.root / STAGING / f"import-{spec.id}"
            shutil.rmtree(staging, ignore_errors=True)
            staging.mkdir(parents=True)
            try:
                hashes: dict[Path, str] = {}

                def digest(p: Path) -> str:
                    if p not in hashes:
                        hashes[p] = sha256_file(p)
                    return hashes[p]

                missing: list[str] = []
                for mf in spec.files:
                    source = _match(mf.filename, mf.sha256.lower(), mf.size, candidates, digest)
                    if source is None:
                        missing.append(mf.filename)
                        continue
                    destination = staging / _safe_relative(mf.filename)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, destination)
                if missing:
                    raise IntegrityError(
                        "Fichiers absents ou dont l'empreinte SHA-256 ne correspond pas : "
                        + ", ".join(missing)
                        + "."
                    )
                self._verify_folder(spec, staging)
                return self._install(spec, staging)
            finally:
                shutil.rmtree(staging, ignore_errors=True)

    # -- removal ------------------------------------------------------------------------

    def remove(self, model_id: str) -> None:
        spec = self._spec(model_id)
        with self._lock:
            target = self.root / spec.id
            if target.exists():
                trash = self.root / f".{spec.id}.removing"
                shutil.rmtree(trash, ignore_errors=True)
                try:
                    os.replace(target, trash)
                except OSError as exc:
                    raise ModelError(_IN_USE) from exc
                shutil.rmtree(trash, ignore_errors=True)
            for partial in (self.root / STAGING).glob(f"{spec.id}-*"):
                shutil.rmtree(partial, ignore_errors=True)
            log.info("model %s removed", spec.id)

    def partial_bytes(self, model_id: str) -> int:
        """Bytes already downloaded for an unfinished download (resume)."""
        spec = self._spec(model_id)
        folder = self.root / STAGING / f"{spec.id}-{spec.revision[:12] or 'latest'}"
        if not folder.is_dir():
            return 0
        return sum(p.stat().st_size for p in folder.rglob("*") if p.is_file())


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _read_manifest(folder: Path) -> dict[str, Any]:
    try:
        data = json.loads((folder / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_manifest(folder: Path, data: dict[str, Any]) -> None:
    tmp = folder / (MANIFEST + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, folder / MANIFEST)


def _candidates(files: Sequence[Path]) -> list[tuple[Path, PurePosixPath | None]]:
    """(file, path relative to the given folder or None for loose files)."""
    out: list[tuple[Path, PurePosixPath | None]] = []
    for raw in files:
        item = Path(raw)
        if item.is_dir():
            for p in sorted(item.rglob("*")):
                if p.is_file():
                    out.append((p, PurePosixPath(p.relative_to(item).as_posix())))
        elif item.is_file():
            out.append((item, None))
        else:
            raise ModelError(f"Fichier introuvable : {item.name}.")
    return out


def _match(
    filename: str,
    sha256: str,
    size: int,
    candidates: list[tuple[Path, PurePosixPath | None]],
    digest: Callable[[Path], str],
) -> Path | None:
    """Pick the candidate for a catalogue file: same relative path, then same base
    name, then any file of the right size; the SHA-256 always decides."""
    rel = PurePosixPath(filename)

    def size_ok(p: Path) -> bool:
        return not size or p.stat().st_size == size

    tiers = [
        [p for p, r in candidates if r is not None and (r == rel or _endswith(r, rel))],
        [p for p, _ in candidates if p.name == rel.name],
        [p for p, _ in candidates],
    ]
    seen: set[Path] = set()
    for tier in tiers:
        for p in tier:
            if p in seen:
                continue
            seen.add(p)
            if size_ok(p) and digest(p) == sha256:
                return p
    return None


def _endswith(path: PurePosixPath, suffix: PurePosixPath) -> bool:
    n = len(suffix.parts)
    return len(path.parts) >= n and path.parts[-n:] == suffix.parts


__all__ = [
    "DownloadCancelledError",
    "DownloadProgress",
    "IntegrityError",
    "LicenseCheck",
    "LicenseRefusedError",
    "ModelError",
    "ModelManager",
    "ModelStatus",
    "evaluate_license",
    "live_licenses",
]
