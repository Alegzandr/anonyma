"""Realistic mode: credible organisations, projects, domains and addresses."""

from __future__ import annotations

import random
import re

import pytest

from anonyma import substitutes as fk
from anonyma.config import Config
from anonyma.data import first_name
from anonyma.data.communes import communes
from anonyma.models import Entity, EntityCluster, Regime, ReplacementMode
from anonyma.names import parse_person
from anonyma.replacer import Replacer, SubstitutionTable, _no_extra_period
from anonyma.resolver import resolve
from anonyma.store import InMemoryMappingStore
from anonyma.textutils import normalize

POSTCODES = {cp for cp, _ in communes()}
CITIES = {name for _, name in communes()}
PAIRS = set(communes())


def assign(
    values: list[tuple[str, str]], store: InMemoryMappingStore | None = None
) -> tuple[SubstitutionTable, list[Entity], dict[str, EntityCluster]]:
    cfg = Config(mode=ReplacementMode.REALISTIC)
    entities = [
        Entity(t, v, "d", f"s{i}", 0, len(v), 0.9, "test") for i, (v, t) in enumerate(values)
    ]
    kept, clusters = resolve(entities, cfg)
    return Replacer(cfg, store).assign(clusters, kept), kept, clusters


def sub(table: SubstitutionTable, entities: list[Entity], value: str, kind: str = "") -> str:
    entity = next(e for e in entities if e.value == value and kind in ("", e.entity_type))
    return table.substitute_for(entity)


# -- organisations and projects --------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "parts"),
    [
        ("Groupe Sorbel", ("Groupe ", "Sorbel", "")),
        ("Sorbel SAS", ("", "Sorbel", " SAS")),
        ("Dupont & Associés", ("", "Dupont", " & Associés")),
        ("DocuSign, Inc.", ("", "DocuSign", ", Inc.")),
        ("Projet Colibri", ("Projet ", "Colibri", "")),
        ("Colibri", ("", "Colibri", "")),
        ("PROJET-X", ("PROJET-", "X", "")),
        ("Groupe", ("", "Groupe", "")),
    ],
)
def test_organization_split_keeps_designators(name: str, parts: tuple[str, str, str]) -> None:
    assert fk.split_organization(name) == parts


def test_organization_keeps_its_shape() -> None:
    for seed in range(40):
        rng = random.Random(seed)
        group = fk.fake_company("Groupe Sorbel", rng)
        assert group.startswith("Groupe ") and len(group.split()) == 2
        core = group.split()[1]
        assert first_name(core) is None, group  # never « Groupe Jean »
        legal = fk.fake_company("Sorbel SAS", rng)
        assert legal.endswith(" SAS") and len(legal.split()) == 2
        single = fk.fake_company("Colibri", rng)
        assert " " not in single and not single.endswith(".")  # no « S.A.R.L. » added
        assert fk.fake_company("SORBEL", rng).isupper()


def test_word_pools_hold_no_given_names() -> None:
    assert len(fk._org_word_pool()) >= 150
    assert len(fk._project_pool()) >= 60
    for word in (*fk._org_word_pool(), *fk._project_pool()):
        assert first_name(word) is None, word
        assert word.isalpha()


def test_no_doubled_final_period() -> None:
    assert _no_extra_period("Colibri", "Briand Lambert S.A.R.L.") == "Briand Lambert S.A.R.L"
    assert _no_extra_period("Dupont S.A.R.L.", "Lacroix S.A.R.L.") == "Lacroix S.A.R.L."
    assert _no_extra_period("J.", "P.") == "P."
    # A substitute stored by an older version still loses the extra period.
    store = InMemoryMappingStore()
    cfg = Config(mode=ReplacementMode.REALISTIC, regime=Regime.PSEUDONYMIZATION)
    entities = [Entity("ORGANISATION", "Colibri", "d", "s", 0, 7, 0.9, "test")]
    kept, clusters = resolve(entities, cfg)
    replacer = Replacer(cfg, store)
    replacer.remember(next(iter(clusters.values())), "Briand Lambert S.A.R.L.")
    table = replacer.assign(clusters, kept)
    assert table.substitute_for(kept[0]) == "Briand Lambert S.A.R.L"


def test_project_and_its_bare_name_share_the_code_name() -> None:
    table, kept, _ = assign(
        [("Projet Colibri", "ORGANISATION"),
         ("Colibri", "ORGANISATION"), ("Sorbel", "PROJET"), ("Projet Sorbel", "PROJET")]
    )  # fmt: skip
    projet = sub(table, kept, "Projet Colibri")
    bare = sub(table, kept, "Colibri")
    assert projet == f"Projet {bare}"
    assert bare in fk.PROJECT_WORDS  # a code name, not a company name
    assert sub(table, kept, "Projet Sorbel") == f"Projet {sub(table, kept, 'Sorbel')}"
    assert sub(table, kept, "Sorbel") in fk.PROJECT_WORDS


def test_organization_variants_share_the_core() -> None:
    table, kept, _ = assign(
        [("Groupe Sorbel", "ORGANISATION"), ("Sorbel SAS", "ORGANISATION"),
         ("GROUPE SORBEL", "ORGANISATION")]
    )  # fmt: skip
    group = sub(table, kept, "Groupe Sorbel")
    core = group.removeprefix("Groupe ")
    assert sub(table, kept, "Sorbel SAS") == f"{core} SAS"
    assert sub(table, kept, "GROUPE SORBEL") == group.upper()


# -- domains ------------------------------------------------------------------------------


