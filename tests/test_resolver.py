"""Resolver: variant linking, stable ids, whitelist; propagation everywhere."""

from __future__ import annotations

from pathlib import Path

from anonyma.config import Config
from anonyma.models import (
    FILENAME_SEGMENT_ID,
    Document,
    DocumentStatus,
    Entity,
    EntityCluster,
    ReviewStatus,
    Segment,
    SegmentContext,
    SegmentKind,
)
from anonyma.names import login_forms, parse_person
from anonyma.resolver import identity_key, phone_key, propagate, resolve


def ent(value: str, entity_type: str = "PERSONNE", *, seg: str = "s", start: int = 0) -> Entity:
    return Entity(entity_type, value, "d", seg, start, start + len(value), 0.9, "test")


def mentions(*values: str, entity_type: str = "PERSONNE") -> list[Entity]:
    return [ent(v, entity_type, seg=f"s{i}") for i, v in enumerate(values)]


def clusters_of(entities: list[Entity], config: Config | None = None) -> dict[str, EntityCluster]:
    _, clusters = resolve(entities, config or Config())
    return clusters


# -- names -----------------------------------------------------------------------


def test_parse_person_roles() -> None:
    p = parse_person("M. DUPONT Jean-Pierre")
    assert p is not None
    assert p.civility == "M." and p.surname == ("DUPONT",) and p.given == ("Jean-Pierre",)
    assert p.surname_first and p.gender == "m" and p.initials == "jp"
    q = parse_person("Mme Claire de La Fontaine")
    assert q is not None and q.surname == ("de", "La", "Fontaine") and q.gender == "f"
    initials = parse_person("J. Dupont")
    assert initials is not None and initials.identity_key() == "i:j|dupont"
    reverse = parse_person("Dupont Jean")
    assert reverse is not None and reverse.given == ("Jean",) and reverse.surname_first
    assert login_forms(parse_person("Jean Dupont"))["is"] == "jdupont"  # type: ignore[arg-type]


def test_identity_keys() -> None:
    assert phone_key("+33 (0)6 12 34 56 78") == "0612345678"
    assert phone_key("0033 6 12 34 56 78") == phone_key("06.12.34.56.78")
    assert identity_key("IBAN", "fr76 3000 1007 9400") == identity_key("IBAN", "FR7630001007 9400")
    assert identity_key("IP", "2001:DB8::0:1") == identity_key("IP", "2001:db8::1")
    assert identity_key("EMAIL", "Jean.Dupont@Exemple.test") == "jean.dupont@exemple.test"


# -- clustering ------------------------------------------------------------------


def test_person_variants_share_a_cluster() -> None:
    entities = mentions(
        "Jean Dupont", "J. Dupont", "J. DUPONT", "DUPONT Jean", "Dupont", "jean dupont",
        "M. Dupont", "Jean", "Dupont Jean",
    )  # fmt: skip
    clusters = clusters_of(entities)
    assert len(clusters) == 1
    (cluster,) = clusters.values()
    assert cluster.canonical == "Jean Dupont"
    assert cluster.attributes["gender"] == "m"
    assert len({e.cluster_id for e in entities}) == 1


def test_accents_and_case() -> None:
    clusters = clusters_of(mentions("Zéphyrin Quintavalle", "ZEPHYRIN QUINTAVALLE", "Quintavalle"))
    assert len(clusters) == 1


def test_ambiguous_surname_is_not_linked() -> None:
    entities = mentions("Jean Dupont", "Marie Dupont", "Dupont", "M. Dupont")
    clusters = clusters_of(entities)
    full = {e.value: e.cluster_id for e in entities}
    assert full["Jean Dupont"] != full["Marie Dupont"]
    assert full["Dupont"] not in (full["Jean Dupont"], full["Marie Dupont"])
    assert full["M. Dupont"] == full["Dupont"]
    assert len(clusters) == 3


def test_initials_pick_the_matching_given_name() -> None:
    entities = mentions("Jean Dupont", "Marie Dupont", "M. Dupont", "J. Dupont")
    clusters_of(entities)
    ids = {e.value: e.cluster_id for e in entities}
    assert ids["J. Dupont"] == ids["Jean Dupont"]


