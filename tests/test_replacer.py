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
    assert person is not None and len(person.given) == 1 and person.surname
    given, surname = person.given[0], " ".join(person.surname)
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
    given, surname = a.split(" ", 1)  # fake surnames may hold particles ("Da Silva")
    assert b == f"{given} {surname.upper()}"
    assert match_case("Claire FONTAINE", "Paul Martin") == "Paul MARTIN"
    assert match_case("Claire FONTAINE", "Paul Da Silva") == "Paul DA SILVA"
    assert match_case("Jean Dupont", "Noé Dos Santos") == "Noé Dos Santos"
    assert match_case("JEAN DUPONT", "Noé Dos Santos") == "NOÉ DOS SANTOS"


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
    assert local.startswith(ascii_given + ".") and domain.endswith(".test")
    assert "dupont" not in domain  # the suffix is kept, the name is not
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
        [("03/04/1985", "DATE"), ("10/04/1985", "DATE"), ("1er mars 2021", "DATE"),
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


def test_one_instant_gets_one_substitute_in_every_format() -> None:
    """D365 export: the ISO value, its Dataverse label, the mail header and the CSV
    cell of one e-mail move together; time of day and zone are kept."""
    from datetime import datetime, timedelta
    from email.utils import parsedate_to_datetime

    values = [
        ("2026-09-01T08:14:29Z", "DATE"),
        ("2026-09-01T08:14:29.123Z", "DATE"),
        ("2026-09-01T10:14:29+02:00", "DATE"),
        ("01/09/2026 10:14", "DATE"),
        ("01 Sep 2026 08:14:29 +0000", "DATE"),
        ("2026-09-01T08:17:29Z", "DATE"),  # modified 3 minutes later
        ("2026-08-31T23:59:00Z", "DATE"),  # the day before
        ("12 mars 2026", "DATE"),
        ("12/03/2026", "DATE"),
    ]
    table, kept, _ = assign(values, ReplacementMode.REALISTIC)
    out = {v: sub(table, kept, v) for v, _ in values}
    created = datetime.fromisoformat(out["2026-09-01T08:14:29Z"])
    shift = created - datetime.fromisoformat("2026-09-01T08:14:29Z")
    assert shift.seconds == 0 and shift.days != 0  # whole days only
    assert datetime.fromisoformat(out["2026-09-01T08:14:29.123Z"]) == created + timedelta(
        microseconds=123000
    )
    assert out["2026-09-01T10:14:29+02:00"].endswith("T10:14:29+02:00")
    assert datetime.fromisoformat(out["2026-09-01T10:14:29+02:00"]) == created
    assert out["01/09/2026 10:14"] == created.strftime("%d/%m/%Y") + " 10:14"
    assert parsedate_to_datetime(out["01 Sep 2026 08:14:29 +0000"]) == created
    assert datetime.fromisoformat(out["2026-09-01T08:17:29Z"]) - created == timedelta(minutes=3)
    assert datetime.fromisoformat(out["2026-08-31T23:59:00Z"]) < created  # order kept
    textual = shift_date("12 mars 2026", shift.days)
    assert out["12 mars 2026"] == textual and out["12/03/2026"] == shift_date(
        "12/03/2026", shift.days
    )


def test_birth_dates_have_their_own_offset() -> None:
    """A birth date does not follow the batch offset (one known event date would give
    it away) but every format of it agrees, and stays near the original."""
    from anonyma.substitutes import parse_date

    def days(value: str) -> int:
        moved, original = parse_date(sub(table, kept, value)), parse_date(value)
        assert moved is not None and original is not None
        return (moved - original).days

    births = [f"{d:02d}/04/1985" for d in range(1, 29)]
    values = [("2026-03-12", "DATE"), ("3 avril 1985", "DATE_NAISSANCE")]
    values += [(b, "DATE_NAISSANCE") for b in births]
    table, kept, _ = assign(values, ReplacementMode.REALISTIC)
    shifts = {days(b) for b in births}
    assert all(30 <= abs(s) <= 365 for s in shifts)
    assert len(shifts) > 1 and shifts != {days("2026-03-12")}
    assert days("3 avril 1985") == days("03/04/1985")


def test_pseudonymized_dates_are_stable_and_older_substitutes_are_fixed() -> None:
    from anonyma.replacer import DATE_SHIFT_KEY, PARAMETER_TYPE

    store = InMemoryMappingStore()
    cfg = Config(mode=ReplacementMode.REALISTIC, regime=Regime.PSEUDONYMIZATION)
    first = [ent("2026-09-01T08:14:29Z", "DATE")]
    kept, clusters = resolve(first, cfg)
    one = Replacer(cfg, store).assign(clusters, kept).substitute_for(kept[0])
    shift = int(store.lookup(PARAMETER_TYPE, DATE_SHIFT_KEY) or "0")
    # An older version stored a random time of day: the next run corrects it.
    stale = [
        k for k, (_, substitute) in store._data.items() if k[0] == "DATE" and substitute == one
    ]
    assert stale
    for k in stale:
        store._data[k] = (store._data[k][0], one[:11] + "17:03:59Z")
    kept, clusters = resolve([ent("2026-09-01T08:14:29Z", "DATE")], cfg)
    two = Replacer(cfg, store).assign(clusters, kept).substitute_for(kept[0])
    assert two == one and two.endswith("T08:14:29Z")
    assert int(store.lookup(PARAMETER_TYPE, DATE_SHIFT_KEY) or "0") == shift
    from anonyma.store import reidentification_map

    assert reidentification_map(store.items())[one] == "2026-09-01T08:14:29Z"


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
         ("Lemaire Conseil SARL", "ORGANISATION"), ("PROJET-X", "PROJET"),
         ("Kardex", "MACHINE")],
        ReplacementMode.REALISTIC,
    )  # fmt: skip
    address = sub(table, kept, "12 rue des Lilas, 75011 Paris")
    assert re.fullmatch(r"\d+ .+, \d{5} .+", address) and "Lilas" not in address
    assert sub(table, kept, "Nantes") != "Nantes"
    project = sub(table, kept, "PROJET-X")
    assert re.fullmatch(r"PROJET-[A-Z]", project) and project != "PROJET-X"
    assert sub(table, kept, "Kardex") == "[MACHINE_001]"  # no realistic generator


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


