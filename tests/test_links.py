"""Links between pieces of information survive realistic mode, and no new link appears.

A consultant sorts anonymised mails with a language model (by correspondent,
organisation, thread): the same original value must get the same substitute
everywhere, distinct values distinct substitutes, and derived forms (given name,
initials, local part, domain) must follow their entity. Synthetic data only.
"""

from __future__ import annotations

import dataclasses
import email
import email.policy
import json
import re
from email.message import EmailMessage
from email.utils import getaddresses
from pathlib import Path

import pytest
from typer.testing import CliRunner

from anonyma import cli, paths
from anonyma import config as config_module
from anonyma.config import Config
from anonyma.models import DocumentStatus, Regime, ReplacementMode
from anonyma.pipeline import Pipeline
from anonyma.store import InMemoryMappingStore, LinkMemory
from anonyma.textutils import normalize

#: 20 people of 3 organisations; « Dupont » twice (relatives share a surname).
PEOPLE = [
    ("Ysaline", "Quintavalle", "sorbel-industrie.fr"),
    ("Bérénice", "Tournesol", "sorbel-industrie.fr"),
    ("Aymeric", "Braquemart", "sorbel-industrie.fr"),
    ("Ludivine", "Kowalczyk", "sorbel-industrie.fr"),
    ("Gaspard", "Delacroix", "sorbel-industrie.fr"),
    ("Jean", "Dupont", "sorbel-industrie.fr"),
    ("Marie", "Dupont", "altavia-conseil.fr"),
    ("Octave", "Vasseur", "altavia-conseil.fr"),
    ("Philomène", "Carrasco", "altavia-conseil.fr"),
    ("Anatole", "Ferrandis", "altavia-conseil.fr"),
    ("Clémence", "Rousselot", "altavia-conseil.fr"),
    ("Florian", "Mazerolles", "altavia-conseil.fr"),
    ("Héloïse", "Pradelle", "altavia-conseil.fr"),
    ("Thibault", "Rambert", "moreau-avocats.fr"),
    ("Grégoire", "Moreau", "moreau-avocats.fr"),
    ("Maëlle", "Le Goff", "moreau-avocats.fr"),
    ("Karim", "Benhaddou", "moreau-avocats.fr"),
    ("Solène", "Vermorel", "moreau-avocats.fr"),
    ("Édouard", "Castagnier", "moreau-avocats.fr"),
    ("Apolline", "Severac", "moreau-avocats.fr"),
]


def _address(given: str, surname: str, domain: str) -> str:
    local = normalize(f"{given[0]}{surname}").replace(" ", "")
    return f"{given} {surname} <{local}@{domain}>"


def _mails(src: Path, people: list[tuple[str, str, str]], start: int = 0) -> None:
    src.mkdir(parents=True)
    n = len(people)
    for i, (given, surname, domain) in enumerate(people):
        to = people[(i + 1) % n]
        number = start + i
        reply = f"In-Reply-To: <m{number - 1}@mail.exemple.test>\r\n" if i else ""
        body = f"Bonjour {to[0]},\nMerci pour votre retour.\n{given} {surname}\n"
        (src / f"m{number:02d}.eml").write_bytes(
            (
                f"From: {_address(given, surname, domain)}\r\n"
                f"To: {_address(*to)}\r\n"
                f"Subject: Point {number}\r\n"
                f"Message-ID: <m{number}@mail.exemple.test>\r\n{reply}"
                "MIME-Version: 1.0\r\nContent-Type: text/plain; charset=utf-8\r\n"
                "Content-Transfer-Encoding: 8bit\r\n\r\n" + body.replace("\n", "\r\n")
            ).encode("utf-8")
        )


def _outputs(config: Config, src: Path, out: Path, **kwargs: object) -> dict[str, EmailMessage]:
    result = Pipeline(config, **kwargs).run([src], out)  # type: ignore[arg-type]
    messages: dict[str, EmailMessage] = {}
    for r in result.results:
        assert r.status is DocumentStatus.ANONYMIZED, r.status
        assert r.output_path is not None
        message = email.message_from_bytes(r.output_path.read_bytes(), policy=email.policy.default)
        assert isinstance(message, EmailMessage)
        messages[r.output_path.name] = message
    return messages


def _mailbox(message: EmailMessage, header: str) -> tuple[str, str]:
    [(name, address)] = getaddresses([str(message[header])])
    return name, address


