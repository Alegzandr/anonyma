from __future__ import annotations

import email
import zipfile
from email.policy import compat32
from pathlib import Path

import pytest

from anonyma import registry
from anonyma.config import Config
from anonyma.models import DocumentStatus
from anonyma.pipeline import Pipeline, extract_file
from tests.canary import CANARY_EMAIL, CANARY_PERSON

pytest.importorskip("pypff")

from tests.fixtures.generators.mail_archive_image_pst import (
    PstAttachment,
    PstFolder,
    PstMessage,
    PstRecipient,
    pst_bytes,
)


def _pst() -> bytes:
    message = PstMessage(
        subject=f"Dossier {CANARY_PERSON}",
        sender_name=CANARY_PERSON,
        sender_email=CANARY_EMAIL,
        body=f"Bonjour {CANARY_PERSON}\r\n",
        recipients=[
            PstRecipient("Bérénice Fauxnom", "berenice.fauxnom@exemple.test", 1),
            PstRecipient(CANARY_PERSON, CANARY_EMAIL, 3),
        ],
        attachments=[PstAttachment(f"{CANARY_PERSON}.txt", b"contenu", "text/plain")],
    )
    return pst_bytes([PstFolder(f"Dossier {CANARY_PERSON}", messages=[message])])


def test_pst_is_registered_and_sniffed(tmp_path: Path) -> None:
    path = tmp_path / "sans-extension"
    path.write_bytes(_pst())
    assert registry.detect_format(path) == "pst"


def test_pst_children_are_eml(tmp_path: Path, canary_config: Config) -> None:
    path = tmp_path / "boite.pst"
    path.write_bytes(_pst())
    doc = extract_file(path, "boite.pst", canary_config, tmp_path / "w")
    assert doc.status is DocumentStatus.EXTRACTED
    [child] = doc.children
    assert child.format == "eml"
    assert child.name == f"Dossier {CANARY_PERSON}/message-0001.eml"
    parsed = email.message_from_bytes(child.source.read_bytes(), policy=compat32)
    assert CANARY_EMAIL in parsed["From"]
    assert "berenice.fauxnom@exemple.test" in parsed["To"]
    assert CANARY_EMAIL in parsed["Bcc"]
    assert [c.name for c in child.children] == [f"{CANARY_PERSON}.txt"]


def test_pst_output_is_zip_of_anonymized_eml(tmp_path: Path, canary_config: Config) -> None:
    src = tmp_path / "in"
    src.mkdir()
    (src / "boite.pst").write_bytes(_pst())
    top = Pipeline(canary_config).run([src / "boite.pst"], tmp_path / "out").results[0]
    assert top.status is DocumentStatus.ANONYMIZED, [i.message for i in top.issues]
    assert top.output_name == "boite.zip"
    assert top.output_format == "zip"
    assert top.output_path is not None
    with zipfile.ZipFile(top.output_path) as zf:
        assert zf.namelist() == ["Dossier [PERSONNE_001]/message-0001.eml"]
        data = zf.read(zf.namelist()[0])
    assert b"uintavalle" not in data.lower()
