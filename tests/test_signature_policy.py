"""E-signed documents: signer names in any case, timestamps, opaque ids, barcodes,
organization policy and tag numbering. Synthetic values only."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from anonyma.config import Config
from anonyma.detectors.codes import CodeDetector
from anonyma.detectors.opaque import OpaqueIdDetector, looks_opaque
from anonyma.detectors.regex_fr import RegexDetector
from anonyma.models import (
    BBox,
    Document,
    DocumentStatus,
    Entity,
    EntityType,
    Regime,
    ReplacementMode,
    Segment,
    SegmentContext,
    SegmentKind,
)
from anonyma.policy import apply_policies, default_whitelist, keeps, url_personal_parts
from anonyma.replacer import Replacer, build_plan
from anonyma.resolver import propagate, resolve
from anonyma.store import InMemoryMappingStore
from anonyma.substitutes import shift_timestamp
from anonyma.verifier import _TAG_LIKE
from tests.canary import CANARY_PERSON

HEX_REF = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b"
TRANSACTION = "TRX-a8F3k2-xyz-9Qm4Lp"


def seg(text: str, kind: SegmentKind = SegmentKind.BODY, sid: str = "s") -> Segment:
    return Segment(sid, text, (), SegmentContext(kind))


def found(detector: object, text: str, entity_type: str, **kw: object) -> list[str]:
    entities = detector.detect([seg(text, **kw)], "d")  # type: ignore[attr-defined]
    return [e.value for e in entities if e.entity_type == entity_type]


# -- persons ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Signé électroniquement par zéphyrin QUINTAVALLE le 31/10/2026", "zéphyrin QUINTAVALLE"),
        ("Signé par :\nZÉPHYRIN QUINTAVALLE", "ZÉPHYRIN QUINTAVALLE"),
        ("Signataire : Mme  zéphyrine   quintavalle", "zéphyrine   quintavalle"),
        ("Nom et prénom : zéphyrin quintavalle", "zéphyrin quintavalle"),
        ("Nom : QUINTAVALLE", "QUINTAVALLE"),
        ("Cordialement,\nZéphyrin Quintavalle\nGérant", "Zéphyrin Quintavalle"),
        ("Digitally signed by zephyrin quintavalle", "zephyrin quintavalle"),
        ("Best regards,\nZephyrin Quintavalle", "Zephyrin Quintavalle"),
        ("M. jean quintavalle est présent.", "jean quintavalle"),
        ("rendez-vous avec jean QUINTAVALLE demain", "jean QUINTAVALLE"),
    ],
)
def test_person_after_cues_in_any_case(text: str, expected: str) -> None:
    assert expected in found(RegexDetector(Config()), text, EntityType.PERSON, kind=SegmentKind.OCR)


@pytest.mark.parametrize(
    "text",
    [
        "Le contrat a été signé par erreur.",
        "Monsieur le maire a signé.",
        "Cordialement,\n\nL'équipe Support",
        "Nom du fichier : rapport.pdf",
        "From: support@editeur.example",
        "La réunion budgétaire du comité de direction est reportée au trimestre prochain.",
        "Merci de noter que le chevalier blanc reste une figure de style dans ce rapport.",
    ],
)
def test_no_person_without_a_name(text: str) -> None:
    assert found(RegexDetector(Config()), text, EntityType.PERSON) == []


# -- dates and times ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Signé par Zéphyrin Quintavalle le 31/10/2026 à 14:22:05 +01:00",
         "31/10/2026 à 14:22:05 +01:00"),
        ("Horodatage : 2026-10-31T14:22:05Z", "2026-10-31T14:22:05Z"),
        ("Signature : D:20261031142205+01'00'", "D:20261031142205+01'00'"),
        ("Signé le 31 octobre 2026 à 14h22", "31 octobre 2026 à 14h22"),
        ("Digitally signed by Zephyrin Quintavalle\nDate: 2026.10.31 14:22:05 +01'00'",
         "2026.10.31 14:22:05 +01'00'"),
        ("eulalie QUINTAVALLE\n14:22:05 UTC", "14:22:05 UTC"),
    ],
)  # fmt: skip
def test_signature_timestamps_masked_even_without_detect_dates(text: str, expected: str) -> None:
    detector = RegexDetector(Config(detect_dates=False))
    assert expected in found(detector, text, EntityType.DATE)


def test_timestamp_cue_on_the_previous_ocr_line() -> None:
    detector = RegexDetector(Config(detect_dates=False))
    lines = [
        seg("Signé électroniquement le", SegmentKind.OCR, "a"),
        seg("31/10/2026 14:22", SegmentKind.OCR, "b"),
    ]
    values = [e.value for e in detector.detect(lines, "d") if e.entity_type == EntityType.DATE]
    assert values == ["31/10/2026 14:22"]


def test_ordinary_dates_and_times_follow_detect_dates() -> None:
    text = "Le comité se réunira le 12 mars 2026 à 14 h 30 en salle Bleue."
    assert found(RegexDetector(Config(detect_dates=False)), text, EntityType.DATE) == []
    assert found(RegexDetector(Config()), text, EntityType.DATE) == ["12 mars 2026 à 14 h 30"]
    # A time alone is never an ordinary date.
    assert found(RegexDetector(Config()), "Rendez-vous à 14:30 en salle.", EntityType.DATE) == []
    # Signature segments: every date is a signing time.
    detector = RegexDetector(Config(detect_dates=False))
    assert found(detector, "2026-10-31 14:22:05", EntityType.DATE, kind=SegmentKind.SIGNATURE)


def test_realistic_timestamps_are_shifted() -> None:
    import random

    rng = random.Random(1)
    out = shift_timestamp("31/10/2026 à 14:22:05", 10, rng)
    assert out is not None and out.startswith("10/11/2026 à ") and "14:22:05" not in out
    pdf = shift_timestamp("D:20261031142205+01'00'", -1, rng)
    assert pdf is not None and pdf.startswith("D:20261030") and pdf.endswith("+01'00'")
    assert shift_timestamp("sans heure", 3, rng) is None


# -- opaque identifiers -------------------------------------------------------------------


@pytest.mark.parametrize(
    "token",
    [TRANSACTION, "AB12-CD34-EF56-GH78", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9", "7KQ2M9XW4PZ8R3TL",
     "Zm9vYmFyYmF6cXV4MTIzNDU2Nzg5MA==", "env_3Fk9Lm2Qp7Zx"],
)  # fmt: skip
def test_opaque_tokens(token: str) -> None:
    assert looks_opaque(token)


@pytest.mark.parametrize(
    "token",
    ["Jean-Baptiste", "ISO-8859-1", "COVID-19", "Windows-10-x64", "iPhone15ProMax", "UTF-8",
     "ThisIsMyVeryLongIdentifier", "FR7630006000011234567890189", "rapport-2024-v2",
     "DOS-2024-00123", "AA-123-BB", "1234567890123456", "MicrosoftOffice365ProPlus",
     "x86_64-linux-gnu", "AES256-GCM-SHA384", "PERSONNE_K7Q2", "numero_dossier_0"],
)  # fmt: skip
def test_ordinary_tokens_are_not_opaque(token: str) -> None:
    assert not looks_opaque(token)


def test_opaque_detector_rules_and_whitelist() -> None:
    text = (
        f"Référence {HEX_REF}, transaction {TRANSACTION}, dossier "
        "3f2b8c1e-4d5a-4b6c-9e7f-0a1b2c3d4e5f. Empreinte 3A 5F 9C 01 22 B4 7E 88 90 AB. "
        "Montant 1 250,00 euros, tél. 06 12 34 56 78, version 2.3.1, couleur #ff00aa."
    )
    values = found(OpaqueIdDetector(Config()), text, EntityType.OPAQUE_ID)
    assert values == [
        "3f2b8c1e-4d5a-4b6c-9e7f-0a1b2c3d4e5f",
        HEX_REF,
        "3A 5F 9C 01 22 B4 7E 88 90 AB",
        TRANSACTION,
    ]
    allowed = Config(opaque_id_whitelist=[HEX_REF.upper(), r"re:TRX-.*", "re:(broken"])
    assert found(OpaqueIdDetector(allowed), text, EntityType.OPAQUE_ID) == [
        "3f2b8c1e-4d5a-4b6c-9e7f-0a1b2c3d4e5f",
        "3A 5F 9C 01 22 B4 7E 88 90 AB",
    ]
    assert found(OpaqueIdDetector(Config(mask_opaque_ids=False)), text, EntityType.OPAQUE_ID) == []
    # Signature and barcode segments are read too.
    for kind in (SegmentKind.SIGNATURE, SegmentKind.BARCODE):
        assert found(OpaqueIdDetector(Config()), f"ref={HEX_REF}", EntityType.OPAQUE_ID, kind=kind)


def test_office_suite_build_ids_are_not_opaque() -> None:
    # LibreOffice stamps its build id on every file it saves (legacy doc/xls/ppt
    # outputs): masking it at input made every converted output a residue.
    generator = f"LibreOffice/24.2.7.2$Windows_X86_64 LibreOffice_project/{HEX_REF}"
    assert found(OpaqueIdDetector(Config()), generator, EntityType.OPAQUE_ID) == []
    assert found(OpaqueIdDetector(Config()), f"project/{HEX_REF}", EntityType.OPAQUE_ID)


# -- barcodes -------------------------------------------------------------------------------


def _doc(segments: list[Segment], name: str = "a.pdf") -> Document:
    doc = Document(source=Path(name), name=name, format="pdf", segments=segments)
    doc.status = DocumentStatus.EXTRACTED
    return doc


def test_barcode_masked_whole_and_content_still_propagated() -> None:
    box = BBox(0, 0, 10, 10)
    code_text = "https://verif.exemple.test/d/" + HEX_REF
    code = Segment(
        "qr", code_text, (), SegmentContext(SegmentKind.BARCODE, bbox=box), (box,) * len(code_text)
    )
    body = seg(f"Référence du dossier : {HEX_REF}", sid="body")
    doc = _doc([code, body])
    config = Config()
    raw: list[Entity] = []
    for detector in (CodeDetector(config), OpaqueIdDetector(config), RegexDetector(config)):
        raw += detector.detect(doc.segments, doc.id)
    assert any(e.entity_type == EntityType.BARCODE and e.end == len(code_text) for e in raw)
    entities, clusters = resolve(raw, config)
    extra = propagate([doc], clusters, entities, config)
    entities, clusters = resolve([*entities, *extra], config, clusters)
    types = {c.entity_type for c in clusters.values()}
    assert {EntityType.BARCODE, EntityType.URL, EntityType.OPAQUE_ID} <= types
    in_body = [e for e in entities if e.segment_id == "body" and e.value == HEX_REF]
    assert in_body, "an id read inside the code is masked everywhere"
    table = Replacer(config).assign(clusters, entities, [doc])
    plan = build_plan(doc, entities, table)
    (rep,) = plan.for_segment("qr")
    assert (rep.start, rep.end) == (0, len(code_text))
    assert rep.substitute == "[CODE_BARRES_001]"


# -- organization policy --------------------------------------------------------------------


def test_default_whitelist_is_bundled() -> None:
    entries = default_whitelist()
    assert "DocuSign" in entries and "gouv.fr" in entries
    assert not any(e.startswith("#") for e in entries)


@pytest.mark.parametrize(
    ("entity_type", "value", "kept"),
    [
        (EntityType.ORGANIZATION, "DocuSign", True),
        (EntityType.ORGANIZATION, "DocuSign, Inc.", True),
        (EntityType.ORGANIZATION, "Adobe Acrobat Sign", True),
        (EntityType.ORGANIZATION, "Transports Valmont", False),
        (EntityType.URL, "https://www.docusign.com/fr/produits", True),
        (EntityType.URL, "https://eu.docusign.net/Signing/", True),
        (EntityType.URL, "lex-persona.com", True),
        (EntityType.ORGANIZATION, "Oodrive Sign", True),
        (EntityType.URL, "www.oodrive-sign.com", True),
        (EntityType.URL, "https://oodrive-valmont.example/", False),
        (EntityType.URL, "https://www.legifrance.gouv.fr/loda/", True),
        (EntityType.URL, "https://www.transports-valmont.example/suivi", False),
        (EntityType.EMAIL, "noreply@docusign.net", True),
        (EntityType.EMAIL, "zephyrin.quintavalle@docusign.net", False),
        (EntityType.PERSON, "Yousign", True),
        (EntityType.PERSON, CANARY_PERSON, False),
        (EntityType.PHONE, "0612345678", False),
    ],
)
def test_keeps_default_policy(entity_type: str, value: str, kept: bool) -> None:
    assert keeps(entity_type, value, Config()) is kept


def test_keeps_other_policies() -> None:
    everything = Config(organization_policy="toutes")
    assert not keeps(EntityType.ORGANIZATION, "DocuSign", everything)
    assert not keeps(EntityType.URL, "https://www.docusign.com/", everything)
    black = Config(organization_policy="liste_noire", organization_blacklist=["Signaturio"])
    assert keeps(EntityType.ORGANIZATION, "Transports Valmont", black)
    assert not keeps(EntityType.ORGANIZATION, "Signaturio SAS", black)
    assert not keeps(EntityType.URL, "https://app.signaturio.example/x", black)
    custom = Config(organization_whitelist=["Signaturio"])
    assert keeps(EntityType.URL, "https://www.signaturio.example/aide", custom)
    assert not keeps(EntityType.ORGANIZATION, "DocuSign", custom)


def test_url_personal_parts() -> None:
    url = "https://acme.sharepoint.com/users/zquintavalle?email=z%40exemple.test&lang=fr"
    parts = [(url[s:e], t) for s, e, t in url_personal_parts(url)]
    assert parts == [
        ("acme", EntityType.ORGANIZATION),
        ("zquintavalle", EntityType.USERNAME),
        ("z%40exemple.test", EntityType.OPAQUE_ID),
    ]


def _analyze(texts: list[str], config: Config) -> tuple[list[Entity], dict[str, object], Document]:
    doc = _doc(
        [seg(t, SegmentKind.OCR if i else SegmentKind.BODY, f"s{i}") for i, t in enumerate(texts)]
    )
    raw: list[Entity] = []
    for detector in (RegexDetector(config), OpaqueIdDetector(config)):
        raw += detector.detect(doc.segments, doc.id)
    raw.append(Entity(EntityType.ORGANIZATION, "DocuSign", doc.id, "s1", 0, 8, 0.8, "test"))
    entities, clusters = resolve(raw, config)
    extra = propagate([doc], clusters, entities, config)
    entities, clusters = resolve([*entities, *extra], config, clusters)
    entities, clusters = apply_policies([doc], entities, clusters, config)
    return entities, clusters, doc  # type: ignore[return-value]


SIGNED = [
    "Voir https://www.docusign.com/fr/aide et https://www.docusign.com/users/zquintavalle "
    f"- Signé par {CANARY_PERSON}",
    "DocuSign",  # the logo, read by OCR
]


def test_whitelisted_publisher_kept_everywhere_personal_parts_masked() -> None:
    entities, clusters, _ = _analyze(SIGNED, Config())
    values = {e.value for e in entities}
    assert "DocuSign" not in values  # logo and name kept
    assert "https://www.docusign.com/fr/aide" not in values
    assert "zquintavalle" in values  # inside a kept URL
    assert CANARY_PERSON in values
    assert not any(c.entity_type == EntityType.URL for c in clusters.values())  # type: ignore[attr-defined]


def test_masked_publisher_masked_everywhere() -> None:
    entities, _, _ = _analyze(SIGNED, Config(organization_policy="toutes"))
    values = [e.value for e in entities]
    assert "https://www.docusign.com/fr/aide" in values
    assert values.count("DocuSign") >= 1  # logo text found by the bundled names
    # A publisher outside the list: its URL is masked, so is its name in the text.
    entities, _, _ = _analyze(["Site https://www.signaturio.example/aide", "Signaturio"], Config())
    assert "Signaturio" in [e.value for e in entities]


def test_blacklisted_name_searched_whatever_the_detectors() -> None:
    config = Config(organization_policy="liste_noire", organization_blacklist=["Signaturio"])
    entities, _, _ = _analyze(["Le prestataire Signaturio a signé.", "x"], config)
    assert "Signaturio" in [e.value for e in entities]
    assert "DocuSign" not in [e.value for e in entities]  # not blacklisted: kept


# -- tags ------------------------------------------------------------------------------------


def _clusters(values: list[str], config: Config) -> tuple[list[Entity], dict[str, object]]:
    entities = [
        Entity(EntityType.PERSON, v, f"d{i}", "s", 0, len(v), 0.9, "test")
        for i, v in enumerate(values)
    ]
    return resolve(entities, config)  # type: ignore[return-value]


def test_sequential_tags_follow_input_order_not_entity_order() -> None:
    config = Config()
    docs = [
        _doc([seg("Zéphyrin Quintavalle")], "a.txt"),
        _doc([seg("Eulalie Vermandois")], "b.txt"),
    ]
    entities = [
        Entity(EntityType.PERSON, "Eulalie Vermandois", docs[1].id, "s", 0, 18, 0.9, "t"),
        Entity(EntityType.PERSON, CANARY_PERSON, docs[0].id, "s", 0, 20, 0.9, "t"),
    ]
    kept, clusters = resolve(entities, config)
    for order in (kept, list(reversed(kept))):
        table = Replacer(config).assign(clusters, order, docs)
        tags = {e.value: table.substitute_for(e) for e in kept}
        assert tags == {CANARY_PERSON: "[PERSONNE_001]", "Eulalie Vermandois": "[PERSONNE_002]"}


def test_random_tags_and_pseudonymization_never_count() -> None:
    config = Config(tag_numbering="aleatoire")
    kept, clusters = _clusters([CANARY_PERSON, "Eulalie Vermandois"], config)
    table = Replacer(config).assign(clusters, kept)
    tags = {table.substitute_for(e) for e in kept}
    assert len(tags) == 2
    assert all(re.fullmatch(r"\[PERSONNE_[A-HJ-KM-NP-Z2-9]{4}\]", t) for t in tags)
    assert all(any(c.isdigit() for c in t) for t in tags)
    # Pseudonymization: a known value keeps its tag, a new one never reveals the size.
    store = InMemoryMappingStore()
    pseudo = Config(regime=Regime.PSEUDONYMIZATION, mode=ReplacementMode.TAG)
    for i in range(5):
        store.save(EntityType.PERSON, f"k{i}", f"v{i}", f"[PERSONNE_00{i + 1}]")
    kept, clusters = _clusters([CANARY_PERSON], pseudo)
    first = Replacer(pseudo, store).assign(clusters, kept).substitute_for(kept[0])
    assert first != "[PERSONNE_006]" and re.fullmatch(r"\[PERSONNE_[A-Z2-9]{4}\]", first)
    kept, clusters = _clusters([CANARY_PERSON], pseudo)
    assert Replacer(pseudo, store).assign(clusters, kept).substitute_for(kept[0]) == first


@pytest.mark.parametrize(
    ("text", "tag"),
    [("[PERSONNE_001]", True), ("[PERSONNE_K7Q2]", True), ("PERSONNE_O01", True),
     ("(EMAIL_7ZQ3)", True), ("TODO_LIST", False), ("MAIN_DATA", False)],
)  # fmt: skip
def test_verifier_recognizes_both_tag_schemes(text: str, tag: bool) -> None:
    assert bool(_TAG_LIKE.search(text)) is tag


def test_file_name_form_joins_the_text_cluster() -> None:
    config = Config()
    entities = [
        Entity(EntityType.PERSON, "zephyrin_QUINTAVALLE-BREZAC", "d", "f", 0, 27, 0.8, "t"),
        Entity(EntityType.PERSON, "Zéphyrin QUINTAVALLE-BREZAC", "d", "s", 0, 27, 0.9, "t"),
    ]
    _, clusters = resolve(entities, config)
    assert len(clusters) == 1


@pytest.mark.parametrize(
    "entity_type", [EntityType.PERSON, EntityType.LOCATION, EntityType.PROPER_NOUN]
)
def test_whitelisted_organization_kept_whatever_its_type(entity_type: str) -> None:
    config = Config(organization_whitelist=["Signaturio"])
    assert keeps(entity_type, "Signaturio", config)
    assert keeps(entity_type, "Signaturio SAS", config)
    assert not keeps(entity_type, CANARY_PERSON, config)
    assert not keeps(entity_type, "Signaturio", Config(organization_policy="toutes"))