def _iban_fr() -> str:
    bank, branch, account = "30001", "00794", "00012345678"
    bban = bank + branch + account + checksums.rib_key(bank, branch, account)
    iban = "FR" + checksums.iban_check_digits("FR", bban) + bban
    return " ".join(iban[i : i + 4] for i in range(0, len(iban), 4))


@pytest.mark.parametrize(
    ("first", "then"),
    [
        (ReplacementMode.TAG, ReplacementMode.REALISTIC),
        (ReplacementMode.REALISTIC, ReplacementMode.TAG),
    ],
)
def test_pseudonymization_table_reused_in_another_mode(
    first: ReplacementMode, then: ReplacementMode
) -> None:
    """A table filled in one mode never leaks a tag (or the original) into the other."""
    store = InMemoryMappingStore()
    nir_body = "2850478006084"
    nir = f"{nir_body} {checksums.nir_key(nir_body)}"
    values = [("Camille Durand", "PERSONNE"), (_iban_fr(), "IBAN"), (nir, "NIR")]
    tables = []
    for mode in (first, then, first):
        cfg = Config(mode=mode, regime=Regime.PSEUDONYMIZATION)
        entities = [ent(v, t, f"s{i}") for i, (v, t) in enumerate(values)]
        kept, clusters = resolve(entities, cfg)
        table = Replacer(cfg, store).assign(clusters, kept)
        tables.append((mode, {e.value: table.substitute_for(e) for e in kept}))
    for mode, subs in tables:
        for original, substitute in subs.items():
            tag = bool(re.fullmatch(r"\[[A-Z_]+_[A-Z0-9]+\]", substitute))
            assert tag is (mode is ReplacementMode.TAG), (mode, substitute)
            if mode is ReplacementMode.REALISTIC:
                assert "[" not in substitute
                # No run of original digits survives (« FR76 3000 1007 ... »).
                digits = checksums.digits(original)
                assert all(
                    digits[i : i + 6] not in checksums.digits(substitute)
                    for i in range(2, len(digits) - 5)
                ), (original, substitute)
    realistic = next(s for m, s in tables if m is ReplacementMode.REALISTIC)
    assert checksums.iban_valid(checksums.compact(realistic[_iban_fr()]))
    assert checksums.nir_valid(checksums.compact(realistic[nir]))
    # Each mode stays stable across batches.
    assert tables[0][1] == tables[2][1]


