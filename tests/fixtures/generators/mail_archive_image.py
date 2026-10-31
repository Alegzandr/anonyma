"""Generate mail / archive / image / HTML contract fixtures.

``uv run python -m tests.fixtures.generators.mail_archive_image``

Every fixture holds the canary person and e-mail (``tests/canary.py``) in its body
and in every metadata field of the format. Images render the canary text with a
TrueType font that has French accents (Arial on Windows, DejaVu Sans elsewhere) so
that the bundled OCR model reads it back.
"""

from __future__ import annotations

import io
import struct
import zipfile
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import formataddr
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont, TiffImagePlugin
from PIL.PngImagePlugin import PngInfo

from tests.canary import CANARY_EMAIL, CANARY_PERSON

OUT = Path(__file__).resolve().parent.parent / "contract"
COLLEAGUE = "Bérénice Fauxnom"
COLLEAGUE_EMAIL = "berenice.fauxnom@exemple.test"
FONT_CANDIDATES = (
    "C:/Windows/Fonts/arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/Library/Fonts/Arial.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
)


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------


def font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for candidate in FONT_CANDIDATES:
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size)
    raise SystemExit("Aucune police TrueType avec accents trouvée pour générer les images.")


def canary_image(
    lines: tuple[str, ...] | None = None, size: tuple[int, int] = (1300, 330)
) -> Image.Image:
    lines = lines or (f"Rapport de {CANARY_PERSON}", f"Contact : {CANARY_EMAIL}")
    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    f = font(46)
    for i, line in enumerate(lines):
        draw.text((40, 50 + i * 120), line, fill="black", font=f)
    return image


def exif_bytes() -> bytes:
    exif = Image.Exif()
    exif[0x013B] = CANARY_PERSON  # Artist
    exif[0x8298] = f"(c) {CANARY_PERSON}"  # Copyright
    exif[0x010E] = f"Photo de {CANARY_PERSON} {CANARY_EMAIL}"  # ImageDescription
    exif[0x9C9D] = CANARY_PERSON.encode("utf-16-le") + b"\0\0"  # XPAuthor
    exif[0x9C9C] = f"Contact {CANARY_EMAIL}".encode("utf-16-le") + b"\0\0"  # XPComment
    exif_ifd = exif.get_ifd(0x8769)
    exif_ifd[0x9286] = b"UNICODE\0" + f"Note {CANARY_PERSON}".encode("utf-16-be")  # UserComment
    exif_ifd[0xA430] = CANARY_PERSON  # CameraOwnerName
    gps = exif.get_ifd(0x8825)
    gps[1] = "N"
    gps[2] = (48.0, 51.0, 30.0)
    gps[3] = "E"
    gps[4] = (2.0, 17.0, 40.0)
    return exif.tobytes()


def xmp_packet() -> bytes:
    return (
        '<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF '
        'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/">'
        '<rdf:Description rdf:about=""><dc:creator><rdf:Seq>'
        f"<rdf:li>{CANARY_PERSON}</rdf:li></rdf:Seq></dc:creator>"
        f"<dc:rights>{CANARY_EMAIL}</dc:rights></rdf:Description></rdf:RDF></x:xmpmeta>"
        '<?xpacket end="w"?>'
    ).encode()


def png_bytes() -> bytes:
    info = PngInfo()
    info.add_text("Author", CANARY_PERSON)
    info.add_itxt("Description", f"Image de {CANARY_PERSON}", lang="fr")
    info.add_text("Comment", f"Contact {CANARY_EMAIL}", zip=True)
    buffer = io.BytesIO()
    canary_image().save(buffer, "PNG", pnginfo=info, exif=exif_bytes())
    return buffer.getvalue()


