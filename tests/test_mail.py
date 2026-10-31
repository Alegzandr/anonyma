from __future__ import annotations

import email
import io
import re
import struct
import zipfile
from email.message import EmailMessage, Message
from email.policy import SMTP, compat32
from pathlib import Path

import extract_msg
import olefile
import pytest
from extract_msg.enums import AttachmentType
from PIL import Image

from anonyma import ocr
from anonyma.config import Config
from anonyma.extractors._mime import (
    decode_header_value,
    encode_header_text,
    fold_header,
    write_mime,
)
from anonyma.extractors.mbox import split_mbox
from anonyma.models import (
    DocumentStatus,
    OcrLine,
    Replacement,
    ReplacementPlan,
    SegmentKind,
    Severity,
)
from anonyma.pipeline import Pipeline, extract_file
from tests.canary import CANARY_EMAIL, CANARY_PERSON


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


def _mail() -> EmailMessage:
    msg = EmailMessage(policy=SMTP)
    msg["Received"] = "from relais.exemple.test ([192.0.2.7]) by mx.exemple.test"
    msg["From"] = f"{CANARY_PERSON} <{CANARY_EMAIL}>"
    msg["To"] = "Bérénice Fauxnom <berenice.fauxnom@exemple.test>"
    msg["Bcc"] = CANARY_EMAIL
    msg["Subject"] = f"Re: dossier {CANARY_PERSON}"
    msg["X-Originating-IP"] = "[192.0.2.7]"
    msg["DKIM-Signature"] = "v=1; d=exemple.test; b=AAAA"
    msg["Authentication-Results"] = "mx.exemple.test; spf=pass smtp.mailfrom=exemple.test"
    msg.set_content(f"Bonjour,\n> {CANARY_PERSON} a écrit :\n> merci\n-- \n{CANARY_PERSON}\n")
    msg.add_alternative(
        f'<p>Bonjour <a href="mailto:{CANARY_EMAIL}">{CANARY_PERSON}</a>'
        '<img src="cid:logo@exemple.test"></p>',
        subtype="html",
    )
    msg.get_payload()[1].add_related(
        _png(), maintype="image", subtype="png", cid="<logo@exemple.test>"
    )
    msg.add_attachment(
        b"Pi\xc3\xa8ce de " + CANARY_PERSON.encode(),
        maintype="text",
        subtype="plain",
        filename=f"Pièce {CANARY_PERSON}.txt",
    )
    inner = EmailMessage(policy=SMTP)
    inner["From"] = CANARY_EMAIL
    inner["Subject"] = f"Transfert {CANARY_PERSON}"
    inner.set_content(f"Signé {CANARY_PERSON}")
    msg.add_attachment(inner)
    return msg


def _run(tmp_path: Path, name: str, data: bytes, config: Config) -> tuple[Path, str]:
    src = tmp_path / "in"
    src.mkdir(exist_ok=True)
    (src / name).write_bytes(data)
    result = Pipeline(config).run([src / name], tmp_path / "out")
    top = result.results[0]
    assert top.status is DocumentStatus.ANONYMIZED, [i.message for i in top.issues]
    assert top.output_path is not None
    return top.output_path, top.output_name


# -- headers -------------------------------------------------------------------


def test_decode_header_value_rfc2047() -> None:
    assert decode_header_value("=?utf-8?q?Z=C3=A9phyrin?= =?utf-8?b?IFF1aW50YXZhbGxl?=") == (
        "Zéphyrin Quintavalle"
    )
    # Multibyte character split across two encoded words.
    assert decode_header_value("=?utf-8?q?Z=C3?=\r\n =?utf-8?q?=A9?=") == "Zé"
    assert (
        decode_header_value("=?iso-8859-1?q?Ren=E9?= <a@exemple.test>") == "René <a@exemple.test>"
    )
    raw_8bit = "Zéphyrin".encode().decode("ascii", "surrogateescape")
    assert decode_header_value(raw_8bit) == "Zéphyrin"
    assert decode_header_value("=?x-inconnu?q?abc?=") == "abc"


def test_encode_header_text_round_trip() -> None:
    for value in (
        f'"{CANARY_PERSON}" <{CANARY_EMAIL}>',
        f"{CANARY_PERSON} <{CANARY_EMAIL}>, Élise Fauxnom <e@exemple.test>",
        "Réunion : budget (révisé) ; suite",
        "plain ascii",
    ):
        encoded = encode_header_text(value)
        assert encoded.isascii()
        assert f"<{CANARY_EMAIL}>" in encoded or CANARY_EMAIL not in value
        decoded = decode_header_value(encoded)
        assert decoded.replace('"', "") == value.replace('"', "")
    folded = fold_header("Subject", encode_header_text("é" * 200), "\r\n")
    assert all(len(line) <= 78 for line in folded.split("\r\n"))
    assert decode_header_value(folded.split(":", 1)[1].strip()) == "é" * 200


