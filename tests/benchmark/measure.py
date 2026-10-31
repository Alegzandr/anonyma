"""Measure Anonyma on the benchmark corpus: ``uv run python -m tests.benchmark.measure``.

Anonyma is used as a black box (its command line). Its outputs are re-read with
independent means only: ``zipfile`` + regular expressions for OOXML/ODF/zip,
``email`` (stdlib) for mail, ``olefile`` for msg, ``py7zr`` for 7z, PyMuPDF
``get_text`` for PDF text, Pillow for images, and raw byte searches in several
encodings for everything. A ground-truth value is *leaked* when one of its
renderings (``common.py``) survives anywhere in the output tree of its document,
file and folder names included, or when an image region where it was drawn is
still readable (same pixels as the original).

Precision: original and output texts are diffed token by token; every replaced
span that overlaps no ground-truth value is a false positive.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import contextlib
import difflib
import email
import email.policy
import html
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
import zlib
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import numpy as np
from PIL import Image

from tests.benchmark.common import (
    CORPUS,
    DIRECT_TYPES,
    INDIRECT_TYPES,
    RESULTS,
    TRUTH,
    norm_compact,
    norm_light,
    norm_raw,
    norm_text,
    norm_word,
    tokens,
    word_forms,
)

QUARANTINE = "_quarantaine"
PROFILES = {"leger": "LIGHT", "standard": "STANDARD", "complet": "FULL"}
MAX_DEPTH = 12

# Kinds of haystack items: ``clean`` = decoded document text (used for precision),
# ``meta`` = metadata text, ``raw`` = raw byte decodings / markup, ``name`` = names.
CLEAN, META, RAW, NAME = "clean", "meta", "raw", "name"


@dataclass
class Item:
    label: str
    text: str
    kind: str


@dataclass
class Hay:
    items: list[Item] = field(default_factory=list)
    gps: list[str] = field(default_factory=list)

    def add(self, label: str, text: str, kind: str) -> None:
        if text and text.strip():
            self.items.append(Item(label, text, kind))


# ---------------------------------------------------------------------------
# Independent readers
# ---------------------------------------------------------------------------

_BLOCK_TAGS = re.compile(
    r"</(?:w:p|a:p|text:p|text:h|si|c|p|div|td|tr|li|h\d|table:table-cell|row|title|"
    r"address|blockquote|comment|dc:\w+|cp:\w+|vt:\w+|Company|Manager|Application)>"
    r"|<(?:w:tab|w:br|text:line-break|text:tab|br|a:br)\b[^>]*/?>",
    re.IGNORECASE,
)
_TAG = re.compile(r"<[^>]*>", re.DOTALL)
_ZIP_MAGIC = b"PK\x03\x04"
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_B64_BLOB = re.compile(r"(?:[A-Za-z0-9+/]{4}\s*){12,}(?:[A-Za-z0-9+/]{2,3}={0,2})?")
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".webp", ".heic"}
TEXT_EXT = {".txt", ".csv", ".tsv", ".log", ".md"}
MARKUP_EXT = {".html", ".htm", ".xml", ".xhtml", ".vml", ".rels"}


def decode_text(data: bytes) -> str:
    for bom, enc in (
        (b"\xef\xbb\xbf", "utf-8"),
        (b"\xff\xfe", "utf-16-le"),
        (b"\xfe\xff", "utf-16-be"),
    ):
        if data.startswith(bom):
            return data[len(bom) :].decode(enc, errors="replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def markup_views(text: str) -> tuple[str, str]:
    """(paragraph-joined text, tag-separated text) of an XML/HTML string."""
    joined = html.unescape(_TAG.sub("", _BLOCK_TAGS.sub("\n", text)))
    spaced = html.unescape(_TAG.sub(" ", text))
    return joined, spaced


def rtf_text(text: str) -> str:
    def hexchar(m: re.Match[str]) -> str:
        return bytes([int(m.group(1), 16)]).decode("cp1252", errors="replace")

    def uchar(m: re.Match[str]) -> str:
        n = int(m.group(1))
        return chr(n + 65536 if n < 0 else n)

    out = re.sub(r"\\'([0-9a-fA-F]{2})", hexchar, text)
    out = re.sub(r"\\u(-?\d+)\??", uchar, out)
    out = re.sub(r"\\(?:par|line|cell|row|tab)\b ?", "\n", out)
    out = re.sub(r"\\[a-zA-Z]+-?\d* ?", "", out)
    return out.replace("{", "").replace("}", "")


def walk_bytes(hay: Hay, data: bytes, name: str, label: str, depth: int = 0) -> None:
    """Add every readable rendering of ``data`` (a file called ``name``) to ``hay``."""
    if depth > MAX_DEPTH:
        return
    ext = Path(name).suffix.lower()
    raw_decodes(hay, data, label)
    try:
        if data.startswith(_ZIP_MAGIC):
            walk_zip(hay, data, label, depth)
        elif data.startswith(b"7z\xbc\xaf\x27\x1c"):
            walk_7z(hay, data, label, depth)
        elif data.startswith(b"%PDF"):
            walk_pdf(hay, data, label, depth)
        elif data.startswith(_OLE_MAGIC):
            walk_msg(hay, data, label, depth)
        elif ext == ".mbox":
            walk_mbox(hay, data, label, depth)
        elif ext == ".eml" or (ext == "" and re.match(rb"^[A-Za-z-]+: ", data[:200])):
            walk_mail(
                hay, email.message_from_bytes(data, policy=email.policy.default), label, depth
            )
        elif ext in IMAGE_EXT or data[:4] in (b"\x89PNG", b"\xff\xd8\xff\xe0", b"\xff\xd8\xff\xe1"):
            walk_image(hay, data, label)
        elif ext == ".rtf" or data.startswith(b"{\\rtf"):
            hay.add(label, rtf_text(decode_text(data)), CLEAN)
        elif ext in MARKUP_EXT or data.lstrip()[:1] == b"<":
            text = decode_text(data)
            joined, spaced = markup_views(text)
            hay.add(label, joined, CLEAN)
            hay.add(label + " (balises)", spaced, RAW)
            hay.add(label + " (décodé)", unquote(html.unescape(text)), RAW)
        elif ext in {".json", ".geojson"}:
            walk_json(hay, decode_text(data), label)
        else:
            hay.add(label, decode_text(data), CLEAN)
    except Exception as exc:  # unreadable output: recorded, never hidden
        hay.add(label + " (illisible)", f"lecture impossible : {type(exc).__name__}", RAW)
    if ext not in IMAGE_EXT and not data.startswith((_ZIP_MAGIC, b"%PDF", _OLE_MAGIC)):
        scan_base64(hay, decode_text(data), label, depth)


def raw_decodes(hay: Hay, data: bytes, label: str) -> None:
    if len(data) > 30_000_000:
        return
    hay.add(label + " [octets utf-8]", data.decode("utf-8", errors="ignore"), RAW)
    hay.add(label + " [octets latin-1]", data.decode("latin-1"), RAW)
    for offset in (0, 1):
        chunk = data[offset:]
        chunk = chunk[: len(chunk) // 2 * 2]
        hay.add(label + " [octets utf-16le]", chunk.decode("utf-16-le", errors="ignore"), RAW)
    hay.add(
        label + " [octets utf-16be]",
        data[: len(data) // 2 * 2].decode("utf-16-be", errors="ignore"),
        RAW,
    )


def scan_base64(hay: Hay, text: str, label: str, depth: int) -> None:
    for m in _B64_BLOB.finditer(text):
        blob = re.sub(r"\s+", "", m.group(0))
        if len(blob) < 48:
            continue
        try:
            data = base64.b64decode(blob + "=" * (-len(blob) % 4), validate=False)
        except (binascii.Error, ValueError):
            continue
        if data.startswith((_ZIP_MAGIC, b"%PDF", _OLE_MAGIC, b"\x89PNG", b"\xff\xd8")):
            walk_bytes(hay, data, "blob", label + " (base64)", depth + 1)
        else:
            decoded = data.decode("utf-8", errors="ignore")
            if decoded and sum(c.isprintable() for c in decoded) > 0.9 * len(decoded):
                hay.add(label + " (base64)", decoded, RAW)


def walk_zip(hay: Hay, data: bytes, label: str, depth: int) -> None:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        if zf.comment:
            hay.add(label + " (commentaire zip)", decode_text(zf.comment), META)
        for info in zf.infolist():
            hay.add(f"{label}!{info.filename} [nom]", info.filename, NAME)
            if info.comment:
                hay.add(f"{label}!{info.filename} (commentaire)", decode_text(info.comment), META)
            if info.is_dir():
                continue
            member = zf.read(info)
            sub = f"{label}!{info.filename}"
            is_meta = (
                info.filename.startswith(("docProps/", "meta.xml")) or info.filename == "meta.xml"
            )
            if info.filename.endswith((".xml", ".rels", ".vml")) or member.lstrip()[:5] == b"<?xml":
                text = decode_text(member)
                joined, spaced = markup_views(text)
                hay.add(sub, joined, META if is_meta else CLEAN)
                hay.add(sub + " (balises)", spaced, RAW)
                hay.add(sub + " (décodé)", unquote(html.unescape(text)), RAW)
                raw_decodes(hay, member, sub)
            else:
                walk_bytes(hay, member, info.filename, sub, depth + 1)


def walk_7z(hay: Hay, data: bytes, label: str, depth: int) -> None:
    import py7zr

    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "a.7z"
        src.write_bytes(data)
        with py7zr.SevenZipFile(src) as archive:
            names = archive.getnames()
            archive.extractall(path=Path(tmp) / "x")
        for name in names:
            hay.add(f"{label}!{name} [nom]", name, NAME)
            path = Path(tmp) / "x" / name
            if path.is_file():
                walk_bytes(hay, path.read_bytes(), name, f"{label}!{name}", depth + 1)


def walk_pdf(hay: Hay, data: bytes, label: str, depth: int) -> None:
    import pymupdf

    pdf = pymupdf.open(stream=data, filetype="pdf")
    try:
        for key, value in (pdf.metadata or {}).items():
            if value and key not in {"format", "encryption"}:
                hay.add(f"{label} (métadonnée {key})", str(value), META)
        hay.add(label + " (XMP)", pdf.get_xml_metadata() or "", META)
        for _, title, *_ in pdf.get_toc():
            hay.add(label + " (signet)", title, META)
        for n, page in enumerate(pdf):
            hay.add(f"{label} (page {n + 1})", page.get_text(), CLEAN)
            for annot in page.annots() or []:
                for key, value in annot.info.items():
                    if isinstance(value, str) and value:
                        hay.add(f"{label} (page {n + 1}, annotation {key})", value, CLEAN)
            for widget in page.widgets() or []:
                for value in (widget.field_value, widget.field_label, widget.field_name):
                    if isinstance(value, str):
                        hay.add(f"{label} (page {n + 1}, formulaire)", value, CLEAN)
            for link in page.get_links():
                hay.add(f"{label} (page {n + 1}, lien)", unquote(str(link.get("uri", ""))), RAW)
        for name in pdf.embfile_names():
            info = pdf.embfile_info(name)
            hay.add(f"{label} (pièce jointe) [nom]", f"{name} {info.get('filename', '')}", NAME)
            hay.add(f"{label} (pièce jointe, description)", str(info.get("desc", "")), META)
            walk_bytes(
                hay,
                pdf.embfile_get(name),
                str(info.get("filename") or name),
                f"{label}!{name}",
                depth + 1,
            )
        for xref in range(1, pdf.xref_length()):
            try:
                obj = pdf.xref_object(xref, compressed=False)
            except Exception:
                continue
            hay.add(f"{label} (objet {xref})", obj, RAW)
            for hexstr in re.findall(r"<(FEFF[0-9A-Fa-f]+)>", obj):
                with contextlib.suppress(ValueError):
                    hay.add(
                        f"{label} (objet {xref}, utf-16)",
                        bytes.fromhex(hexstr).decode("utf-16-be"),
                        RAW,
                    )
            if pdf.xref_is_stream(xref) and "/Image" not in obj:
                try:
                    stream = pdf.xref_stream(xref)
                except Exception:
                    continue
                if stream and len(stream) < 5_000_000:
                    raw_decodes(hay, stream, f"{label} (flux {xref})")
    finally:
        pdf.close()


def header_texts(msg: email.message.Message) -> Iterator[tuple[str, str]]:
    for key, value in msg.items():
        try:
            yield key, str(value)
        except Exception:
            yield key, repr(value)


def walk_mail(hay: Hay, msg: email.message.Message, label: str, depth: int) -> None:
    for n, part in enumerate(msg.walk()):
        plabel = f"{label}#partie{n}"
        for key, value in header_texts(part):
            lower = key.lower()
            if lower.startswith("content-") or lower == "mime-version":
                kind = RAW
            elif lower in {"message-id", "in-reply-to", "references"}:
                kind = META
            else:
                kind = CLEAN
            hay.add(f"{plabel} (en-tête {key})", value, kind)
        if part.is_multipart():
            continue
        if part.get_content_type() == "message/rfc822":
            continue
        filename = part.get_filename() or ""
        if filename:
            hay.add(f"{plabel} [nom]", filename, NAME)
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            continue
        ctype = part.get_content_type()
        if ctype.startswith("text/"):
            charset = part.get_content_charset() or "utf-8"
            try:
                text = payload.decode(charset, errors="replace")
            except LookupError:
                text = payload.decode("utf-8", errors="replace")
            if ctype == "text/html":
                joined, spaced = markup_views(text)
                hay.add(plabel, joined, CLEAN)
                hay.add(plabel + " (balises)", spaced, RAW)
                hay.add(plabel + " (décodé)", unquote(html.unescape(text)), RAW)
            else:
                hay.add(plabel, text, CLEAN)
                ext = Path(filename).suffix.lower()
                if ext in {".csv", ".json", ".xml", ".html"}:
                    walk_bytes(hay, payload, filename, plabel + " (fichier)", depth + 1)
        else:
            default = {
                "image/png": "x.png",
                "image/jpeg": "x.jpg",
                "application/pdf": "x.pdf",
                "application/zip": "x.zip",
            }.get(ctype, "x.bin")
            walk_bytes(hay, payload, filename or default, plabel, depth + 1)


def walk_mbox(hay: Hay, data: bytes, label: str, depth: int) -> None:
    chunks = re.split(rb"(?m)^From [^\n]*\n", data)
    for line in re.findall(rb"(?m)^From [^\n]*", data):
        hay.add(label + " (séparateur)", decode_text(line), CLEAN)
    for n, chunk in enumerate(c for c in chunks if c.strip()):
        msg = email.message_from_bytes(chunk, policy=email.policy.default)
        walk_mail(hay, msg, f"{label}#message{n + 1}", depth + 1)


def walk_msg(hay: Hay, data: bytes, label: str, depth: int) -> None:
    import olefile

    ole = olefile.OleFileIO(io.BytesIO(data))
    try:
        streams = ole.listdir(streams=True, storages=False)
        names = {"/".join(s): s for s in streams}
        for path, parts in names.items():
            leaf = parts[-1]
            content = ole.openstream(parts).read()
            slabel = f"{label}!{path}"
            hay.add(slabel + " [nom]", path, NAME)
            tag = leaf.split("_")[-1] if leaf.startswith("__substg1.0_") else ""
            ptype = tag[4:8].upper() if len(tag) >= 8 else ""
            if ptype in {"001F", "101F"}:
                hay.add(slabel, content.decode("utf-16-le", errors="replace"), CLEAN)
            elif ptype == "001E":
                hay.add(slabel, decode_text(content), CLEAN)
            elif tag.upper().startswith("1009"):
                import compressed_rtf

                try:
                    rtf = compressed_rtf.decompress(content)
                    hay.add(
                        slabel + " (RTF)", rtf_text(rtf.decode("cp1252", errors="replace")), CLEAN
                    )
                except Exception:
                    raw_decodes(hay, content, slabel)
            elif tag.upper().startswith("3701") and ptype == "0102":
                prefix = "/".join(parts[:-1])
                fname = ""
                for key in ("__substg1.0_3707001F", "__substg1.0_3704001F"):
                    full = f"{prefix}/{key}" if prefix else key
                    if full in names:
                        fname = (
                            ole.openstream(names[full]).read().decode("utf-16-le", errors="replace")
                        )
                        break
                walk_bytes(hay, content, fname or "piece.bin", slabel, depth + 1)
            elif tag.upper().startswith("1013"):
                joined, spaced = markup_views(decode_text(content))
                hay.add(slabel + " (HTML)", joined, CLEAN)
                hay.add(slabel + " (balises)", spaced, RAW)
            else:
                raw_decodes(hay, content, slabel)
    finally:
        ole.close()


def walk_json(hay: Hay, text: str, label: str) -> None:
    hay.add(label + " (brut)", text, RAW)
    try:
        data = json.loads(text)
    except ValueError:
        hay.add(label, text, CLEAN)
        return
    out: list[str] = []

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                out.append(str(key))
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)
        elif node is not None:
            out.append(str(node))

    visit(data)
    hay.add(label, "\n".join(out), CLEAN)


def walk_image(hay: Hay, data: bytes, label: str) -> None:
    image = Image.open(io.BytesIO(data))
    for key, value in image.info.items():
        if isinstance(value, bytes):
            hay.add(f"{label} (info {key})", value.decode("utf-8", errors="ignore"), META)
            hay.add(
                f"{label} (info {key}, utf-16)", value.decode("utf-16-le", errors="ignore"), META
            )
        elif isinstance(value, str):
            hay.add(f"{label} (info {key})", value, META)
    exif = image.getexif()
    values: list[Any] = list(exif.values())
    for ifd in (0x8769, 0xA005):
        values += list(exif.get_ifd(ifd).values())
    gps = exif.get_ifd(0x8825)
    if gps.get(2) or gps.get(4):
        hay.gps.append(f"{label}: {gps.get(2)} {gps.get(4)}")
    for value in values:
        if isinstance(value, bytes):
            for enc in ("utf-8", "utf-16-le", "utf-16-be"):
                hay.add(f"{label} (EXIF)", value.decode(enc, errors="ignore"), META)
        else:
            hay.add(f"{label} (EXIF)", str(value), META)
    # Compressed PNG text chunks that Pillow may not expose.
    if data.startswith(b"\x89PNG"):
        pos = 8
        while pos + 8 <= len(data):
            length = int.from_bytes(data[pos : pos + 4], "big")
            ctype = data[pos + 4 : pos + 8]
            body = data[pos + 8 : pos + 8 + length]
            if ctype in (b"zTXt", b"iTXt"):
                for start in range(len(body)):
                    try:
                        hay.add(
                            f"{label} ({ctype.decode()})",
                            zlib.decompress(body[start:]).decode("utf-8", errors="ignore"),
                            META,
                        )
                        break
                    except zlib.error:
                        continue
            pos += 12 + length


# ---------------------------------------------------------------------------
# Searching
# ---------------------------------------------------------------------------


@dataclass
class Prepared:
    label: str
    kind: str
    text: str
    word: str
    raw: str
    compact: str
    light: str


def prepare(hay: Hay) -> list[Prepared]:
    out = []
    for item in hay.items:
        out.append(
            Prepared(
                item.label,
                item.kind,
                norm_text(item.text),
                norm_word(item.text),
                norm_raw(item.text),
                norm_compact(item.text),
                norm_light(item.text),
            )
        )
    return out


_COMPACT_CACHE: dict[str, re.Pattern[str]] = {}


def find(rendering: dict[str, str], prepared: list[Prepared], gps: list[str]) -> list[str]:
    mode, value = rendering["mode"], rendering["text"]
    hits: list[str] = []
    if mode == "gps":
        return list(gps)
    if mode == "text":
        needle = norm_text(value)
        hits = [p.label for p in prepared if needle in p.text]
    elif mode == "word":
        forms = word_forms(value)
        hits = [p.label for p in prepared if any(f in p.word for f in forms)]
    elif mode == "raw":
        needle = norm_raw(value)
        pattern = re.compile(r"(?<![a-z0-9])" + re.escape(needle) + r"(?![a-z0-9])")
        hits = [p.label for p in prepared if needle in p.raw and pattern.search(p.raw)]
    elif mode == "compact":
        needle = norm_compact(value)
        if needle.isdigit() and len(needle) < 12:
            pattern = _COMPACT_CACHE.get(needle)
            if pattern is None:
                pattern = re.compile(r"(?<!\d)\d{0,5}" + needle + r"(?!\d)")
                _COMPACT_CACHE[needle] = pattern
            hits = [
                p.label
                for p in prepared
                if needle in p.compact and (pattern.search(p.compact) or pattern.search(p.light))
            ]
        else:
            hits = [p.label for p in prepared if needle in p.compact]
    elif mode == "ip":
        needle = value.lower()
        if ":" in needle:
            pattern = re.compile(r"(?<![0-9a-f:])" + re.escape(needle) + r"(?![0-9a-f:])")
        else:
            pattern = re.compile(r"(?<![0-9.])" + re.escape(needle) + r"(?![0-9])(?!\.[0-9])")
        hits = [p.label for p in prepared if needle in p.raw and pattern.search(p.raw)]
    return hits


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------


def load_image(locator: dict[str, Any], files: dict[str, Path]) -> Image.Image | None:
    path = files.get(locator["file"])
    if path is None or not path.exists():
        return None
    kind = locator["kind"]
    if kind == "file":
        return Image.open(path).convert("L")
    if kind == "zip_member":
        with zipfile.ZipFile(path) as zf:
            if locator["member"] not in zf.namelist():
                return None
            return Image.open(io.BytesIO(zf.read(locator["member"]))).convert("L")
    if kind == "mime_image":
        msg = email.message_from_bytes(path.read_bytes(), policy=email.policy.default)
        images = [p for p in msg.walk() if p.get_content_maintype() == "image"]
        if locator["index"] >= len(images):
            return None
        payload = images[locator["index"]].get_payload(decode=True)
        return Image.open(io.BytesIO(payload)).convert("L") if isinstance(payload, bytes) else None
    if kind == "pdf_page":
        import pymupdf

        pdf = pymupdf.open(path)
        try:
            if locator["page"] >= pdf.page_count:
                return None
            pix = pdf[locator["page"]].get_pixmap(dpi=100)
            return Image.frombytes("RGB", (pix.width, pix.height), pix.samples).convert("L")
        finally:
            pdf.close()
    raise ValueError(kind)


def region_state(
    original: Image.Image, output: Image.Image | None, bbox: list[float]
) -> tuple[bool, str]:
    """(still readable, detail) for a fractional bounding box."""
    if output is None:
        return False, "image absente de la sortie"
    if output.size != original.size:
        output = output.resize(original.size)
    w, h = original.size
    box = (
        int(bbox[0] * w),
        int(bbox[1] * h),
        max(int(bbox[2] * w), int(bbox[0] * w) + 1),
        max(int(bbox[3] * h), int(bbox[1] * h) + 1),
    )
    a = np.asarray(original.crop(box), dtype=float)
    b = np.asarray(output.crop(box), dtype=float)
    if b.std() < 6:
        return False, "zone uniforme (masquée)"
    if a.std() < 1:
        return False, "zone d'origine vide"
    ncc = float(((a - a.mean()) * (b - b.mean())).mean() / (a.std() * b.std()))
    return ncc >= 0.35, f"corrélation {ncc:.2f}"


# ---------------------------------------------------------------------------
# Running Anonyma and evaluating
# ---------------------------------------------------------------------------


def load_truth() -> list[dict[str, Any]]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(TRUTH.glob("*.json"))]


def run_anonyma(
    profile: str, output: Path, report: Path, workers: int, home: Path | None = None
) -> float:
    """Run the command line on the whole corpus (one batch); ``home``: ANONYMA_HOME."""
    if output.exists():
        shutil.rmtree(output)
    inputs = [str(CORPUS / d) for d in sorted(os.listdir(CORPUS)) if (CORPUS / d).is_dir()]
    cmd = [
        sys.executable,
        "-m",
        "anonyma.cli",
        "run",
        *inputs,
        "-o",
        str(output),
        "--profil",
        profile,
        "--rapport",
        str(report),
        "-q",
        "-j",
        str(workers),
    ]
    env = dict(os.environ)
    if home is not None:
        env["ANONYMA_HOME"] = str(home)
    start = time.perf_counter()
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        env=env,
    )
    elapsed = time.perf_counter() - start
    if proc.returncode not in (0, 1):
        raise SystemExit(f"Échec d'Anonyma (code {proc.returncode}) :\n{proc.stderr[-3000:]}")
    return elapsed


def doc_output_files(out_root: Path, slug: str) -> list[tuple[str, Path, bool]]:
    found = []
    for base, quarantined in ((out_root / slug, False), (out_root / QUARANTINE / slug, True)):
        if base.exists():
            for path in sorted(base.rglob("*")):
                if path.is_file():
                    found.append((path.relative_to(base).as_posix(), path, quarantined))
    return found


def match_files(original: list[str], outputs: list[tuple[str, Path, bool]]) -> dict[str, Path]:
    """Map original relative paths to output files (same depth and extension, in order)."""
    groups: dict[tuple[int, str], list[tuple[str, Path, bool]]] = defaultdict(list)
    for rel, path, q in outputs:
        groups[(rel.count("/"), Path(rel).suffix.lower())].append((rel, path, q))
    mapping: dict[str, Path] = {}
    used: dict[tuple[int, str], int] = defaultdict(int)
    for rel in sorted(original, key=lambda r: (r.count("/"), Path(r).suffix.lower())):
        key = (rel.count("/"), Path(rel).suffix.lower())
        candidates = groups.get(key) or groups.get(
            (rel.count("/"), ".xlsx" if key[1] == ".xls" else key[1])
        )
        if candidates and used[key] < len(candidates):
            mapping[rel] = candidates[used[key]][1]
            used[key] += 1
    return mapping


def build_hay(files: list[tuple[str, Path]]) -> Hay:
    hay = Hay()
    for rel, path in files:
        for part in Path(rel).parts:
            hay.add(f"{rel} [nom]", part, NAME)
        walk_bytes(hay, path.read_bytes(), path.name, rel)
    return hay


def clean_text(hay: Hay) -> str:
    return "\n".join(i.text for i in hay.items if i.kind == CLEAN)


def mark_truth(orig_tokens: list[str], entries: list[dict[str, Any]]) -> list[bool]:
    marked = [False] * len(orig_tokens)
    for entry in entries:
        forms = {entry["value"], *entry["surfaces"]}
        forms |= {
            r["text"]
            for r in entry["renderings"] + entry.get("partial", [])
            if r["mode"] in {"text", "word"}
        }
        for form in forms:
            seq = tokens(form)
            if not seq:
                continue
            n = len(seq)
            first = seq[0]
            for i, tok in enumerate(orig_tokens):
                if tok == first and orig_tokens[i : i + n] == seq:
                    for j in range(i, i + n):
                        marked[j] = True
    return marked


def precision(orig_text: str, out_text: str, entries: list[dict[str, Any]]) -> dict[str, Any]:
    a, b = tokens(orig_text), tokens(out_text)
    marked = mark_truth(a, entries)
    matcher = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    tp, fp, fp_dates = 0, 0, 0
    fp_spans: list[str] = []
    for op, i1, i2, _j1, _j2 in matcher.get_opcodes():
        if op != "replace":
            continue
        if any(marked[i1:i2]):
            tp += 1
        else:
            fp += 1
            span = " ".join(a[i1:i2])
            fp_dates += 1 if _DATE_SPAN.match(span) else 0
            fp_spans.append(span[:80])
    return {
        "spans": tp + fp,
        "vrais": tp,
        "faux": fp,
        "faux_dates": fp_dates,
        "faux_exemples": fp_spans,
    }


#: A replaced span that is only a date (meeting, sending date): masked by the date
#: policy, not by a detection error; reported apart.
_DATE_SPAN = re.compile(
    r"^(?:\d{1,2} (?:janv|fevr|mars|avr|mai|juin|juil|aout|sept|oct|nov|dec|jan|feb|mar|apr|"
    r"may|jun|jul|aug|sep)[a-z]* \d{4}|\d{4} \d{2} \d{2}|\d{1,2} \d{1,2} \d{2,4})$"
)


def evaluate_doc(truth: dict[str, Any], out_root: Path) -> dict[str, Any]:
    slug = truth["slug"]
    outputs = doc_output_files(out_root, slug)
    orig_files = [(rel, CORPUS / slug / rel) for rel in truth["files"]]
    mapping = match_files(truth["files"], outputs)
    hay = build_hay([(f"{slug}/{rel}{' (quarantaine)' if q else ''}", p) for rel, p, q in outputs])
    prepared = prepare(hay)
    # Original images, for the pixel comparison.
    orig_map = {rel: path for rel, path in orig_files}
    image_state: dict[str, tuple[Image.Image | None, Image.Image | None]] = {}
    for image_id, locator in truth["images"].items():
        image_state[image_id] = (load_image(locator, orig_map), load_image(locator, mapping))
    entries_out = []
    for entry in truth["entries"]:
        hits: list[str] = []
        for rendering in entry["renderings"]:
            for label in find(rendering, prepared, hay.gps):
                hits.append(f"{label} « {rendering['text']} »")
        for region in entry["regions"]:
            original, output = image_state[region["image"]]
            if original is None:
                continue
            readable, detail = region_state(original, output, region["bbox"])
            if readable:
                hits.append(f"image {region['image']} ({detail})")
        partial = []
        for rendering in entry.get("partial", []):
            partial += find(rendering, prepared, hay.gps)
        entries_out.append(
            {
                "id": entry["id"],
                "type": entry["type"],
                "direct": entry["direct"],
                "value": entry["value"],
                "where": entry["where"],
                "leaked": bool(hits),
                "hits": sorted(set(hits))[:12],
                "first_name_residue": bool(partial),
            }
        )
    # Precision on decoded text (original vs output, same independent reader).
    orig_hay = build_hay([(rel, p) for rel, p in orig_files])
    out_sorted = [(rel, mapping[rel]) for rel in truth["files"] if rel in mapping]
    out_hay = build_hay(out_sorted)
    prec = precision(clean_text(orig_hay), clean_text(out_hay), truth["entries"])
    out_clean = norm_text(clean_text(out_hay))
    kept = [k for k in truth["keep"] if norm_text(k) in out_clean]
    return {
        "slug": slug,
        "format": truth["format"],
        "outputs": [(rel, q) for rel, _, q in outputs],
        "produced": bool(outputs),
        "quarantined": any(q for _, _, q in outputs),
        "entries": entries_out,
        "precision": prec,
        "keep_total": len(truth["keep"]),
        "keep_ok": len(kept),
        "keep_lost": [k for k in truth["keep"] if k not in kept],
    }


def statuses(report: dict[str, Any]) -> dict[str, list[str]]:
    by_slug: dict[str, list[str]] = defaultdict(list)
    for row in report.get("fichiers", []):
        slug = row["fichier"].split("/")[0]
        by_slug[slug].append(row["statut"])
    return dict(by_slug)


def aggregate(docs: list[dict[str, Any]]) -> dict[str, Any]:
    by_type: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    by_fmt_type: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    direct = [0, 0]
    indirect = [0, 0]
    for doc in docs:
        for e in doc["entries"]:
            ok = 0 if e["leaked"] else 1
            for bucket in (
                by_type[e["type"]],
                by_fmt_type[doc["format"]][e["type"]],
                direct if e["direct"] else indirect,
            ):
                bucket[0] += ok
                bucket[1] += 1
    prec: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    total_prec = [0, 0]
    no_dates = [0, 0]
    for doc in docs:
        p = doc["precision"]
        for bucket in (prec[doc["format"]], total_prec):
            bucket[0] += p["vrais"]
            bucket[1] += p["spans"]
        no_dates[0] += p["vrais"]
        no_dates[1] += p["spans"] - p.get("faux_dates", 0)
    return {
        "direct": direct,
        "indirect": indirect,
        "by_type": dict(by_type),
        "by_format_type": {f: dict(v) for f, v in by_fmt_type.items()},
        "precision_by_format": dict(prec),
        "precision": total_prec,
        "precision_without_dates": no_dates,
        "keep": [sum(d["keep_ok"] for d in docs), sum(d["keep_total"] for d in docs)],
    }


def pct(pair: list[int]) -> str:
    ok, total = pair
    return "—" if total == 0 else f"{100 * ok / total:.1f} %"


def markdown(results: dict[str, Any]) -> str:
    lines = [f"# Résultats du banc d'essai — {results['name']}", ""]
    for prof, res in results["profiles"].items():
        agg = res["aggregate"]
        lines += [
            f"## Profil {prof}",
            "",
            f"- Durée du lot : {res['seconds']:.1f} s pour {len(res['docs'])} documents",
            f"- Rappel identifiants directs : **{pct(agg['direct'])}** "
            f"({agg['direct'][0]}/{agg['direct'][1]})",
            f"- Rappel autres types : {pct(agg['indirect'])} "
            f"({agg['indirect'][0]}/{agg['indirect'][1]})",
            f"- Précision (segments remplacés correspondant à une valeur de référence) : "
            f"{pct(agg['precision'])} ({agg['precision'][0]}/{agg['precision'][1]}) ; "
            f"hors dates non liées à une naissance : {pct(agg['precision_without_dates'])}",
            f"- Phrases témoins intactes : {pct(agg['keep'])} ({agg['keep'][0]}/{agg['keep'][1]})",
            "",
            "| Type | Rappel | Valeurs |",
            "|---|---|---|",
        ]
        for kind in (*DIRECT_TYPES, *INDIRECT_TYPES):
            if kind in agg["by_type"]:
                pair = agg["by_type"][kind]
                lines.append(f"| {kind} | {pct(pair)} | {pair[1]} |")
        lines += ["", "| Format | Rappel direct | Précision | Statuts |", "|---|---|---|---|"]
        fmt_direct: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        fmt_status: dict[str, list[str]] = defaultdict(list)
        for doc in res["docs"]:
            for e in doc["entries"]:
                if e["direct"]:
                    fmt_direct[doc["format"]][0] += 0 if e["leaked"] else 1
                    fmt_direct[doc["format"]][1] += 1
            fmt_status[doc["format"]] += res["statuses"].get(doc["slug"], ["(aucun)"])
        for fmt in sorted(fmt_direct):
            counts: dict[str, int] = defaultdict(int)
            for s in fmt_status[fmt]:
                counts[s] += 1
            status = ", ".join(f"{k} ×{v}" for k, v in sorted(counts.items()))
            lines.append(
                f"| {fmt} | {pct(fmt_direct[fmt])} | "
                f"{pct(agg['precision_by_format'].get(fmt, [0, 0]))} | {status} |"
            )
        lines += ["", "### Fuites", ""]
        for doc in res["docs"]:
            for e in doc["entries"]:
                if e["leaked"]:
                    tag = "direct" if e["direct"] else "autre"
                    lines.append(
                        f"- `{doc['slug']}` {e['type']} ({tag}) « {e['value']} » — "
                        f"{'; '.join(e['hits'][:4])}"
                    )
        lines += ["", "### Faux positifs (exemples)", ""]
        for doc in res["docs"]:
            ex = doc["precision"]["faux_exemples"]
            if ex:
                lines.append(f"- `{doc['slug']}` ({len(ex)}) : " + " | ".join(ex[:8]))
        lines.append("")
    return "\n".join(lines)


def evaluate_profile(
    profile: str, work: Path, workers: int, reuse: bool, home: Path | None = None
) -> dict[str, Any]:
    out = work / profile
    report_path = work / f"rapport-{profile}.json"
    seconds = 0.0
    if not (reuse and out.exists() and report_path.exists()):
        seconds = run_anonyma(profile, out, report_path, workers, home)
        (work / f"duree-{profile}.txt").write_text(f"{seconds}", encoding="utf-8")
    elif (work / f"duree-{profile}.txt").exists():
        seconds = float((work / f"duree-{profile}.txt").read_text(encoding="utf-8"))
    report = json.loads(report_path.read_text(encoding="utf-8"))
    docs = [evaluate_doc(t, out) for t in load_truth()]
    return {
        "seconds": seconds,
        "statuses": statuses(report),
        "docs": docs,
        "aggregate": aggregate(docs),
        "report_counts": report.get("statuts", {}),
    }


def main(argv: list[str] | None = None) -> dict[str, Any]:
    parser = argparse.ArgumentParser(description="Mesure du rappel et de la précision d'Anonyma.")
    parser.add_argument(
        "--profil", nargs="+", default=["leger", "standard"], choices=list(PROFILES)
    )
    parser.add_argument("--nom", default="mesure", help="nom des fichiers de résultats")
    parser.add_argument(
        "--travail", type=Path, default=Path(tempfile.gettempdir()) / "anonyma-bench"
    )
    parser.add_argument("--processus", type=int, default=0)
    parser.add_argument(
        "--reutiliser", action="store_true", help="réutiliser les sorties existantes"
    )
    parser.add_argument("--sans-ecrire", action="store_true")
    parser.add_argument(
        "--accueil", type=Path, default=None, help="dossier ANONYMA_HOME d'Anonyma (modèles)"
    )
    args = parser.parse_args(argv)
    args.travail.mkdir(parents=True, exist_ok=True)
    results: dict[str, Any] = {"name": args.nom, "profiles": {}}
    for profile in args.profil:
        results["profiles"][profile] = evaluate_profile(
            profile, args.travail, args.processus, args.reutiliser, args.accueil
        )
        agg = results["profiles"][profile]["aggregate"]
        print(
            f"{profile}: direct {pct(agg['direct'])} autres {pct(agg['indirect'])} "
            f"précision {pct(agg['precision'])} témoins {pct(agg['keep'])} "
            f"({results['profiles'][profile]['seconds']:.0f} s)"
        )
    if not args.sans_ecrire:
        RESULTS.mkdir(parents=True, exist_ok=True)
        (RESULTS / f"{args.nom}.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n"
        )
        (RESULTS / f"{args.nom}.md").write_text(markdown(results), encoding="utf-8", newline="\n")
    return results


if __name__ == "__main__":
    main()
