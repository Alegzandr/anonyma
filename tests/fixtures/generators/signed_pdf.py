"""Regression fixtures for the e-signed PDF incident (synthetic data only).

``uv run python -m tests.fixtures.generators.signed_pdf [DOSSIER]``

A fictitious signer (:data:`SIGNER`) appears in every place where a real e-signed
contract leaked it:

* a form page (text layer, form field) and a signature-certificate page holding
  signature-stamp **images** with an alpha channel: the name in mixed case
  (``zéphyrin QUINTAVALLE-BREZAC``) and a **vertical** (90°) timestamp;
* a 40-hex-digit reference drawn in an image and encoded in a **QR code**;
* a transaction id in **invisible** text (render mode 3);
* an embedded **digital signature** (incremental update, self-signed certificate
  whose CN is the signer, ``/Name``, ``/M``, PKCS#7 detached ``/Contents``);
* nominative metadata (Info + XMP author/title, title = the file name);
* a whitelisted publisher (:data:`PUBLISHER`): name in the text, its generic public
  URL and a logo image showing its name — all three must be kept;
* a file name containing the signer's name.

Variants: the same content as a docx (stamp, reference, QR and logo images, hidden
run), an eml carrying the PDF as an attachment whose name holds the signer's name,
and a zip containing the three.
"""

from __future__ import annotations

import datetime as dt
import io
import re
import sys
import zipfile
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import formataddr
from pathlib import Path

from PIL import Image, ImageDraw

from tests.fixtures.generators.mail_archive_image import font
from tests.fixtures.generators.office import (
    NS_A,
    NS_R,
    NS_W,
    PKG_REL,
    REL,
    WML,
    XML_DECL,
    w_para,
)

GIVEN = "Zéphyrin"
SURNAME = "Quintavalle-Brezac"
SIGNER = f"{GIVEN} {SURNAME}"
#: How the stamp writes the name: given name lower case, surname upper case.
STAMP_NAME = f"{GIVEN.lower()} {SURNAME.upper()}"
SIGNER_EMAIL = "zephyrin.quintavalle-brezac@exemple.test"
#: Every form of the signer that must disappear (surname parts alone included).
SIGNER_FORMS = (SIGNER, STAMP_NAME, SURNAME, "Quintavalle", "Brezac", SIGNER_EMAIL)
TIMESTAMP = "2026-03-14 10:42:07 UTC"
PDF_DATE = "D:20260314104207Z"
HEX_REF = "9f3a7c21e8b04d56a1f2c3e4d5b6a7980c1d2e3f"
TRANSACTION_ID = "TRX-a8F3k2-xyz-9Qm4Lp"
PUBLISHER = "Paraphéo"
PUBLISHER_URL = "https://www.parapheo.example/verification"
#: Settings that keep the publisher (``Config`` fields).
PUBLISHER_WHITELIST = [PUBLISHER, "parapheo.example", PUBLISHER_URL]

PDF_NAME = "Contrat_zephyrin_QUINTAVALLE-BREZAC_signe.pdf"
DOCX_NAME = "QUINTAVALLE-BREZAC-Zephyrin.docx"
EML_NAME = "Envoi du contrat signé.eml"
ATTACHMENT_NAME = "Contrat_Quintavalle-Brezac_Zephyrin.pdf"
ZIP_NAME = "Dossier QUINTAVALLE-BREZAC.zip"

INK = (20, 40, 140, 255)


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------


def _png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def stamp_image() -> bytes:
    """Transparent signature stamp: name in mixed case + vertical timestamp."""
    stamp = Image.new("RGBA", (1400, 560), (0, 0, 0, 0))
    draw = ImageDraw.Draw(stamp)
    draw.rounded_rectangle((150, 20, 1380, 540), radius=30, outline=INK, width=6)
    draw.text((200, 70), "Signé électroniquement par", fill=INK, font=font(48))
    draw.text((200, 180), STAMP_NAME, fill=INK, font=font(64))
    draw.text((200, 330), "Signature avancée", fill=INK, font=font(44))
    # The timestamp is written bottom-to-top along the left edge.
    band = Image.new("RGBA", (540, 90), (0, 0, 0, 0))
    ImageDraw.Draw(band).text((10, 15), TIMESTAMP, fill=INK, font=font(40))
    stamp.alpha_composite(band.rotate(90, expand=True), (20, 10))
    return _png(stamp)


