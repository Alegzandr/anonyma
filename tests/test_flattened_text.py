"""Mail texts as exports give them: signatures flattened on one line, form fields,
function mailboxes, case references, kept organizations and their domains."""

from __future__ import annotations

import random
import re
from pathlib import Path

from anonyma.config import Config
from anonyma.data import first_name
from anonyma.detectors._custom import AddressRecognizer, PersonRecognizer, PlaceRecognizer
from anonyma.detectors.regex_fr import RegexDetector
from anonyma.models import (
    FILENAME_SEGMENT_ID,
    Document,
    DocumentStatus,
    Entity,
    EntityCluster,
    EntityType,
    Profile,
    ReplacementMode,
    Segment,
    SegmentContext,
    SegmentKind,
)
from anonyma.names import after_saint
from anonyma.pipeline import Pipeline
from anonyma.policy import url_personal_parts
from anonyma.resolver import before_brand, brand_keys, generic_local, in_generic_mailbox, propagate
from anonyma.substitutes import random_like
from anonyma.verifier import Verifier


def persons(text: str) -> list[str]:
    return [text[r.start : r.end] for r in PersonRecognizer().analyze(text, ["PERSONNE"])]


def detected(text: str, entity_type: str) -> list[str]:
    segment = Segment("s", text, (), SegmentContext(SegmentKind.BODY))
    return [
        e.value
        for e in RegexDetector(Config()).detect([segment], "d")
        if e.entity_type == entity_type
    ]


# -- names ------------------------------------------------------------------------


def test_words_after_a_given_name_that_are_no_surname() -> None:
    assert "Sylvie Envoyé" not in persons("Bien à vous\nMme Durand Sylvie Envoyé de mon iPhone")
    assert "Durand Sylvie" in persons("Bien à vous\nMme Durand Sylvie Envoyé de mon iPhone")
    assert not any("Holding" in p for p in persons("Contrat signé avec Rose Holding hier."))
    assert not any("Chargée" in p for p in persons("Merci, Lucie Chargée de clientèle"))
    assert not any("Courriel" in p for p in persons("Rose Courriel : rose@exemple.test"))
    assert not any("SERVICE" in p for p in persons("Merci Sylvie SERVICE CLIENTS"))


def test_maiden_name_relative_and_capital_particles() -> None:
    assert "VALMONT" in persons("Mme Jeanne MARTEAU née VALMONT le 03/02/1950")
    assert "VALMONT Gaspard" in persons("le décès de mon oncle VALMONT Gaspard.")
    assert "Jeanne DE VALMONT" in persons("Jeanne DE VALMONT\nResponsable")
    assert not persons("née le 12/03/1950 à Lyon")


def test_civility_in_capitals() -> None:
    assert "VALMONT" in persons("la facture de MME VALMONT pour paiement")
    found = persons("COMMANDE MME VALMONT JEANNE 12345")
    assert found and all(not p.startswith("MME") for p in found)


def test_lone_given_name_between_dashes() -> None:
    assert "Pierre" in persons("Voyez avec notre stagiaire - Pierre - demain matin.")
    assert not persons("- Pierre passe demain")


# -- addresses and places ----------------------------------------------------------


def addresses(text: str) -> list[str]:
    return [text[r.start : r.end] for r in AddressRecognizer().analyze(text, ["ADRESSE"])]


def test_street_name_in_lower_case_after_a_particle() -> None:
    assert "12 rue des tilleuls" in addresses("12 rue des tilleuls\n69002 Lyon")
    assert "5 rue des Lilas le matin je passe" not in addresses("5 rue des Lilas le matin je passe")


def test_city_stops_before_contact_labels() -> None:
    assert "76000 Valmont" in addresses("76000 Valmont Tél. 02 00 00 00 00")
    assert not any("Tél" in a for a in addresses("76000 Valmont Tél. 02 00 00 00 00"))
    assert "1 Bld S. Valmont" in addresses("1 Bld S. Valmont – 76000 Lyon")


def test_place_of_an_event() -> None:
    places = [
        t[r.start : r.end]
        for t in ("accident survenu à VALMONT hier", "hospitalisé le 03/02/2026 à Valmont")
        for r in PlaceRecognizer().analyze(t, ["LIEU"])
    ]
    assert places == ["VALMONT", "Valmont"]


