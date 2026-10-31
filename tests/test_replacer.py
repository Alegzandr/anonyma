"""Replacer: tags, realistic substitutes (consistency, gender, format), blackout."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from anonyma import checksums
from anonyma.config import Config
from anonyma.data import gender_of
from anonyma.models import (
    Document,
    DocumentStatus,
    Entity,
    EntityCluster,
    Regime,
    ReplacementMode,
    Segment,
    SegmentContext,
    SegmentKind,
)
from anonyma.names import parse_person
from anonyma.replacer import BLACKOUT_CHAR, Replacer, SubstitutionTable, build_plan
from anonyma.resolver import resolve
from anonyma.store import InMemoryMappingStore
from anonyma.substitutes import match_case, shift_date
from anonyma.textutils import strip_accents


def ent(value: str, entity_type: str = "PERSONNE", seg: str = "s") -> Entity:
    return Entity(entity_type, value, "d", seg, 0, len(value), 0.9, "test")


def assign(
    values: list[tuple[str, str]], mode: ReplacementMode, **config: object
) -> tuple[SubstitutionTable, list[Entity], dict[str, EntityCluster]]:
    cfg = Config(mode=mode, **config)  # type: ignore[arg-type]
    entities = [ent(v, t, f"s{i}") for i, (v, t) in enumerate(values)]
    kept, clusters = resolve(entities, cfg)
    store = InMemoryMappingStore() if cfg.regime is Regime.PSEUDONYMIZATION else None
    return Replacer(cfg, store).assign(clusters, kept), kept, clusters


def ascii_of(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", strip_accents(name).casefold())


def sub(table: SubstitutionTable, entities: list[Entity], value: str) -> str:
    entity = next(e for e in entities if e.value == value)
    return table.substitute_for(entity)


# -- tags ---------------------------------------------------------------------------


def test_tags_numbered_by_first_appearance_and_civility_kept() -> None:
    table, kept, _ = assign(
        [("Claire Fontaine", "PERSONNE"), ("Jean Dupont", "PERSONNE"), ("M. Dupont", "PERSONNE"),
         ("jean@exemple.test", "EMAIL")],
        ReplacementMode.TAG,
    )  # fmt: skip
    assert sub(table, kept, "Claire Fontaine") == "[PERSONNE_001]"
    assert sub(table, kept, "Jean Dupont") == "[PERSONNE_002]"
    assert sub(table, kept, "M. Dupont") == "M. [PERSONNE_002]"
    assert sub(table, kept, "jean@exemple.test") == "[EMAIL_001]"


def test_blackout_keeps_length() -> None:
    table, kept, _ = assign([("Jean Dupont", "PERSONNE")], ReplacementMode.BLACKOUT)
    assert sub(table, kept, "Jean Dupont") == BLACKOUT_CHAR * len("Jean Dupont")


# -- realistic persons ----------------------------------------------------------------


def test_realistic_person_variants_are_consistent() -> None:
    values = ["Jean Dupont", "J. Dupont", "DUPONT Jean", "M. Dupont", "jdupont", "Dupont_Jean",
              "JEAN DUPONT"]  # fmt: skip
    table, kept, _ = assign([(v, "PERSONNE") for v in values], ReplacementMode.REALISTIC)
    full = sub(table, kept, "Jean Dupont")
    person = parse_person(full)
    assert person is not None and len(person.given) == 1 and len(person.surname) == 1
    given, surname = person.given[0], person.surname[0]
    assert given != "Jean" and surname != "Dupont"
    assert sub(table, kept, "J. Dupont") == f"{given[0]}. {surname}"
    assert sub(table, kept, "DUPONT Jean") == f"{surname.upper()} {given}"
    assert sub(table, kept, "M. Dupont") == f"M. {surname}"
    assert sub(table, kept, "JEAN DUPONT") == f"{given.upper()} {surname.upper()}"
    ascii_surname = ascii_of(surname)
    assert sub(table, kept, "jdupont").endswith(ascii_surname)
    assert sub(table, kept, "jdupont")[0] == ascii_of(given)[0]
    assert sub(table, kept, "Dupont_Jean").split("_")[0].lower() == ascii_surname


@pytest.mark.parametrize(
    ("value", "gender"),
    [("Mme Claire Fontaine", "f"), ("Bérénice Martin", "f"), ("M. Paul Petit", "m")],
)
def test_realistic_person_keeps_gender(value: str, gender: str) -> None:
    for _ in range(5):
        table, kept, _ = assign([(value, "PERSONNE")], ReplacementMode.REALISTIC)
        fake = parse_person(sub(table, kept, value))
        assert fake is not None and gender_of(fake.given[0]) == gender


def test_case_variants_follow_each_mention() -> None:
    table, kept, _ = assign(
        [("Claire Fontaine", "PERSONNE"), ("Claire FONTAINE", "PERSONNE")],
        ReplacementMode.REALISTIC,
    )
    a, b = sub(table, kept, "Claire Fontaine"), sub(table, kept, "Claire FONTAINE")
    given, surname = a.split(" ")
    assert b == f"{given} {surname.upper()}"
    assert match_case("Claire FONTAINE", "Paul Martin") == "Paul MARTIN"


def test_no_collision_with_real_values() -> None:
    names = [f"{g} {s}" for g, s in zip(
        ["Jean", "Marie", "Pierre", "Sophie", "Luc", "Anne"],
        ["Martin", "Bernard", "Dubois", "Thomas", "Robert", "Richard"], strict=True
    )]  # fmt: skip
    table, _kept, _clusters = assign([(n, "PERSONNE") for n in names], ReplacementMode.REALISTIC)
    real_tokens = {t.lower() for n in names for t in n.split()}
    substitutes = list(table.by_cluster.values())
    assert len(set(substitutes)) == len(names)
    for s in substitutes:
        assert not {t.lower() for t in s.split()} & real_tokens


# -- realistic contact and identifiers --------------------------------------------------


def test_email_built_from_substitute_person() -> None:
    table, kept, _ = assign(
        [("Jean Dupont", "PERSONNE"), ("jean.dupont@dupont-avocats.test", "EMAIL"),
         ("jdupont@gmail.com", "EMAIL")],
        ReplacementMode.REALISTIC,
    )  # fmt: skip
    person = parse_person(sub(table, kept, "Jean Dupont"))
    assert person is not None
    ascii_given = ascii_of(person.given[0])
    email = sub(table, kept, "jean.dupont@dupont-avocats.test")
    local, domain = email.split("@")
    assert local.startswith(ascii_given + ".") and domain.endswith(".example")
    other = sub(table, kept, "jdupont@gmail.com")
    assert other.endswith("@gmail.com") and other[0] == ascii_given[0]


def test_phone_keeps_format_and_variants() -> None:
    table, kept, _ = assign(
        [("06 12 34 56 78", "TELEPHONE"), ("+33 (0)6 12 34 56 78", "TELEPHONE"),
         ("0612345678", "TELEPHONE")],
        ReplacementMode.REALISTIC,
    )  # fmt: skip
    spaced = sub(table, kept, "06 12 34 56 78")
    intl = sub(table, kept, "+33 (0)6 12 34 56 78")
    compact = sub(table, kept, "0612345678")
    assert re.fullmatch(r"06 \d\d \d\d \d\d \d\d", spaced)
    assert re.fullmatch(r"\+33 \(0\)6 \d\d \d\d \d\d \d\d", intl)
    assert compact == spaced.replace(" ", "") != "0612345678"
    assert compact.startswith("063998")  # ARCEP range reserved for fiction


def test_identifiers_have_valid_checksums_and_shape() -> None:
    bank, branch, account = "30001", "00794", "00012345678"
    bban = bank + branch + account + checksums.rib_key(bank, branch, account)
    iban = "FR" + checksums.iban_check_digits("FR", bban) + bban
    grouped = " ".join(iban[i : i + 4] for i in range(0, len(iban), 4))
    card_body = "497010123456789"
    card = card_body + checksums.luhn_check_digit(card_body)
    siren_body = "73282932"
    siren = siren_body + checksums.luhn_check_digit(siren_body)
    siret_body = siren + "0007"
    siret = siret_body + checksums.luhn_check_digit(siret_body)
    nir = "1850478006084" + (checksums.nir_key("1850478006084") or "")
    table, kept, _ = assign(
        [(grouped, "IBAN"), (card, "CARTE_BANCAIRE"), (siren, "SIREN"), (siret, "SIRET"),
         (nir, "NIR"), ("AB-123-CD", "PLAQUE"), ("192.168.1.20", "IP")],
        ReplacementMode.REALISTIC,
    )  # fmt: skip
    fake_iban = sub(table, kept, grouped)
    assert checksums.iban_valid(fake_iban) and fake_iban != grouped
    assert [len(g) for g in fake_iban.split(" ")] == [len(g) for g in grouped.split(" ")]
    assert checksums.luhn_valid(sub(table, kept, card))
    fake_siren, fake_siret = sub(table, kept, siren), sub(table, kept, siret)
    assert checksums.siren_valid(fake_siren) and checksums.siret_valid(fake_siret)
    assert fake_siret.startswith(fake_siren)  # SIREN/SIRET relation kept
    assert checksums.nir_valid(sub(table, kept, nir))
    assert re.fullmatch(r"[A-HJ-NP-TV-Z]{2}-\d{3}-[A-HJ-NP-TV-Z]{2}", sub(table, kept, "AB-123-CD"))
    assert sub(table, kept, "192.168.1.20").startswith(("192.0.2.", "198.51.100.", "203.0.113."))


def test_dates_shifted_consistently() -> None:
    table, kept, _ = assign(
        [("03/04/1985", "DATE_NAISSANCE"), ("10/04/1985", "DATE"), ("1er mars 2021", "DATE"),
         ("2021-03-08", "DATE")],
        ReplacementMode.REALISTIC,
    )  # fmt: skip
    from datetime import date

    a = sub(table, kept, "03/04/1985")
    b = sub(table, kept, "10/04/1985")
    da = date(int(a[6:]), int(a[3:5]), int(a[:2]))
    db = date(int(b[6:]), int(b[3:5]), int(b[:2]))
    assert (db - da).days == 7 and da != date(1985, 4, 3)
    iso = sub(table, kept, "2021-03-08")
    textual = sub(table, kept, "1er mars 2021")
    shift = (da - date(1985, 4, 3)).days
    assert iso == (
        date(2021, 3, 8).toordinal() + shift
        and date.fromordinal(date(2021, 3, 8).toordinal() + shift).isoformat()
    )
    assert re.fullmatch(r"(1er|\d{1,2}) \w+ \d{4}", textual)


def test_shift_date_formats() -> None:
    assert shift_date("31/12/1999", 1) == "01/01/2000"
    assert shift_date("1er janvier 2020", -1) == "31 décembre 2019"
    assert shift_date("3 MARS 2021", 1) == "4 MARS 2021"
    assert shift_date("March 3, 1985", 1) == "March 4, 1985"
    assert shift_date("12.03.85", 1) == "13.03.85"
    assert shift_date("pas une date", 1) is None


def test_other_types_realistic_and_fallback_tags() -> None:
    table, kept, _ = assign(
        [("12 rue des Lilas, 75011 Paris", "ADRESSE"), ("Nantes", "LIEU"),
         ("Lemaire Conseil SARL", "ORGANISATION"), ("PROJET-X", "PROJET")],
        ReplacementMode.REALISTIC,
    )  # fmt: skip
    address = sub(table, kept, "12 rue des Lilas, 75011 Paris")
    assert re.fullmatch(r"\d+ .+, \d{5} .+", address) and "Lilas" not in address
    assert sub(table, kept, "Nantes") != "Nantes"
    assert sub(table, kept, "PROJET-X") == "[PROJET_001]"  # no realistic generator


def test_pseudonymization_realistic_is_stable_across_batches() -> None:
    store = InMemoryMappingStore()
    cfg = Config(mode=ReplacementMode.REALISTIC, regime=Regime.PSEUDONYMIZATION)
    outputs = []
    for _ in range(2):
        kept, clusters = resolve([ent("Jean Dupont"), ent("03/04/1985", "DATE", "t")], cfg)
        table = Replacer(cfg, store).assign(clusters, kept)
        outputs.append([table.substitute_for(e) for e in kept])
    assert outputs[0] == outputs[1]
    assert store.reverse(outputs[0][0]) in ("Jean Dupont", "03/04/1985")


# -- plans ------------------------------------------------------------------------------


def test_plan_merges_overlaps_with_longest_head() -> None:
    doc = Document(source=Path("x"), name="a.txt", format="txt", status=DocumentStatus.EXTRACTED)
    text = "Voir Jean Dupont-Martin ici"
    doc.segments = [Segment("s", text, (), SegmentContext(SegmentKind.BODY))]
    e1 = Entity("PERSONNE", "Jean Dupont", doc.id, "s", 5, 16, 0.9, "x")
    e2 = Entity("PERSONNE", "Dupont-Martin", doc.id, "s", 10, 23, 0.9, "x")
    kept, clusters = resolve([e1, e2], Config())
    table = Replacer(Config()).assign(clusters, kept)
    plan = build_plan(doc, kept, table)
    out = plan.apply(doc.segments[0])
    assert "Dupont" not in out and "Martin" not in out and out.startswith("Voir [PERSONNE_")
