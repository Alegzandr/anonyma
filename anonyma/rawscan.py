"""Raw backstop of the verification: original values searched in the output's bytes.

The segment-based verification only sees what the extractors expose. This pass reads
the written file itself, so that a value an extractor missed (markup a parser skips,
a stream nobody reads) cannot survive unseen: every original value of the batch is
searched in the raw bytes decoded as UTF-8, cp1252, UTF-16 (both byte orders and
alignments), after XML/HTML character references, in quoted-printable and base64
MIME content and RFC 2047 words, in the inflated streams of a PDF, and in every
member of a zip (Office, ODF, archives) or stream of a compound file (msg), names
included. A hit is a certain residue: the file goes to quarantine. Its label
gives the byte offset of the first occurrence in the buffer it was found in.

Embedded files: a base64 block (or ``data:`` URI) of the output whose bytes are a
known file type (image, PDF, Office, archive) must be an item the extractors
re-read and verified (a mail attachment, a file embedded in a JSON value); any
other one is a file nobody checked, whatever it shows: a certain residue of type
:data:`EMBEDDED_TYPE` (fail closed).

False positives are kept away by searching only identifying values (persons,
contacts, account numbers, business terms...: not organizations, places or dates,
which legitimately occur in namespaces and metadata of binary formats), at least
:data:`MIN_RAW_LENGTH` characters long, never a single common word, with strict word
boundaries (no letter or digit on either side), by ignoring occurrences inside
a substitute or a kept (whitelisted) value, and by never searching structure: OData
annotations and ``_x_value`` names, or a name of the batch's own schema (JSON keys,
CSV headers, XML names: :mod:`anonyma.structure`). Bounded: :data:`MAX_RAW_BYTES`
decoded bytes per output, :data:`MAX_DEPTH` levels of nesting.
"""

from __future__ import annotations

import base64
import binascii
import bisect
import hashlib
import html
import io
import quopri
import re
import zipfile
import zlib
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from email.header import decode_header

import ahocorasick

from anonyma.config import Config
from anonyma.data import is_common_word
from anonyma.extractors._embedded import file_signature
from anonyma.models import Document, EntityType, Residue
from anonyma.names import needs_capital
from anonyma.policy import keeps
from anonyma.resolver import NAME_TYPES
from anonyma.structure import is_structural_token
from anonyma.textutils import normalize, strip_accents

RAW_DETECTOR = "octets_bruts"
#: Residue type of a file embedded in an output that no extractor re-read.
EMBEDDED_TYPE = "FICHIER_INCORPORE"
#: Formats whose members are items of the output tree (not parsed again here).
ARCHIVE_FORMATS = frozenset({"zip", "7z"})
#: Values shorter than this (spaces collapsed) are not searched in raw bytes.
MIN_RAW_LENGTH = 6
#: Decoded bytes scanned per output at most (the rest is skipped, never failed).
MAX_RAW_BYTES = 512 * 1024**2
MAX_DEPTH = 4
#: Members of an archive or streams of a compound file read at most.
MAX_MEMBERS = 10_000
#: Entity types never searched in raw bytes: their values (``Microsoft``, ``Paris``,
#: ``2024``) legitimately occur in the namespaces and metadata of binary formats.
SKIPPED_TYPES = frozenset(
    {
        EntityType.ORGANIZATION,
        EntityType.LOCATION,
        EntityType.URL,
        EntityType.DATE,
        EntityType.POSTAL_CODE,
        EntityType.JOB_TITLE,
        EntityType.PROPER_NOUN,
        EntityType.SUSPICIOUS_TOKEN,
        EntityType.INDIRECT,
        EntityType.OPAQUE_ID,
        EntityType.MESSAGE_ID,
        EntityType.BARCODE,
        EntityType.BIC,
    }
)

_SPACES = re.compile(r"\s+")
_ZIP_MAGIC = b"PK\x03\x04"
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_QP_MARKER = re.compile(rb"quoted-printable", re.I)
_BASE64_BLOCK = re.compile(
    rb"(?:[A-Za-z0-9+/]{16,}[ \t]*\r?\n)+[A-Za-z0-9+/]*={0,2}|[A-Za-z0-9+/]{32,}={0,2}"
)
_ENCODED_WORD = re.compile(rb"=\?[\w.-]+\?[BbQq]\?[^?\s]*\?=")
_PDF_STREAM = re.compile(rb"stream\r?\n")
#: A base64 file in text: a long run, then runs after line ends written raw, as
#: JSON escapes (``\\r\\n``, ``\\/``) or as character references.
_FILE_BLOB = re.compile(
    rb"[A-Za-z0-9+/]{40,}(?:(?:\\[rn/]|&#1[03];|&#x[dDaA];|\r|\n)+[A-Za-z0-9+/]+)*={0,2}"
)
_BLOB_SEPARATORS = re.compile(rb"\\[rn]|&#1[03];|&#x[dDaA];|\r|\n")


