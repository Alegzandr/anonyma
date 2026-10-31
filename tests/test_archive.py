from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path

import py7zr
import pytest

from anonyma.config import ArchiveLimits, Config
from anonyma.extractors._archive import clean_entry_name, copy_limited, empty_directories
from anonyma.extractors.sevenzip import _LimitedFile
from anonyma.models import ArchiveBombError, DocumentStatus
from anonyma.pipeline import Pipeline, extract_file
from tests.canary import CANARY_EMAIL, CANARY_PERSON

NOTE = f"Dossier de {CANARY_PERSON} ({CANARY_EMAIL})\n".encode()


def _zip(entries: dict[str, bytes], comment: bytes = b"") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.comment = comment
        for name, data in entries.items():
            info = zipfile.ZipInfo(name, (2024, 5, 6, 7, 8, 10))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.comment = b"commentaire " + CANARY_EMAIL.encode()
            zf.writestr(info, data)
    return buffer.getvalue()


def _run(tmp_path: Path, name: str, data: bytes, config: Config):  # type: ignore[no-untyped-def]
    src = tmp_path / "in"
    src.mkdir(exist_ok=True)
    (src / name).write_bytes(data)
    return Pipeline(config).run([src / name], tmp_path / "out").results[0]


def test_clean_entry_name_is_zip_slip_safe() -> None:
    assert clean_entry_name("../../etc/passwd") == "etc/passwd"
    assert clean_entry_name("C:\\Users\\x\\..\\doc.txt") == "Users/x/doc.txt"
    assert clean_entry_name("/abs/./a.txt") == "abs/a.txt"
    assert clean_entry_name("../") is None


def test_empty_directories() -> None:
    assert empty_directories(["a", "a/b", "c", "d/e"], ["a/b/x.txt", "d/f.txt"]) == ["c", "d/e"]


def test_zip_round_trip_purges_metadata(tmp_path: Path, canary_config: Config) -> None:
    data = _zip(
        {
            f"{CANARY_PERSON}/note.txt": NOTE,
            f"{CANARY_PERSON}/vide/": b"",
            "../../evasion.txt": b"rien",
        },
        comment=CANARY_PERSON.encode(),
    )
    top = _run(tmp_path, "a.zip", data, canary_config)
    assert top.status is DocumentStatus.ANONYMIZED, [i.message for i in top.issues]
    with zipfile.ZipFile(top.output_path) as zf:
        assert zf.comment == b""
        names = sorted(zf.namelist())
        assert names == ["[PERSONNE_001]/note.txt", "[PERSONNE_001]/vide/", "evasion.txt"]
        for info in zf.infolist():
            assert info.date_time == (1980, 1, 1, 0, 0, 0)
            assert info.comment == b""
            assert info.extra == b""
        assert zf.read("[PERSONNE_001]/note.txt") == b"Dossier de [PERSONNE_001] ([EMAIL_001])\n"


def test_duplicate_anonymized_names_are_kept_apart(tmp_path: Path, canary_config: Config) -> None:
    data = _zip({f"{CANARY_PERSON}.txt": b"a", f"{CANARY_PERSON.upper()}.txt": b"b"})
    top = _run(tmp_path, "a.zip", data, canary_config)
    with zipfile.ZipFile(top.output_path) as zf:
        assert sorted(zf.namelist()) == ["[PERSONNE_001] (2).txt", "[PERSONNE_001].txt"]


def _bomb_status(tmp_path: Path, data: bytes, config: Config, name: str = "b.zip") -> None:
    path = tmp_path / name
    path.write_bytes(data)
    doc = extract_file(path, name, config, tmp_path / "w")
    assert doc.status is DocumentStatus.FAILED
    assert [i.code for i in doc.issues] == ["archive.bomb"]


def test_bomb_ratio(tmp_path: Path, canary_config: Config) -> None:
    _bomb_status(tmp_path, _zip({"zeros.bin": b"\0" * 5_000_000}), canary_config)


def test_bomb_total_size(tmp_path: Path, canary_config: Config) -> None:
    canary_config.limits = ArchiveLimits(max_total_bytes=10_000)
    entries = {f"f{i}.txt": os.urandom(4000) for i in range(4)}
    _bomb_status(tmp_path, _zip(entries), canary_config)


def test_bomb_file_count(tmp_path: Path, canary_config: Config) -> None:
    canary_config.limits = ArchiveLimits(max_files=3)
    _bomb_status(tmp_path, _zip({f"f{i}.txt": b"x" for i in range(5)}), canary_config)