def test_date_after_another_event_is_no_birth_date() -> None:
    text = "Mme Jeanne Valmont née Marteau, mariée le 3 mars 2020 à Lyon"
    assert detected(text, EntityType.DATE) == ["3 mars 2020"]
    assert detected("Jeanne Valmont née le 12/03/1950", EntityType.DATE_OF_BIRTH)


# -- references, addresses, links ---------------------------------------------------


def test_case_references_without_keyword() -> None:
    refs = detected("ABC012345 - VALMONT", EntityType.CASE_NUMBER)
    refs += detected("[#TCK5A7B91C3D] Suivi de votre demande", EntityType.CASE_NUMBER)
    refs += detected("commande N°48XYZ0012 merci", EntityType.CASE_NUMBER)
    refs += detected("DOSSIER M. VALMONT PAUL 183456780", EntityType.CASE_NUMBER)
    assert refs == ["ABC012345", "TCK5A7B91C3D", "48XYZ0012", "183456780"]
    assert not detected("Normes ISO9001 et N° 12", EntityType.CASE_NUMBER)


def test_mail_domain_cut_by_a_space_and_quoted_address() -> None:
    assert not detected("j.martin @valmont-transports.fr", EntityType.USERNAME)
    assert detected("; 'achats@valmont.fr' <achats@valmont.fr>", EntityType.EMAIL) == [
        "achats@valmont.fr",
        "achats@valmont.fr",
    ]
    assert detected("lien.example/p?uid=AB12CD&page=2", EntityType.USERNAME) == ["AB12CD"]


def test_query_values_of_kept_links() -> None:
    url = "https://x.example/p?lang=francais&to=achats@valmont.fr&id=AB12CD34"
    parts = [(url[s:e], t) for s, e, t in url_personal_parts(url)]
    assert parts == [
        ("achats@valmont.fr", EntityType.EMAIL),
        ("AB12CD34", EntityType.OPAQUE_ID),
    ]


def test_function_mailboxes() -> None:
    assert generic_local("gestioncommandes")
    assert generic_local("servicepaie")
    assert not generic_local("jdupont")
    assert not generic_local("pierremartin")
    text = "écrire à gestion_atlas@valmont.fr"
    start = text.index("atlas")
    assert in_generic_mailbox(text, start, start + 5)
    text = "écrire à jean.petit@valmont.fr"
    start = text.index("petit")
    assert not in_generic_mailbox(text, start, start + 5)


def test_given_name_after_saint_is_a_place() -> None:
    text = "à Saint-Étienne"
    assert after_saint(text, text.index("Étienne"))
    assert not after_saint("chez Étienne", 5)


def test_hexadecimal_identifier_stays_hexadecimal() -> None:
    fake = random_like("3f2b8c1e-4a6d-4e8a-b93c-0d1e2f3a4b5c", random.Random(1))
    assert re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", fake)


# -- organizations of the batch ------------------------------------------------------


def mention(value: str, entity_type: str, seg: str = "s") -> Entity:
    return Entity(entity_type, value, "d", seg, 0, len(value), 0.9, "test")


def test_brands_and_names_written_before_them() -> None:
    entities = [
        mention("ventes@transports-valmont.fr", EntityType.EMAIL),
        mention("https://www.transports-valmont.fr/", EntityType.URL),
        mention("rose@exemple.fr", EntityType.EMAIL),
    ]
    brands = brand_keys(entities)
    assert "transportsvalmont" in brands
    assert "exemple" not in brands
    value = "Sylvie Transports Valmont"
    span = before_brand(value, brands)
    assert span is not None and value[span[0] : span[1]] == "Sylvie"
    assert before_brand("Sylvie Durand", brands) is None


def test_verifier_derives_uncertain_values_as_propagation() -> None:
    doc = Document(source=Path("x"), name="o.txt", format="txt", status=DocumentStatus.EXTRACTED)
    doc.segments = [Segment("s", "Le Bureau est fermé.", (), SegmentContext(SegmentKind.BODY))]
    sensitive = [("Sylvie Bureau", "PERSONNE")]
    verifier = Verifier(Config(), [])
    assert verifier.verify(doc, sensitive, set(), []).residues
    assert not verifier.verify(doc, sensitive, set(), [], uncertain={"Sylvie Bureau"}).residues