def test_reshape_never_keeps_original_characters() -> None:
    from anonyma.substitutes import reshape

    assert reshape("FR76 3000 1007", "AB12") == "AB12 0000 0000"


def test_realistic_nir_follows_the_substitute_birth_date() -> None:
    body = "2850478006084"
    nir = f"{body}{checksums.nir_key(body)}"
    table, kept, _ = assign(
        [("Camille Durand", "PERSONNE"), ("03/04/1985", "DATE_NAISSANCE"), (nir, "NIR")],
        ReplacementMode.REALISTIC,
    )
    birth = sub(table, kept, "03/04/1985")
    fake = sub(table, kept, nir)
    _day, month, year = birth.split("/")
    assert fake[1:5] == f"{year[2:]}{month}", (birth, fake)
    assert fake[0] == "2" and checksums.nir_valid(fake)


@pytest.mark.parametrize(
    ("person", "address"),
    [
        ("Jean-Baptiste Kerguelen", "jean-baptiste.kerguelen@exemple.test"),
        ("Bérénice Lagarde-Moreau", "berenice.lagarde-moreau@exemple.test"),
    ],
)
def test_realistic_address_follows_the_display_name(person: str, address: str) -> None:
    """Hyphens in the local part: the fake address still matches the fake name."""
    table, kept, _ = assign([(person, "PERSONNE"), (address, "EMAIL")], ReplacementMode.REALISTIC)
    name, mail = sub(table, kept, person), sub(table, kept, address)
    given, surname = name.split(" ", 1)
    local = mail.split("@")[0]
    assert ascii_of(surname) in ascii_of(local) and ascii_of(given)[:1] == local[:1], (name, mail)


def test_realistic_case_is_per_word_for_names_only() -> None:
    from anonyma.replacer import _case_of

    assert _case_of("ADRESSE", "12 rue des Lilas", "46 rue Emmanuelle Colas") == (
        "46 rue Emmanuelle Colas"
    )
    assert _case_of("ADRESSE", "12 RUE DES LILAS", "46 rue Colas") == "46 RUE COLAS"
    assert _case_of("PERSONNE", "Claire FONTAINE", "Paul Martin") == "Paul MARTIN"


def test_stored_person_takes_the_case_of_each_mention() -> None:
    """Same table, another run (or profile): the stored « CLÉMENT Hubert » was made for
    « FONTAINE Régis »; « Régis Fontaine » must read « Hubert Clément », not « Hubert
    CLÉMENT » (D365: « Tylio Collin » / « Tylio COLLIN » between leger and standard)."""
    store = InMemoryMappingStore()
    cfg = Config(mode=ReplacementMode.REALISTIC, regime=Regime.PSEUDONYMIZATION)

    def run(*values: str) -> dict[str, str]:
        entities = [ent(v, "PERSONNE", f"s{i}") for i, v in enumerate(values)]
        kept, clusters = resolve(entities, cfg)
        table = Replacer(cfg, store).assign(clusters, kept)
        return {e.value: table.substitute_for(e) for e in kept}

    [upper] = run("FONTAINE Régis").values()
    # The surname may hold several words (« LE GOFF Leonard »): all upper case.
    words = upper.split(" ")
    cut = next(i for i, w in enumerate(words) if not w.isupper())
    surname, given = " ".join(words[:cut]), " ".join(words[cut:])
    assert surname and given
    again = run("Régis Fontaine", "Régis FONTAINE", "Régis")
    assert again["Régis Fontaine"] == f"{given} {surname.title()}"
    assert again["Régis FONTAINE"] == f"{given} {surname}"
    assert again["Régis"] == given


def test_compound_surname_keeps_the_lines_of_the_original() -> None:
    from anonyma.replacer import _lines_of

    assert _lines_of("Gaspard\n\nVALMONT", "Kleber DA COSTA") == "Kleber\n\nDA COSTA"
    assert _lines_of("LE GOFF\nJean", "DA COSTA Paul") == "DA COSTA\nPaul"
    assert _lines_of("Jean\nDupont", "Paul Da Costa") == "Paul\nDa Costa"
    assert _lines_of("Jean\nDUPONT", "Paul MARTIN") == "Paul\nMARTIN"
