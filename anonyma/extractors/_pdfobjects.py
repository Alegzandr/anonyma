"""PDF object helpers shared by the PDF extractor and writer (PyMuPDF).

* opening with every optional content group (layer) visible, so that hidden
  layers are analysed, rendered and anonymized like the rest;
* image XObjects: decoded pixels with their soft mask, in-place replacement that
  keeps transparency, lossless JPEG metadata removal;
* digital signatures: readable entries and certificate fields.
"""

from __future__ import annotations

import io
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pymupdf

from anonyma.extractors._pdf import _decode_literal, iter_strings
from anonyma.models import BBox

if TYPE_CHECKING:
    from PIL import Image

# ---------------------------------------------------------------------------
# Opening: optional content (layers) made visible
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LayerState:
    """Original default configuration of the optional content of a document.

    Hidden layers are switched on while a document is analysed and written; the
    writer restores the original configuration before saving.
    """

    #: ``(xref, key path)`` of the default configuration dictionary ``/D``
    #: (empty path: ``/D`` is an indirect object of its own).
    location: tuple[int, str]
    source: str  # original dictionary source
    hidden: frozenset[str]  # names of the layers hidden by default


def _oc_config_location(doc: pymupdf.Document) -> tuple[int, str] | None:
    catalog = doc.pdf_catalog()
    kind, value = doc.xref_get_key(catalog, "OCProperties")
    if kind == "null":
        return None
    holder, key = catalog, "OCProperties/D"
    if kind == "xref":
        holder, key = int(value.split()[0]), "D"
    kind, value = doc.xref_get_key(holder, key)
    if kind == "xref":
        return int(value.split()[0]), ""
    if kind == "dict":
        return holder, key
    return None


def _key(location: tuple[int, str], key: str) -> tuple[int, str]:
    xref, path = location
    return xref, f"{path}/{key}" if path else key


def open_document(source: Path) -> tuple[pymupdf.Document, LayerState | None]:
    """Open ``source`` (empty user password accepted) with every layer visible.

    Raises ``PermissionError`` for password-protected files. The extractor and the
    writer must both open files through this function: segment locators (block
    indices, xrefs) depend on it.
    """
    doc = pymupdf.open(source, filetype="pdf")
    if doc.needs_pass and not doc.authenticate(""):
        doc.close()
        raise PermissionError("PDF chiffré")
    try:
        location = _oc_config_location(doc)
        if location is None:
            return doc, None
        ocgs = doc.get_ocgs()
        hidden = frozenset(str(v.get("name", "")) for v in ocgs.values() if not v.get("on", True))
        base_kind, base = doc.xref_get_key(*_key(location, "BaseState"))
        if not ocgs or (not hidden and base.strip("/") != "OFF"):
            return doc, None
        xref, path = location
        original = doc.xref_get_key(xref, path)[1] if path else doc.xref_object(xref)
        refs = " ".join(f"{x} 0 R" for x in ocgs)
        doc.xref_set_key(*_key(location, "ON"), f"[{refs}]")
        doc.xref_set_key(*_key(location, "OFF"), "[]")
        doc.xref_set_key(*_key(location, "AS"), "null")
        if base_kind != "null":
            doc.xref_set_key(*_key(location, "BaseState"), "/ON")
        data = doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_NONE)
    except Exception:  # broken optional content: analysed as rendered
        return doc, None
    doc.close()
    return pymupdf.open(stream=data, filetype="pdf"), LayerState(location, original, hidden)


def restore_layers(doc: pymupdf.Document, state: LayerState | None) -> None:
    """Put back the original visibility of the layers (before saving)."""
    if state is None:
        return
    xref, path = state.location
    if path:
        doc.xref_set_key(xref, path, state.source)
    else:
        doc.update_object(xref, state.source)


