"""Anonymized e-mail headers stay valid (RFC 5322) and inline images keep working."""

from __future__ import annotations

import dataclasses
import email
import io
import re
from email.message import EmailMessage, Message
from email.policy import SMTP, compat32
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path

import extract_msg
import pytest
from PIL import Image

from anonyma import ocr
from anonyma.config import Config
from anonyma.extractors._mailheaders import (
    exposed_text,
    map_cid_url,
    neutral_date,
    safe_address_list,
    safe_date,
    safe_from_line,
    safe_header,
    safe_message_ids,
    thread_depth,
    with_hidden_tail,
)
from anonyma.models import DocumentStatus, OcrLine, Replacement, ReplacementMode
from anonyma.pipeline import Pipeline
from anonyma.policy import is_mail_placeholder
from tests.canary import CANARY_EMAIL, CANARY_PERSON

MSG_ID = re.compile(r"<[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]+@[A-Za-z0-9.-]+>")
ADDRESS = re.compile(r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")


class _NoText:
    def recognize(self, image: Image.Image) -> list[OcrLine]:
        return []


@pytest.fixture(autouse=True)
def fake_ocr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ocr, "create_engine", lambda config: _NoText())


def _png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (20, 10), "white").save(buffer, "PNG")
    return buffer.getvalue()


def _rep(text: str, value: str, substitute: str, cluster: str = "c") -> Replacement:
    start = text.index(value)
    return Replacement(start, start + len(value), value, substitute, "X", cluster)


# -- helpers -------------------------------------------------------------------


def test_address_list_in_tag_mode_is_valid() -> None:
    original = f"{CANARY_PERSON} <{CANARY_EMAIL}>, autre@exemple.test"
    reps = [
        _rep(original, CANARY_PERSON, "[PERSONNE_009]"),
        _rep(original, CANARY_EMAIL, "[EMAIL_001]"),
        _rep(original, "autre@exemple.test", "[EMAIL_002]"),
    ]
    value = safe_header("From", original, reps)
    assert value == '"[PERSONNE_009]" <EMAIL_001@anonyme.invalid>, EMAIL_002@anonyme.invalid'
    assert getaddresses([value]) == [
        ("[PERSONNE_009]", "EMAIL_001@anonyme.invalid"),
        ("", "EMAIL_002@anonyme.invalid"),
    ]


def test_address_list_edge_cases() -> None:
    # A partly replaced address is replaced as a whole (nothing original survives).
    assert safe_address_list("M. [PERSONNE_001] <[PERSONNE_001].x@gmail.com>") == (
        '"M. [PERSONNE_001]" <PERSONNE_001@anonyme.invalid>'
    )
    assert safe_address_list("████ <████████>") == "████ <MASQUE_000@anonyme.invalid>"
    assert safe_address_list("Équipe: [PERSONNE_001] <[EMAIL_001]>;") == (
        'Équipe: "[PERSONNE_001]" <EMAIL_001@anonyme.invalid>;'
    )
    # Realistic substitutes are kept, a display name with specials gets quoted.
    assert safe_address_list("Paul Martin <p.martin@exemple.test>") == (
        "Paul Martin <p.martin@exemple.test>"
    )
    assert safe_address_list('"Martin, Paul" <p.martin@exemple.test>') == (
        '"Martin, Paul" <p.martin@exemple.test>'
    )


def test_message_ids() -> None:
    original = "<a.1@exemple.test> <b.2@exemple.test>"
    reps = [
        _rep(original, "<a.1@exemple.test>", "[ID_MESSAGE_001]", "x"),
        _rep(original, "<b.2@exemple.test>", "[ID_MESSAGE_002]", "y"),
    ]
    assert safe_header("References", original, reps) == (
        "<ID_MESSAGE_001@anonyme.invalid> <ID_MESSAGE_002@anonyme.invalid>"
    )
    # Blacked out or partly replaced ids: placeholders keyed by the replaced text and
    # the batch cluster ids (same value -> same id), never by an original value.
    black = [Replacement(0, 18, "<a.1@exemple.test>", "█" * 18, "X", "x")]
    first = safe_header("Message-ID", "<a.1@exemple.test>", black)
    assert first == safe_header("In-Reply-To", "<a.1@exemple.test>", black)
    assert re.fullmatch(r"<ID_MESSAGE_\d{8}@anonyme\.invalid>", first)
    assert MSG_ID.fullmatch(safe_message_ids("<ab.[PERSONNE_001]@[URL_002]> (note)"))


