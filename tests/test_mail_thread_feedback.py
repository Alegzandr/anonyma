"""Mail-thread cases from a field test (realistic mode on client e-mails): lone given
names across files, dates without a year, greeting words, lower-case signatures,
initials and the verifier's view of its own substitutes. Synthetic data only."""

from __future__ import annotations

import dataclasses
import email
import email.policy
import re
from email.header import decode_header, make_header
from email.message import EmailMessage
from pathlib import Path

from anonyma.config import Config
from anonyma.models import DocumentStatus, ReplacementMode
from anonyma.pipeline import Pipeline
from anonyma.substitutes import shift_date, slug
from anonyma.textutils import normalize
from anonyma.verifier import _Plan


def _readable(path: Path) -> str:
    """Text of an output; for a mail, its raw headers then its decoded body (the
    writer may choose quoted-printable or base64)."""
    data = path.read_bytes()
    if path.suffix != ".eml":
        return data.decode("utf-8")
    head = data.split(b"\r\n\r\n", 1)[0].decode("utf-8")
    message = email.message_from_bytes(data, policy=email.policy.default)
    assert isinstance(message, EmailMessage)
    body = message.get_body(("plain",))
    assert body is not None
    return head + "\n\n" + str(body.get_content())


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
        texts.append(_readable(r.output_path))
    return "\n".join(texts)


def _run_files(
    tmp_path: Path, config: Config, files: dict[str, str], mode: ReplacementMode
) -> dict[str, str]:
    """Outputs by output file name; every file must come out « Anonymisé »."""
    src = tmp_path / "lot"
    src.mkdir()
    for name, text in files.items():
        (src / name).write_text(text, encoding="utf-8", newline="")
    result = Pipeline(dataclasses.replace(config, mode=mode)).run([src], tmp_path / "out")
    out: dict[str, str] = {}
    for r in result.results:
        assert r.status is DocumentStatus.ANONYMIZED, r.status
        assert r.output_path is not None
        out[r.output_path.name] = _readable(r.output_path)
    return out


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
    assert "reporté au 9 octobre" not in out and "le 9/10" not in out
    full, partial, numeric = m.groups()
    # Same offset: seven days after the shifted full date.
    for days in range(-330, -29):
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
    pair = re.fullmatch(r"(\S+) .+ et (\S+) .+\.", lines[2])  # surnames may hold spaces
    assert pair is not None, lines[2]
    assert lines[3] == f"Super {pair.group(1)}. Merci {pair.group(2)}."


def test_lower_case_full_name_gets_distinct_words(tmp_path: Path, canary_config: Config) -> None:
    out = _run(
        tmp_path,
        canary_config,
        {"a.txt": "Nguyen Thi Lan\nCompte rendu joint.\nCordialement,\nlan nguyen\n"},
        ReplacementMode.REALISTIC,
    )
    assert "nguyen" not in normalize(out) and "lan\n" not in out
    # A substitute given name or surname may hold two words (« Le Gall »).
    words = normalize(out.rstrip("\n").rsplit("\n", 1)[-1]).split()
    assert len(words) >= 2 and len(set(words)) == len(words), words
    full = normalize(out.splitlines()[0]).split()
    assert set(words) <= set(full), (words, full)


def test_initials_follow_the_substitute(tmp_path: Path, canary_config: Config) -> None:
    out = _run_files(
        tmp_path,
        canary_config,
        {
            "a.txt": "Jean-Baptiste Kowalczyk a validé.\nHélène Marchetti\n",
            "b.txt": "Ok pour moi.\n\nJB\n",
            "c.txt": "Vu avec J.-B. ce matin.\nPS\n",
            "d.txt": "Merci\nHM\n",
            "e.txt": "Merci,\nH.\n",
        },
        ReplacementMode.REALISTIC,
    )
    # Whatever the substitutes drawn: one person signs with the same initials in
    # every file, never the original ones, and « PS » is no signature.
    jb = out["b.txt"].rstrip("\n").rsplit("\n", 1)[-1]
    assert len(jb) == 2 and jb.isalpha() and jb.isupper() and jb != "JB", out["b.txt"]
    assert out["c.txt"] == f"Vu avec {jb[0]}.-{jb[1]}. ce matin.\nPS\n"
    assert out["a.txt"][0] == jb[0] and "Jean-Baptiste" not in out["a.txt"]
    hm = out["d.txt"].rstrip("\n").rsplit("\n", 1)[-1]
    assert len(hm) == 2 and hm.isalpha() and hm.isupper() and hm != "HM", out["d.txt"]
    assert out["a.txt"].splitlines()[1][0] == hm[0]
    assert out["e.txt"] == f"Merci,\n{hm[0]}.\n"