# -- eml -----------------------------------------------------------------------


def test_eml_extraction_exposes_everything(tmp_path: Path, canary_config: Config) -> None:
    path = tmp_path / "m.eml"
    path.write_bytes(_mail().as_bytes())
    doc = extract_file(path, "m.eml", canary_config, tmp_path / "w")
    assert doc.status is DocumentStatus.EXTRACTED
    labels = {s.context.label for s in doc.segments if s.context.kind is SegmentKind.HEADER}
    assert {"Received", "From", "To", "Bcc", "Subject", "X-Originating-IP"} <= labels
    assert "Authentication-Results" in labels
    assert "DKIM-Signature" not in labels
    bodies = [s.text for s in doc.segments if s.context.kind is SegmentKind.BODY]
    assert any(CANARY_PERSON in b for b in bodies)
    hrefs = [s.text for s in doc.segments if s.context.label.endswith("href")]
    assert hrefs == [f"mailto:{CANARY_EMAIL}"]
    names = {c.name for c in doc.children}
    assert names == {"message-joint-3.eml", "piece-jointe-1.png", f"Pièce {CANARY_PERSON}.txt"}
    assert all(c.status is DocumentStatus.EXTRACTED for c in doc.children)


def test_eml_round_trip(tmp_path: Path, canary_config: Config) -> None:
    output, name = _run(tmp_path, "m.eml", _mail().as_bytes(), canary_config)
    data = output.read_bytes()
    assert b"uintavalle" not in data.lower()
    parsed = email.message_from_bytes(data, policy=compat32)
    assert parsed["DKIM-Signature"] is None
    assert "192.0.2.7" not in parsed["X-Originating-IP"]  # IPs are masked
    assert parsed["Authentication-Results"] is not None
    structure = [p.get_content_type() for p in parsed.walk()]
    assert structure == [
        "multipart/mixed",
        "multipart/alternative",
        "text/plain",
        "multipart/related",
        "text/html",
        "image/png",
        "text/plain",
        "message/rfc822",
        "text/plain",
    ]
    attachment = next(p for p in parsed.walk() if p.get_filename())
    assert attachment.get_filename() == "Pièce [PERSONNE_001].txt"
    assert b"[PERSONNE_001]" in (attachment.get_payload(decode=True) or b"")
    reread = extract_file(output, name, canary_config, tmp_path / "re")
    for doc in reread.walk():
        assert doc.status is DocumentStatus.EXTRACTED
        for segment in doc.segments:
            assert "uintavalle" not in segment.text.casefold()


def test_eml_keeps_legacy_charset_and_encoding(tmp_path: Path, canary_config: Config) -> None:
    raw = (
        b"From: a@exemple.test\r\nSubject: =?iso-8859-1?q?R=E9union?=\r\nMIME-Version: 1.0\r\n"
        b"Content-Type: text/plain; charset=iso-8859-1\r\n"
        b"Content-Transfer-Encoding: quoted-printable\r\n\r\n"
        b"Le dossier de Z=E9phyrin Quintavalle est pr=EAt.\r\n"
    )
    output, _ = _run(tmp_path, "m.eml", raw, canary_config)
    parsed = email.message_from_bytes(output.read_bytes(), policy=compat32)
    assert parsed.get_content_charset() == "iso-8859-1"
    assert parsed["Content-Transfer-Encoding"] == "quoted-printable"
    assert parsed["Subject"] == "=?iso-8859-1?q?R=E9union?="  # untouched header kept verbatim
    body = parsed.get_payload(decode=True)
    assert isinstance(body, bytes)
    assert body.decode("iso-8859-1") == "Le dossier de [PERSONNE_001] est prêt.\r\n"