def test_one_substitute_per_value_and_no_new_link(tmp_path: Path, canary_config: Config) -> None:
    _mails(tmp_path / "lot", PEOPLE)
    config = dataclasses.replace(canary_config, mode=ReplacementMode.REALISTIC)
    out = _outputs(config, tmp_path / "lot", tmp_path / "out")
    n = len(PEOPLE)
    senders = [_mailbox(out[f"m{i:02d}.eml"], "From") for i in range(n)]
    # The same person as recipient of the previous mail: same name, same address.
    for i in range(n):
        assert _mailbox(out[f"m{i:02d}.eml"], "To") == senders[(i + 1) % n]
        body = out[f"m{i:02d}.eml"].get_body(("plain",))
        assert body is not None
        text = str(body.get_content())
        assert text.splitlines()[-1] == senders[i][0]  # signature = display name
        assert text.startswith(f"Bonjour {senders[(i + 1) % n][0].split()[0]},")
    # Injective per component: distinct originals never share a substitute.
    names = [name.split(" ", 1) for name, _ in senders]
    givens = [g for g, _ in names]
    surnames = [s for _, s in names]
    assert len(set(givens)) == n, givens
    by_original: dict[str, set[str]] = {}
    for (_, surname, _), fake in zip(PEOPLE, surnames, strict=True):
        by_original.setdefault(surname, set()).add(fake)
    assert all(len(fakes) == 1 for fakes in by_original.values()), by_original
    fakes = [next(iter(f)) for f in by_original.values()]
    assert len(set(fakes)) == len(fakes), fakes  # « Dupont » twice, one substitute
    addresses = [address for _, address in senders]
    assert len(set(addresses)) == n
    domains: dict[str, set[str]] = {}
    for (_, _, domain), address in zip(PEOPLE, addresses, strict=True):
        domains.setdefault(domain, set()).add(address.split("@")[1])
    assert all(len(d) == 1 for d in domains.values()), domains
    assert len({next(iter(d)) for d in domains.values()}) == 3
    # No original left anywhere, and no substitute word reused across families.
    text = "\n".join(m.as_string() for m in out.values())
    for _given, surname, domain in PEOPLE:
        assert normalize(surname) not in normalize(text), surname
        assert domain not in text
    # Threads: every In-Reply-To points at a Message-ID of the output.
    ids = {str(m["Message-ID"]) for m in out.values()}
    replies = [str(m["In-Reply-To"]) for m in out.values() if m["In-Reply-To"]]
    assert len(replies) == n - 1 and all(r in ids for r in replies)


def test_same_substitutes_across_batches_with_a_table(
    tmp_path: Path, canary_config: Config
) -> None:
    config = dataclasses.replace(
        canary_config, mode=ReplacementMode.REALISTIC, regime=Regime.PSEUDONYMIZATION
    )
    store = InMemoryMappingStore()
    first, second = PEOPLE[:12], PEOPLE[8:]  # 4 people in both batches
    _mails(tmp_path / "a", first)
    _mails(tmp_path / "b", second, start=50)
    out_a = _outputs(config, tmp_path / "a", tmp_path / "oa", store=store)
    out_b = _outputs(config, tmp_path / "b", tmp_path / "ob", store=store)
    sent_a = {PEOPLE[i]: _mailbox(out_a[f"m{i:02d}.eml"], "From") for i in range(12)}
    sent_b = {second[i]: _mailbox(out_b[f"m{50 + i:02d}.eml"], "From") for i in range(12)}
    common = set(sent_a) & set(sent_b)
    assert len(common) == 4
    for person in common:
        assert sent_a[person] == sent_b[person]
    # New people of the second batch take no substitute surname of the first one.
    old = {name.split(" ", 1)[1] for name, _ in sent_a.values()}
    new = {sent_b[p][0].split(" ", 1)[1] for p in set(sent_b) - common}
    assert not old & new, old & new


@pytest.fixture
def realistic_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """The application's own default (tests otherwise default to tags)."""
    monkeypatch.setattr(config_module, "DEFAULT_MODE", ReplacementMode.REALISTIC)


def _senders(out: Path, numbers: range) -> dict[int, tuple[str, str]]:
    found: dict[int, tuple[str, str]] = {}
    for number in numbers:
        path = next(out.rglob(f"m{number:02d}.eml"))
        message = email.message_from_bytes(path.read_bytes(), policy=email.policy.default)
        assert isinstance(message, EmailMessage)
        found[number] = _mailbox(message, "From")
    return found


