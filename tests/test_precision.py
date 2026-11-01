"""Precision without recall loss: functional names, business references, lone ordinary
words, file-name forms (synthetic values only)."""

from __future__ import annotations

from pathlib import Path

import pytest

from anonyma.config import Config
from anonyma.detectors.opaque import IdWhitelist
from anonyma.detectors.regex_fr import RegexDetector
from anonyma.models import (
    Document,
    DocumentStatus,
    Entity,
    EntityCluster,
    EntityType,
    Segment,
    SegmentContext,
    SegmentKind,
)
from anonyma.names import is_functional_name, parse_person
from anonyma.resolver import cluster_forms, generic_local, propagate, reliable_person, resolve
from anonyma.textsearch import MultiMatcher, in_identifier


def _detect(config: Config, text: str, *, label: str = "", kind: SegmentKind = SegmentKind.BODY):
    detector = RegexDetector(config)
    return detector.detect([Segment("s", text, (), SegmentContext(kind, label))], "d")


def _values(entities: list[Entity], entity_type: str) -> list[str]:
    return [e.value for e in entities if e.entity_type == entity_type]


# -- functional names ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "local"),
    [
        ("Service Client Entreprises", "service-client"),
        ("Protection des données", "dpo"),
        ("Projets IRVE", "irve"),
        ("Facturation Entreprises", "facturation"),
        ("Hexagone Facturation 4.2", ""),
        ("Promo", "offres"),
    ],
)
def test_functional_names(value: str, local: str) -> None:
    assert is_functional_name(value, local)


@pytest.mark.parametrize(
    ("value", "local"),
    [
        ("Pierre Boulanger", ""),
        ("Boulanger", ""),
        ("Petit Boulanger", ""),
        ("Marc Petit", ""),
        ("Service Client", "jean.dupont"),
        ("DUPONT Service", ""),
    ],
)
def test_person_names_are_not_functional(value: str, local: str) -> None:
    assert not is_functional_name(value, local)


def test_mailbox_display_names_are_not_persons() -> None:
    text = (
        "Service Client Entreprises <service-client@exemple.test>, "
        "Protection des données <dpo@exemple.test>, Ysaline Tournesol <yt@exemple.test>"
    )
    persons = _values(_detect(Config(), text, label="To", kind=SegmentKind.HEADER), "PERSONNE")
    assert persons == ["Ysaline Tournesol"]


def test_software_creator_is_not_a_person() -> None:
    found = _detect(
        Config(), "Hexagone Facturation 4.2", label="creator", kind=SegmentKind.METADATA
    )
    assert not _values(found, "PERSONNE")


@pytest.mark.parametrize(
    ("local", "generic"),
    [
        ("service-client", True),
        ("offres", True),
        ("ne-pas-repondre", True),
        ("dpo", True),
        ("jean.dupont", False),
        ("jdupont", False),
        ("rose.martin", False),
    ],
)
def test_generic_local_parts(local: str, generic: bool) -> None:
    assert generic_local(local) is generic


def test_reliable_persons() -> None:
    def reliable(value: str) -> bool:
        person = parse_person(value)
        assert person is not None
        return reliable_person(person)

    assert reliable("Thi Lan Lambert") and reliable("Marc Petit")
    assert reliable("Zéphyrin Quintavalle") and reliable("Olivier Rivière")
    assert not reliable("Facturation Entreprises")
    assert not reliable("Petit Boulanger")


# -- propagation -----------------------------------------------------------------------


def _cluster(entity_type: str, canonical: str, *variants: str, score: float = 0.9):
    cluster = EntityCluster(id="c", entity_type=entity_type, canonical=canonical)
    cluster.variants.update({canonical, *variants})
    cluster.max_score = score
    cluster.uncertain = score < 0.6
    return cluster


def test_team_cluster_gives_no_derived_forms() -> None:
    forms = {f for f, _ in cluster_forms(_cluster("PERSONNE", "Facturation Entreprises"))}
    assert forms <= {"Facturation Entreprises", "FacturationEntreprises"}


def test_weak_lone_ordinary_word_not_propagated() -> None:
    assert not list(cluster_forms(_cluster("ORGANISATION", "Nouvelle", score=0.4)))