def test_verifier_substitute_before_a_kept_organization() -> None:
    config = Config(organization_policy="liste_noire")
    doc = Document(source=Path("x"), name="o.txt", format="txt", status=DocumentStatus.EXTRACTED)
    doc.segments = [Segment("s", "Paul Transports Valmont", (), SegmentContext(SegmentKind.BODY))]
    verifier = Verifier(config, [RegexDetector(config)])
    assert verifier.verify(doc, [], {"Paul"}, []).residues
    assert not verifier.verify(doc, [], {"Paul"}, [], brands={"transportsvalmont"}).residues


def test_names_never_propagated_over_a_line_break() -> None:
    cluster = EntityCluster(
        id="c1",
        entity_type=EntityType.LOCATION,
        canonical="Valmont Agence",
        variants={"Valmont Agence"},
    )
    doc = Document(source=Path("x"), name="d.txt", format="txt", status=DocumentStatus.EXTRACTED)
    doc.segments = [
        Segment("s0", "Née à Valmont\nAgence de Lyon", (0,), SegmentContext(SegmentKind.BODY)),
        Segment(FILENAME_SEGMENT_ID, "d.txt", (), SegmentContext(SegmentKind.FILENAME)),
    ]
    found = propagate([doc], {"c1": cluster}, [], Config())
    assert not any("\n" in e.value for e in found)


# -- whole batches ------------------------------------------------------------------


def realistic(policy: str = "liste_noire") -> Config:
    return Config(
        profile=Profile.LIGHT,
        workers=1,
        organization_policy=policy,
        mode=ReplacementMode.REALISTIC,
    )


def run(tmp_path: Path, files: dict[str, str], config: Config) -> dict[str, str]:
    src = tmp_path / "src"
    src.mkdir()
    for name, text in files.items():
        (src / name).write_text(text, encoding="utf-8")
    result = Pipeline(config).run([src], tmp_path / "out")
    assert all(r.status is DocumentStatus.ANONYMIZED for r in result.results), [
        r.status for r in result.results
    ]
    return {p.name: p.read_text(encoding="utf-8") for p in (tmp_path / "out").rglob("*.txt")}


def test_kept_domain_stays_in_masked_addresses(tmp_path: Path) -> None:
    text = (
        "Bonjour,\nMerci pour votre commande.\n\nJean Marteau\nTransports Valmont\n"
        "jean.marteau@transports-valmont.fr\nventes@transports-valmont.fr\n"
    )
    out = run(tmp_path, {"a.txt": text}, realistic())
    body = next(iter(out.values()))
    assert "jean.marteau@" not in body
    assert re.search(r"[\w.]+@transports-valmont\.fr", body)
    assert "ventes@transports-valmont.fr" in body


def test_form_given_names_and_shared_given_name(tmp_path: Path) -> None:
    files = {
        "form.txt": "Civilité : Madame\nNom : VALMONT\nPrénom : JEANNE ALICE\n",
        "b.txt": "Rendez-vous avec Pauline Marteau demain.\n",
        "c.txt": "Rendez-vous avec Pauline Lacombe demain.\n",
        "d.txt": "Bien reçu, merci.\n\nPauline\n",
    }
    out = run(tmp_path, files, realistic())
    form = next(v for k, v in out.items() if "Civilité" in v)
    given = re.search(r"Prénom : (.+)", form)
    assert given is not None
    names = given.group(1).split()
    assert len(names) == 2, names
    assert all(first_name(p) is not None for w in names for p in w.split("-")), names
    assert not any(re.search(r"\[[A-Za-z_]+_[A-Z0-9a-z]{3,}\]", v) for v in out.values())


def test_name_over_whole_lines_keeps_its_lines(tmp_path: Path) -> None:
    files = {
        "a.txt": "Bien à vous,\n\nGaspard\n\nVALMONT\nResponsable des achats\n",
        "b.txt": "Signé : Gaspard VALMONT\n",
    }
    out = run(tmp_path, files, realistic())
    body = next(v for v in out.values() if "Bien à vous" in v)
    assert "Gaspard" not in body and "VALMONT" not in body
    assert len(body.splitlines()) == 6, body
    assert body.splitlines()[5] == "Responsable des achats"