def write_images() -> None:
    (OUT / "scan.png").write_bytes(png_bytes())
    canary_image().save(OUT / "photo.jpg", "JPEG", quality=92, exif=exif_bytes(), xmp=xmp_packet())
    page2 = canary_image((f"Page 2 : {CANARY_PERSON}", f"Courriel {CANARY_EMAIL}"))
    tiff_info = TiffImagePlugin.ImageFileDirectory_v2()
    tiff_info[0x013B] = CANARY_PERSON
    tiff_info[0x010E] = f"Numérisation de {CANARY_PERSON}"
    canary_image().save(
        OUT / "numerisation.tiff",
        "TIFF",
        save_all=True,
        append_images=[page2],
        compression="tiff_lzw",
        tiffinfo=tiff_info,
    )
    canary_image().convert("L").save(OUT / "capture.bmp", "BMP")
    canary_image().save(OUT / "photo.webp", "WEBP", quality=95, exif=exif_bytes(), xmp=xmp_packet())
    import pillow_heif

    pillow_heif.register_heif_opener()
    canary_image().save(OUT / "photo.heic", "HEIF", quality=95, exif=exif_bytes(), xmp=xmp_packet())


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------


def html_page() -> str:
    return f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<title>Dossier de Z&eacute;phyrin Quintavalle</title>
<meta name="author" content="{CANARY_PERSON}">
<script type="application/ld+json">{{"@type": "Person", "name": "{CANARY_PERSON}",
 "email": "{CANARY_EMAIL}"}}</script>
</head>
<body>
<!-- Rédigé par {CANARY_PERSON} -->
<h1>Compte rendu</h1>
<p>Le dossier de <b>Z&eacute;phyrin</b> Quintavalle est suivi par {COLLEAGUE}.</p>
<p>Écrire à <a href="mailto:{CANARY_EMAIL}?subject=Dossier%20Z%C3%A9phyrin%20Quintavalle"
 title="Contacter {CANARY_PERSON}">{CANARY_EMAIL}</a>.</p>
<form><input type="text" name="contact" value="{CANARY_EMAIL}" placeholder="{CANARY_PERSON}">
</form>
<div data-owner="{CANARY_PERSON}">Signature : {CANARY_PERSON}</div>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Mails
# ---------------------------------------------------------------------------


def inner_message() -> EmailMessage:
    inner = EmailMessage(policy=SMTP)
    inner["From"] = formataddr((CANARY_PERSON, CANARY_EMAIL))
    inner["To"] = formataddr((COLLEAGUE, COLLEAGUE_EMAIL))
    inner["Subject"] = f"Message transféré de {CANARY_PERSON}"
    inner["Date"] = "Mon, 05 Jan 2026 09:00:00 +0100"
    inner["Message-ID"] = "<interne-0001@exemple.test>"
    inner.set_content(f"Bonjour,\n\nCordialement,\n{CANARY_PERSON}\n{CANARY_EMAIL}\n")
    return inner


def mail_message(with_image: bool = True) -> EmailMessage:
    msg = EmailMessage(policy=SMTP)
    msg["Received"] = (
        "from poste-12.exemple.test (poste.exemple.test [192.0.2.10]) "
        f"by mx.exemple.test with ESMTPS for <{CANARY_EMAIL}>; Mon, 5 Jan 2026 10:00:00 +0100"
    )
    msg["Return-Path"] = f"<{CANARY_EMAIL}>"
    msg["From"] = formataddr((CANARY_PERSON, CANARY_EMAIL))
    msg["To"] = formataddr((COLLEAGUE, COLLEAGUE_EMAIL))
    msg["Cc"] = formataddr((CANARY_PERSON, CANARY_EMAIL))
    msg["Reply-To"] = CANARY_EMAIL
    msg["Subject"] = f"Dossier {CANARY_PERSON} : pièces jointes"
    msg["Date"] = "Mon, 05 Jan 2026 10:00:00 +0100"
    msg["Message-ID"] = "<dossier-0001@exemple.test>"
    msg["X-Sender"] = CANARY_EMAIL
    msg["Thread-Topic"] = f"Dossier {CANARY_PERSON}"
    msg["DKIM-Signature"] = "v=1; a=rsa-sha256; d=exemple.test; s=sel; b=AAAA"
    msg.set_content(
        f"Bonjour,\n\nVoici le dossier de {CANARY_PERSON} ({CANARY_EMAIL}).\n\n"
        f"> Le lun. 5 janv. 2026, {CANARY_PERSON} <{CANARY_EMAIL}> a écrit :\n"
        "> Merci.\n\n-- \n"
        f"{CANARY_PERSON}\n",
        cte="quoted-printable",
    )
    html = (
        f"<html><body><p>Bonjour,</p><p>Voici le dossier de <b>{CANARY_PERSON}</b> "
        f'(<a href="mailto:{CANARY_EMAIL}">{CANARY_EMAIL}</a>).</p>'
        + ('<p><img src="cid:image001@exemple.test" alt="Signature"></p>' if with_image else "")
        + "</body></html>"
    )
    msg.add_alternative(html, subtype="html", cte="base64")
    if with_image:
        html_part = msg.get_payload()[1]
        html_part.add_related(
            png_bytes(),
            maintype="image",
            subtype="png",
            cid="<image001@exemple.test>",
            filename=f"Signature {CANARY_PERSON}.png",
        )
    msg.add_attachment(
        f"Note concernant {CANARY_PERSON}\r\nContact : {CANARY_EMAIL}\r\n".encode(),
        maintype="text",
        subtype="plain",
        filename=f"Note {CANARY_PERSON}.txt",
    )
    msg.add_attachment(inner_message())
    return msg


