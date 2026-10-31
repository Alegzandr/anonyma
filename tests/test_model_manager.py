from __future__ import annotations

import contextlib
import hashlib
import itertools
import os
import socket
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from anonyma import model_catalog, netguard
from anonyma.model_catalog import ModelFile, ModelKind, ModelSpec
from anonyma.model_manager import (
    MANIFEST,
    DownloadCancelledError,
    IntegrityError,
    LicenseRefusedError,
    ModelError,
    ModelManager,
    RemoteStream,
    evaluate_license,
    live_licenses,
)

REVISION = "0123456789abcdef0123456789abcdef01234567"
FILES = {
    "onnx/model.onnx": os.urandom(300_000),
    "tokenizer.json": b'{"version": "1.0"}',
}


def _spec(model_id: str = "faux-gliner", license_id: str = "apache-2.0", **extra: str) -> ModelSpec:
    return ModelSpec(
        id=model_id,
        kind=ModelKind.GLINER,
        repo_id="exemple/faux-modele",
        revision=REVISION,
        files=tuple(
            ModelFile(name, hashlib.sha256(data).hexdigest(), len(data))
            for name, data in FILES.items()
        ),
        license=license_id,
        title="Faux modèle",
        extra=dict(extra),
    )


@pytest.fixture
def spec(monkeypatch: pytest.MonkeyPatch) -> ModelSpec:
    s = _spec()
    monkeypatch.setattr(model_catalog, "CATALOG", [s, _spec("autre-modele")])
    return s


@dataclass
class FakeApi:
    license: Any = "apache-2.0"
    tags: list[str] = field(default_factory=lambda: ["license:apache-2.0", "onnx"])
    calls: list[tuple[str, str | None]] = field(default_factory=list)
    network_allowed: list[bool] = field(default_factory=list)

    def model_info(self, repo_id: str, *, revision: str | None = None) -> Any:
        self.calls.append((repo_id, revision))
        self.network_allowed.append(netguard.is_allowed())
        return SimpleNamespace(card_data={"license": self.license}, tags=self.tags)


@dataclass
class FakeTransport:
    data: dict[str, bytes] = field(default_factory=lambda: dict(FILES))
    #: Stop the stream after this many bytes (simulated network failure).
    fail_after: int | None = None
    ignore_range: bool = False
    starts: list[tuple[str, int]] = field(default_factory=list)

    @contextlib.contextmanager
    def stream(self, spec: ModelSpec, filename: str, start: int) -> Iterator[RemoteStream]:
        assert spec.revision == REVISION
        assert netguard.is_allowed(), "download outside allow_network()"
        self.starts.append((filename, start))
        payload = self.data[filename]
        offset = 0 if self.ignore_range else start

        def chunks() -> Iterator[bytes]:
            sent = 0
            for i in range(offset, len(payload), 65_536):
                piece = payload[i : i + 65_536]
                if self.fail_after is not None and sent + len(piece) > self.fail_after:
                    raise ConnectionResetError("coupure simulée")
                sent += len(piece)
                yield piece

        yield RemoteStream(offset, len(payload), chunks())


def _manager(tmp_path: Path, **kw: Any) -> tuple[ModelManager, FakeApi, FakeTransport]:
    api = kw.pop("api", FakeApi())
    transport = kw.pop("transport", FakeTransport())
    return ModelManager(tmp_path / "models", api=api, transport=transport), api, transport


# -- license policy -------------------------------------------------------------


@pytest.mark.parametrize(
    ("live", "allowed"),
    [
        (["apache-2.0"], True),
        (["Apache-2.0", "apache-2.0"], True),
        (["mit"], False),  # differs from the catalogue
        (["cc-by-nc-4.0"], False),
        (["cc-by-nc-sa-4.0"], False),
        (["apache-2.0", "cc-by-nc-4.0"], False),
        (["mrl"], False),
        (["llama-research-only"], False),
        (["other"], False),
        (["unknown"], False),
        ([], False),
    ],
)
def test_evaluate_license(live: list[str], allowed: bool) -> None:
    check = evaluate_license(_spec(), live)
    assert check.allowed is allowed
    assert check.reason


def test_other_license_allowlisted() -> None:
    spec = _spec(license_id="other", license_allowlist="other")
    assert evaluate_license(spec, ["other"]).allowed
    assert not evaluate_license(_spec(license_id="other"), ["other"]).allowed
    # Non-commercial can never be allowlisted.
    nc = _spec(license_id="cc-by-nc-4.0", license_allowlist="cc-by-nc-4.0")
    assert not evaluate_license(nc, ["cc-by-nc-4.0"]).allowed


def test_live_licenses_reads_card_and_tags() -> None:
    info = SimpleNamespace(
        card_data=SimpleNamespace(license=["apache-2.0"]), tags=["license:mit", "x"]
    )
    assert live_licenses(info) == ["apache-2.0", "mit"]
    assert live_licenses(SimpleNamespace(card_data=None, tags=None)) == []


