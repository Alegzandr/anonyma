"""Verifier: original-value residues (variant-aware), detector residues, policies."""

from __future__ import annotations

from pathlib import Path

from anonyma.config import Config
from anonyma.detectors.regex_fr import RegexDetector
from anonyma.models import (
    Document,
    DocumentStatus,
    Segment,
    SegmentContext,
    SegmentKind,
    Severity,
)
from anonyma.verifier import DETECTOR_HIT, ORIGINAL_VALUE, Verifier, is_ocr_issue, worth_checking


def output(*texts: str, label: str = "") -> Document:
    doc = Document(
        source=Path("x"), name="sortie.txt", format="txt", status=DocumentStatus.EXTRACTED
    )
    doc.segments = [
        Segment(f"s{i}", t, (i,), SegmentContext(SegmentKind.BODY, label))
        for i, t in enumerate(texts)
    ]
    return doc


def verify(
    doc: Document, sensitive: list[tuple[str, str]], subs: set[str], allowed=(), detectors=()
):  # type: ignore[no-untyped-def]
    return Verifier(Config(), list(detectors)).verify(doc, sensitive, subs, list(allowed))


def test_clean_output() -> None:
    result = verify(
        output("Courrier de [PERSONNE_001]."), [("Jean Dupont", "PERSONNE")], {"[PERSONNE_001]"}
    )
    assert result.clean


def test_original_value_residue_accent_and_case_insensitive() -> None:
    result = verify(output("signé JEAN DUPONT"), [("Jean Dupont", "PERSONNE")], set())
    assert [r.reason for r in result.residues] == [ORIGINAL_VALUE]
    assert result.residues[0].detector == "table"
    assert "Dupont" not in repr(result.residues[0])  # value is repr=False


def test_derived_forms_are_residues() -> None:
    doc = output("voir /home/x/jdupont/ et Dupont_Jean.pdf", "rappeler le 06.12.34.56.78")
    result = verify(doc, [("Jean Dupont", "PERSONNE"), ("0612345678", "TELEPHONE")], set())
    found = sorted(r.value for r in result.residues)
    assert found == ["06.12.34.56.78", "Dupont_Jean", "jdupont"]
    assert {r.detector for r in result.residues} == {"table_variante"}


def test_substitutes_and_allowed_values_are_ignored() -> None:
    doc = output("M. Paul Martin habite à Paris. [PERSONNE_001] a vu Rose.")
    result = verify(
        doc,
        [("Paris", "LIEU"), ("Rose", "PERSONNE")],
        {"Paul Martin", "[PERSONNE_001]"},
        allowed=["Paris", "Rose"],
        detectors=[RegexDetector(Config())],
    )
    assert result.clean, [(r.reason, r.detector) for r in result.residues]


def test_short_value_policy() -> None:
    assert not worth_checking("J.")
    assert not worth_checking("12")
    assert not worth_checking("123")
    assert worth_checking("1234")
    assert worth_checking("Luc")
    result = verify(
        output("le petit chat, Petit a signé"), [("Petit", "PERSONNE"), ("Li", "PERSONNE")], set()
    )
    assert [r.value for r in result.residues] == ["Petit"]  # common word: capitalized only


def test_detector_residue_and_partial_substitute_coverage() -> None:
    detectors = [RegexDetector(Config())]
    leaked = verify(output("joindre le 06 12 34 56 78"), [], set(), detectors=detectors)
    assert [r.reason for r in leaked.residues] == [DETECTOR_HIT]
    assert (
        leaked.residues[0].detector == "regex_fr" and leaked.residues[0].entity_type == "TELEPHONE"
    )
    # Civility left outside the substitute is not a residue.
    covered = verify(
        output("Rendez-vous avec Mme Odile Briand"), [], {"Odile Briand"}, detectors=detectors
    )
    assert covered.clean
    # ...but a real surname next to a substitute is.
    partial = verify(output("signé Paul Martin Dupont"), [], {"Paul Martin"}, detectors=detectors)
    assert partial.residues


def test_unreadable_output_is_not_verified() -> None:
    doc = output("x")
    doc.status = DocumentStatus.FAILED
    doc.add_issue(Severity.ERROR, "extract.error", "Échec de la lecture du fichier.")
    result = verify(doc, [], set())
    assert not result.verified and not result.clean


def test_llm_like_detectors_skipped() -> None:
    class Slow(RegexDetector):
        verify = False

    assert Verifier(Config(), [Slow(Config())]).detectors == []


def test_residue_location_and_label() -> None:
    result = verify(output("Jean Dupont", label="dc:creator"), [("Jean Dupont", "PERSONNE")], set())
    (res,) = result.residues
    assert res.location == "sortie.txt" and res.segment_label == "dc:creator"