def test_eml_switches_to_utf8_when_substitute_needs_it(
    tmp_path: Path, canary_config: Config
) -> None:
    raw = (
        b"From: Bob Fauxnom <bob@exemple.test>\r\nMIME-Version: 1.0\r\n"
        b"Content-Type: text/plain; charset=us-ascii\r\nContent-Transfer-Encoding: 7bit\r\n"
        b"\r\nBob Fauxnom\r\n"
    )
    path = tmp_path / "m.eml"
    path.write_bytes(raw)
    doc = extract_file(path, "m.eml", canary_config, tmp_path / "w")
    plan = ReplacementPlan(doc.id)
    for segment in doc.segments:
        start = segment.text.find("Bob Fauxnom")
        if start >= 0:
            plan.by_segment[segment.id] = [
                Replacement(start, start + 11, "Bob Fauxnom", "Zoé Lefèvre", "PERSONNE", "c")
            ]
    data, issues = write_mime(raw, doc, plan, {})
    assert not issues
    parsed = email.message_from_bytes(data, policy=compat32)
    assert parsed.get_content_charset() == "utf-8"
    assert parsed["Content-Transfer-Encoding"] == "quoted-printable"
    assert parsed.get_payload(decode=True) == "Zoé Lefèvre\r\n".encode()
    assert parsed["From"].startswith("=?utf-8?b?")
    assert decode_header_value(parsed["From"]) == "Zoé Lefèvre <bob@exemple.test>"


def test_encrypted_mail_fails(tmp_path: Path, canary_config: Config) -> None:
    raw = (
        b"From: a@exemple.test\r\nMIME-Version: 1.0\r\n"
        b'Content-Type: application/pkcs7-mime; smime-type=enveloped-data; name="smime.p7m"\r\n'
        b"Content-Transfer-Encoding: base64\r\n\r\nAAAA\r\n"
    )
    path = tmp_path / "s.eml"
    path.write_bytes(raw)
    doc = extract_file(path, "s.eml", canary_config, tmp_path / "w")
    assert doc.status is DocumentStatus.FAILED
    assert any(i.code == "mail.encrypted" and i.severity is Severity.ERROR for i in doc.issues)


def test_signature_part_is_dropped(tmp_path: Path, canary_config: Config) -> None:
    raw = (
        "From: a@exemple.test\r\nMIME-Version: 1.0\r\n"
        'Content-Type: multipart/signed; protocol="application/pkcs7-signature"; '
        'micalg=sha-256; boundary="b"\r\n\r\n'
        f"--b\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n{CANARY_PERSON}\r\n"
        "--b\r\nContent-Type: application/pkcs7-signature; name=smime.p7s\r\n"
        "Content-Transfer-Encoding: base64\r\n\r\nAAAA\r\n--b--\r\n"
    ).encode()
    output, _ = _run(tmp_path, "s.eml", raw, canary_config)
    parsed = email.message_from_bytes(output.read_bytes(), policy=compat32)
    assert [p.get_content_type() for p in parsed.walk()] == ["multipart/signed", "text/plain"]


def test_unix_from_line_is_anonymized(tmp_path: Path, canary_config: Config) -> None:
    raw = (
        f"From {CANARY_EMAIL} Mon Jan  5 10:00:00 2026\r\nFrom: x@exemple.test\r\n\r\ncorps\r\n"
    ).encode()
    output, _ = _run(tmp_path, "u.eml", raw, canary_config)
    # A valid address, and the date of the line is never kept (no Date header: neutral).
    line = rb"From EMAIL_\d{3}@anonyme\.invalid Sun Mar  1 00:00:00 1970\r\n"
    assert re.match(line, output.read_bytes())


def test_nested_mail_in_zip_in_mail(tmp_path: Path, canary_config: Config) -> None:
    inner = EmailMessage(policy=SMTP)
    inner["From"] = f"{CANARY_PERSON} <{CANARY_EMAIL}>"
    inner["Subject"] = "Interne"
    inner.set_content(f"Texte de {CANARY_PERSON}")
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr(f"{CANARY_PERSON}/interne.eml", inner.as_bytes())
    outer = EmailMessage(policy=SMTP)
    outer["From"] = "x@exemple.test"
    outer["Subject"] = "Archive"
    outer.set_content("Voir pièce jointe.")
    outer.add_attachment(
        archive.getvalue(), maintype="application", subtype="zip", filename="dossier.zip"
    )
    output, name = _run(tmp_path, "outer.eml", outer.as_bytes(), canary_config)
    reread = extract_file(output, name, canary_config, tmp_path / "re")
    formats = [d.format for d in reread.walk()]
    assert formats == ["eml", "zip", "eml"]
    leaf = list(reread.walk())[-1]
    assert leaf.name == "[PERSONNE_001]/interne.eml"
    assert all("uintavalle" not in s.text.casefold() for d in reread.walk() for s in d.segments)