def test_domain_of_an_organization_follows_its_substitute() -> None:
    table, kept, _ = assign(
        [("Jean Dupont", "PERSONNE"), ("jean.dupont@sorbel.fr", "EMAIL"),
         ("contact@groupe-sorbel.fr", "EMAIL"), ("Groupe Sorbel", "ORGANISATION"),
         ("https://www.groupe-sorbel.fr/offres", "URL")]
    )  # fmt: skip
    core = sub(table, kept, "Groupe Sorbel").removeprefix("Groupe ")
    label = fk.slug(core)
    assert sub(table, kept, "jean.dupont@sorbel.fr").endswith(f"@{label}.fr")
    assert sub(table, kept, "contact@groupe-sorbel.fr") == f"contact@groupe-{label}.fr"
    assert f"www.groupe-{label}.fr" in sub(table, kept, "https://www.groupe-sorbel.fr/offres")


def test_other_domains_are_plausible_and_keep_their_suffix() -> None:
    table, kept, _ = assign(
        [("a.b@cabinet-lenoir.fr", "EMAIL"), ("x.y@mail.lenoir-partners.co.uk", "EMAIL"),
         ("z@gmail.com", "EMAIL"), ("w@ministere.gouv.fr", "EMAIL")]
    )  # fmt: skip
    fr = sub(table, kept, "a.b@cabinet-lenoir.fr").split("@")[1]
    assert fr.endswith(".fr") and not fr.endswith(".example") and "lenoir" not in fr
    assert first_name(fr.split(".")[0].split("-")[0]) is None  # never ``raymond.fr``
    uk = sub(table, kept, "x.y@mail.lenoir-partners.co.uk").split("@")[1]
    assert re.fullmatch(r"mail\.[a-z-]+\.co\.uk", uk) and "lenoir" not in uk
    assert sub(table, kept, "z@gmail.com").endswith("@gmail.com")
    gov = sub(table, kept, "w@ministere.gouv.fr").split("@")[1]
    assert gov.endswith(".fr") and not gov.endswith(".gouv.fr")


def test_surnames_never_reappear_as_organization_or_domain_words(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A tiny pool makes the collision certain without the guard: every person is a
    # « Courtois », and organisations and domains draw from the same three names.
    real_fake_person = fk.fake_person

    def courtois(*args: object, **kwargs: object) -> fk.FakePerson:
        person = real_fake_person(*args, **kwargs)  # type: ignore[arg-type]
        return fk.FakePerson(person.given, "Courtois", person.gender)

    monkeypatch.setattr(fk, "fake_person", courtois)
    monkeypatch.setattr(fk, "_org_word_pool", lambda: ("Courtois", "Lacroix", "Delmas"))
    table, kept, _ = assign(
        [("Maître Paul Lenoir", "PERSONNE"), ("Groupe Sorbel", "ORGANISATION"),
         ("a.b@cabinet-xyz.fr", "EMAIL"), ("Vautrin SAS", "ORGANISATION")]
    )  # fmt: skip
    person = parse_person(sub(table, kept, "Maître Paul Lenoir"))
    assert person is not None and person.surname[-1] == "Courtois"
    group_core = sub(table, kept, "Groupe Sorbel").removeprefix("Groupe ")
    other_core = sub(table, kept, "Vautrin SAS").removesuffix(" SAS")
    domain_words = re.split(r"[.-]", sub(table, kept, "a.b@cabinet-xyz.fr").split("@")[1])
    words = [normalize(group_core), normalize(other_core), *domain_words]
    assert "courtois" not in words
    assert normalize(group_core) != normalize(other_core)  # unrelated organisations
    assert normalize(group_core) not in domain_words and normalize(other_core) not in domain_words


# -- addresses ----------------------------------------------------------------------------


def test_communes_are_well_formed() -> None:
    pairs = communes()
    assert 150 <= len(pairs) <= 400 and len(set(pairs)) == len(pairs)
    for postcode, name in pairs:
        assert re.fullmatch(r"\d{5}", postcode) and name[:1].isupper()


def test_addresses_use_real_postcode_and_commune_pairs() -> None:
    for seed in range(30):
        address = fk.fake_address("12 rue des Lilas, 75008 Paris", random.Random(seed))
        m = re.fullmatch(r"\d+ .+, (\d{5}) (.+)", address)
        assert m is not None and (m.group(1), m.group(2)) in PAIRS, address
        spaced = fk.fake_address("3 place du Marché 75 008 PARIS", random.Random(seed))
        m = re.fullmatch(r"\d+ .+ (\d{2}) (\d{3}) (.+)", spaced)
        assert m is not None and m.group(3).isupper(), spaced
        assert (m.group(1) + m.group(2), m.group(3)) in {(c, n.upper()) for c, n in PAIRS}
        assert fk.fake_address("75008", random.Random(seed)) in POSTCODES


def test_cities_and_postcodes_are_real() -> None:
    table, kept, _ = assign(
        [("Lyon", "LIEU"), ("44000", "CODE_POSTAL"), ("8 rue Haute, 69003 Lyon", "ADRESSE")]
    )
    assert sub(table, kept, "Lyon") in CITIES - {"Lyon"}
    assert sub(table, kept, "44000") in POSTCODES - {"44000"}
    address = sub(table, kept, "8 rue Haute, 69003 Lyon")
    m = re.fullmatch(r"\d+ .+, (\d{5}) (.+)", address)
    assert m is not None and (m.group(1), m.group(2)) in PAIRS


def test_address_complement_keeps_its_words() -> None:
    rng = random.Random(3)
    for original in ("LOT 47 - BAT D", "PORTE C210", "esc. 2, 3e étage"):
        fake = fk.fake_address(original, rng)
        assert fake != original
        assert re.sub(r"[A-Z0-9]", "#", fake.replace("étage", "")) == re.sub(
            r"[A-Z0-9]", "#", original.replace("étage", "")
        ), "same words, same code shapes"
        assert not re.search(r"(?<!\d)0\d", fake), "no leading zero"