def test_dates() -> None:
    assert safe_date("Tue, [DATE_002]") == "01 Mar 1970 00:00:00 +0000"
    assert safe_date("████████", 2) == neutral_date(2) == "01 Mar 1970 00:02:00 +0000"
    # A realistic substitute is kept, its day of week corrected.
    assert safe_date("Mon, 08 Apr 2025 16:14:18 +0100") == "Tue, 08 Apr 2025 16:14:18 +0100"
    assert parsedate_to_datetime(neutral_date(5)) > parsedate_to_datetime(neutral_date(4))
    assert thread_depth([("In-Reply-To", "<a@b>")]) == 1
    assert thread_depth([("References", "<a@b> <c@d>"), ("In-Reply-To", "<c@d>")]) == 2
    received = "from a by b; Tue, 14 Jan 2025 10:12:00 +0100"
    reps = [_rep(received, "14 Jan 2025 10:12:00 +0100", "[DATE_001]")]
    assert safe_header("Received", received, reps) == "from a by b; 01 Mar 1970 00:00:00 +0000"


def test_from_line_and_cid_urls() -> None:
    assert safe_from_line("[EMAIL_001] [DATE_003]") == (
        "EMAIL_001@anonyme.invalid Sun Mar  1 00:00:00 1970"
    )
    assert safe_from_line("a@b.test Mon Jan  5 10:00:00 2026") == (
        "a@b.test Mon Jan  5 10:00:00 2026"
    )
    assert safe_from_line("") == "MAILER-DAEMON Sun Mar  1 00:00:00 1970"
    ids = {"logo@exemple.test": "CONTENU_001@anonyme.invalid"}
    assert map_cid_url("cid:logo@exemple.test", ids) == "cid:CONTENU_001@anonyme.invalid"
    assert map_cid_url("CID:<Logo@exemple.test>", ids) == "cid:CONTENU_001@anonyme.invalid"
    assert map_cid_url("https://exemple.test/logo.png", ids) is None


def test_placeholders_are_not_residues_but_names_are() -> None:
    for value in (
        "EMAIL_001@anonyme.invalid",
        "<ID_MESSAGE_00012345@anonyme.invalid>",
        "cid:CONTENU_001@anonyme.invalid",
        "EMAIL_K7Q2@anonyme.invalid",
        "anonyme.invalid",
    ):
        assert is_mail_placeholder(value), value
    for value in (
        "jean.dupont@anonyme.invalid",
        "JEAN_DUPONT@anonyme.invalid",
        "EMAIL_001@exemple.test",
    ):
        assert not is_mail_placeholder(value), value


# -- end to end ----------------------------------------------------------------


def _mail(message_id: str, reply_to: str | None = None) -> bytes:
    msg = EmailMessage(policy=SMTP)
    msg["From"] = f"{CANARY_PERSON} <{CANARY_EMAIL}>"
    msg["To"] = "Bérénice Fauxnom <berenice.fauxnom@exemple.test>"
    msg["Subject"] = "Dossier"
    msg["Date"] = "Tue, 14 Jan 2025 10:12:00 +0100"
    msg["Message-ID"] = message_id
    if reply_to:
        msg["In-Reply-To"] = reply_to
        msg["References"] = reply_to
    msg.set_content(f"Bonjour,\n{CANARY_PERSON}\n")
    msg.add_alternative(
        f'<p>{CANARY_PERSON}<img src="cid:{CANARY_EMAIL}"></p>'
        '<p><img src="cid:image001.png@01DB6667.1A2B3C40"></p>',
        subtype="html",
    )
    html = msg.get_payload()[1]
    html.add_related(_png(), maintype="image", subtype="png", cid=f"<{CANARY_EMAIL}>")
    html.add_related(
        _png(), maintype="image", subtype="png", cid="<image001.png@01DB6667.1A2B3C40>"
    )
    return msg.as_bytes()


