"""Files embedded in text values: base64 strings and ``data:`` URIs (JSON, XML, CSV).

A Dataverse export carries its attachment bodies (``activitymimeattachment.body``,
``annotation.documentbody``) as base64 strings, other exports embed ``data:`` URIs
in HTML fields. Such a value is a file, not text: left as is it would reach the
output byte for byte (an identity card scan, an invoice). The structured extractors
therefore extract each one as a child document, exactly like a mail attachment (OCR
for images, PDF, Office...), and expose the rest of the value with an object mark
(:data:`MARK`) in its place. Their writers put the anonymized child back, encoded
the same way (alphabet, padding, line length and line ends, ``data:`` header); a
child that could not be anonymized leaves an empty value (fail closed: the original
bytes never reach the output, the container is « À revoir »).

A value is taken for a file when it is base64 as a whole (or a ``data:...;base64,``
URI anywhere in it) and its bytes are a known file type (magic bytes), text, or a
long binary; sibling fields (``mimetype``, ``filename``, a key such as ``body`` or
``documentbody``) only lower the size threshold and name the child's extension.
Hexadecimal strings and GUIDs (identifiers, conversation indexes) are never files.
"""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from anonyma import registry
from anonyma.extractors._html import extension_for, mime_for
from anonyma.models import ChildOutput, Document, ExtractionContext, Issue, Severity
from anonyma.textutils import normalize, safe_suffix

#: Stands for an embedded file in the text exposed to detection (one character).
MARK = "￼"
#: Document property: ``{unit index: [EmbeddedRef, ...]}``.
PROPERTY = "embedded"
#: Issue code of an embedded file dropped because its place in the value was lost.
DROPPED_CODE = "embedded.dropped"

#: Binary payloads of unknown type at least this long are files (an unsupported
#: child is then omitted: never copied).
MIN_BINARY_BYTES = 1024
#: Base64 text (characters) at least this long can be a text file without any hint.
MIN_TEXT_CHARS = 48
#: With a hint (sibling ``mimetype``/``filename``, ``body`` key), shorter payloads.
MIN_HINTED_CHARS = 16

_STD = re.compile(r"[A-Za-z0-9+/]+={0,2}")
_URLSAFE = re.compile(r"[A-Za-z0-9_-]+={0,2}")
_HEXISH = re.compile(r"[0-9A-Fa-f{}-]+")
_NEWLINE = re.compile(r"\r\n|\r|\n")
_DATA_URI = re.compile(
    r"data:(?P<mime>[\w.+-]+/[\w.+-]+)?(?:;[\w.+-]+=[^;,\s\"'<>]*)*;base64,"
    r"(?P<payload>[A-Za-z0-9+/_-]+={0,2})",
    re.I,
)
#: Normalized key / column / element names that announce a file body.
BODY_NAMES = frozenset(
    {
        "body", "documentbody", "contentbytes", "content", "contents", "data", "file",
        "filecontent", "filedata", "attachment", "attachmentbody", "base64", "bytes",
        "payload", "image", "entityimage", "photo", "picture", "binary", "blob", "document",
    }
)  # fmt: skip
#: Normalized names of the sibling fields giving the MIME type / the file name.
MIME_NAMES = frozenset(
    {"mimetype", "mime type", "mime", "contenttype", "content type", "mediatype",
     "odata mediacontenttype"}
)  # fmt: skip
FILENAME_NAMES = frozenset(
    {"filename", "file name", "name", "nom", "nomfichier", "nom fichier", "documentname",
     "attachmentname", "title"}
)  # fmt: skip


@dataclass(frozen=True, slots=True)
class Hint:
    """What the surroundings of a value say about it (never logged)."""

    #: A sibling field or the value's own name announces a file.
    strong: bool = False
    mime: str = ""
    #: Extension of the sibling file name (``.png``), never the name itself.
    suffix: str = ""


NO_HINT = Hint()


def hint_from(name: str, siblings: Mapping[str, str]) -> Hint:
    """Hint of a value named ``name`` (key, column, element) among ``siblings``
    (normalized sibling name -> value)."""
    mime = next((v.strip() for k, v in siblings.items() if k in MIME_NAMES and v), "")
    suffix = ""
    for key, value in siblings.items():
        if key in FILENAME_NAMES and value.strip():
            suffix = safe_suffix(value.strip())
            if suffix:
                break
    mime = mime.split(";")[0].strip().lower() if "/" in mime else ""
    strong = name_key(name) in BODY_NAMES or bool(mime) or bool(suffix)
    return Hint(strong, mime, suffix)


def name_key(name: str) -> str:
    """Normalized form of a field name (``fileName`` -> ``filename``)."""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", normalize(name)).split())