def test_structured_types_normalized() -> None:
    entities = [
        *mentions("06 12 34 56 78", "+33 6 12 34 56 78", "0612345678", entity_type="TELEPHONE"),
        *mentions(
            "FR76 3000 1007 9412 3456 7890 185", "FR7630001007941234567890185", entity_type="IBAN"
        ),
    ]
    for i, e in enumerate(entities):
        e.segment_id = f"x{i}"
    clusters = clusters_of(entities)
    assert sorted(c.entity_type for c in clusters.values()) == ["IBAN", "TELEPHONE"]


def test_email_linked_to_person() -> None:
    entities = [
        ent("Jean Dupont", seg="a"),
        ent("jdupont@exemple.test", "EMAIL", seg="b"),
        ent("contact@exemple.test", "EMAIL", seg="c"),
    ]
    clusters = clusters_of(entities)
    person = next(c for c in clusters.values() if c.entity_type == "PERSONNE")
    email = next(c for c in clusters.values() if c.canonical.startswith("jdupont"))
    other = next(c for c in clusters.values() if c.canonical.startswith("contact"))
    assert email.attributes["person"] == person.id
    assert "person" not in other.attributes


def test_dedupe_same_span_keeps_best() -> None:
    low = Entity("ORGANISATION", "Dupont", "d", "s", 0, 6, 0.4, "gliner")
    high = Entity("PERSONNE", "Dupont", "d", "s", 0, 6, 0.9, "regex_fr")
    kept, clusters = resolve([low, high], Config())
    assert len(kept) == 1 and kept[0].entity_type == "PERSONNE"
    (cluster,) = clusters.values()
    assert cluster.detectors == {"gliner", "regex_fr"}


def test_whitelist() -> None:
    config = Config(whitelist=["Paris", "01 23 45 67 89"])
    entities = [
        ent("Paris", "LIEU", seg="a"),
        ent("M. Paris", seg="b"),
        ent("0123456789", "TELEPHONE", seg="c"),
        Entity("LIEU", "Paris", "d", "e", 0, 5, 1.0, "manuel", review=ReviewStatus.MANUAL),
    ]
    kept, _ = resolve(entities, config)
    assert [e.segment_id for e in kept] == ["e"]  # manual additions always win


def test_cluster_ids_are_stable_across_calls() -> None:
    config = Config()
    first = mentions("Jean Dupont", "Claire Fontaine")
    kept, clusters = resolve(first, config)
    ids = {e.value: e.cluster_id for e in kept}
    later = [ent("J. Dupont", seg="z"), ent("Mme Fontaine", seg="y"), ent("Paul Martin", seg="x")]
    kept2, clusters2 = resolve([*kept, *later], config, clusters)
    assert clusters2 is clusters
    by_value = {e.value: e.cluster_id for e in kept2}
    assert by_value["Jean Dupont"] == ids["Jean Dupont"] == by_value["J. Dupont"]
    assert by_value["Mme Fontaine"] == ids["Claire Fontaine"]
    assert len(clusters2) == 3


def test_uncertain_flag() -> None:
    low = Entity("PROJET", "Hélios", "d", "s", 0, 6, 0.4, "x")
    high = Entity("PROJET", "Sélène", "d", "s", 10, 16, 0.9, "x")
    kept, clusters = resolve([low, high], Config())
    assert len(kept) == 2  # an uncertain entity is masked all the same
    flags = {c.canonical: c.uncertain for c in clusters.values()}
    assert flags == {"Hélios": True, "Sélène": False}


# -- propagation -------------------------------------------------------------------


def _doc(*texts: tuple[str, SegmentKind], name: str = "doc.txt") -> Document:
    doc = Document(source=Path("x"), name=name, format="txt", status=DocumentStatus.EXTRACTED)
    doc.segments = [Segment(f"s{i}", t, (i,), SegmentContext(k)) for i, (t, k) in enumerate(texts)]
    doc.segments.append(
        Segment(FILENAME_SEGMENT_ID, name, (), SegmentContext(SegmentKind.FILENAME))
    )
    return doc