@dataclass(slots=True)
class _Needle:
    entity_type: str
    #: Occurrence must start with a capital letter (common-word surnames).
    capital: bool


@dataclass(slots=True)
class RawHit:
    entity_type: str
    value: str = field(repr=False)
    #: Neutral label of where it was found (never a name or a value).
    label: str
    #: Byte offset of the occurrence in the buffer ``label`` names, when known.
    offset: int | None = None


class RawScanner:
    """Original values of a batch, searched in output files."""

    def __init__(
        self,
        sensitive: Sequence[tuple[str, str]],
        substitutes: set[str],
        allowed_keys: set[str],
        kept: Sequence[str],
        config: Config,
        structure: frozenset[str] = frozenset(),
    ) -> None:
        self.config = config
        cover_keys = {_key(s) for s in [*substitutes, *kept] if len(_key(s)) >= 2}
        self._needles: dict[str, list[_Needle]] = {}
        for value, entity_type in sensitive:
            if not _searchable(value, entity_type, allowed_keys):
                continue
            if is_structural_token(value) or _key(value) in structure:
                continue  # schema (an OData annotation, a JSON key): never personal data
            needle = _Needle(entity_type, entity_type in NAME_TYPES and needs_capital(value))
            for key in {_key(value), strip_accents(_key(value))}:
                if key not in cover_keys and len(key) >= MIN_RAW_LENGTH:
                    self._needles.setdefault(key, []).append(needle)
        self._values = _automaton(self._needles)
        self._covers = _automaton({k: [] for k in cover_keys})
        self._budget = 0

    def __bool__(self) -> bool:
        return self._values is not None

    def scan(self, output: Document) -> list[tuple[str, RawHit]]:
        """``(location, hit)`` in the files of a re-extracted output tree: the output
        itself and every item its extractors materialized (archive members,
        attachments, embedded files), located by their output names. Embedded files
        that are none of these items are hits too (:data:`EMBEDDED_TYPE`)."""
        self._budget = MAX_RAW_BYTES
        hits: list[tuple[str, RawHit]] = []
        seen: set[tuple[str, str, str, str]] = set()
        known: set[bytes] | None = None
        for doc in output.walk():
            if not doc.source.is_file() or self._budget <= 0:
                continue
            try:
                data = doc.source.read_bytes()
            except OSError:
                continue
            if file_signature(data) is None and _FILE_BLOB.search(data):
                if known is None:
                    known = _digests(output)
                # Quoted-printable soft breaks would cut a data: URI of an HTML part.
                view = quopri.decodestring(data) if _QP_MARKER.search(data) else data
                hits.extend((doc.name, hit) for hit in _unverified_files(view, known))
            if self._values is None:
                continue
            # Plain archives: their members are items of their own, scanned as such.
            members = doc.format not in ARCHIVE_FORMATS
            for hit in self._bytes(data, "contenu brut", 0, members):
                key = (doc.id, hit.label, hit.entity_type, normalize(hit.value))
                if key not in seen:  # the same bytes read under several encodings
                    seen.add(key)
                    hits.append((doc.name, hit))
        return hits

    # -- decoding ---------------------------------------------------------------

    def _charge(self, size: int) -> bool:
        if size > self._budget:
            return False
        self._budget -= size
        return True

    def _bytes(
        self, data: bytes, label: str, depth: int, members: bool = True, *, mime: bool = True
    ) -> Iterator[RawHit]:
        if depth > MAX_DEPTH or not data or not self._charge(len(data)):
            return
        if data.startswith(_ZIP_MAGIC) and members:
            yield from self._zip(data, label, depth)
            return
        if data.startswith(_OLE_MAGIC):
            yield from self._compound(data, label, depth)
            return
        text = data.decode("utf-8", "replace")
        yield from self._text(text, label, _utf8_offset(text))
        if not data.isascii():
            yield from self._text(data.decode("cp1252", "replace"), label, _same_offset)
        if b"\x00" in data:
            for offset in (0, 1):
                for codec in ("utf-16-le", "utf-16-be"):
                    decoded = data[offset:].decode(codec, "replace")
                    yield from self._text(decoded, label, _utf16_offset(offset))
        if b"&" in data:
            unescaped = html.unescape(text)
            if unescaped != text:
                yield from self._text(unescaped, f"{label} (références décodées)")
        if mime and _QP_MARKER.search(data):
            # Once per buffer: the decoded copy still holds the header asking for it.
            yield from self._bytes(
                quopri.decodestring(data), f"{label} (quoted-printable)", depth + 1, mime=False
            )
        for match in _ENCODED_WORD.finditer(data):
            where = f"{label}, mot encodé à l'octet {match.start()}"
            yield from self._text(_decode_word(match.group(0)), where)
        for match in _BASE64_BLOCK.finditer(data):
            block = _base64(match.group(0))
            if block:
                yield from self._bytes(
                    block, f"{label}, bloc base64 à l'octet {match.start()}", depth + 1
                )
        if data.startswith(b"%PDF"):
            yield from self._pdf_streams(data, label, depth)

    def _zip(self, data: bytes, label: str, depth: int) -> Iterator[RawHit]:
        try:
            archive = zipfile.ZipFile(io.BytesIO(data))
            members = archive.infolist()[:MAX_MEMBERS]
        except (zipfile.BadZipFile, OSError, ValueError):
            yield from self._text(data.decode("cp1252", "replace"), label)
            return
        with archive:
            for number, info in enumerate(members, start=1):
                where = f"{label}, élément n°{number}"
                yield from self._text(info.filename, f"{where} (nom)")
                if info.is_dir() or info.file_size > self._budget:
                    continue
                try:
                    content = archive.read(info)
                except (zipfile.BadZipFile, OSError, ValueError, NotImplementedError, RuntimeError):
                    continue
                yield from self._bytes(content, where, depth + 1)

    def _compound(self, data: bytes, label: str, depth: int) -> Iterator[RawHit]:
        import olefile

        try:
            ole = olefile.OleFileIO(io.BytesIO(data))
        except (OSError, ValueError):
            yield from self._text(data.decode("utf-16-le", "replace"), label)
            return
        try:
            for number, entry in enumerate(ole.listdir(streams=True)[:MAX_MEMBERS], start=1):
                where = f"{label}, flux n°{number}"
                yield from self._text(" ".join(entry), f"{where} (nom)")
                try:
                    content = ole.openstream(entry).read()
                except (OSError, ValueError):
                    continue
                yield from self._bytes(content, where, depth + 1)
                if entry[-1].startswith("__substg1.0_10090102"):  # compressed RTF body
                    yield from self._bytes(_rtf_decompressed(content), where, depth + 1)
        finally:
            ole.close()

    def _pdf_streams(self, data: bytes, label: str, depth: int) -> Iterator[RawHit]:
        """Inflated content of the Flate streams (images and fonts skipped)."""
        view = memoryview(data)
        for number, match in enumerate(_PDF_STREAM.finditer(data), start=1):
            if number > MAX_MEMBERS or self._budget <= 0:
                return
            dictionary = data[max(0, match.start() - 512) : match.start()]
            if b"/Image" in dictionary or b"/FontFile" in dictionary or b"/Length1" in dictionary:
                continue
            try:
                content = zlib.decompressobj().decompress(view[match.end() :], self._budget)
            except zlib.error:
                continue
            yield from self._bytes(content, f"{label}, flux n°{number}", depth + 1)

    # -- matching ----------------------------------------------------------------

    def _text(
        self, text: str, label: str, to_byte: Callable[[int], int] | None = None
    ) -> Iterator[RawHit]:
        """Hits in ``text``; ``to_byte`` maps a character index of ``text`` to the
        byte offset in the buffer it was decoded from."""
        if self._values is None or len(text) < MIN_RAW_LENGTH:
            return
        collapsed = _SPACES.sub(" ", text)
        lower = collapsed.lower()
        if len(lower) != len(collapsed):  # a few characters lower to several
            lower = "".join(c if len(c.lower()) != 1 else c.lower() for c in collapsed)
        covered: list[tuple[int, int]] | None = None
        for end, key in self._values.iter(lower):
            start = end - len(key) + 1
            stop = end + 1
            if (start > 0 and lower[start - 1].isalnum()) or (
                stop < len(lower) and lower[stop].isalnum()
            ):
                continue
            if covered is None:
                covered = self._covered(lower)
            if _inside(start, stop, covered):
                continue
            value = collapsed[start:stop]
            for needle in self._needles[key]:
                if needle.capital and not value[:1].isupper():
                    continue
                if keeps(needle.entity_type, value, self.config):
                    continue
                offset = None
                if to_byte is not None:
                    offset = to_byte(_uncollapsed(text, start))
                yield RawHit(needle.entity_type, value, label, offset)
                break

    def _covered(self, lower: str) -> list[tuple[int, int]]:
        if self._covers is None:
            return []
        spans = sorted((end - len(key) + 1, end + 1) for end, key in self._covers.iter(lower))
        merged: list[tuple[int, int]] = []
        for start, end in spans:
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        return merged


