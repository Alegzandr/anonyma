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
from email.message import EmailMessage
from email.utils import getaddresses
from pathlib import Path

from anonyma.config import Config
from anonyma.models import DocumentStatus, Regime, ReplacementMode
from anonyma.pipeline import Pipeline
from anonyma.store import InMemoryMappingStore
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