def test_propagation_in_file_names_and_urls() -> None:
    config = Config()
    doc = _doc(
        ("Courrier de Jean Dupont.", SegmentKind.BODY),
        ("https://exemple.test/profil/jean-dupont", SegmentKind.ATTRIBUTE),
        ("Dossier DUPONT ; voir jdupont_2023.pdf et JeanDupont.docx", SegmentKind.BODY),
        name="Clients/Dupont_Jean/CV.pdf",
    )
    seed = Entity("PERSONNE", "Jean Dupont", doc.id, "s0", 12, 23, 0.9, "regex_fr")
    kept, clusters = resolve([seed], config)
    extra = propagate([doc], clusters, kept, config)
    values = {(e.segment_id, e.value) for e in extra}
    assert ("s1", "jean-dupont") in values
    assert ("s2", "DUPONT") in values
    assert ("s2", "jdupont") in values
    assert ("s2", "JeanDupont") in values
    assert (FILENAME_SEGMENT_ID, "Dupont_Jean") in values
    assert {e.cluster_id for e in extra} == {seed.cluster_id}
    # Never overlapping what was already found.
    assert not any(e.segment_id == "s0" for e in extra)


def test_propagation_respects_letter_boundaries_and_capitals() -> None:
    config = Config()
    doc = _doc(("Le petit Petit, Dupontel et ADupont.", SegmentKind.BODY))
    seeds = [ent("Petit", seg="other"), ent("Jean Dupont", seg="other2")]
    kept, clusters = resolve(seeds, config)
    values = [e.value for e in propagate([doc], clusters, kept, config)]
    assert values == ["Petit"]  # lower-case common word, glued letters: not matched


def test_propagation_of_numbers_in_other_formats() -> None:
    config = Config()
    doc = _doc(("Rappeler le +33 6 12 34 56 78 ou 06.12.34.56.78", SegmentKind.BODY))
    kept, clusters = resolve([ent("0612345678", "TELEPHONE", seg="z")], config)
    values = sorted(e.value for e in propagate([doc], clusters, kept, config))
    assert values == ["+33 6 12 34 56 78", "06.12.34.56.78"]


def test_person_derived_from_email() -> None:
    config = Config()
    doc = _doc(("Réponse de Claire Fontaine ce matin.", SegmentKind.BODY))
    kept, clusters = resolve([ent("claire.fontaine@exemple.test", "EMAIL", seg="z")], config)
    extra = propagate([doc], clusters, kept, config)
    assert [(e.entity_type, e.value, e.cluster_id) for e in extra] == [
        ("PERSONNE", "Claire Fontaine", None)
    ]
    kept2, clusters2 = resolve([*kept, *extra], config, clusters)
    assert len(clusters2) == 2 and all(e.cluster_id for e in kept2)


def test_rejected_clusters_are_not_propagated() -> None:
    config = Config()
    doc = _doc(("Hélios encore", SegmentKind.BODY))
    kept, clusters = resolve([ent("Hélios", "PROJET", seg="z")], config)
    next(iter(clusters.values())).review = ReviewStatus.REJECTED
    assert propagate([doc], clusters, kept, config) == []


def test_propagation_scales_to_many_segments() -> None:
    config = Config()
    seeds = [ent(f"Personne{i:04d} Nom{i:04d}", seg=f"z{i}") for i in range(300)]
    kept, clusters = resolve(seeds, config)
    doc = _doc(*((f"ligne {i} Nom{i % 300:04d} fin", SegmentKind.CELL) for i in range(3000)))
    extra = propagate([doc], clusters, kept, config)
    assert len(extra) == 3000


# -- leak classes found by the benchmark (tests/benchmark) ------------------------