#: Where the certificate page (page 2, 595 x 842 pt) shows each image.
STAMP_RECT = (72.0, 140.0, 422.0, 280.0)  # stamp image, 1400 x 560 px
REFERENCE_RECT = (72.0, 300.0, 447.0, 330.0)  # reference image, 1500 x 120 px
QR_RECT = (72.0, 350.0, 202.0, 480.0)
PAGE_SIZE = (595.0, 842.0)


def cert_page_regions() -> dict[str, tuple[float, float, float, float]]:
    """Fractional page boxes of the values drawn in the certificate page images:
    ``name`` (stamp), ``timestamp`` (vertical, stamp), ``reference``, ``qr``."""
    draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))

    def to_page(
        rect: tuple[float, ...], size: tuple[int, int], box: tuple[float, ...]
    ) -> tuple[float, float, float, float]:
        sx, sy = (rect[2] - rect[0]) / size[0], (rect[3] - rect[1]) / size[1]
        w, h = PAGE_SIZE
        return (
            (rect[0] + box[0] * sx - 2) / w,
            (rect[1] + box[1] * sy - 2) / h,
            (rect[0] + box[2] * sx + 2) / w,
            (rect[1] + box[3] * sy + 2) / h,
        )

    name = draw.textbbox((200, 180), STAMP_NAME, font=font(64))
    left, top, right, bottom = draw.textbbox((10, 15), TIMESTAMP, font=font(40))
    # Band (540 x 90) rotated by 90° (counter-clockwise), pasted at (20, 10).
    stamp_time = (20 + top, 10 + 540 - right, 20 + bottom, 10 + 540 - left)
    prefix = font(44).getlength("Réf. ")
    ref = draw.textbbox((20 + prefix, 30), HEX_REF, font=font(44))
    return {
        "name": to_page(STAMP_RECT, (1400, 560), name),
        "timestamp": to_page(STAMP_RECT, (1400, 560), stamp_time),
        "reference": to_page(REFERENCE_RECT, (1500, 120), ref),
        "qr": (
            QR_RECT[0] / PAGE_SIZE[0],
            QR_RECT[1] / PAGE_SIZE[1],
            QR_RECT[2] / PAGE_SIZE[0],
            QR_RECT[3] / PAGE_SIZE[1],
        ),
    }


def reference_image() -> bytes:
    image = Image.new("RGB", (1500, 120), "white")
    ImageDraw.Draw(image).text((20, 30), f"Réf. {HEX_REF}", fill="black", font=font(44))
    return _png(image)


def qr_image() -> bytes:
    import qrcode

    qr = qrcode.QRCode(border=4, box_size=10)
    qr.add_data(HEX_REF)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white").get_image()
    return _png(image.convert("RGB"))


def logo_image() -> bytes:
    image = Image.new("RGBA", (700, 180), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((10, 20, 150, 160), fill=(230, 120, 20, 255))
    draw.text((180, 45), PUBLISHER, fill=(30, 30, 30, 255), font=font(80))
    return _png(image)


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------


def _pdf_text(value: str) -> str:
    """PDF text string (UTF-16BE hex) for an arbitrary value."""
    return "<FEFF" + value.encode("utf-16-be").hex().upper() + ">"


def self_signed_certificate() -> tuple[object, object]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, SIGNER),
            x509.NameAttribute(NameOID.EMAIL_ADDRESS, SIGNER_EMAIL),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, PUBLISHER),
        ]
    )
    start = dt.datetime(2026, 3, 1, tzinfo=dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(0x5A17)
        .not_valid_before(start)
        .not_valid_after(start + dt.timedelta(days=365))
        .sign(key, hashes.SHA256())
    )
    return cert, key