def raw_residues(hits: Sequence[tuple[str, RawHit]]) -> list[Residue]:
    from anonyma.verifier import ORIGINAL_VALUE

    return [
        Residue(
            location=location,
            segment_label=hit.label if hit.offset is None else f"{hit.label}, octet {hit.offset}",
            entity_type=hit.entity_type,
            reason=ORIGINAL_VALUE,
            detector=RAW_DETECTOR,
            value=hit.value,
            certain=True,
        )
        for location, hit in hits
    ]


def _same_offset(index: int) -> int:
    return index


def _utf8_offset(text: str) -> Callable[[int], int]:
    def to_byte(index: int) -> int:
        return len(text[:index].encode("utf-8", "surrogatepass"))

    return to_byte


def _utf16_offset(shift: int) -> Callable[[int], int]:
    def to_byte(index: int) -> int:
        return shift + 2 * index

    return to_byte


def _uncollapsed(text: str, index: int) -> int:
    """Index in ``text`` of the character found at ``index`` once its runs of white
    space are collapsed to one space."""
    shift = 0
    for match in _SPACES.finditer(text):
        if match.start() - shift >= index:
            break
        shift += len(match.group(0)) - 1
    return index + shift


def _digests(output: Document) -> set[bytes]:
    """SHA-256 of every item of a re-extracted output tree (verified items)."""
    digests: set[bytes] = set()
    for doc in output.walk():
        try:
            digests.add(hashlib.sha256(doc.source.read_bytes()).digest())
        except OSError:
            continue
    return digests