def test_substitute_written_otherwise_is_no_residue() -> None:
    plan = _Plan({"Groupe Courtois", "Maggy Besson", "Cyril Hubert"})
    assert plan.holds("groupe_courtois")
    assert plan.holds("GROUPE COURTOIS")
    assert plan.holds("Courtois")
    assert plan.holds("Point Groupe Courtois")  # an ordinary word around it
    assert plan.holds("Hubert Avocats")
    assert not plan.holds("Groupe Sorbel")
    assert not plan.holds("Hubert Sorbel")


def _mail(frm: str, to: str, subject: str, body: str) -> str:
    """A mail with raw UTF-8 headers (RFC 6532), as many clients send them."""
    return (
        f"From: {frm}\r\nTo: {to}\r\nSubject: {subject}\r\nMIME-Version: 1.0\r\n"
        "Content-Type: text/plain; charset=utf-8\r\nContent-Transfer-Encoding: 8bit\r\n\r\n"
        + body.replace("\n", "\r\n")
    )


def test_mail_thread_round_two(tmp_path: Path, canary_config: Config) -> None:
    helene = "Hélène Marchetti <hmarchetti@groupe-sorbel.fr>"
    maelle = "Maëlle Le Goff <mlegoff@altavia-conseil.fr>"
    moreau = "Grégoire Moreau <gmoreau@moreau-avocats.fr>"
    out = _run_files(
        tmp_path,
        canary_config,
        {
            "008 - Point Groupe Sorbel.eml": _mail(
                maelle,
                helene,
                "Point Groupe Sorbel",
                "Bonjour Hélène,\nLe point sur le Groupe Sorbel.\n"
                "Maëlle Le Goff\nAltavia Conseil\n",
            ),
            "009.eml": _mail(helene, maelle, "RE: Point", "Merci Maëlle.\nH.\n"),
            "010.eml": _mail(
                moreau, maelle, "Note", "Bonjour Maëlle,\nGrégoire Moreau\nMoreau Avocats\n"
            ),
            "021.eml": _mail(moreau, maelle, "Avis", "Avis favorable.\nGM\n"),
        },
        ReplacementMode.REALISTIC,
    )
    # Every file verified with no false alarm (« _run_files »), the file name included.
    assert len(out) == 4
    text = "\n".join(out.values())
    plain = normalize(text)
    for original in ("sorbel", "altavia", "moreau", "marchetti", "helene", "maelle", "gregoire"):
        assert original not in plain, original
    # Raw UTF-8 headers are read as such: no U+FFFD read or written back.
    assert "�" not in text
    sender = re.search(r"^From: (.+) <", out["009.eml"], re.M)
    assert sender is not None
    name = str(make_header(decode_header(sender.group(1))))
    # « H. » signs with the initial of Hélène's substitute.
    assert out["009.eml"].rstrip().endswith(f"\n{name[0]}.")
    # The firm's name is masked like its domain, which follows its substitute; the
    # lawyer's domain follows his substitute surname.
    first = next(v for k, v in out.items() if k.startswith("008 - Point Groupe "))
    org = re.search(r"\n([^\n]+) Conseil\r?\n", first)
    assert org is not None and f"@{slug(org.group(1))}-conseil.fr>" in plain
    lawyer = re.search(r"\n([^\n]+) Avocats\r?\n", out["010.eml"])
    assert lawyer is not None and f"@{slug(lawyer.group(1))}-avocats.fr>" in plain


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