def delete_keys(doc: pymupdf.Document, xref: int, keys: Iterable[str]) -> None:
    """Remove top-level ``keys`` from object ``xref``.

    ``xref_set_key(..., "null")`` leaves ``/Key null`` in the object (and in the
    saved file): the entries are dropped from the object source instead.
    """
    try:
        present = [k for k in keys if doc.xref_get_key(xref, k)[0] != "null"]
    except Exception:
        return
    if not present:
        return
    for key in present:
        doc.xref_set_key(xref, key, "null")
    source = doc.xref_object(xref, compressed=False)
    for key in present:
        source = re.sub(rf"/{re.escape(key)}\s+null(?=[\s/>\]]|$)", "", source)
    doc.update_object(xref, source)


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------


def page_rect_to_pixels(page: pymupdf.Page, rect: pymupdf.Rect, zoom: float) -> pymupdf.Rect:
    """Map an unrotated page rectangle to rendered-pixmap pixels."""
    return (pymupdf.Rect(rect) * page.rotation_matrix * pymupdf.Matrix(zoom, zoom)).normalize()


def image_box_to_page(box: BBox, size: tuple[int, int], matrix: pymupdf.Matrix) -> BBox:
    """Map an image-pixel rectangle to page coordinates through the placement
    ``matrix`` (unit square -> page, as returned by ``get_image_rects``)."""
    width, height = max(size[0], 1), max(size[1], 1)
    points = [
        pymupdf.Point(x / width, y / height) * matrix
        for x, y in ((box.x0, box.y0), (box.x1, box.y0), (box.x0, box.y1), (box.x1, box.y1))
    ]
    return BBox(
        min(p.x for p in points),
        min(p.y for p in points),
        max(p.x for p in points),
        max(p.y for p in points),
    )


def overlaps(a: BBox, b: BBox) -> bool:
    return a.x0 < b.x1 and b.x0 < a.x1 and a.y0 < b.y1 and b.y0 < a.y1


# ---------------------------------------------------------------------------
# Image XObjects
# ---------------------------------------------------------------------------

#: How an image XObject is stored back after its pixels were changed.
STENCIL = "stencil"  # /ImageMask: 1-bit mask painted with the fill colour
JPEG = "jpeg"  # DCT-encoded, no transparency
RASTER = "raster"  # anything else (stored deflated)


def image_kind(doc: pymupdf.Document, xref: int) -> str:
    if doc.xref_get_key(xref, "ImageMask")[1] == "true":
        return STENCIL
    filters = doc.xref_get_key(xref, "Filter")[1]
    masked = (
        doc.xref_get_key(xref, "SMask")[0] != "null" or doc.xref_get_key(xref, "Mask")[0] != "null"
    )
    return JPEG if "DCTDecode" in filters and not masked else RASTER


def image_pixels(doc: pymupdf.Document, xref: int) -> Image.Image:
    """Decoded pixels of an image XObject, soft mask applied.

    Stencil masks become black ink on white (mode ``L``); images with a soft mask
    ``RGBA``/``LA``; others ``RGB``/``L``. Raises on undecodable images.

    ``Document.extract_image`` returns the base image *without* its soft mask: a
    transparent stamp whose shape lives in the mask then reads as a uniform
    rectangle (often black), so it must never be used for OCR.
    """
    from PIL import Image

    pix = pymupdf.Pixmap(doc, xref)
    if pix.colorspace is None:  # stencil: alpha only, 255 = painted
        alpha = Image.frombytes("L", (pix.width, pix.height), pix.samples)
        return alpha.point(lambda v: 255 - v)
    if pix.alpha:
        pix = pymupdf.Pixmap(pix, 0)
    if pix.colorspace.n not in (1, 3):
        pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
    kind, value = doc.xref_get_key(xref, "SMask")
    if kind == "xref":
        try:
            mask = pymupdf.Pixmap(doc, int(value.split()[0]))
            if mask.n != 1 or mask.alpha:
                mask = pymupdf.Pixmap(pymupdf.csGRAY, mask)
            if (mask.width, mask.height) != (pix.width, pix.height):
                scaled = Image.frombytes("L", (mask.width, mask.height), mask.samples).resize(
                    (pix.width, pix.height)
                )
                mask = pymupdf.Pixmap(
                    pymupdf.csGRAY, pix.width, pix.height, scaled.tobytes(), False
                )
            pix = pymupdf.Pixmap(pix, mask)
        except Exception:  # unreadable soft mask: opaque image
            pass
    mode = "L" if pix.colorspace.n == 1 else "RGB"
    if pix.alpha:
        mode += "A"
    return Image.frombytes(mode, (pix.width, pix.height), pix.samples)