def test_function_mailbox_local_part_not_searched() -> None:
    forms = {f for f, _ in cluster_forms(_cluster("EMAIL", "offres@exemple.test"))}
    assert "offres" not in forms


def test_common_word_surname_still_needs_capital() -> None:
    forms = dict(cluster_forms(_cluster("PERSONNE", "Marc Petit")))
    assert forms.get("Petit") is True  # still searched, capitalized only


def test_month_never_searched_alone() -> None:
    forms = {f for f, _ in cluster_forms(_cluster("PERSONNE", "Jeanne Septembre"))}
    assert "Septembre" not in forms
    assert "Jeanne Septembre" in forms


def _doc(text: str, name: str = "a.txt") -> Document:
    doc = Document(source=Path(name), name=name, format="txt")
    doc.status = DocumentStatus.EXTRACTED
    doc.segments.append(Segment("s0", text, (), SegmentContext(SegmentKind.BODY, "")))
    return doc


def _propagated(text: str, *mentions: tuple[str, str]) -> list[str]:
    source = _doc("\n".join(m for _, m in mentions), "source.txt")
    target = _doc(text, "cible.txt")
    entities = []
    pos = 0
    full = source.segments[0].text
    for entity_type, value in mentions:
        start = full.index(value, pos)
        pos = start + len(value)
        entities.append(
            Entity(
                entity_type=entity_type,
                value=value,
                document_id=source.id,
                segment_id="s0",
                start=start,
                end=pos,
                score=0.9,
                detector="regex_fr",
            )
        )
    config = Config()
    kept, clusters = resolve(entities, config)
    found = propagate([source, target], clusters, kept, config)
    return [e.value for e in found if e.document_id == target.id]


def test_known_surname_found_in_lower_case_file_name() -> None:
    found = _propagated(
        "Pièce jointe : lettre_resiliation_riviere.docx, voir la rivière.",
        ("PERSONNE", "Olivier Rivière"),
    )
    assert "riviere" in found
    assert "rivière" not in found  # prose stays readable


def test_given_name_after_layout_spaces_is_masked() -> None:
    found = _propagated("Cordialement,\nThi     Lan Lambert", ("PERSONNE", "Lan Lambert"))
    assert any(v.startswith("Thi") for v in found)


@pytest.mark.parametrize(
    ("text", "word", "expected"),
    [
        ("lettre_resiliation_riviere.docx", "riviere", True),
        ("contrat-riviere.pdf", "riviere", True),
        ("riviere.pdf", "riviere", True),
        ("la riviere. Le", "riviere", False),
        ("la riviere est", "riviere", False),
    ],
)
def test_in_identifier(text: str, word: str, expected: bool) -> None:
    i = text.index(word)
    assert in_identifier(text, i, i + len(word)) is expected


def test_capital_only_needle_in_file_name() -> None:
    matcher: MultiMatcher[str] = MultiMatcher()
    matcher.add("Rivière", "p", require_capital=True)
    assert [m.start for m in matcher.find("dossier_riviere.pdf et la rivière")] == [8]


# -- business references -----------------------------------------------------------------

REFERENCES = (
    "Votre demande est enregistrée sous la référence CAS-11478-QRV7QP. "
    "Le contrat CTR-4297539 et la facture FAC-2026-166002 sont joints. "
    "Mon numéro client est le HX-C-941260. Passeport n° 12AB34567."
)


def test_references_masked_by_default() -> None:
    found = _values(_detect(Config(), REFERENCES), "NUMERO_DOSSIER")
    assert {"CAS-11478-QRV7QP", "CTR-4297539", "FAC-2026-166002", "HX-C-941260"} <= set(found)


def test_allowed_identifier_prefix() -> None:
    found = _values(_detect(Config(opaque_id_whitelist=["re:^CAS-"]), REFERENCES), "NUMERO_DOSSIER")
    assert "CAS-11478-QRV7QP" not in found
    assert "FAC-2026-166002" in found