def test_check_license_uses_pinned_revision(tmp_path: Path, spec: ModelSpec) -> None:
    manager, api, _ = _manager(tmp_path)
    assert manager.check_license(spec.id).allowed
    assert api.calls == [(spec.repo_id, REVISION)]
    assert api.network_allowed == [True]


def test_check_license_network_error(tmp_path: Path, spec: ModelSpec) -> None:
    class Broken(FakeApi):
        def model_info(self, repo_id: str, *, revision: str | None = None) -> Any:
            raise OSError("boom")

    manager, _, _ = _manager(tmp_path, api=Broken())
    with pytest.raises(ModelError, match="Impossible de lire la licence"):
        manager.check_license(spec.id)


def test_hf_offline_mode_lifted_only_during_calls(tmp_path: Path, spec: ModelSpec) -> None:
    from huggingface_hub import constants

    seen: list[bool] = []

    class Spy(FakeApi):
        def model_info(self, repo_id: str, *, revision: str | None = None) -> Any:
            seen.append(constants.HF_HUB_OFFLINE)
            return super().model_info(repo_id, revision=revision)

    before = constants.HF_HUB_OFFLINE
    manager, _, _ = _manager(tmp_path, api=Spy())
    manager.check_license(spec.id)
    assert seen == [False]
    assert before == constants.HF_HUB_OFFLINE


# -- download -------------------------------------------------------------------


def test_download_installs_verified(tmp_path: Path, spec: ModelSpec) -> None:
    manager, _, _ = _manager(tmp_path)
    progress: list[tuple[int, int]] = []
    path = manager.download(spec.id, progress=lambda d, t: progress.append((d, t)))
    assert path == tmp_path / "models" / spec.id
    for name, data in FILES.items():
        assert (path / name).read_bytes() == data
    total = sum(len(d) for d in FILES.values())
    assert progress[-1] == (total, total)
    assert all(a[0] <= b[0] for a, b in itertools.pairwise(progress))
    status = manager.status(spec.id)
    assert status.installed and status.verified
    assert status.size_on_disk == total
    assert not (tmp_path / "models" / ".staging").exists()


def test_download_refused_license_downloads_nothing(tmp_path: Path, spec: ModelSpec) -> None:
    manager, _, transport = _manager(tmp_path, api=FakeApi(license="cc-by-nc-4.0", tags=[]))
    with pytest.raises(LicenseRefusedError, match="non commercial"):
        manager.download(spec.id)
    assert transport.starts == []
    assert not manager.is_installed(spec.id)


def test_download_refuses_changed_license(tmp_path: Path, spec: ModelSpec) -> None:
    manager, _, _ = _manager(tmp_path, api=FakeApi(license="mit", tags=["license:mit"]))
    with pytest.raises(LicenseRefusedError, match="diffère"):
        manager.download(spec.id)


def test_download_resumes_after_failure(tmp_path: Path, spec: ModelSpec) -> None:
    transport = FakeTransport(fail_after=150_000)
    manager, _, _ = _manager(tmp_path, transport=transport)
    with pytest.raises(ModelError, match="relancez"):
        manager.download(spec.id)
    assert 0 < manager.partial_bytes(spec.id) < len(FILES["onnx/model.onnx"])
    transport.fail_after = None
    path = manager.download(spec.id)
    assert (path / "onnx/model.onnx").read_bytes() == FILES["onnx/model.onnx"]
    resumed = [s for name, s in transport.starts if name == "onnx/model.onnx"]
    assert resumed[0] == 0 and resumed[1] > 0


def test_download_when_server_ignores_range(tmp_path: Path, spec: ModelSpec) -> None:
    transport = FakeTransport(fail_after=150_000)
    manager, _, _ = _manager(tmp_path, transport=transport)
    with pytest.raises(ModelError):
        manager.download(spec.id)
    transport.fail_after = None
    transport.ignore_range = True
    path = manager.download(spec.id)
    assert (path / "onnx/model.onnx").read_bytes() == FILES["onnx/model.onnx"]


def test_download_cancel_keeps_partial(tmp_path: Path, spec: ModelSpec) -> None:
    manager, _, _ = _manager(tmp_path)
    calls = {"n": 0}

    def cancel() -> bool:
        calls["n"] += 1
        return calls["n"] > 3

    with pytest.raises(DownloadCancelledError):
        manager.download(spec.id, cancel=cancel)
    assert not manager.is_installed(spec.id)
    assert manager.partial_bytes(spec.id) > 0
    assert manager.download(spec.id).is_dir()


def test_download_bad_hash(tmp_path: Path, spec: ModelSpec) -> None:
    corrupted = dict(FILES)
    corrupted["tokenizer.json"] = b'{"version": "1.1"}'  # same size, other content
    manager, _, _ = _manager(tmp_path, transport=FakeTransport(data=corrupted))
    with pytest.raises(IntegrityError, match="SHA-256"):
        manager.download(spec.id)
    assert not manager.is_installed(spec.id)