def store_image_pixels(doc: pymupdf.Document, xref: int, image: Image.Image, kind: str) -> None:
    """Replace the pixels of image XObject ``xref`` in place: every placement, on
    every page and inside forms, sees the change; transparency is kept as a new
    soft mask."""
    if kind == STENCIL:
        bits = image.convert("L").point(lambda v: 0 if v < 128 else 255).convert("1")
        doc.update_stream(xref, bits.tobytes(), compress=True)
        delete_keys(doc, xref, ("Decode", "DecodeParms", "SMask", "Mask"))
        doc.xref_set_key(xref, "Width", str(image.width))
        doc.xref_set_key(xref, "Height", str(image.height))
        doc.xref_set_key(xref, "BitsPerComponent", "1")
        return
    alpha = image.getchannel("A") if image.mode in ("RGBA", "LA") else None
    base = image.convert("L" if image.mode in ("L", "LA") else "RGB")
    if kind == JPEG and alpha is None:
        buffer = io.BytesIO()
        base.save(buffer, "JPEG", quality=92)
        doc.update_stream(xref, buffer.getvalue(), compress=False)
        doc.xref_set_key(xref, "Filter", "/DCTDecode")
    else:
        doc.update_stream(xref, base.tobytes(), compress=True)
    delete_keys(doc, xref, ("DecodeParms", "Decode", "Mask", "SMaskInData", "SMask"))
    doc.xref_set_key(xref, "Width", str(base.width))
    doc.xref_set_key(xref, "Height", str(base.height))
    doc.xref_set_key(xref, "BitsPerComponent", "8")
    doc.xref_set_key(xref, "ColorSpace", "/DeviceGray" if base.mode == "L" else "/DeviceRGB")
    if alpha is None or alpha.getextrema() == (255, 255):
        return
    mask_xref = doc.get_new_xref()
    doc.update_object(
        mask_xref,
        f"<< /Type /XObject /Subtype /Image /Width {alpha.width} /Height {alpha.height} "
        "/ColorSpace /DeviceGray /BitsPerComponent 8 >>",
    )
    doc.update_stream(mask_xref, alpha.tobytes(), compress=True)
    doc.xref_set_key(xref, "SMask", f"{mask_xref} 0 R")


def strip_jpeg_metadata(data: bytes) -> bytes:
    """Remove EXIF, XMP, IPTC, Photoshop resources and comments from a JPEG,
    losslessly (JFIF, ICC profile and Adobe colour transform are kept).

    Returns ``data`` unchanged when it is not a well-formed JPEG.
    """
    if not data.startswith(b"\xff\xd8"):
        return data
    out = bytearray(b"\xff\xd8")
    i, n = 2, len(data)
    while i + 4 <= n:
        if data[i] != 0xFF:
            return data
        marker = data[i + 1]
        if marker == 0xFF:  # fill byte
            i += 1
            continue
        if marker == 0xDA:  # start of scan: entropy-coded data follows
            out += data[i:]
            return bytes(out)
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            out += data[i : i + 2]
            i += 2
            continue
        length = int.from_bytes(data[i + 2 : i + 4], "big")
        segment = data[i : i + 2 + length]
        if length < 2 or len(segment) < 2 + length:
            return data
        icc = marker == 0xE2 and segment[4:16] == b"ICC_PROFILE\x00"
        dropped = (0xE1 <= marker <= 0xEF and marker != 0xEE and not icc) or marker == 0xFE
        if not dropped:
            out += segment
        i += 2 + length
    return data


# ---------------------------------------------------------------------------
# Digital signatures
# ---------------------------------------------------------------------------

#: Text entries of a signature dictionary.
SIGNATURE_KEYS = ("Name", "Reason", "Location", "ContactInfo", "M")


def is_signature_dict(doc: pymupdf.Document, xref: int) -> bool:
    try:
        kind, value = doc.xref_get_key(xref, "Type")
        if kind == "name" and value in ("/Sig", "/DocTimeStamp"):
            return True
        return bool(
            doc.xref_get_key(xref, "ByteRange")[0] == "array"
            and doc.xref_get_key(xref, "Contents")[0] == "string"
        )
    except Exception:
        return False


