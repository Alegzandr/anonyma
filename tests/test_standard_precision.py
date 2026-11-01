"""Model (GLiNER, LLM) spans on structured exports: identifiers stay whole, schema
stays schema, catalogue names and job titles are not people, references kept on
purpose are no residues (synthetic values only, no model needed)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from anonyma.atomic import AtomicIndex
from anonyma.config import Config
from anonyma.detectors._filters import plausible
from anonyma.detectors.gliner import _skip_document_words, in_context, job_titles_near_people
from anonyma.detectors.opaque import IdWhitelist
from anonyma.detectors.regex_fr import RegexDetector, kept_reference
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
from anonyma.pipeline import extract_file
from anonyma.resolver import cluster_forms
from anonyma.structure import schema_valued
from anonyma.verifier import Verifier

GUID = "054f2f39-6c1a-4bda-9f3e-2b7c41d8e0aa"


# -- atomic tokens -----------------------------------------------------------------


@pytest.mark.parametrize(
    "entity_type", [EntityType.USERNAME, EntityType.ADDRESS, EntityType.PERSON, "PERSONNE"]
)
def test_a_guid_is_an_identifier_or_nothing(entity_type: str) -> None:
    text = f"ownerid {GUID}"
    start = text.index(GUID)
    assert AtomicIndex().fit_typed(text, start, len(text), entity_type) is None


@pytest.mark.parametrize("entity_type", [EntityType.OPAQUE_ID, EntityType.URL, "PROJET"])
def test_identifier_types_keep_a_guid(entity_type: str) -> None:
    text = f"ownerid {GUID}"
    start = text.index(GUID)
    assert AtomicIndex().fit_typed(text, start, len(text), entity_type) == (start, len(text))


def test_a_name_with_a_guid_is_kept() -> None:
    text = f"Jean Dupont {GUID}"
    assert AtomicIndex().fit_typed(text, 0, len(text), EntityType.PERSON) == (0, len(text))


# -- schema values ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "value", "schema"),
    [
        ("_ownerid_value@Microsoft.Dynamics.CRM.lookuplogicalname", "systemuser", True),
        (
            "_ownerid_value@Microsoft.Dynamics.CRM.associatednavigationproperty",
            "ownerid_email",
            True,
        ),
        ("@odata.type", "#Microsoft.Dynamics.CRM.email", False),
        ("_partyid_value@OData.Community.Display.V1.FormattedValue", "Jean Dupont", False),
        ("lookuplogicalname", "Jean Dupont", False),
    ],
)
def test_schema_valued(key: str, value: str, schema: bool) -> None:
    assert schema_valued(key, value) is schema


def test_lookup_annotation_values_are_not_exposed(tmp_path: Path) -> None:
    record = {
        "_ownerid_value": GUID,
        "_ownerid_value@Microsoft.Dynamics.CRM.lookuplogicalname": "systemuser",
        "_ownerid_value@OData.Community.Display.V1.FormattedValue": "Ysaline Tournesol",
    }
    source = tmp_path / "page.json"
    source.write_text(json.dumps({"value": [record]}), encoding="utf-8")
    doc = extract_file(source, "page.json", Config(), tmp_path / "work")
    texts = [s.text for s in doc.segments]
    assert "systemuser" not in texts
    assert "Ysaline Tournesol" in texts


# -- context of model spans ----------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "value", "entity_type"),
    [
        ("la mise en service de la Borne Hexa Charge 22 kW", "Borne Hexa Charge 22 kW", "PERSONNE"),
        ("je souscris l'offre Hexa Flex 24 dès lundi", "Hexa Flex 24", "ORGANISATION"),
        ("les aides (programme Advenir) sont", "Advenir", "ORGANISATION"),
        ("trackingtoken CRM:0004104", "CRM", "ORGANISATION"),
        ("Devis DEV-2026-6304", "DEV", "PERSONNE"),
        ("écrire au Service Client Entreprises", "Service Client Entreprises", "ORGANISATION"),
    ],
)
def test_catalogue_names_and_prefixes_are_not_names(
    text: str, value: str, entity_type: str
) -> None:
    start = text.index(value)
    assert not in_context(entity_type, text, start, start + len(value))


@pytest.mark.parametrize(
    ("text", "value", "entity_type"),
    [
        ("le dossier de Mme Durand est complet", "Durand", "PERSONNE"),
        ("une commande chez Renault à Paris", "Renault", "ORGANISATION"),
        (
            "Gérante | Transports Vercors Logistique",
            "Transports Vercors Logistique",
            "ORGANISATION",
        ),
    ],
)
def test_names_in_context_are_kept(text: str, value: str, entity_type: str) -> None:
    start = text.index(value)
    assert in_context(entity_type, text, start, start + len(value))


@pytest.mark.parametrize("value", ["chambre froide à l'arrêt", "Notre technicien"])
def test_ordinary_phrases_are_not_persons(value: str) -> None:
    assert not plausible(EntityType.PERSON, value, 0.95)


@pytest.mark.parametrize("value", ["Jean Dupont", "Marc Petit", "Notre Dame", "D'Artagnan"])
def test_names_stay_plausible(value: str) -> None:
    assert plausible(EntityType.PERSON, value, 0.95)


def _entity(text: str, value: str, entity_type: str) -> Entity:
    start = text.index(value)
    return Entity(
        entity_type=entity_type,
        value=value,
        document_id="d",
        segment_id="s",
        start=start,
        end=start + len(value),
        score=0.9,
        detector="gliner",
    )


def test_job_titles_need_their_person_close_by() -> None:
    text = (
        "Bonjour, notre technicien va procéder au diagnostic de la borne demain matin "
        "et vous rappellera ensuite.\n\nBien cordialement,\nKarim Ouchène\n"
        "Délégué à la protection des données (DPO)\nHexagone Énergie Services"
    )
    found = job_titles_near_people(
        text,
        [
            _entity(text, "technicien", EntityType.JOB_TITLE),
            _entity(text, "Karim Ouchène", EntityType.PERSON),
            _entity(text, "Délégué à la protection des données", EntityType.JOB_TITLE),
        ],
    )
    titles = [e.value for e in found if e.entity_type == EntityType.JOB_TITLE]
    assert titles == ["Délégué à la protection des données"]


def test_a_title_far_from_any_person_is_dropped() -> None:
    text = "Notre délégué à la protection des données vous répondra.\nCordialement,\nKarim Ouchène"
    found = job_titles_near_people(
        text,
        [
            _entity(text, "délégué à la protection des données", EntityType.JOB_TITLE),
            _entity(text, "Karim Ouchène", EntityType.PERSON),
        ],
    )
    assert [e.entity_type for e in found] == [EntityType.PERSON]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("RIB chateau-lescure", "chateau-lescure"),
        ("CV Bérénice Lagarde-Moreau", "Bérénice Lagarde-Moreau"),
        ("Lettre resiliation riviere", "Lettre resiliation riviere"),
    ],
)
def test_document_words_lead_file_names(text: str, expected: str) -> None:
    assert text[_skip_document_words(text, 0, len(text)) :] == expected


def test_model_only_lone_ordinary_word_does_not_spread() -> None:
    cluster = EntityCluster(id="c", entity_type=EntityType.ORGANIZATION, canonical="Entreprises")
    cluster.variants.add("Entreprises")
    cluster.detectors = {"gliner"}
    cluster.max_score = 0.9
    assert not list(cluster_forms(cluster))
    cluster.detectors = {"gliner", "regex_fr"}
    assert list(cluster_forms(cluster))


# -- references kept on purpose ----------------------------------------------------------

KEEP = Config(mask_opaque_ids=False)


@pytest.mark.parametrize(
    ("text", "value", "kept"),
    [
        ("Facture_FAC-2026-743117.pdf", "FAC-2026-743117", True),
        ("Facture_FAC-2026-743117.pdf", "FAC", True),
        ("Facture FAC-2026-639133", "Facture FAC-2026-639133", True),
        ("Devis_DEV-2026-6304.pdf", "DEV", True),
        ("la facture FAC-2026-6391 de Paul", "FAC-2026-6391 de Paul", False),
        ("mon numéro client HX-C-941260", "HX-C-941260", False),
        ("Facture_PAUL-2026-1234.pdf", "PAUL", False),
    ],
)
def test_kept_references(text: str, value: str, kept: bool) -> None:
    start = text.index(value)
    assert kept_reference(KEEP, IdWhitelist([]), text, start, start + len(value)) is kept


def test_allowed_identifiers_whatever_the_option() -> None:
    text = "ticket CAS-11478-QRV7QP"
    ids = IdWhitelist(["re:^CAS-"])
    assert kept_reference(Config(), ids, text, 7, len(text))
    assert not kept_reference(Config(), IdWhitelist([]), text, 7, len(text))


def _output(text: str, kind: SegmentKind = SegmentKind.HEADER, label: str = "To") -> Document:
    doc = Document(source=Path("x"), name="sortie.txt", format="txt")
    doc.status = DocumentStatus.EXTRACTED
    doc.segments = [Segment("s0", text, (0,), SegmentContext(kind, label))]
    return doc


def test_team_words_around_a_substitute_are_no_residue() -> None:
    doc = _output("Service Client Bruneau S.A.R.L. <service-client@exemple.test>")
    result = Verifier(Config(), [RegexDetector(Config())]).verify(
        doc,
        [("Entreprises", "ORGANISATION")],
        {"Bruneau S.A.R.L."},
        ["service-client@exemple.test"],
    )
    assert result.clean, [(r.reason, r.entity_type) for r in result.residues]


def test_kept_reference_is_no_residue() -> None:
    doc = _output("Pièce jointe : Facture_FAC-2026-743117.pdf", SegmentKind.BODY, "")
    config = Config(mask_opaque_ids=False)
    result = Verifier(config, [RegexDetector(config)]).verify(doc, [], set(), [])
    assert result.clean, [(r.reason, r.entity_type) for r in result.residues]