def test_unsupported_attachment_is_omitted(tmp_path: Path, canary_config: Config) -> None:
    msg = EmailMessage(policy=SMTP)
    msg["From"] = "x@exemple.test"
    msg.set_content("corps")
    msg.add_attachment(
        b"MZ\x00\x01", maintype="application", subtype="octet-stream", filename="outil.exe"
    )
    src = tmp_path / "in"
    src.mkdir()
    (src / "m.eml").write_bytes(msg.as_bytes())
    result = Pipeline(canary_config).run([src / "m.eml"], tmp_path / "out")
    top = result.results[0]
    assert top.status is DocumentStatus.NEEDS_REVIEW  # attachment left out
    assert any(i.code == "child.omitted" for i in top.issues)
    assert top.output_path is not None
    parsed: Message = email.message_from_bytes(top.output_path.read_bytes(), policy=compat32)
    assert [p.get_content_type() for p in parsed.walk()] == ["multipart/mixed", "text/plain"]


# -- msg -----------------------------------------------------------------------

FIXTURES = Path(__file__).parent / "fixtures" / "contract"


def _msg_output(tmp_path: Path, config: Config) -> Path:
    output, name = _run(tmp_path, "message.msg", (FIXTURES / "message.msg").read_bytes(), config)
    assert name == "message.msg"
    return output


def test_msg_native_output_has_no_canary_bytes(tmp_path: Path, canary_config: Config) -> None:
    data = _msg_output(tmp_path, canary_config).read_bytes().lower()
    for needle in ("quintavalle", "zephyrin", "zéphyrin"):
        for encoding in ("utf-8", "utf-16-le", "cp1252"):
            assert needle.encode(encoding) not in data, (needle, encoding)


def test_msg_native_output_structure(tmp_path: Path, canary_config: Config) -> None:
    output = _msg_output(tmp_path, canary_config)
    with olefile.OleFileIO(str(output)) as ole:
        streams = {"/".join(p) for p in ole.listdir()}
    # Binary identity carriers, RTF body, X500 DN, local path and icon are gone.
    for gone in ("0C190102", "0C1D0102", "00710102", "10090102", "0065001F"):
        assert f"__substg1.0_{gone}" not in streams
    assert "__recip_version1.0_#00000000/__substg1.0_0FFF0102" not in streams
    assert "__attach_version1.0_#00000000/__substg1.0_370D001F" not in streams
    assert "__attach_version1.0_#00000000/__substg1.0_37090102" not in streams
    assert "__substg1.0_10130102" in streams  # HTML body kept (anonymized)
    assert "__nameid_version1.0/__substg1.0_00020102" in streams

    msg = extract_msg.openMsg(str(output), strict=False)
    try:
        assert msg.subject == "Dossier [PERSONNE_001]"
        assert msg.sender == '"[PERSONNE_001]" <EMAIL_001@anonyme.invalid>'
        assert "[PERSONNE_001]" in (msg.body or "")
        assert b"[PERSONNE_001]" in (msg.htmlBody or b"")
        assert "X-Originating-Email: [EMAIL_001]" in (msg.headerText or "")
        # The recipient is not a canary but is found by the regular detectors.
        [(name, address)] = [(r.name, r.email) for r in msg.recipients]
        assert "Fauxnom" not in name and "fauxnom" not in address
        assert name.startswith("[PERSONNE_") and address.startswith("EMAIL_")
        assert address.endswith("@anonyme.invalid")  # a valid address carrying the tag
        data_att, embedded = msg.attachments
        assert data_att.longFilename == "CV [PERSONNE_001].txt"
        assert data_att.data == b"CV de [PERSONNE_001]\r\n[EMAIL_001]\r\n"
        assert embedded.type is AttachmentType.MSG
        assert embedded.data.subject == "Transfert [PERSONNE_001]"
        assert "[PERSONNE_001]" in embedded.data.body
    finally:
        msg.close()