def _base_pdf() -> tuple[bytes, dict[str, str]]:
    """The two-page document before signing, plus the trailer keys to carry over."""
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_image(pymupdf.Rect(400, 30, 540, 66), stream=logo_image())
    page.insert_text((72, 90), "Formulaire d'adhésion", fontsize=16)
    page.insert_text((72, 130), f"Nom du signataire : {GIVEN} {SURNAME.upper()}", fontsize=11)
    page.insert_text((72, 150), f"Courriel : {SIGNER_EMAIL}", fontsize=11)
    page.insert_text((72, 190), f"Document émis via {PUBLISHER} : {PUBLISHER_URL}", fontsize=10)
    # Invisible text layer (render mode 3), as e-signature platforms add.
    page.insert_text((72, 230), f"Transaction {TRANSACTION_ID}", fontsize=8, render_mode=3)
    widget = pymupdf.Widget()
    widget.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT
    widget.field_name = "signataire"
    widget.field_value = SIGNER
    widget.rect = pymupdf.Rect(72, 260, 300, 280)
    page.add_widget(widget)

    cert_page = doc.new_page()
    cert_page.insert_text((72, 80), "Certificat de signature", fontsize=16)
    cert_page.insert_text((72, 110), f"Plateforme : {PUBLISHER} ({PUBLISHER_URL})", fontsize=10)
    cert_page.insert_image(pymupdf.Rect(*STAMP_RECT), stream=stamp_image())
    cert_page.insert_image(pymupdf.Rect(*REFERENCE_RECT), stream=reference_image())
    cert_page.insert_image(pymupdf.Rect(*QR_RECT), stream=qr_image())

    doc.set_metadata(
        {
            "title": PDF_NAME,
            "author": SIGNER,
            "subject": f"Contrat signé par {SIGNER}",
            "keywords": f"{HEX_REF} {TRANSACTION_ID}",
            "creator": PUBLISHER,
            "producer": PUBLISHER,
            "creationDate": PDF_DATE,
            "modDate": PDF_DATE,
        }
    )
    doc.set_xml_metadata(
        '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/'
        '22-rdf-syntax-ns#"><rdf:Description xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:xmp="http://ns.adobe.com/xap/1.0/">'
        f"<dc:creator><rdf:Seq><rdf:li>{SIGNER}</rdf:li></rdf:Seq></dc:creator>"
        f'<dc:title><rdf:Alt><rdf:li xml:lang="x-default">{PDF_NAME}</rdf:li></rdf:Alt>'
        f"</dc:title><xmp:CreatorTool>{PUBLISHER}</xmp:CreatorTool>"
        f"<xmp:CreateDate>2026-03-14T10:42:07Z</xmp:CreateDate>"
        "</rdf:Description></rdf:RDF></x:xmpmeta>"
    )
    data = bytes(doc.tobytes(garbage=3, deflate=True))
    doc.close()

    reopened = pymupdf.open(stream=data, filetype="pdf")
    try:
        catalog = reopened.pdf_catalog()
        info = {
            "catalog_xref": str(catalog),
            "catalog": reopened.xref_object(catalog, compressed=True),
            "page_xref": str(reopened[1].xref),
            "page": reopened.xref_object(reopened[1].xref, compressed=True),
            "size": str(reopened.xref_length()),
            "info": reopened.xref_get_key(-1, "Info")[1],
            "id": reopened.xref_get_key(-1, "ID")[1],
            "acroform": reopened.xref_get_key(catalog, "AcroForm")[1],
        }
    finally:
        reopened.close()
    return data, info


def _add_key(dictionary: str, key: str, value: str) -> str:
    body = dictionary.strip()
    assert body.startswith("<<") and body.endswith(">>")
    return f"{body[:-2]}/{key} {value}>>"