def _ocr_output(*texts: str) -> Document:
    doc = Document(source=Path("x"), name="scan.png", format="png", status=DocumentStatus.EXTRACTED)
    doc.segments = [
        Segment(f"o{i}", t, ("ocr", 0, i), SegmentContext(SegmentKind.OCR, "image"))
        for i, t in enumerate(texts)
    ]
    return doc


def test_mixed_case_and_hyphen_variants() -> None:
    sensitive = [("Zéphyrin Quintavalle-Brezac", "PERSONNE")]
    for text in (
        "signé zéphyrin QUINTAVALLE-BREZAC",
        "QUINTAVALLE BREZAC",
        "par QuintavalleBrezac",
    ):
        assert verify(output(text), sensitive, set()).certain_residues, text


def test_ocr_skeleton_forms() -> None:
    sensitive = [("Zéphyrin Quintavalle", "PERSONNE"), ("9f3a7c21e8b04d56a1f2", "IDENTIFIANT")]
    misread = _ocr_output("Signé par Zephyrln Qu1ntavalle", "Ref 9f3a7c2le8bO4d56alf2")
    result = verify(misread, sensitive, set())
    assert {r.detector for r in result.residues} == {"table_ocr"}
    assert len(result.certain_residues) == 2
    # Only OCR text is read with this tolerance.
    assert verify(output("Zephyrln Qu1ntavalle"), sensitive, set()).clean


def test_original_names_are_searched() -> None:
    doc = output("Titre : Contrat_zephyrin_QUINTAVALLE_signe", "voir Quintavalle demain")
    doc.name = "Contrat_[PERSONNE_001]_signe.pdf"
    result = Verifier(Config(), []).verify(
        doc, [], set(), names=["Dossier/Contrat_zephyrin_QUINTAVALLE_signe.pdf"]
    )
    by_value = {r.value: r for r in result.residues}
    assert by_value["Contrat_zephyrin_QUINTAVALLE_signe"].certain
    assert by_value["Contrat_zephyrin_QUINTAVALLE_signe"].entity_type == "NOM_FICHIER"
    assert not by_value["Quintavalle"].certain  # a word of the name: possible only


def test_names_still_in_output_names_are_not_residues() -> None:
    doc = output("Rapport annuel Zorvalix 2026")
    doc.name = "Rapport_Zorvalix_2026.pdf"
    result = Verifier(Config(), []).verify(doc, [], set(), names=["Rapport_Zorvalix_2026.pdf"])
    assert result.clean and not result.residues
    # Common words alone never count.
    plain = output("Le rapport annuel")
    plain.name = "document_0001.pdf"
    assert (
        not Verifier(Config(), []).verify(plain, [], set(), names=["rapport_annuel.pdf"]).residues
    )


def test_ocr_issues_make_verification_incomplete() -> None:
    doc = output("rien")
    doc.add_issue(Severity.WARNING, "pdf.ocr_unavailable", "OCR indisponible.")
    child = _ocr_output()
    child.status = DocumentStatus.FAILED
    child.add_issue(Severity.ERROR, "ocr.unavailable", "OCR indisponible.")
    child.segments.append(
        Segment(
            "m0", "Auteur Zéphyrin Quintavalle", ("meta", 0), SegmentContext(SegmentKind.METADATA)
        )
    )
    doc.children.append(child)
    result = verify(doc, [("Zéphyrin Quintavalle", "PERSONNE")], set())
    assert result.verified and not result.complete and result.unread == 2
    # The readable part of an item whose OCR failed is still scanned.
    assert [r.segment_label for r in result.residues] == [""]


def test_is_ocr_issue() -> None:
    assert is_ocr_issue("ocr.failed") and is_ocr_issue("pdf.ocr_unavailable")
    assert not is_ocr_issue("office.media_blanked") and not is_ocr_issue("extract.error")


def test_save_time_of_today_is_not_a_residue() -> None:
    """LibreOffice dates the legacy files it saves: a "modified" of today in the output
    is its own stamp, even when the original was saved today as well."""
    from datetime import date

    def metadata(text: str, label: str) -> Document:
        doc = output()
        doc.segments = [Segment("m", text, (0,), SegmentContext(SegmentKind.METADATA, label))]
        return doc

    today = date.today().isoformat()
    stamped = metadata(f"{today}T10:15:00Z", "modified")
    assert verify(stamped, [(today, "DATE")], set(), detectors=[RegexDetector(Config())]).clean
    assert not verify(metadata(f"{today}T10:15:00Z", "created"), [(today, "DATE")], set()).clean
    old = metadata("2019-03-04T10:15:00Z", "modified")
    assert not verify(old, [("2019-03-04", "DATE")], set()).clean
