from __future__ import annotations

import base64
import io
import quopri
import zipfile
import zlib
from pathlib import Path

import pytest

from anonyma.config import Config
from anonyma.extractors import _html
from anonyma.models import Document, DocumentStatus
from anonyma.pipeline import Pipeline
from anonyma.rawscan import RAW_DETECTOR, RawScanner
from tests.canary import CANARY_EMAIL, CANARY_PERSON

SENSITIVE = [(CANARY_PERSON, "PERSONNE"), (CANARY_EMAIL, "EMAIL")]


def _scanner(substitutes: set[str] | None = None, kept: list[str] | None = None) -> RawScanner:
    return RawScanner(SENSITIVE, substitutes or set(), set(), kept or [], Config())


def _hits(tmp_path: Path, data: bytes, scanner: RawScanner | None = None, fmt: str = "x") -> int:
    path = tmp_path / "sortie.bin"
    path.write_bytes(data)
    return len((scanner or _scanner()).scan(Document(source=path, name="sortie", format=fmt)))


def _zip(name: str, data: bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(name, data)
    return buffer.getvalue()


@pytest.mark.parametrize(
    "data",
    [
        f"<x>{CANARY_PERSON}</x>".encode(),
        f"<x>{CANARY_PERSON.upper()}</x>".encode(),
        f"<x>{CANARY_PERSON}</x>".encode("cp1252"),
        f"\x00\x00{CANARY_PERSON}\x00".encode("utf-16-le"),
        b"\x00" + f"{CANARY_PERSON}".encode("utf-16-be"),
        b"<p>Z&eacute;phyrin&#32;Quintavalle</p>",
        b"<p>Zephyrin   \n  Quintavalle</p>",
        b"Content-Transfer-Encoding: quoted-printable\n\n"
        + quopri.encodestring(f"Bonjour {CANARY_PERSON} {'x' * 80}".encode()),
        b"Content-Transfer-Encoding: base64\n\n"
        + base64.encodebytes(f"Bonjour {CANARY_EMAIL}".encode()),
        b"Subject: =?utf-8?B?" + base64.b64encode(CANARY_PERSON.encode()) + b"?=\n",
        _zip("word/document.xml", f"<w:t>{CANARY_EMAIL}</w:t>".encode()),
        _zip(f"{CANARY_PERSON}.txt", b"rien"),
        b"%PDF-1.7\n1 0 obj<</Length 20/Filter/FlateDecode>>stream\n"
        + zlib.compress(f"BT ({CANARY_PERSON}) Tj ET".encode())
        + b"\nendstream",
    ],
)
def test_values_are_found_in_every_raw_form(tmp_path: Path, data: bytes) -> None:
    assert _hits(tmp_path, data) >= 1


@pytest.mark.parametrize(
    "data",
    [
        b"<x>[PERSONNE_001] et [EMAIL_001]</x>",
        b"<x>Zephyrin Quintavallex, xzephyrin.quintavalle@exemple.test</x>",
        b"<x>Quintavalle</x>",  # a variant never seen in the batch
        base64.b64encode(bytes(range(256)) * 64),
    ],
)
def test_no_false_hit(tmp_path: Path, data: bytes) -> None:
    assert _hits(tmp_path, data) == 0


def test_values_inside_substitutes_or_kept_values_are_ignored(tmp_path: Path) -> None:
    data = f"<x>M. {CANARY_PERSON} Junior ; contact-{CANARY_EMAIL}.fr</x>".encode()
    assert _hits(tmp_path, data) == 2
    scanner = _scanner({f"M. {CANARY_PERSON} Junior"}, [f"contact-{CANARY_EMAIL}.fr"])
    assert _hits(tmp_path, data, scanner) == 0


def test_short_common_and_non_identifying_values_are_not_searched(tmp_path: Path) -> None:
    scanner = RawScanner(
        [("Rose", "PERSONNE"), ("Martine", "PERSONNE"), ("Microsoft", "ORGANISATION")],
        set(),
        set(),
        [],
        Config(),
    )
    assert _hits(tmp_path, b"Rose ; schemas.microsoft.com ; Martine", scanner) == 1
    assert not RawScanner([("2026-01-01T00:00", "PERSONNE")], set(), set(), [], Config())


def test_archive_members_are_items_of_their_own(tmp_path: Path) -> None:
    data = _zip("note.txt", CANARY_PERSON.encode())
    assert _hits(tmp_path, data, fmt="docx") == 1
    assert _hits(tmp_path, data, fmt="zip") == 0  # its member is scanned as a child


def test_unexposed_markup_is_caught_by_the_raw_backstop(
    tmp_path: Path, canary_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Simulate an extractor that misses a construct: the bytes still betray it.
    monkeypatch.setattr(_html.HtmlSource, "_markup", lambda self, *args: None)
    src = tmp_path / "in"
    src.mkdir()
    (src / "page.html").write_bytes(f"<p>{CANARY_EMAIL}</p><?note {CANARY_EMAIL}?>".encode())
    top = Pipeline(canary_config).run([src / "page.html"], tmp_path / "out").results[0]
    assert top.status is DocumentStatus.RESIDUE
    assert top.verification is not None
    assert [r.detector for r in top.verification.certain_residues] == [RAW_DETECTOR]
    assert top.output_path is not None
    assert top.output_path.parent.name == "out_quarantaine"