def _unverified_files(data: bytes, known: set[bytes]) -> Iterator[RawHit]:
    """Base64 blocks of ``data`` holding a known file type that is no item of the
    re-extracted tree (``known`` digests): files nobody verified."""
    for match in _FILE_BLOB.finditer(data):
        compact = _BLOB_SEPARATORS.sub(b"", match.group(0).replace(b"\\/", b"/"))
        if file_signature(_base64(compact[:64])) is None:
            continue
        decoded = _base64(compact)
        if file_signature(decoded) is None or hashlib.sha256(decoded).digest() in known:
            continue
        yield RawHit(EMBEDDED_TYPE, "", "fichier incorporé non vérifié", match.start())


def _key(value: str) -> str:
    return _SPACES.sub(" ", value).strip().lower()


def _searchable(value: str, entity_type: str, allowed_keys: set[str]) -> bool:
    if entity_type in SKIPPED_TYPES or normalize(value) in allowed_keys:
        return False
    key = _key(value)
    if len(key) < MIN_RAW_LENGTH:
        return False
    if entity_type == EntityType.PERSON:
        # A "person" made mostly of digits (a timestamp read in an author field) is
        # a detection slip, and such strings recur in binary formats.
        alnum = [c for c in key if c.isalnum()]
        if sum(c.isalpha() for c in alnum) * 2 < len(alnum):
            return False
    return " " in key or not is_common_word(key)


def _automaton(keys: dict[str, list[_Needle]]) -> ahocorasick.Automaton | None:
    if not keys:
        return None
    automaton = ahocorasick.Automaton()
    for key in keys:
        automaton.add_word(key, key)
    automaton.make_automaton()
    return automaton


def _inside(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    """``spans``: sorted, non-overlapping."""
    index = bisect.bisect_right(spans, start, key=lambda span: span[0]) - 1
    return index >= 0 and end <= spans[index][1]


def _base64(block: bytes) -> bytes:
    compact = re.sub(rb"\s+", b"", block).rstrip(b"=")
    if len(compact) % 4 == 1:
        compact = compact[:-1]
    try:
        return base64.b64decode(compact + b"=" * (-len(compact) % 4))
    except (binascii.Error, ValueError):
        return b""


def _decode_word(word: bytes) -> str:
    try:
        parts = decode_header(word.decode("ascii", "replace"))
    except (ValueError, LookupError):
        return ""
    out: list[str] = []
    for chunk, charset in parts:
        if isinstance(chunk, bytes):
            try:
                out.append(chunk.decode(charset or "utf-8", "replace"))
            except LookupError:
                out.append(chunk.decode("utf-8", "replace"))
        else:
            out.append(chunk)
    return "".join(out)


def _rtf_decompressed(data: bytes) -> bytes:
    try:
        import compressed_rtf  # type: ignore[import-untyped]

        result = compressed_rtf.decompress(data)
    except Exception:
        return b""
    return result if isinstance(result, bytes) else b""


__all__ = [
    "EMBEDDED_TYPE",
    "MIN_RAW_LENGTH",
    "RAW_DETECTOR",
    "RawHit",
    "RawScanner",
    "raw_residues",
]
