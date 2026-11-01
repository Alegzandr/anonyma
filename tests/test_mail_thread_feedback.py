"""Mail-thread cases from a field test (realistic mode on client e-mails): lone given
names across files, dates without a year, greeting words, lower-case signatures,
initials and the verifier's view of its own substitutes. Synthetic data only."""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

from anonyma.config import Config
from anonyma.models import DocumentStatus, ReplacementMode
from anonyma.pipeline import Pipeline
from anonyma.substitutes import shift_date
from anonyma.textutils import normalize
from anonyma.verifier import _Plan


def _run(tmp_path: Path, config: Config, files: dict[str, str], mode: ReplacementMode) -> str:
    src = tmp_path / "lot"
    src.mkdir()
    for name, text in files.items():
        (src / name).write_text(text, encoding="utf-8", newline="")
    result = Pipeline(dataclasses.replace(config, mode=mode)).run([src], tmp_path / "out")
    texts = []
    for r in result.results:
        assert r.status is not DocumentStatus.FAILED
        assert r.output_path is not None
        texts.append(r.output_path.read_text(encoding="utf-8"))
    return "\n".join(texts)


def test_lone_given_name_masked_in_every_file(tmp_path: Path, canary_config: Config) -> None:
    out = _run(
        tmp_path,
        canary_config,
        {
            "a.txt": "Signé : Maëlle Le Goff\n",
            "b.txt": "C'est fait. Maëlle\n",
            "c.txt": "Maëlle, pouvez-vous réserver la salle ?\n",
            "d.txt": "Pierre Durand, chef de projet.\n",
            "e.txt": "Merci, à bientôt\n\npierre\n",
            "f.txt": "Une pierre est tombée du mur.\n",
        },
        ReplacementMode.TAG,
    )
    assert "maelle" not in normalize(out)
    assert out.count("[PERSONNE_001]") == 3
    assert "\npierre\n" not in out  # lower-case signature alone on its line
    assert "Une pierre est tombée" in out  # the ordinary word in prose stays


def test_lone_given_name_masked_across_mails(tmp_path: Path, canary_config: Config) -> None:
    def mail(sender: str, body: str) -> str:
        return (
            f"From: {sender}\r\nTo: equipe@cabinet.exemple.test\r\nSubject: Point\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n\r\n"
            f"{body}\r\n"
        )

    out = _run(
        tmp_path,
        canary_config,
        {
            "1.eml": mail(
                "Thibault Rambert <t.rambert@client.exemple.test>", "Bonjour,\r\nThibault Rambert"
            ),
            "2.eml": mail("equipe@cabinet.exemple.test", "Merci Thibault, c'est noté."),
        },
        ReplacementMode.REALISTIC,
    )
    assert "thibault" not in normalize(out)


def test_date_without_year_shifted_like_the_full_date(
    tmp_path: Path, canary_config: Config
) -> None:
    out = _run(
        tmp_path,
        canary_config,
        {"a.txt": "Le comité du 2 octobre 2026 est reporté au 9 octobre. Rendez-vous le 9/10.\n"},
        ReplacementMode.REALISTIC,
    )
    m = re.search(r"du (\d+ \w+ \d{4}) est reporté au (\d+ \w+)\. Rendez-vous le (\d+/\d+)", out)
    assert m is not None, out
    assert "9 octobre" not in out and "le 9/10" not in out
    full, partial, numeric = m.groups()
    # Same offset: seven days after the shifted full date.
    for days in range(-400, 0):
        if shift_date("2 octobre 2026", days) == full:
            assert shift_date("9 octobre", days, 2026) == partial
            assert shift_date("9/10", days, 2026) == numeric
            break
    else:
        raise AssertionError(f"full date not shifted back in time: {full}")


def test_partial_date_formats() -> None:
    assert shift_date("14 octobre 2026", -10) == "4 octobre 2026"  # no « 04 octobre »
    assert shift_date("04 octobre 2026", 1) == "05 octobre 2026"  # the author's zero
    assert shift_date("3 sept. 2026", -123) == "3 mai 2026"  # never « mai. »
    assert shift_date("3 mai 2026", 123) == "3 septembre 2026"
    assert shift_date("9 octobre", 7, 2026) == "16 octobre"
    assert shift_date("28/02", 1, 2028) == "29/02"
    # A mail header stays in English: « 22 Sept 2026 » does not parse as RFC 5322.
    assert shift_date("02 Oct 2026", -10) == "22 Sep 2026"