def _run(tmp_path: Path, files: dict[str, bytes], config: Config) -> dict[str, Message]:
    src = tmp_path / "in"
    src.mkdir()
    for name, data in files.items():
        (src / name).write_bytes(data)
    result = Pipeline(config).run([src / n for n in files], tmp_path / "out")
    out: dict[str, Message] = {}
    for top in result.results:
        assert top.status in (DocumentStatus.ANONYMIZED, DocumentStatus.NEEDS_REVIEW), [
            i.message for i in top.issues
        ]
        assert top.residue_count == 0 and top.output_path is not None
        data = top.output_path.read_bytes()
        assert b"uintavalle" not in data.lower()
        out[top.output_name] = email.message_from_bytes(data, policy=compat32)
    return out


def _cids(message: Message) -> tuple[list[str], list[str]]:
    html = next(p for p in message.walk() if p.get_content_type() == "text/html")
    text = (html.get_payload(decode=True) or b"").decode()
    refs = re.findall(r'src="cid:([^"]+)"', text)
    ids = [str(p["Content-ID"]).strip("<>") for p in message.walk() if p["Content-ID"]]
    return refs, ids


@pytest.mark.parametrize("mode", list(ReplacementMode))
def test_eml_headers_stay_valid(tmp_path: Path, canary_config: Config, mode: str) -> None:
    config = dataclasses.replace(canary_config, mode=ReplacementMode(mode))
    [message] = _run(tmp_path, {"m.eml": _mail("<a1.zq@exemple.test>")}, config).values()
    for header in ("From", "To"):
        [(name, address)] = getaddresses([str(message[header])])
        assert ADDRESS.fullmatch(address), (header, message[header])
    assert MSG_ID.fullmatch(str(message["Message-ID"]))
    assert parsedate_to_datetime(str(message["Date"])) is not None
    refs, ids = _cids(message)
    assert refs == ids and len(ids) == 2  # inline images still resolve
    if mode == ReplacementMode.TAG:
        [(name, address)] = getaddresses([str(message["From"])])
        tag = name.strip("[]")
        assert name.startswith("[PERSONNE_") and address.startswith("EMAIL_")
        body = next(p for p in message.walk() if p.get_content_type() == "text/plain")
        assert f"[{tag}]" in (body.get_payload(decode=True) or b"").decode()


def test_thread_survives_in_tag_mode(tmp_path: Path, canary_config: Config) -> None:
    files = {
        "a.eml": _mail("<a1.zq@exemple.test>"),
        "b.eml": _mail("<b2.zq@exemple.test>", reply_to="<a1.zq@exemple.test>"),
    }
    out = _run(tmp_path, files, canary_config)
    first, reply = (out[n] for n in sorted(out))
    if first["In-Reply-To"] is not None:
        first, reply = reply, first
    assert str(reply["In-Reply-To"]) == str(first["Message-ID"])
    assert str(reply["References"]) == str(first["Message-ID"])
    assert MSG_ID.fullmatch(str(first["Message-ID"]))
    assert parsedate_to_datetime(str(reply["Date"])) > parsedate_to_datetime(str(first["Date"]))


def test_mbox_from_lines_stay_valid(tmp_path: Path, canary_config: Config) -> None:
    mbox = b"".join(
        b"From " + CANARY_EMAIL.encode() + b" le 14/01/2025\n" + _mail(i) + b"\n"
        for i in ("<a1.zq@exemple.test>", "<b2.zq@exemple.test>")
    )
    src = tmp_path / "in"
    src.mkdir()
    (src / "boite.mbox").write_bytes(mbox)
    top = Pipeline(canary_config).run([src / "boite.mbox"], tmp_path / "out").results[0]
    assert top.output_path is not None
    data = top.output_path.read_bytes()
    lines = [ln.rstrip(b"\r") for ln in data.split(b"\n") if ln.startswith(b"From ")]
    assert lines == [b"From EMAIL_001@anonyme.invalid Sun Mar  1 00:00:00 1970"] * 2


