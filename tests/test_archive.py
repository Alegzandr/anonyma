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
    # A member left out: the archive is incomplete, never « Anonymisé ».
    assert top.status is DocumentStatus.NEEDS_REVIEW
    assert any(i.code == "container.incomplete" for i in top.issues)
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
    # Nothing could be kept: no empty archive is produced, the file fails.
    assert top.status is DocumentStatus.FAILED
    assert [c.status for c in top.children] == [DocumentStatus.FAILED]
    assert any(i.code == "container.empty" for i in top.issues)
    assert top.output_path is None


def test_7z_encrypted_headers(tmp_path: Path, canary_config: Config) -> None:
    path = tmp_path / "in.7z"
    with py7zr.SevenZipFile(path, "w", password="secret", header_encryption=True) as sz:
        sz.writestr(NOTE, "note.txt")
    doc = extract_file(path, "h.7z", canary_config, tmp_path / "w")
    assert doc.status is DocumentStatus.FAILED
    assert [i.code for i in doc.issues] == ["archive.encrypted"]


# -- memory: sizes charged before anything is materialised -------------------------


@pytest.mark.usefixtures("blank_ocr")  # the budget, not OCR
def test_pdf_embedded_file_is_charged_before_it_is_read(
    tmp_path: Path, canary_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pymupdf

    pdf = pymupdf.open()
    pdf.new_page().insert_text((72, 72), "Bonjour.")
    pdf.embfile_add("gros.bin", os.urandom(50_000), filename="gros.bin")
    data = pdf.tobytes()

    def never(*args: object) -> bytes:
        raise AssertionError("embedded file read beyond the budget")

    monkeypatch.setattr(pymupdf.Document, "embfile_get", never)
    canary_config.limits = ArchiveLimits(max_total_bytes=10_000)
    top = _run(tmp_path, "doc.pdf", data, canary_config)
    assert top.status is DocumentStatus.FAILED
    assert "archive.bomb" in [i.code for i in top.issues]


def test_compound_file_streams_are_charged_before_they_are_read() -> None:
    from anonyma.extractors._cfb import read_cfb

    fixture = Path(__file__).parent / "fixtures" / "contract" / "message.msg"
    sizes: list[int] = []
    _, entries = read_cfb(fixture, sizes.append)
    assert sum(sizes) == sum(len(e.data or b"") for e in entries if not e.storage) > 0


def test_images_beyond_the_pixel_cap_are_refused(
    tmp_path: Path, canary_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    from PIL import Image

    from anonyma import paths

    paths.limit_images()
    assert Image.MAX_IMAGE_PIXELS == paths.MAX_IMAGE_PIXELS
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1_000)
    buffer = io.BytesIO()
    Image.new("RGB", (40, 40), "white").save(buffer, "PNG")  # 1 600 pixels: a warning
    with pytest.raises(Image.DecompressionBombWarning):
        Image.open(io.BytesIO(buffer.getvalue()))
    top = _run(tmp_path, "image.png", buffer.getvalue(), canary_config)
    assert top.status is DocumentStatus.FAILED