def write_mails() -> None:
    (OUT / "courriel.eml").write_bytes(mail_message().as_bytes(policy=SMTP))
    first = mail_message(with_image=False).as_bytes(policy=SMTP.clone(linesep="\n"))
    second = inner_message().as_bytes(policy=SMTP.clone(linesep="\n"))
    mbox = (
        f"From {CANARY_EMAIL} Mon Jan  5 10:00:00 2026\n".encode()
        + first.rstrip(b"\n")
        + b"\n\n"
        + f"From {CANARY_EMAIL} Mon Jan  5 09:00:00 2026\n".encode()
        + second.rstrip(b"\n")
        + b"\n\n"
    )
    (OUT / "boite.mbox").write_bytes(mbox)
    (OUT / "message.msg").write_bytes(msg_bytes())


# ---------------------------------------------------------------------------
# Outlook .msg (compound file written with extract-msg's OleWriter)
# ---------------------------------------------------------------------------

PT_LONG = 0x0003
PT_SYSTIME = 0x0040
PT_UNICODE = 0x001F
PT_BINARY = 0x0102


def _props_stream(
    header: bytes, fixed: dict[int, tuple[int, int]], variable: dict[str, bytes]
) -> bytes:
    """``__properties_version1.0``: header then one 16-byte entry per property."""
    out = bytearray(header)
    for tag, (ptype, value) in sorted(fixed.items()):
        out += struct.pack("<IIQ", (tag << 16) | ptype, 6, value)
    for name, data in sorted(variable.items()):
        tag, ptype = int(name[:4], 16), int(name[4:], 16)
        size = len(data) + (2 if ptype == PT_UNICODE else 0)
        out += struct.pack("<IIII", (tag << 16) | ptype, 6, size, 0)
    return bytes(out)


def _add_props(
    writer: Any,
    prefix: str,
    header: bytes,
    fixed: dict[int, tuple[int, int]],
    strings: dict[int, str],
    binaries: dict[int, bytes],
) -> None:
    variable: dict[str, bytes] = {}
    for tag, text in strings.items():
        variable[f"{tag:04X}{PT_UNICODE:04X}"] = text.encode("utf-16-le")
    for tag, data in binaries.items():
        variable[f"{tag:04X}{PT_BINARY:04X}"] = data
    for name, data in variable.items():
        writer.addEntry(f"{prefix}__substg1.0_{name}", data)
    writer.addEntry(f"{prefix}__properties_version1.0", _props_stream(header, fixed, variable))


