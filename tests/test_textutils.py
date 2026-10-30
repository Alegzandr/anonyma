from __future__ import annotations

from anonyma.codec import chunk_text, decode_bytes
from anonyma.textutils import find_all, normalize, safe_filename


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