def signed_pdf() -> bytes:
    """The fixture PDF: base document + incremental update holding the signature."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.serialization import Encoding, pkcs7

    base, info = _base_pdf()
    prev = int(re.findall(rb"startxref\s+(\d+)", base)[-1])
    size = int(info["size"])
    sig_x, field_x, form_x = size, size + 1, size + 2
    placeholder = "0" * 8192
    byte_range = "/ByteRange [0 0000000000 0000000000 0000000000]"
    sig = (
        "<</Type/Sig/Filter/Adobe.PPKLite/SubFilter/adbe.pkcs7.detached"
        f"/Name {_pdf_text(SIGNER)}/M ({PDF_DATE})/Reason {_pdf_text('Signature du contrat')}"
        f"/ContactInfo ({SIGNER_EMAIL}){byte_range}/Contents <{placeholder}>>>"
    )
    field = (
        f"<</Type/Annot/Subtype/Widget/FT/Sig/T (Signature1)/V {sig_x} 0 R/F 132"
        f"/Rect [0 0 0 0]/P {info['page_xref']} 0 R>>"
    )
    fields = re.search(r"/Fields\s*\[([^\]]*)\]", info["acroform"])
    existing = fields.group(1) if fields else ""
    form = f"<</Fields [{existing} {field_x} 0 R]/SigFlags 3>>"
    page = info["page"]
    page = (
        re.sub(r"/Annots\s*\[", f"/Annots [{field_x} 0 R ", page)
        if "/Annots" in page
        else _add_key(page, "Annots", f"[{field_x} 0 R]")
    )
    catalog = re.sub(r"/AcroForm\s*(\d+\s+0\s+R|<<.*?>>)", "", info["catalog"], flags=re.S)
    catalog = _add_key(catalog, "AcroForm", f"{form_x} 0 R")
    objects = {
        int(info["page_xref"]): page,
        int(info["catalog_xref"]): catalog,
        sig_x: sig,
        field_x: field,
        form_x: form,
    }
    out = bytearray(base if base.endswith(b"\n") else base + b"\n")
    offsets: dict[int, int] = {}
    for xref, body in sorted(objects.items()):
        offsets[xref] = len(out)
        out += f"{xref} 0 obj\n{body}\nendobj\n".encode("latin-1")
    xref_pos = len(out)
    out += b"xref\n"
    for xref in sorted(offsets):
        out += f"{xref} 1\n{offsets[xref]:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<</Size {form_x + 1}/Root {info['catalog_xref']} 0 R/Info {info['info']}"
        f"/ID {info['id']}/Prev {prev}>>\nstartxref\n{xref_pos}\n%%EOF\n"
    ).encode("latin-1")

    data = bytes(out)
    start = data.index(b"/Contents <" + placeholder.encode()) + len(b"/Contents ")
    end = start + len(placeholder) + 2
    ranges = f"/ByteRange [0 {start:010d} {end:010d} {len(data) - end:010d}]".encode()
    data = data.replace(byte_range.encode(), ranges, 1)
    cert, key = self_signed_certificate()
    signature = (
        pkcs7.PKCS7SignatureBuilder()
        .set_data(data[:start] + data[end:])
        .add_signer(cert, key, hashes.SHA256())  # type: ignore[arg-type]
        .sign(Encoding.DER, [pkcs7.PKCS7Options.DetachedSignature])
    )
    hex_sig = signature.hex().upper().ljust(len(placeholder), "0")
    assert len(hex_sig) == len(placeholder), "signature too large for its placeholder"
    return data[: start + 1] + hex_sig.encode() + data[end - 1 :]


def original_pdf_id(data: bytes) -> list[str]:
    """Hex strings of the trailer /ID of ``data`` (to check they do not survive)."""
    import pymupdf

    doc = pymupdf.open(stream=data, filetype="pdf")
    try:
        raw = doc.xref_get_key(-1, "ID")[1]
    finally:
        doc.close()
    return re.findall(r"<([0-9A-Fa-f]+)>", raw)


# ---------------------------------------------------------------------------
# docx
# ---------------------------------------------------------------------------


def _drawing(rid: str, number: int, cx: int, cy: int, descr: str = "") -> str:
    return (
        '<w:p><w:r><w:drawing><wp:inline xmlns:wp="http://schemas.openxmlformats.org/'
        'drawingml/2006/wordprocessingDrawing">'
        f'<wp:extent cx="{cx}" cy="{cy}"/><wp:docPr id="{number}" name="Image{number}" '
        f'descr="{descr}"/>'
        f'<a:graphic xmlns:a="{NS_A}"><a:graphicData uri="http://schemas.openxmlformats.org/'
        'drawingml/2006/picture"><pic:pic xmlns:pic="http://schemas.openxmlformats.org/'
        f'drawingml/2006/picture"><pic:nvPicPr><pic:cNvPr id="{number}" name="Image{number}"/>'
        f'<pic:cNvPicPr/></pic:nvPicPr><pic:blipFill><a:blip r:embed="{rid}"/></pic:blipFill>'
        f'<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr></pic:pic></a:graphicData>'
        "</a:graphic></wp:inline></w:drawing></w:r></w:p>"
    )


def signed_docx() -> bytes:
    images = {
        "logo.png": logo_image(),
        "stamp.png": stamp_image(),
        "reference.png": reference_image(),
        "qr.png": qr_image(),
    }
    emu = 9525  # EMU per pixel at 96 dpi
    sizes = {"logo.png": (233, 60), "stamp.png": (467, 187), "reference.png": (500, 40)}
    sizes["qr.png"] = (160, 160)
    hidden = (
        "<w:p><w:r><w:rPr><w:vanish/></w:rPr>"
        f'<w:t xml:space="preserve">Transaction {TRANSACTION_ID}</w:t></w:r></w:p>'
    )
    body = (
        _drawing("rIdImg1", 1, sizes["logo.png"][0] * emu, sizes["logo.png"][1] * emu)
        + w_para("Formulaire d'adhésion")
        + w_para(f"Nom du signataire : {GIVEN} {SURNAME.upper()}")
        + w_para(f"Courriel : {SIGNER_EMAIL}")
        + w_para(f"Document émis via {PUBLISHER} : {PUBLISHER_URL}")
        + hidden
        + w_para("Certificat de signature")
        + _drawing("rIdImg2", 2, sizes["stamp.png"][0] * emu, sizes["stamp.png"][1] * emu)
        + _drawing("rIdImg3", 3, sizes["reference.png"][0] * emu, sizes["reference.png"][1] * emu)
        + _drawing("rIdImg4", 4, sizes["qr.png"][0] * emu, sizes["qr.png"][1] * emu)
        + "<w:sectPr/>"
    )
    rels = "".join(
        f'<Relationship Id="rIdImg{i}" Type="{REL}/image" Target="media/{name}"/>'
        for i, name in enumerate(images, start=1)
    )
    core = (
        XML_DECL
        + '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/'
        'core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:dcterms="http://purl.org/dc/terms/" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        f"<dc:title>{DOCX_NAME}</dc:title><dc:creator>{SIGNER}</dc:creator>"
        f"<cp:lastModifiedBy>{STAMP_NAME}</cp:lastModifiedBy>"
        f"<cp:keywords>{HEX_REF}</cp:keywords>"
        '<dcterms:created xsi:type="dcterms:W3CDTF">2026-03-14T10:42:07Z</dcterms:created>'
        "</cp:coreProperties>"
    )
    entries: dict[str, bytes | str] = {
        "[Content_Types].xml": XML_DECL
        + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.'
        'relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
        '<Default Extension="png" ContentType="image/png"/>'
        f'<Override PartName="/word/document.xml" ContentType="{WML}.document.main+xml"/>'
        '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-'
        'package.core-properties+xml"/></Types>',
        "_rels/.rels": XML_DECL + f'<Relationships xmlns="{PKG_REL}">'
        f'<Relationship Id="rId1" Type="{REL}/officeDocument" Target="word/document.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/'
        'relationships/metadata/core-properties" Target="docProps/core.xml"/></Relationships>',
        "word/document.xml": XML_DECL
        + f'<w:document xmlns:w="{NS_W}" xmlns:r="{NS_R}"><w:body>{body}</w:body></w:document>',
        "word/_rels/document.xml.rels": XML_DECL
        + f'<Relationships xmlns="{PKG_REL}">{rels}</Relationships>',
        "docProps/core.xml": core,
    }
    entries.update({f"word/media/{name}": data for name, data in images.items()})
    return _zip(entries)


# ---------------------------------------------------------------------------
# eml / zip
# ---------------------------------------------------------------------------


def signed_eml(pdf: bytes) -> bytes:
    msg = EmailMessage()
    msg["From"] = formataddr((PUBLISHER, "notification@parapheo.example"))
    msg["To"] = formataddr((SIGNER, SIGNER_EMAIL))
    msg["Subject"] = f"Contrat signé par {SIGNER}"
    msg["Date"] = "Sat, 14 Mar 2026 10:42:07 +0000"
    msg["Message-ID"] = f"<{TRANSACTION_ID}@parapheo.example>"
    msg.set_content(
        f"Bonjour {GIVEN},\n\nVotre contrat a été signé le {TIMESTAMP} "
        f"(référence {HEX_REF}).\nVérification : {PUBLISHER_URL}\n\n{PUBLISHER}\n"
    )
    msg.add_attachment(pdf, maintype="application", subtype="pdf", filename=ATTACHMENT_NAME)
    return msg.as_bytes(policy=SMTP)


def _zip(entries: dict[str, bytes | str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            info = zipfile.ZipInfo(name, date_time=(2026, 3, 14, 10, 42, 6))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, content.encode() if isinstance(content, str) else content)
    return buffer.getvalue()


def write_fixtures(folder: Path) -> dict[str, Path]:
    """Write the four variants into ``folder``; returns ``{kind: path}``."""
    folder.mkdir(parents=True, exist_ok=True)
    pdf = signed_pdf()
    docx = signed_docx()
    eml = signed_eml(pdf)
    archive = _zip({f"signes/{PDF_NAME}": pdf, DOCX_NAME: docx, EML_NAME: eml})
    paths = {
        "pdf": folder / PDF_NAME,
        "docx": folder / DOCX_NAME,
        "eml": folder / EML_NAME,
        "zip": folder / ZIP_NAME,
    }
    for kind, data in (("pdf", pdf), ("docx", docx), ("eml", eml), ("zip", archive)):
        paths[kind].write_bytes(data)
    return paths


def main() -> None:
    folder = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("signed-pdf-fixtures")
    for path in write_fixtures(folder).values():
        print(path)


if __name__ == "__main__":
    main()