def _msg_with_inline_image() -> bytes:
    import struct

    from extract_msg.ole_writer import OleWriter

    from tests.fixtures.generators.mail_archive_image import PT_LONG, _add_props

    writer = OleWriter()
    cid = f"{CANARY_EMAIL.split('@')[0]}.sig@exemple.test"
    html = f'<html><body><p>{CANARY_PERSON}</p><img src="cid:{cid}"></body></html>'
    _add_props(
        writer,
        "",
        b"\0" * 8 + struct.pack("<IIII", 1, 1, 1, 1) + b"\0" * 8,
        {0x1016: (PT_LONG, 3)},
        {
            0x001A: "IPM.Note",
            0x0037: f"Dossier {CANARY_PERSON}",
            0x0C1A: CANARY_PERSON,
            0x0C1F: CANARY_EMAIL,
            0x5D01: CANARY_EMAIL,
            0x1035: "<a1.zq@exemple.test>",
            0x1042: "<a0.zq@exemple.test>",
            0x1000: f"Bonjour {CANARY_PERSON}\r\n",
        },
        {0x1013: html.encode("utf-8")},
    )
    _add_props(
        writer,
        "__recip_version1.0_#00000000/",
        b"\0" * 8,
        {0x0C15: (PT_LONG, 1), 0x3000: (PT_LONG, 0)},
        {0x3001: CANARY_PERSON, 0x39FE: CANARY_EMAIL, 0x3003: CANARY_EMAIL, 0x3002: "SMTP"},
        {},
    )
    _add_props(
        writer,
        "__attach_version1.0_#00000000/",
        b"\0" * 8,
        {0x3705: (PT_LONG, 1), 0x0E21: (PT_LONG, 0)},
        {0x3707: "signature.png", 0x370E: "image/png", 0x3712: cid},
        {0x3701: _png()},
    )
    for name in ("00020102", "00030102", "00040102"):
        writer.addEntry(f"__nameid_version1.0/__substg1.0_{name}", b"")
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def test_msg_cid_and_ids_stay_valid(tmp_path: Path, canary_config: Config) -> None:
    src = tmp_path / "in"
    src.mkdir()
    (src / "m.msg").write_bytes(_msg_with_inline_image())
    top = Pipeline(canary_config).run([src / "m.msg"], tmp_path / "out").results[0]
    assert top.status in (DocumentStatus.ANONYMIZED, DocumentStatus.NEEDS_REVIEW)
    assert top.output_path is not None
    msg = extract_msg.openMsg(str(top.output_path), strict=False)
    try:
        [attachment] = msg.attachments
        html = (msg.htmlBody or b"").decode()
        assert f'src="cid:{attachment.cid}"' in html
        assert attachment.cid == "CONTENU_001@anonyme.invalid"
        assert MSG_ID.fullmatch(msg.messageId or "")
        [recipient] = msg.recipients
        assert ADDRESS.fullmatch(recipient.email)
        assert recipient.email.startswith("EMAIL_")
    finally:
        msg.close()


def test_neutral_dates_are_not_exposed_again() -> None:
    # Re-read for verification, the neutral date must not look like a birth date.
    assert exposed_text("Date", neutral_date(2)) == ""
    assert exposed_text("Date", "Tue, 14 Jan 2025 10:12:00 +0100") != ""
    received = f"from a by b; {neutral_date()}"
    assert exposed_text("Received", received) == "from a by b"
    assert with_hidden_tail(received, "from a by b", "from [URL_001] by b") == (
        f"from [URL_001] by b; {neutral_date()}"
    )