#: People of the second batch only: names the generator never draws (given names
#: rarer than its pool, surnames outside Faker's), so that no substitute of the first
#: batch can be one of them (that substitute would then be replaced, see
#: ``_Realistic.remembered``).
LATE_SURNAMES = ["Quillardet", "Brossolin", "Ternisien", "Valdeyron", "Morizur", "Jacquelot"]
LATE_DOMAINS = ["sorbel-industrie.fr", "altavia-conseil.fr", "moreau-avocats.fr"] * 2


def _late_people() -> list[tuple[str, str, str]]:
    from anonyma.data import first_names

    rare = sorted(
        r.display
        for r in first_names().values()
        if 300 <= r.count < 1000 and r.display.isalpha() and r.gender in ("m", "f")
    )
    return [
        (rare[i * 7], s, d)
        for i, (s, d) in enumerate(zip(LATE_SURNAMES, LATE_DOMAINS, strict=True))
    ]


def test_default_run_is_realistic_and_linked_across_batches(
    tmp_path: Path, realistic_default: None
) -> None:
    """« anonyma run lot1 -o s1 » then « anonyma run lot2 -o s2 », no option, no
    passphrase: realistic substitutes, the same for the people of both batches, never
    shared by two different people."""
    lot1 = PEOPLE[:12]
    lot2 = [*PEOPLE[8:12], *_late_people()]  # 4 people in both batches
    _mails(tmp_path / "lot1", lot1)
    _mails(tmp_path / "lot2", lot2, start=8)
    runner = CliRunner()
    for lot, out, people in (("lot1", "s1", lot1), ("lot2", "s2", lot2)):
        result = runner.invoke(cli.app, ["run", str(tmp_path / lot), "-o", str(tmp_path / out)])
        assert result.exit_code == 0, result.output
        assert "phrase secrète" not in result.output.lower()
        text = "\n".join(p.read_text(encoding="utf-8") for p in (tmp_path / out).rglob("*.eml"))
        for _given, surname, domain in people:
            assert normalize(surname) not in normalize(text) and domain not in text
        # The memory holds no original value, only keyed indexes and fake values (a
        # fake « Jeanne » holds the letters of « Jean »: whole words are compared).
        memory = paths.links_path().read_bytes()
        for person in people:
            for original in person:
                word = rb"(?<![\w.-])" + re.escape(original.encode("utf-8")) + rb"(?![\w-])"
                assert re.search(word, memory) is None, original
    first = _senders(tmp_path / "s1", range(12))
    second = _senders(tmp_path / "s2", range(8, 8 + len(lot2)))
    for number in range(8, 12):
        assert first[number] == second[number]
    everyone = {**first, **second}
    people = {**dict(enumerate(lot1)), **{8 + i: p for i, p in enumerate(lot2)}}
    assert all("[" not in name for name, _ in everyone.values())  # realistic, no tag
    names = [name for name, _ in everyone.values()]
    assert len(set(names)) == len(people)  # one substitute per person, never shared
    assert len({address for _, address in everyone.values()}) == len(people)
    domains: dict[str, set[str]] = {}
    for number, (_, _, domain) in people.items():
        domains.setdefault(domain, set()).add(everyone[number][1].split("@")[1])
    assert all(len(d) == 1 for d in domains.values()), domains


def test_new_substitutes_command_forgets_the_links(tmp_path: Path, realistic_default: None) -> None:
    _mails(tmp_path / "lot", PEOPLE[:3])
    runner = CliRunner()
    assert runner.invoke(cli.app, ["run", str(tmp_path / "lot"), "-o", str(tmp_path / "a")])
    assert paths.links_path().is_file()
    # Without a console nor « --oui »: nothing is done, and the exit code says so.
    result = runner.invoke(cli.app, ["nouveaux-substituts"])
    assert result.exit_code == 2 and paths.links_path().is_file()
    result = runner.invoke(cli.app, ["nouveaux-substituts", "--oui"])
    assert result.exit_code == 0 and not paths.links_path().exists()
    with LinkMemory.open(paths.links_path()) as memory:
        assert memory.lookup("PERSONNE", "x") is None


def test_settings_saved_with_tags_by_default_become_realistic(tmp_path: Path) -> None:
    old = tmp_path / "ancien.json"
    old.write_text(json.dumps({"mode": "balises"}), encoding="utf-8")
    assert Config.load(old).mode is config_module.DEFAULT_MODE
    chosen = tmp_path / "choisi.json"
    chosen.write_text(json.dumps({"mode": "balises", "version": 2}), encoding="utf-8")
    assert Config.load(chosen).mode is ReplacementMode.TAG
    saved = tmp_path / "enregistre.json"
    Config(mode=ReplacementMode.TAG).save(saved)
    assert Config.load(saved).mode is ReplacementMode.TAG