def test_realistic_mail_date_header_is_shifted(tmp_path: Path, canary_config: Config) -> None:
    out = _run(
        tmp_path,
        canary_config,
        {
            "m.eml": (
                "From: Hélène Marchetti <h.marchetti@client.exemple.test>\r\n"
                "Subject: Point\r\nDate: Fri, 02 Oct 2026 10:00:00 +0200\r\n"
                "Content-Type: text/plain; charset=utf-8\r\n\r\nBonjour.\r\n"
            )
        },
        ReplacementMode.REALISTIC,
    )
    date = next(line for line in out.splitlines() if line.startswith("Date:"))
    assert "1970" not in date and "02 Oct 2026" not in date, date
    assert re.fullmatch(r"Date: \w{3}, \d{2} \w{3} 20\d\d 10:00:00 \+0200", date), date


def test_greeting_word_before_known_given_name(tmp_path: Path, canary_config: Config) -> None:
    out = _run(
        tmp_path,
        canary_config,
        {
            "a.txt": (
                "Bonjour Hélène Marchetti,\nParfait Hélène, merci.\n"
                "Rose Durand et Karim Benali.\nSuper Rose. Merci Karim.\n"
            )
        },
        ReplacementMode.REALISTIC,
    )
    lines = out.splitlines()
    helene = lines[0].removeprefix("Bonjour ").split()[0]
    assert lines[1] == f"Parfait {helene}, merci."
    rose, karim = lines[2].split()[0], lines[2].split()[3]
    assert lines[3] == f"Super {rose}. Merci {karim}."


def test_lower_case_full_name_gets_distinct_words(tmp_path: Path, canary_config: Config) -> None:
    out = _run(
        tmp_path,
        canary_config,
        {"a.txt": "Nguyen Thi Lan\nCompte rendu joint.\nCordialement,\nlan nguyen\n"},
        ReplacementMode.REALISTIC,
    )
    assert "nguyen" not in normalize(out) and "lan\n" not in out
    signature = out.rstrip("\n").rsplit("\n", 1)[-1]
    first, second = signature.split()
    assert first != second, signature
    full = normalize(out.splitlines()[0]).split()
    assert {first, second} <= set(full), (signature, full)


def test_initials_follow_the_substitute(tmp_path: Path, canary_config: Config) -> None:
    out = _run(
        tmp_path,
        canary_config,
        {
            "a.txt": "Jean-Baptiste Kowalczyk a validé.\nHélène Marchetti\n",
            "b.txt": "Ok pour moi.\n\nJB\n",
            "c.txt": "Vu avec J.-B. ce matin.\nPS\n",
            "d.txt": "Merci\nHM\n",
        },
        ReplacementMode.REALISTIC,
    )
    lines = out.splitlines()
    jb = "".join(p[0] for p in lines[0].split()[0].split("-"))
    hm = "".join(w[0] for w in lines[1].split())
    assert f"\n{jb}\n" in out and f"Vu avec {jb[0]}.-{jb[1]}. ce matin." in out
    assert out.rstrip("\n").endswith(f"\n{hm}")
    assert "\nPS\n" in out


def test_substitute_written_otherwise_is_no_residue() -> None:
    plan = _Plan({"Groupe Courtois", "Maggy Besson"})
    assert plan.holds("groupe_courtois")
    assert plan.holds("GROUPE COURTOIS")
    assert plan.holds("Courtois")
    assert not plan.holds("Groupe Sorbel")


def test_domain_follows_organisation_detected_by_its_core(
    tmp_path: Path, canary_config: Config
) -> None:
    out = _run(
        tmp_path,
        canary_config,
        {
            "m.eml": (
                "From: Hélène Marchetti <hmarchetti@groupe-sorbel.fr>\r\n"
                "Subject: Point\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
                "Parfait, le Groupe Sorbel valide.\r\n"
            )
        },
        ReplacementMode.REALISTIC,
    )
    m = re.search(r"le Groupe (\w+) valide", out)
    assert m is not None and m.group(1) != "Sorbel", out
    assert f"@groupe-{normalize(m.group(1))}.fr>" in normalize(out), out