def test_bomb_nested_quine_depth(tmp_path: Path, canary_config: Config) -> None:
    """A self-containing archive recurses until the depth budget stops it."""
    data = _zip({"fin.txt": b"x"})
    for _ in range(70):  # deeper than the default safety net (64)
        data = _zip({"soi-meme.zip": data})
    _bomb_status(tmp_path, data, canary_config)


def test_bomb_nested_in_mail_fails_whole_container(tmp_path: Path, canary_config: Config) -> None:
    canary_config.limits = ArchiveLimits(max_depth=2)
    data = _zip({"a.zip": _zip({"b.zip": _zip({"c.txt": b"x"})})})
    _bomb_status(tmp_path, data, canary_config)


def test_actual_size_is_enforced_while_streaming(tmp_path: Path) -> None:
    with pytest.raises(ArchiveBombError):
        copy_limited(io.BytesIO(b"x" * 100), tmp_path / "out", 99)
    assert copy_limited(io.BytesIO(b"x" * 100), tmp_path / "ok", 100) == 100
    limited = _LimitedFile(tmp_path / "7z", 10)
    limited.write(b"x" * 10)
    with pytest.raises(ArchiveBombError):
        limited.write(b"y")


def _encrypt_first_entry(data: bytes) -> bytes:
    """Set the 'encrypted' flag on the first entry (local and central headers)."""
    raw = bytearray(data)
    local = raw.find(b"PK\x03\x04")
    raw[local + 6] |= 0x01
    central = raw.find(b"PK\x01\x02")
    raw[central + 8] |= 0x01
    return bytes(raw)


def test_encrypted_entry_is_failed_child_and_omitted(tmp_path: Path, canary_config: Config) -> None:
    data = _encrypt_first_entry(_zip({"secret.txt": b"chiffre", "clair.txt": NOTE}))
    top = _run(tmp_path, "c.zip", data, canary_config)
    assert top.status is DocumentStatus.ANONYMIZED
    failed = [c for c in top.children if c.status is DocumentStatus.FAILED]
    assert len(failed) == 1
    assert any(i.code == "archive.encrypted" for i in failed[0].issues)
    assert any(i.code == "child.omitted" for i in top.issues)
    with zipfile.ZipFile(top.output_path) as zf:
        assert zf.namelist() == ["clair.txt"]


def test_7z_round_trip(tmp_path: Path, canary_config: Config) -> None:
    buffer = io.BytesIO()
    with py7zr.SevenZipFile(buffer, "w") as sz:
        sz.writestr(NOTE, f"{CANARY_PERSON}/note.txt")
        sz.writestr(b"autre", "b.txt")
    top = _run(tmp_path, "a.7z", buffer.getvalue(), canary_config)
    assert top.status is DocumentStatus.ANONYMIZED, [i.message for i in top.issues]
    with py7zr.SevenZipFile(top.output_path, "r") as sz:
        names = sorted(e.filename for e in sz.list())
        assert names == ["[PERSONNE_001]/note.txt", "b.txt"]


def test_7z_bomb_ratio(tmp_path: Path, canary_config: Config) -> None:
    buffer = io.BytesIO()
    with py7zr.SevenZipFile(buffer, "w") as sz:
        sz.writestr(b"\0" * 5_000_000, "zeros.bin")
    _bomb_status(tmp_path, buffer.getvalue(), canary_config, "b.7z")


def test_7z_encrypted_content(tmp_path: Path, canary_config: Config) -> None:
    path = tmp_path / "in.7z"
    with py7zr.SevenZipFile(path, "w", password="secret") as sz:
        sz.writestr(NOTE, "note.txt")
    top = _run(tmp_path, "e.7z", path.read_bytes(), canary_config)
    assert top.status is DocumentStatus.ANONYMIZED
    assert [c.status for c in top.children] == [DocumentStatus.FAILED]
    with py7zr.SevenZipFile(top.output_path, "r") as sz:
        assert sz.list() == []


def test_7z_encrypted_headers(tmp_path: Path, canary_config: Config) -> None:
    path = tmp_path / "in.7z"
    with py7zr.SevenZipFile(path, "w", password="secret", header_encryption=True) as sz:
        sz.writestr(NOTE, "note.txt")
    doc = extract_file(path, "h.7z", canary_config, tmp_path / "w")
    assert doc.status is DocumentStatus.FAILED
    assert [i.code for i in doc.issues] == ["archive.encrypted"]