def test_longer_form_around_a_partial_mention_is_propagated() -> None:
    """``Kowalczyk Jean-Baptiste`` (a JSON key) where only ``Jean-Baptiste`` was found."""
    config = Config()
    doc = _doc(("Kowalczyk Jean-Baptiste", SegmentKind.ATTRIBUTE))
    partial = Entity("PERSONNE", "Jean-Baptiste", doc.id, "s0", 10, 23, 0.65, "regex_fr")
    seed = ent("Jean-Baptiste Kowalczyk", seg="other")
    kept, clusters = resolve([partial, seed], config)
    extra = propagate([doc], clusters, kept, config)
    assert ("s0", "Kowalczyk Jean-Baptiste") in {(e.segment_id, e.value) for e in extra}
    # Forms inside an existing mention add nothing.
    assert not any(e.segment_id == "s0" and e.start >= 10 for e in extra)


def test_initials_are_propagated() -> None:
    config = Config()
    doc = _doc(
        ("Signé F.D.V. ; le badge de J.-B. Kowalczyk ; J.-B. K.", SegmentKind.BODY),
    )
    seeds = [ent("Firmin Delacroix-Vasseur", seg="a"), ent("Jean-Baptiste Kowalczyk", seg="b")]
    kept, clusters = resolve(seeds, config)
    values = {e.value for e in propagate([doc], clusters, kept, config)}
    assert {"F.D.V.", "J.-B. Kowalczyk", "J.-B. K."} <= values


def test_names_glued_by_ocr_are_propagated() -> None:
    config = Config()
    doc = _doc(("responsable RH—TransportsValmontSAS", SegmentKind.OCR))
    kept, clusters = resolve([ent("Transports Valmont SAS", "ORGANISATION", seg="z")], config)
    values = [e.value for e in propagate([doc], clusters, kept, config)]
    assert values == ["TransportsValmontSAS"]


def test_job_titles_stay_in_their_file() -> None:
    """« présidente » next to Mme Vidal is not masked in every other file of the batch."""
    config = Config()
    first = _doc(("Mme Vidal, présidente. La présidente a signé.", SegmentKind.BODY))
    other = _doc(("La présidente du club est absente.", SegmentKind.BODY), name="autre.txt")
    seed = Entity("FONCTION", "présidente", first.id, "s0", 11, 21, 0.8, "gliner")
    kept, clusters = resolve([seed], config)
    extra = propagate([first, other], clusters, kept, config)
    assert {e.document_id for e in extra} == {first.id}


def test_organization_made_of_a_person_and_ordinary_words_is_the_person() -> None:
    """« Dossier Lefèvre » (GLiNER, mail title) is the person, not a company; a name
    with a designator (« Cabinet Lefèvre ») stays an organization."""
    entities = [
        ent("Grégoire Lefèvre", seg="a"),
        ent("Dossier Lefèvre", "ORGANISATION", seg="b"),
        ent("Re dossier Lefèvre", "ORGANISATION", seg="c"),
        ent("Cabinet Lefèvre", "ORGANISATION", seg="d"),
    ]
    kept, clusters = resolve(entities, Config())
    by_seg = {e.segment_id: (e.entity_type, e.value) for e in kept}
    assert by_seg["b"] == ("PERSONNE", "Lefèvre")
    assert by_seg["c"] == ("PERSONNE", "Lefèvre")
    assert by_seg["d"] == ("ORGANISATION", "Cabinet Lefèvre")
    person = next(c for c in clusters.values() if "Grégoire Lefèvre" in c.variants)
    assert {e.cluster_id for e in kept if e.segment_id in "abc"} == {person.id}


def test_ocr_misreadings_of_a_name_join_its_cluster() -> None:
    """The scan of a letter reads « Ludovlc Braquernart »: same person, same tag."""
    entities = [
        ent("Ludovic Braquemart", seg="a"),
        ent("Ludovlc Braquernart", seg="b"),
        ent("BRAQUERNART", seg="c"),
        ent("Ludivine Braquemart", seg="d"),  # another person of the family
    ]
    kept, _clusters = resolve(entities, Config())
    by_seg = {e.segment_id: e.cluster_id for e in kept}
    assert by_seg["a"] == by_seg["b"] == by_seg["c"]
    assert by_seg["d"] != by_seg["a"]