def test_download_requires_hashes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    unhashed = ModelSpec(
        "brouillon", ModelKind.OCR, "exemple/x", REVISION, (ModelFile("a.onnx"),), "mit", "x"
    )
    monkeypatch.setattr(model_catalog, "CATALOG", [unhashed])
    manager, api, _ = _manager(tmp_path)
    with pytest.raises(IntegrityError, match="manquante"):
        manager.download("brouillon")
    assert api.calls == []


def test_download_replaces_existing_install(tmp_path: Path, spec: ModelSpec) -> None:
    manager, _, _ = _manager(tmp_path)
    path = manager.download(spec.id)
    (path / "extra.bin").write_bytes(b"x")
    path = manager.download(spec.id)
    assert not (path / "extra.bin").exists()
    assert manager.status(spec.id).verified


def test_unknown_model(tmp_path: Path, spec: ModelSpec) -> None:
    manager, _, _ = _manager(tmp_path)
    with pytest.raises(ModelError, match="inconnu"):
        manager.status("nope")


def test_netguard_blocks_real_transport_outside_window(tmp_path: Path, spec: ModelSpec) -> None:
    netguard.install()
    with pytest.raises(netguard.NetworkBlockedError):
        socket.getaddrinfo("huggingface.co", 443)


# -- status / verification cache -----------------------------------------------------


def test_status_detects_corruption_and_caches(
    tmp_path: Path, spec: ModelSpec, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, _, _ = _manager(tmp_path)
    path = manager.download(spec.id)
    assert (path / MANIFEST).is_file()

    from anonyma import model_manager

    hashed: list[Path] = []
    real = model_manager.sha256_file

    def counting(p: Path, cancel: Any = None) -> str:
        hashed.append(p)
        return real(p, cancel)

    monkeypatch.setattr(model_manager, "sha256_file", counting)
    assert manager.status(spec.id).verified
    assert hashed == []  # cached by size + mtime
    target = path / "tokenizer.json"
    target.write_bytes(b'{"version": "6.6"}')
    status = manager.status(spec.id)
    assert status.installed and not status.verified
    assert hashed == [target]
    target.unlink()
    assert not manager.status(spec.id).verified


def test_statuses_lists_catalog(tmp_path: Path, spec: ModelSpec) -> None:
    manager, _, _ = _manager(tmp_path)
    assert [s.spec.id for s in manager.statuses()] == ["faux-gliner", "autre-modele"]
    assert not any(s.installed for s in manager.statuses())


# -- import / remove -------------------------------------------------------------------


def _write_tree(root: Path, files: dict[str, bytes]) -> None:
    for name, data in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(data)


def test_import_folder(tmp_path: Path, spec: ModelSpec) -> None:
    src = tmp_path / "usb" / "snapshot"
    _write_tree(src, FILES)
    (src / "README.md").write_text("autre fichier")
    manager, api, _ = _manager(tmp_path)
    path = manager.import_files(spec.id, [src])
    assert api.calls == []  # offline
    assert manager.status(spec.id).verified
    assert not (path / "README.md").exists()


def test_import_loose_files_flat(tmp_path: Path, spec: ModelSpec) -> None:
    src = tmp_path / "usb"
    src.mkdir()
    (src / "model.onnx").write_bytes(FILES["onnx/model.onnx"])
    (src / "tokenizer.json").write_bytes(FILES["tokenizer.json"])
    manager, _, _ = _manager(tmp_path)
    path = manager.import_files(spec.id, [src / "model.onnx", src / "tokenizer.json"])
    assert (path / "onnx" / "model.onnx").is_file()  # repo-relative layout restored


def test_import_renamed_file_matched_by_hash(tmp_path: Path, spec: ModelSpec) -> None:
    src = tmp_path / "usb"
    _write_tree(src, {"renomme.bin": FILES["onnx/model.onnx"], "tok.json": FILES["tokenizer.json"]})
    manager, _, _ = _manager(tmp_path)
    assert manager.import_files(spec.id, [src]).is_dir()


def test_import_rejects_wrong_files(tmp_path: Path, spec: ModelSpec) -> None:
    src = tmp_path / "usb"
    _write_tree(src, {"onnx/model.onnx": b"faux", "tokenizer.json": FILES["tokenizer.json"]})
    manager, _, _ = _manager(tmp_path)
    with pytest.raises(IntegrityError, match=r"onnx/model.onnx"):
        manager.import_files(spec.id, [src])
    assert not manager.is_installed(spec.id)
    with pytest.raises(ModelError, match="introuvable"):
        manager.import_files(spec.id, [tmp_path / "absent"])


def test_remove(tmp_path: Path, spec: ModelSpec) -> None:
    manager, _, transport = _manager(tmp_path)
    transport.fail_after = 100_000
    with pytest.raises(ModelError):
        manager.download(spec.id)
    transport.fail_after = None
    manager.download(spec.id)
    manager.remove(spec.id)
    assert not manager.is_installed(spec.id)
    assert manager.partial_bytes(spec.id) == 0
    manager.remove(spec.id)  # idempotent
