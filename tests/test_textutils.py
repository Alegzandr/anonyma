from __future__ import annotations

import pytest

from anonyma.codec import chunk_text, decode_bytes
from anonyma.textutils import MAX_NAME_BYTES, find_all, normalize, safe_filename, safe_suffix


def test_normalize() -> None:
    assert normalize("  Zéphyrin   QUINTAVALLE ") == "zephyrin quintavalle"


def test_find_all_accents_case_and_boundaries() -> None:
    text = "M. ZEPHYRIN Quintavalle et zéphyrin  quintavalle ; Quintavallex non."
    spans = list(find_all(text, "Zéphyrin Quintavalle"))
    assert [text[s:e] for s, e in spans] == ["ZEPHYRIN Quintavalle", "zéphyrin  quintavalle"]
    assert [text[s:e] for s, e in find_all(text, "quintavalle")] == ["Quintavalle", "quintavalle"]


def test_find_all_ligatures_map_back() -> None:
    text = "Straße Weiß"
    assert [text[s:e] for s, e in find_all(text, "weiss")] == ["Weiß"]


def test_decode_roundtrip() -> None:
    for data in ("héllo\r\nmonde".encode("cp1252"), "﻿hé".encode(), "hé".encode("utf-16")):
        text, enc = decode_bytes(data)
        assert enc.encode(text) == data


def test_chunk_text_covers_everything() -> None:
    text = ("ligne\n" * 400 + "\n") * 5
    chunks = chunk_text(text)
    assert "".join(c for _, c in chunks) == text
    assert all(text[o : o + len(c)] == c for o, c in chunks)
    assert len(chunks) > 1


def test_safe_filename() -> None:
    assert safe_filename('a/b:c*?"<>|') == "a_b_c______"
    assert safe_filename("CON.txt") == "_CON.txt"


@pytest.mark.parametrize(
    "name", ["NUL .pdf", "nul  .tar.gz", "COM¹.txt", "lpt³", "CONIN$.log", "conout$", "AUX"]
)
def test_safe_filename_reserved_device_names(name: str) -> None:
    assert safe_filename(name).startswith("_")


def test_safe_filename_caps_the_length_and_keeps_the_extension() -> None:
    long = "é" * 300 + ".docx"
    out = safe_filename(long)
    assert out.endswith("é.docx") and len(out.encode("utf-8")) <= MAX_NAME_BYTES
    assert len(safe_filename("a" * 400).encode("utf-8")) == MAX_NAME_BYTES


def test_safe_suffix_from_archive_entries() -> None:
    assert safe_suffix("dossier/Note.PDF") == ".pdf"
    assert safe_suffix("note.txt:flux") == ""  # NTFS alternate data stream
    assert safe_suffix("a.tx t") == ""
    assert safe_suffix("archive") == ""
    assert safe_suffix("x." + "a" * 11) == ""