def test_id_whitelist_semantics() -> None:
    allowed = IdWhitelist(["re:^CAS-", r"re:FAC-\d{4}-\d{6}", "CTR-1", "re:(broken"])
    assert allowed.allows("CAS-11478-QRV7QP")
    assert allowed.allows("FAC-2026-166002") and not allowed.allows("XFAC-2026-166002")
    assert allowed.allows("ctr-1") and not allowed.allows("CTR-12")
    assert not IdWhitelist([])


def test_whitelisted_reference_from_any_detector_is_dropped() -> None:
    entity = Entity(
        entity_type=EntityType.CASE_NUMBER,
        value="CAS-11478-QRV7QP",
        document_id="d",
        segment_id="s",
        start=0,
        end=16,
        score=0.9,
        detector="gliner",
    )
    kept, _ = resolve([entity], Config(opaque_id_whitelist=["re:^CAS-"]))
    assert not kept


# -- survivors -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["Badge : Bérènice Lagarde-Moreau", "Badge : Berenlce Lagarde-Moreau", "Titulaire : J. Durand"],
)
def test_badge_cue_gives_a_person(text: str) -> None:
    assert _values(_detect(Config(), text, kind=SegmentKind.OCR), "PERSONNE")


def test_badge_cue_needs_a_name() -> None:
    assert not _values(_detect(Config(), "Badge : accès parking"), "PERSONNE")


@pytest.mark.parametrize(
    "zone",
    ["ZA des Écureuils", "ZI Nord", "ZAC des Portes de l'Ouest", "Parc d'activités du Moulin"],
)
def test_business_park_addresses(zone: str) -> None:
    found = _values(_detect(Config(), f"Le site se trouve {zone}, au fond."), "ADRESSE")
    assert zone in found


def test_signature_line_organisation() -> None:
    text = "Thi Lan Lambert\nGérante | Transports Vercors Logistique\nTél. : 01 23 45 67 89 | Fax"
    assert _values(_detect(Config(), text), "ORGANISATION") == ["Transports Vercors Logistique"]


def test_street_name_stops_before_postcode() -> None:
    text = "Leduc\n88 rue de Lenoir 55846 Mendès\nMob. : 06 39 98 50 08"
    assert "88 rue de Lenoir 55846 Mendès" in _values(_detect(Config(), text), "ADRESSE")
    assert not any("Mob" in v for v in _values(_detect(Config(), text), "ADRESSE"))


def test_street_name_does_not_make_a_timestamp() -> None:
    text = "coupure entre 8h et 14h sur notre site du 97 boulevard Louise Mahe, 70510 Carre."
    found = _values(_detect(Config(), text), "DATE")
    assert "8h" not in found and "14h" not in found


@pytest.mark.parametrize(
    "text",
    ["Bonne journée,\nÉlodie\nEnvoyé de mon iPhone", "Merci d'avance,\nLaëtitia\nEnvoyé"],
)
def test_closings_announce_the_signer(text: str) -> None:
    assert _values(_detect(Config(), text), "PERSONNE")


def test_function_mailbox_keeps_its_local_part_in_realistic_mode(tmp_path: Path) -> None:
    from anonyma.models import ReplacementMode
    from anonyma.pipeline import Pipeline

    source = tmp_path / "in"
    source.mkdir()
    (source / "a.txt").write_text(
        "Écrire à offres@cartouches.example ou à ysaline.tournesol@cartouches.example.",
        encoding="utf-8",
    )
    config = Config(mode=ReplacementMode.REALISTIC, workers=1)
    result = Pipeline(config).run([source], tmp_path / "out")
    assert result.results
    text = next((tmp_path / "out").rglob("a.txt")).read_text(encoding="utf-8")
    assert "offres@" in text and "cartouches.example" not in text
    assert "ysaline" not in text.lower()


@pytest.mark.parametrize(
    "css", ["@page WordSection1 {size:612pt}", "@font-face {font-family:Aptos}"]
)
def test_css_at_rules_are_not_handles(css: str) -> None:
    assert not _values(_detect(Config(), css), "IDENTIFIANT")


def test_initials_not_found_inside_identifiers() -> None:
    matcher: MultiMatcher[str] = MultiMatcher()
    matcher.add("A.B", "p")
    found = [m.start for m in matcher.find("image004.png@01DBDC2A.B5B90004, signé A.B")]
    assert found == [38]