def _rtf_only_msg() -> bytes:
    import compressed_rtf
    from extract_msg.ole_writer import OleWriter

    from tests.fixtures.generators.mail_archive_image import PT_LONG, _add_props

    writer = OleWriter()
    rtf = b"{\\rtf1\\ansi Dossier de Z\\'e9phyrin Quintavalle\\par}"
    _add_props(
        writer,
        "",
        b"\0" * 8 + struct.pack("<IIII", 0, 3, 0, 3) + b"\0" * 8,
        {0x0E1F: (0x000B, 1), 0x1016: (PT_LONG, 2)},
        {0x001A: "IPM.Note", 0x0037: "Objet"},
        {0x1009: compressed_rtf.compress(rtf, compressed=True)},
    )
    _add_props(  # unsupported attachment: its child cannot be anonymized
        writer,
        "__attach_version1.0_#00000000/",
        b"\0" * 8,
        {0x3705: (PT_LONG, 1)},
        {0x3707: "outil.exe"},
        {0x3701: b"MZ\x00\x01"},
    )
    _add_props(  # OLE object: removed
        writer,
        "__attach_version1.0_#00000001/",
        b"\0" * 8,
        {0x3705: (PT_LONG, 6)},
        {0x3707: "objet"},
        {},
    )
    writer.addEntry("__attach_version1.0_#00000001/__substg1.0_3701000D", storage=True)
    writer.addEntry("__attach_version1.0_#00000001/__substg1.0_3701000D/CONTENTS", b"ole")
    _add_props(
        writer,
        "__attach_version1.0_#00000002/",
        b"\0" * 8,
        {0x3705: (PT_LONG, 1)},
        {0x3707: f"Note {CANARY_PERSON}.txt"},
        {0x3701: CANARY_PERSON.encode()},
    )
    for name in ("00020102", "00030102", "00040102"):
        writer.addEntry(f"__nameid_version1.0/__substg1.0_{name}", b"")
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def test_msg_rtf_only_body_and_removed_attachments(tmp_path: Path, canary_config: Config) -> None:
    src = tmp_path / "in"
    src.mkdir()
    (src / "r.msg").write_bytes(_rtf_only_msg())
    top = Pipeline(canary_config).run([src / "r.msg"], tmp_path / "out").results[0]
    # An attachment left out: « À revoir », never « Anonymisé ».
    assert top.status is DocumentStatus.NEEDS_REVIEW, [i.message for i in top.issues]
    assert any(i.code == "msg.ole_attachment" for i in top.issues)
    assert any(i.code == "child.omitted" for i in top.issues)
    assert top.output_path is not None
    msg = extract_msg.openMsg(str(top.output_path), strict=False)
    try:
        assert msg.body == "Dossier de [PERSONNE_001]\n"
        assert msg.rtfBody is None
        assert [a.longFilename for a in msg.attachments] == ["Note [PERSONNE_001].txt"]
    finally:
        msg.close()
    with olefile.OleFileIO(str(top.output_path)) as ole:
        header = ole.openstream("__properties_version1.0").read(32)
    assert struct.unpack_from("<I", header, 20)[0] == 1  # attachment count updated


def test_msg_unreadable_rtf_body_is_reported(
    tmp_path: Path, canary_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    import compressed_rtf

    def broken(_data: bytes) -> bytes:
        raise ValueError("corrupt")

    source = tmp_path / "r.msg"
    source.write_bytes(_rtf_only_msg())
    monkeypatch.setattr(compressed_rtf, "decompress", broken)
    doc = extract_file(source, "r.msg", canary_config, tmp_path / "w")
    assert "msg.rtf_unreadable" in {i.code for i in doc.issues}


# -- mbox ----------------------------------------------------------------------


def test_split_mbox() -> None:
    data = (
        b"From a@exemple.test Mon Jan  5 10:00:00 2026\nSubject: 1\n\nligne\n"
        b"From pas un separateur\n\n"
        b"From b@exemple.test Mon Jan  5 11:00:00 2026\nSubject: 2\n\ncorps\n\n"
    )
    messages = split_mbox(data)
    assert [m[0] for m in messages] == [
        b"a@exemple.test Mon Jan  5 10:00:00 2026",
        b"b@exemple.test Mon Jan  5 11:00:00 2026",
    ]
    assert messages[0][1] == b"Subject: 1\n\nligne\nFrom pas un separateur\n"


def test_mbox_round_trip(tmp_path: Path, canary_config: Config) -> None:
    data = (
        f"From {CANARY_EMAIL} Mon Jan  5 10:00:00 2026\n"
        f"From: {CANARY_EMAIL}\nSubject: un\n\nBonjour {CANARY_PERSON}\n\n"
        f"From {CANARY_EMAIL} Mon Jan  5 11:00:00 2026\n"
        f"From: {CANARY_EMAIL}\nSubject: deux\n\nAu revoir\n\n"
    ).encode()
    output, _ = _run(tmp_path, "boite.mbox", data, canary_config)
    out = output.read_bytes()
    neutral = rb"From EMAIL_\d{3}@anonyme\.invalid Sun Mar  1 00:00:00 1970"
    assert len(re.findall(neutral, out)) == 2  # no Date header: neutral separator date
    assert b"uintavalle" not in out.lower()
    assert len(split_mbox(out)) == 2