@dataclass(frozen=True, slots=True)
class Base64Style:
    urlsafe: bool = False
    padded: bool = True
    #: Characters per line, 0 for a single line.
    line: int = 0
    newline: str = "\n"
    trailing_newline: bool = False
    #: ``data:image/png;base64,`` for a data: URI, "" for a bare value.
    prefix: str = ""

    def encode(self, data: bytes) -> str:
        raw = base64.urlsafe_b64encode(data) if self.urlsafe else base64.b64encode(data)
        text = raw.decode("ascii")
        if not self.padded:
            text = text.rstrip("=")
        if self.line and len(text) > self.line:
            text = self.newline.join(
                text[i : i + self.line] for i in range(0, len(text), self.line)
            )
        if self.trailing_newline:
            text += self.newline
        return self.prefix + text


@dataclass(frozen=True, slots=True)
class Blob:
    """An embedded file found in a value: ``value[start:end]``."""

    start: int
    end: int
    style: Base64Style
    suffix: str
    data: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class EmbeddedRef:
    """An embedded file of a unit, kept for the writer (picklable)."""

    child_id: str
    start: int
    end: int
    style: Base64Style
    #: Extension of the extracted child (to tell a format change on output).
    suffix: str


def parse_base64(text: str) -> tuple[bytes, Base64Style] | None:
    """Bytes and layout of ``text`` if it is canonical base64 as a whole (lines of
    one length, one kind of line end), else None."""
    lines = _NEWLINE.split(text)
    trailing = len(lines) > 1 and lines[-1] == ""
    if trailing:
        lines = lines[:-1]
    if not lines or any(not line for line in lines):
        return None
    ends = set(_NEWLINE.findall(text))
    if len(ends) > 1:
        return None
    compact = "".join(lines)
    if len(compact) < 4:
        return None
    urlsafe = "-" in compact or "_" in compact
    if not (_URLSAFE if urlsafe else _STD).fullmatch(compact):
        return None
    core = compact.rstrip("=")
    if len(core) % 4 == 1 or ("=" in compact and len(compact) % 4):
        return None
    try:
        data = base64.b64decode(
            core + "=" * (-len(core) % 4), altchars=b"-_" if urlsafe else None, validate=True
        )
    except (binascii.Error, ValueError):
        return None
    style = Base64Style(
        urlsafe=urlsafe,
        padded=compact.endswith("=") or len(core) % 4 == 0,
        line=len(lines[0]) if len(lines) > 1 else 0,
        newline=ends.pop() if ends else "\n",
        trailing_newline=trailing,
    )
    # Canonical only: what we would write back is what was read.
    if style.encode(data).replace(style.newline, "") != compact:
        return None
    return data, style


