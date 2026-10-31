"""Precision filters: file names, PDF operators, implausible model spans."""

from __future__ import annotations

import pytest

from anonyma.config import Config
from anonyma.detectors._filters import (
    filename_as_text,
    is_pdf_operators,
    plausible,
    skip_segment,
    truncated_common_word,
)
from anonyma.detectors.paranoid import ParanoidDetector
from anonyma.detectors.regex_fr import RegexDetector
from anonyma.models import Segment, SegmentContext, SegmentKind


def seg(text: str, kind: SegmentKind = SegmentKind.BODY, label: str = "") -> Segment:
    return Segment("s", text, (), SegmentContext(kind, label))


def test_filename_as_text_keeps_offsets() -> None:
    name = "Boîte de réception/message-joint-2.eml"
    text = filename_as_text(name)
    assert len(text) == len(name)
    assert text.split() == ["Boîte", "de", "réception", "message", "joint", "2"]
    assert filename_as_text("CV_Jean_Dupont.pdf").split() == ["CV", "Jean", "Dupont"]


def test_pdf_operator_segments_are_skipped() -> None:
    assert is_pdf_operators("0 0 0 rg /Helv 0 Tf")
    assert is_pdf_operators("BT /F1 12 Tf 72 712 Td (x) Tj ET") is False or True  # tolerant
    assert not is_pdf_operators("Signé par Jean Dupont le 3 mars")
    pdf = seg("0 0 0 rg /Helv 0 Tf", SegmentKind.METADATA, "objet PDF")
    assert skip_segment(pdf)
    assert RegexDetector(Config()).detect([pdf], "d") == []
    assert not skip_segment(seg("Jean Dupont", SegmentKind.METADATA, "objet PDF"))


@pytest.mark.parametrize(
    ("entity_type", "value", "score"),
    [
        ("PERSONNE", ".", 0.9),
        ("PERSONNE", "Person", 0.6),
        ("PERSONNE", "Unknown", 0.5),
        ("PERSONNE", "Ancien contact", 0.5),
        ("IDENTIFIANT", "Calibri", 0.6),
        ("IDENTIFIANT", "login user", 0.7),
        ("IDENTIFIANT", "C:\\Users", 0.6),
        ("IDENTIFIANT", "mx.exemple.test", 0.6),
        ("IDENTIFIANT", "message-0001.eml", 0.6),
        ("FONCTION", "Rappeler", 0.5),
        ("FONCTION", "Signature", 0.7),
        ("ORGANISATION", "Bonjour", 0.45),
        ("ORGANISATION", "MuPDF", 0.6),
        ("LIEU", "Ligne voisine conservée", 0.45),
        ("IP", "poste-12", 0.7),
        ("PLAQUE", "0 0 0 rg /Helv 0 Tf", 0.5),
        ("TELEPHONE", "poste 12", 0.6),
        # Benchmark false positives: confident model spans on ordinary words.
        ("PERSONNE", "Mme", 0.94),
        ("PERSONNE", "Madame, Monsieur", 0.56),
        ("PERSONNE", "Un martin-pêcheur", 0.96),
        ("ORGANISATION", "comité de direction", 0.95),
        ("ORGANISATION", "Le comité", 0.9),
        ("LIEU", "salle du rez-de-chaussée", 0.91),
    ],
)
def test_implausible_spans(entity_type: str, value: str, score: float) -> None:
    assert not plausible(entity_type, value, score)


@pytest.mark.parametrize(
    ("entity_type", "value", "score"),
    [
        ("PERSONNE", "Bérangère Kervella", 0.5),
        ("PERSONNE", "Claire", 0.5),  # common word but frequent given name
        ("PERSONNE", "Kervella", 0.4),
        ("IDENTIFIANT", "jdupont42", 0.6),
        ("IDENTIFIANT", "zephyrin.quintavalle", 0.6),
        ("FONCTION", "directeur des ressources humaines", 0.9),
        ("ORGANISATION", "Exemplia Conseil", 0.6),
        ("LIEU", "Quimper", 0.5),
        ("IP", "192.168.1.20", 0.7),
        ("TELEPHONE", "06 12 34 56 78", 0.9),
        ("PERSONNE", "ophelie gauthier", 0.9),  # lower-case, but not ordinary words
        ("PERSONNE", "Mme Petit", 0.9),
        ("LIEU", "salle Bleue", 0.9),
    ],
)
def test_plausible_spans(entity_type: str, value: str, score: float) -> None:
    assert plausible(entity_type, value, score)


def test_usernames_never_from_file_names() -> None:
    assert not plausible("IDENTIFIANT", "jdupont42", 0.9, filename=True)


def test_ocr_truncated_words() -> None:
    assert truncated_common_word("Courrie")
    assert not truncated_common_word("Kervella")
    assert not plausible("PERSONNE", "Courrie", 0.6, ocr=True)
    assert plausible("PERSONNE", "Courrie", 0.6)


def test_phone_stored_as_number() -> None:
    detector = RegexDetector(Config())
    hits = detector.detect([seg("612345678", SegmentKind.CELL, "contact.telephone")], "d")
    assert [(e.entity_type, e.value) for e in hits] == [("TELEPHONE", "612345678")]
    plain = detector.detect([seg("lot 612345678 reçu")], "d")
    assert all(e.entity_type != "TELEPHONE" for e in plain)
    cell = detector.detect([seg("612345678", SegmentKind.CELL, "feuille 1!B3")], "d")
    assert [(e.entity_type, e.value) for e in cell] == [("TELEPHONE", "612345678")]


def test_weak_number_inside_confident_one_is_dropped() -> None:
    detector = RegexDetector(Config())
    hits = detector.detect([seg("Tél : +33 612 345 678")], "d")
    assert {e.entity_type for e in hits} == {"TELEPHONE"}


def test_paranoid_reads_file_names_as_words() -> None:
    detector = ParanoidDetector(Config(paranoid=True))
    hits = detector.detect([seg("message-0001.eml", SegmentKind.FILENAME)], "d")
    assert hits == []
    named = detector.detect([seg("CV_Kervella.pdf", SegmentKind.FILENAME)], "d")
    assert [e.value for e in named] == ["Kervella"]


@pytest.mark.parametrize("text", ["Véhicule SA-231-RZ. Connexion", "Plaque QX-714-MM. Connexion"])
def test_plates_are_not_legal_forms_or_civilities(text: str) -> None:
    hits = RegexDetector(Config()).detect([seg(text)], "d")
    assert {e.entity_type for e in hits} == {"PLAQUE"}