def msg_bytes() -> bytes:
    from extract_msg.ole_writer import OleWriter

    writer = OleWriter()
    headers = (
        "Received: from poste-12.exemple.test ([192.0.2.10]) by mx.exemple.test; "
        "Mon, 5 Jan 2026 10:00:00 +0100\r\n"
        f"From: {CANARY_PERSON} <{CANARY_EMAIL}>\r\n"
        f"To: {COLLEAGUE} <{COLLEAGUE_EMAIL}>\r\n"
        f"Subject: Dossier {CANARY_PERSON}\r\n"
        f"X-Originating-Email: {CANARY_EMAIL}\r\n"
        "Message-ID: <msg-0001@exemple.test>\r\n"
    )
    html = (
        f"<html><body><p>Bonjour, dossier de <b>{CANARY_PERSON}</b> "
        f'<a href="mailto:{CANARY_EMAIL}">{CANARY_EMAIL}</a></p></body></html>'
    )
    _add_props(
        writer,
        "",
        b"\0" * 8 + struct.pack("<IIII", 1, 1, 1, 1) + b"\0" * 8,
        {0x0039: (PT_SYSTIME, 134117136000000000)},
        {
            0x001A: "IPM.Note",
            0x0037: f"Dossier {CANARY_PERSON}",
            0x0C1A: CANARY_PERSON,
            0x5D01: CANARY_EMAIL,
            0x0C1F: CANARY_EMAIL,
            0x0E04: COLLEAGUE,
            0x1000: f"Bonjour,\r\nDossier de {CANARY_PERSON} ({CANARY_EMAIL}).\r\n",
            0x007D: headers,
            0x0070: f"Dossier {CANARY_PERSON}",
        },
        {0x1013: html.encode("utf-8")},
    )
    _add_props(
        writer,
        "__recip_version1.0_#00000000/",
        b"\0" * 8,
        {0x0C15: (PT_LONG, 1), 0x3000: (PT_LONG, 0)},
        {0x3001: COLLEAGUE, 0x39FE: COLLEAGUE_EMAIL, 0x3003: COLLEAGUE_EMAIL, 0x3002: "SMTP"},
        {},
    )
    _add_props(
        writer,
        "__attach_version1.0_#00000000/",
        b"\0" * 8,
        {0x3705: (PT_LONG, 1), 0x0E21: (PT_LONG, 0)},
        {0x3707: f"CV {CANARY_PERSON}.txt", 0x3704: "CV.txt", 0x370E: "text/plain"},
        {0x3701: f"CV de {CANARY_PERSON}\r\n{CANARY_EMAIL}\r\n".encode()},
    )
    for name in ("00020102", "00030102", "00040102"):
        writer.addEntry(f"__nameid_version1.0/__substg1.0_{name}", b"")
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Archives
# ---------------------------------------------------------------------------


def write_archives() -> None:
    note = f"Dossier de {CANARY_PERSON}\nContact : {CANARY_EMAIL}\n".encode()
    with zipfile.ZipFile(OUT / "archive.zip", "w", zipfile.ZIP_DEFLATED) as zf:
        zf.comment = f"Archive de {CANARY_PERSON}".encode()
        info = zipfile.ZipInfo(f"{CANARY_PERSON}/note.txt", (2026, 1, 5, 10, 0, 0))
        info.comment = CANARY_EMAIL.encode()
        info.compress_type = zipfile.ZIP_DEFLATED
        zf.writestr(info, note)
        zf.writestr(f"{CANARY_PERSON}/vide/", b"")
        zf.writestr("courriel.eml", inner_message().as_bytes(policy=SMTP))
        zf.writestr("page.html", html_page().encode())
        inner = io.BytesIO()
        with zipfile.ZipFile(inner, "w") as nested:
            nested.writestr(f"CV {CANARY_PERSON}.txt", note)
        zf.writestr("imbrique.zip", inner.getvalue())

    import py7zr

    with py7zr.SevenZipFile(OUT / "archive.7z", "w") as sz:
        sz.writestr(note, f"{CANARY_PERSON}/note.txt")
        sz.writestr(html_page().encode(), "page.html")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "page.html").write_text(html_page(), encoding="utf-8")
    write_mails()
    write_archives()
    write_images()


if __name__ == "__main__":
    main()