#: Magic bytes of the file types worth extracting -> extension.
_SIGNATURES: tuple[tuple[bytes, int, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", 0, ".png"),
    (b"\xff\xd8\xff", 0, ".jpg"),
    (b"GIF87a", 0, ".gif"),
    (b"GIF89a", 0, ".gif"),
    (b"II*\x00", 0, ".tiff"),
    (b"MM\x00*", 0, ".tiff"),
    (b"%PDF-", 0, ".pdf"),
    (b"PK\x03\x04", 0, ".zip"),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", 0, ".bin"),
    (b"{\\rtf", 0, ".rtf"),
    (b"7z\xbc\xaf\x27\x1c", 0, ".7z"),
    (b"\x1f\x8b\x08", 0, ".gz"),
    (b"ftyp", 4, ".bin"),
    (b"WEBP", 8, ".webp"),
)


def file_signature(data: bytes) -> str | None:
    """Extension of a known file type from its magic bytes, else None."""
    for magic, offset, ext in _SIGNATURES:
        if data[offset : offset + len(magic)] == magic:
            return ext
    return None


def _textual(data: bytes, lenient: bool) -> bool:
    """Bytes of a text file: decodable, printable, holding letters."""
    if len(data) < 4:
        return False
    text = ""
    codecs = ["utf-8-sig", "cp1252"] if lenient else ["utf-8-sig"]
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        codecs = ["utf-16"]
    for codec in codecs:
        try:
            text = data.decode(codec)
            break
        except UnicodeDecodeError:
            continue
    if not text:
        return False
    printable = sum(1 for c in text if c.isprintable() or c in "\r\n\t")
    return printable >= 0.97 * len(text) and any(c.isalpha() for c in text)


def _mixed(compact: str) -> bool:
    """Base64 of binary data mixes cases and digits (plain words do not)."""
    return (
        any(c.isupper() for c in compact)
        and any(c.islower() for c in compact)
        and any(c.isdigit() or c in "+/-_" for c in compact)
    )


def _kind(data: bytes, compact: str, hint: Hint) -> str | None:
    """Extension of the file held by a base64 payload, or None when it is no file."""
    if _HEXISH.fullmatch(compact):
        return None  # hexadecimal runs and GUIDs are identifiers
    signature = file_signature(data)
    if signature is not None:
        return signature
    if len(compact) >= (MIN_HINTED_CHARS if hint.strong else MIN_TEXT_CHARS) and _textual(
        data, hint.strong
    ):
        return ".txt"
    if not _mixed(compact):
        return None
    if len(data) >= MIN_BINARY_BYTES or (hint.strong and len(compact) >= MIN_HINTED_CHARS):
        return ".bin"
    return None


def _suffix(data: bytes, kind: str, hint: Hint, mime: str = "") -> str:
    """Extension of the child: the sibling file name's, else the sniffed format's,
    else the MIME type's, else the kind found."""
    if hint.suffix:
        return hint.suffix
    fmt = registry.detect_format(Path("embedded"), head=data[:8192])
    if fmt:
        for ext, known in registry.supported_extensions().items():
            if known == fmt:
                return ext
    for declared in (mime, hint.mime):
        if declared:
            ext = extension_for(declared)
            if ext != ".bin":
                return ext
    return kind


def find_blobs(value: str, hint: Hint = NO_HINT) -> list[Blob]:
    """Embedded files of ``value``: the whole (stripped) value when it is base64 of a
    file, else every ``data:...;base64,`` URI holding a file."""
    stripped = value.strip()
    lead = len(value) - len(value.lstrip())
    if stripped and not stripped.startswith("data:"):
        # The whole value (its final line end included, when there is one).
        end = len(value)
        parsed = parse_base64(value[lead:])
        if parsed is None:
            end = lead + len(stripped)
            parsed = parse_base64(stripped)
        if parsed is not None:
            data, style = parsed
            compact = "".join(_NEWLINE.split(stripped))
            kind = _kind(data, compact, hint)
            if kind is not None:
                suffix = _suffix(data, kind, hint)
                return [Blob(lead, end, style, suffix, data)]
            return []
    blobs: list[Blob] = []
    if "base64," not in value:
        return blobs
    for match in _DATA_URI.finditer(value):
        parsed = parse_base64(match.group("payload"))
        if parsed is None:
            continue
        data, style = parsed
        if not data:
            continue
        mime = (match.group("mime") or "").lower()
        kind = file_signature(data) or (".txt" if _textual(data, True) else ".bin")
        prefix = value[match.start() : match.start("payload")]
        blobs.append(
            Blob(
                match.start(),
                match.end(),
                Base64Style(style.urlsafe, style.padded, prefix=prefix),
                _suffix(data, kind, NO_HINT, mime),
                data,
            )
        )
    return blobs


def extract_blobs(
    value: str, hint: Hint, document: Document, ctx: ExtractionContext
) -> list[EmbeddedRef]:
    """Extract the embedded files of ``value`` as children of ``document``."""
    refs: list[EmbeddedRef] = []
    for blob in find_blobs(value, hint):
        ctx.check_cancelled()
        ctx.budget.consume(len(blob.data), depth=ctx.depth + 1)
        number = document.properties.get("embedded_count", 0) + 1
        document.properties["embedded_count"] = number
        child = ctx.extract_child(blob.data, f"fichier-incorpore-{number}{blob.suffix}", document)
        refs.append(EmbeddedRef(child.id, blob.start, blob.end, blob.style, blob.suffix))
    return refs


def masked(value: str, refs: Sequence[EmbeddedRef]) -> str:
    """``value`` with each embedded file replaced by :data:`MARK`."""
    out: list[str] = []
    cursor = 0
    for ref in refs:
        out.append(value[cursor : ref.start])
        out.append(MARK)
        cursor = ref.end
    out.append(value[cursor:])
    return "".join(out)


def visible(text: str) -> bool:
    """Whether a masked value still holds text worth a segment."""
    return bool(text.replace(MARK, "").strip())


def encoded_child(ref: EmbeddedRef, child_outputs: Mapping[str, ChildOutput]) -> str:
    """The anonymized child encoded like the original, or "" when it was omitted."""
    output = child_outputs.get(ref.child_id)
    if output is None or output.path is None:
        return ""
    style = ref.style
    if style.prefix and safe_suffix(output.name) != ref.suffix:
        # Converted on output (xls -> xlsx): the data: header follows.
        style = Base64Style(
            style.urlsafe, style.padded, prefix=f"data:{mime_for(output.name)};base64,"
        )
    return style.encode(output.path.read_bytes())


def rebuild(
    new_masked: str, refs: Sequence[EmbeddedRef], child_outputs: Mapping[str, ChildOutput]
) -> tuple[str, list[Issue]]:
    """The output value: ``new_masked`` (replacements applied) with each mark
    replaced by its re-encoded child. Marks a replacement swallowed drop every
    embedded file of the value (never the original bytes)."""
    pieces = new_masked.split(MARK)
    if len(pieces) != len(refs) + 1:
        issue = Issue(
            Severity.WARNING,
            DROPPED_CODE,
            "Fichier incorporé retiré : sa place dans la valeur n'a pas pu être retrouvée.",
        )
        return new_masked.replace(MARK, ""), [issue]
    out = [pieces[0]]
    for ref, piece in zip(refs, pieces[1:], strict=True):
        out.append(encoded_child(ref, child_outputs))
        out.append(piece)
    return "".join(out), []


__all__ = [
    "MARK",
    "PROPERTY",
    "Base64Style",
    "Blob",
    "EmbeddedRef",
    "Hint",
    "encoded_child",
    "extract_blobs",
    "file_signature",
    "find_blobs",
    "hint_from",
    "masked",
    "name_key",
    "parse_base64",
    "rebuild",
    "visible",
]