def _string_bytes(source: str, key: str) -> bytes | None:
    """Raw bytes of the string value of ``key`` in an object source."""
    for string in iter_strings(source):
        if string.key != key:
            continue
        raw = source[string.start + 1 : string.end - 1]
        if source[string.start] == "<":
            digits = "".join(c for c in raw if c in "0123456789abcdefABCDEF")
            return bytes.fromhex(digits + ("0" if len(digits) % 2 else ""))
        return _decode_literal(raw)
    return None


def certificate_texts(der: bytes) -> list[str]:
    """Readable fields of the certificates of a PKCS#7 / CMS blob, or of a single
    DER certificate: subject and issuer attributes, alternative names, serial."""
    from cryptography import x509
    from cryptography.hazmat.primitives.serialization import pkcs7

    der = der.rstrip(b"\x00")
    certificates: list[x509.Certificate]
    try:
        certificates = list(pkcs7.load_der_pkcs7_certificates(der))
    except Exception:
        try:
            certificates = [x509.load_der_x509_certificate(der)]
        except Exception:
            return []
    texts: list[str] = []

    def add(text: str) -> None:
        if text.strip() and text not in texts:
            texts.append(text)

    for cert in certificates:
        for name in (cert.subject, cert.issuer):
            for attribute in name:
                value = attribute.value
                add(value if isinstance(value, str) else bytes(value).hex())
        try:
            alt = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        except Exception:
            alt = None
        for general in alt or []:
            add(str(getattr(general, "value", "")))
        add(format(cert.serial_number, "X"))
    return texts


def signature_texts(doc: pymupdf.Document, xref: int) -> list[tuple[str, str]]:
    """``(label, text)`` of a signature dictionary: entries and certificates."""
    source = doc.xref_object(xref, compressed=False)
    texts: list[tuple[str, str]] = []
    for string in iter_strings(source):
        if string.key in SIGNATURE_KEYS and string.value.strip():
            texts.append((string.key, string.value))
    contents = _string_bytes(source, "Contents")
    if contents:
        texts.extend(("certificat", text) for text in certificate_texts(contents))
    return texts


def _array_refs(value: str) -> list[int]:
    tokens = value.replace("[", " ").replace("]", " ").split()
    return [
        int(tokens[i])
        for i in range(len(tokens) - 2)
        if tokens[i].isdigit() and tokens[i + 1].isdigit() and tokens[i + 2] == "R"
    ]


def dss_certificates(doc: pymupdf.Document) -> list[str]:
    """Readable fields of the certificates stored in the catalog ``/DSS``
    (long-term validation data)."""
    catalog = doc.pdf_catalog()
    kind, value = doc.xref_get_key(catalog, "DSS")
    if kind == "null":
        return []
    holder, key = (int(value.split()[0]), "Certs") if kind == "xref" else (catalog, "DSS/Certs")
    kind, value = doc.xref_get_key(holder, key)
    if kind == "xref":
        value = doc.xref_object(int(value.split()[0]))
    texts: list[str] = []
    for ref in _array_refs(value):
        try:
            data = doc.xref_stream(ref) or b""
        except Exception:
            continue
        texts.extend(t for t in certificate_texts(data) if t not in texts)
    return texts


def xfa_streams(doc: pymupdf.Document) -> list[int]:
    """Stream xrefs of the XFA form description (``/AcroForm /XFA``)."""
    catalog = doc.pdf_catalog()
    kind, value = doc.xref_get_key(catalog, "AcroForm/XFA")
    if kind == "null":
        acro = doc.xref_get_key(catalog, "AcroForm")
        if acro[0] != "xref":
            return []
        kind, value = doc.xref_get_key(int(acro[1].split()[0]), "XFA")
    if kind == "xref":
        ref = int(value.split()[0])
        if doc.xref_is_stream(ref):
            return [ref]
        value = doc.xref_object(ref)
    return [r for r in _array_refs(value) if doc.xref_is_stream(r)]
