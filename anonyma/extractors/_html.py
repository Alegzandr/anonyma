"""HTML tokenizer working on source offsets, shared by the html and mail plugins.

The markup is never re-serialised: the writer copies the source verbatim and only
substitutes the source spans that hold replaced characters. Every piece of text
(text nodes, attribute values, comments, script/style contents, ``<title>``) is a
:class:`Unit` whose decoded text keeps, for each character, the source span it
comes from, so that character references (``&eacute;``) and percent-encoded URLs
are handled in both directions.
"""

from __future__ import annotations

import base64
import binascii
import html
import mimetypes
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import cached_property
from urllib.parse import quote

from anonyma.models import Replacement, ReplacementPlan, Segment, SegmentContext, SegmentKind

#: Unit kinds.
TEXT = "text"  # text node (character references decoded)
RCDATA = "rcdata"  # <title>, <textarea> (character references decoded)
RAW = "raw"  # <script>, <style> contents (verbatim)
COMMENT = "comment"  # <!-- ... --> (verbatim)
ATTR = "attr"  # attribute value (character references decoded)

_RAW_TEXT_TAGS = frozenset({"script", "style", "xmp", "iframe", "noembed", "noframes"})
_RCDATA_TAGS = frozenset({"title", "textarea"})
#: Tags that end a run of inline text: text on both sides is not one sentence.
_BLOCK_TAGS = frozenset(
    {
        "address", "article", "aside", "blockquote", "body", "br", "caption", "center",
        "dd", "details", "dialog", "dir", "div", "dl", "dt", "fieldset", "figcaption",
        "figure", "footer", "form", "frameset", "h1", "h2", "h3", "h4", "h5", "h6", "head",
        "header", "hgroup", "hr", "html", "iframe", "img", "input", "legend", "li", "main",
        "menu", "meta", "nav", "noscript", "ol", "option", "p", "pre", "script", "section",
        "select", "style", "summary", "table", "tbody", "td", "textarea", "tfoot", "th",
        "thead", "title", "tr", "ul", "button",
    }
)  # fmt: skip
#: Attributes holding layout/enumerated values only (never exposed).
_SKIPPED_ATTRS = frozenset(
    {
        "align", "bgcolor", "border", "cellpadding", "cellspacing", "charset", "class",
        "color", "colspan", "dir", "enctype", "face", "height", "http-equiv", "lang",
        "method", "nowrap", "rel", "role", "rowspan", "shape", "size", "span", "tabindex",
        "target", "type", "valign", "width", "xml:lang", "xmlns",
    }
)  # fmt: skip
#: Attributes holding URLs: their percent-encoding is decoded as well.
_URL_ATTRS = frozenset(
    {
        "action", "background", "cite", "codebase", "data", "formaction", "href",
        "longdesc", "manifest", "ping", "poster", "src", "srcset", "usemap",
    }
)  # fmt: skip

_CHARREF = re.compile(r"&(?:#[0-9]+;?|#[xX][0-9a-fA-F]+;?|[A-Za-z][A-Za-z0-9]*;?)")
_TAG_NAME = re.compile(r"[A-Za-z][^\s/>]*")
_SPACE = " \t\n\r\f"
_META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_.:-]+)""", re.I)
_DATA_URI = re.compile(r"^\s*data:([^,;]*)((?:;[^,;]*)*?);base64,", re.I)


@dataclass(frozen=True, slots=True)
class Unit:
    """A piece of text in the source, ``[start, end)`` excluding quotes/delimiters."""

    index: int
    kind: str
    start: int
    end: int
    tag: str
    #: Attribute name (attributes) or a fixed label.
    name: str = ""
    #: Quote character of an attribute value ('"', "'" or "" when unquoted).
    quote: str = ""


@dataclass(frozen=True, slots=True)
class Decoded:
    """Decoded text of a unit and, per character, its ``(start, end)`` source span."""

    text: str
    #: None when every character maps to one source character from ``base``.
    spans: tuple[tuple[int, int], ...] | None
    base: int = 0

    def span(self, index: int) -> tuple[int, int]:
        if self.spans is None:
            return self.base + index, self.base + index + 1
        return self.spans[index]


@dataclass(frozen=True, slots=True)
class DataUri:
    unit: int
    mime: str
    data: bytes


@dataclass(frozen=True, slots=True)
class HtmlSegmentSpec:
    """A segment to create: concatenated decoded text of ``units``."""

    units: tuple[int, ...]
    text: str
    kind: SegmentKind
    label: str


class HtmlSource:
    """Tokenised HTML text. Deterministic: the writer rebuilds the same units."""

    def __init__(self, text: str) -> None:
        self.text = text
        #: Units in document order, plus flow breaks (indices after which a block
        #: boundary occurs).
        self.units: list[Unit] = []
        self._breaks: set[int] = set()
        self._decoded: dict[int, Decoded] = {}
        self._tokenize()

    # -- tokenizer -------------------------------------------------------------

    def _add(
        self, kind: str, start: int, end: int, tag: str, name: str = "", quote: str = ""
    ) -> None:
        self.units.append(Unit(len(self.units), kind, start, end, tag, name, quote))

    def _break(self) -> None:
        self._breaks.add(len(self.units))

    def _tokenize(self) -> None:
        src = self.text
        n = len(src)
        pos = 0
        text_start = 0

        def flush(upto: int) -> None:
            if upto > text_start:
                self._add(TEXT, text_start, upto, "")

        while pos < n:
            lt = src.find("<", pos)
            if lt == -1 or lt + 1 >= n:
                break
            nxt = src[lt + 1]
            if src.startswith("<!--", lt):
                flush(lt)
                end = src.find("-->", lt + 4)
                stop = n if end == -1 else end
                self._break()
                self._add(COMMENT, lt + 4, stop, "", "commentaire")
                self._break()
                pos = n if end == -1 else end + 3
                text_start = pos
            elif src.startswith("<![CDATA[", lt):
                flush(lt)
                end = src.find("]]>", lt + 9)
                stop = n if end == -1 else end
                self._add(RAW, lt + 9, stop, "", "cdata")
                pos = n if end == -1 else end + 3
                text_start = pos
            elif nxt in "!?":
                flush(lt)
                end = src.find(">", lt + 2)
                pos = n if end == -1 else end + 1
                text_start = pos
            elif nxt == "/" and lt + 2 < n and src[lt + 2].isascii() and src[lt + 2].isalpha():
                flush(lt)
                match = _TAG_NAME.match(src, lt + 2)
                tag = match.group(0).lower() if match else ""
                end = src.find(">", lt + 2)
                if tag in _BLOCK_TAGS:
                    self._break()
                pos = n if end == -1 else end + 1
                text_start = pos
            elif nxt.isascii() and nxt.isalpha():
                flush(lt)
                pos = self._start_tag(lt)
                text_start = pos
            else:
                pos = lt + 1
        flush(n)

    def _start_tag(self, lt: int) -> int:
        """Parse a start tag at ``lt``; returns the position after it (and after the
        raw text content for script/style/title...)."""
        src = self.text
        n = len(src)
        match = _TAG_NAME.match(src, lt + 1)
        assert match is not None
        tag = match.group(0).lower()
        if tag in _BLOCK_TAGS:
            self._break()
        pos = match.end()
        while pos < n:
            while pos < n and (src[pos] in _SPACE or src[pos] == "/"):
                pos += 1
            if pos >= n or src[pos] == ">":
                break
            name_start = pos
            pos += 1  # the first character may be '='
            while pos < n and src[pos] not in _SPACE and src[pos] not in "/>=":
                pos += 1
            name = src[name_start:pos].lower()
            scan = pos
            while scan < n and src[scan] in _SPACE:
                scan += 1
            if scan < n and src[scan] == "=":
                scan += 1
                while scan < n and src[scan] in _SPACE:
                    scan += 1
                if scan < n and src[scan] in "\"'":
                    quote = src[scan]
                    close = src.find(quote, scan + 1)
                    close = n if close == -1 else close
                    self._attribute(tag, name, scan + 1, close, quote)
                    pos = min(close + 1, n)
                else:
                    value_start = scan
                    while scan < n and src[scan] not in _SPACE and src[scan] != ">":
                        scan += 1
                    self._attribute(tag, name, value_start, scan, "")
                    pos = scan
        pos = min(pos + 1, n)  # skip '>'
        if tag in _RAW_TEXT_TAGS or tag in _RCDATA_TAGS or tag == "plaintext":
            if tag == "plaintext":
                close = n
            else:
                found = re.compile(rf"</{re.escape(tag)}[\s/>]", re.I).search(src, pos)
                close = found.start() if found else n
            kind = RCDATA if tag in _RCDATA_TAGS else RAW
            if close > pos:
                self._break()
                self._add(kind, pos, close, tag, tag)
                self._break()
            pos = close
        return pos

    def _attribute(self, tag: str, name: str, start: int, end: int, quote: str) -> None:
        if name in _SKIPPED_ATTRS or name.startswith("xmlns") or start >= end:
            return
        self._add(ATTR, start, end, tag, name, quote)

    # -- decoding --------------------------------------------------------------

    def decoded(self, index: int) -> Decoded:
        cached = self._decoded.get(index)
        if cached is None:
            unit = self.units[index]
            if unit.kind in (RAW, COMMENT):
                cached = Decoded(self.text[unit.start : unit.end], None, unit.start)
            else:
                cached = _decode_refs(self.text, unit.start, unit.end)
                if unit.kind == ATTR and unit.name in _URL_ATTRS:
                    cached = _decode_percent(cached)
            self._decoded[index] = cached
        return cached

    @cached_property
    def data_uris(self) -> list[DataUri]:
        """``data:...;base64,`` attribute values (embedded images...)."""
        found: list[DataUri] = []
        for unit in self.units:
            if unit.kind != ATTR:
                continue
            raw = self.text[unit.start : unit.end]
            match = _DATA_URI.match(html.unescape(raw))
            if not match:
                continue
            payload = html.unescape(raw)[match.end() :]
            try:
                data = base64.b64decode(re.sub(r"\s+", "", payload), validate=False)
            except (binascii.Error, ValueError):
                continue
            found.append(DataUri(unit.index, (match.group(1) or "").strip().lower(), data))
        return found

    # -- segments --------------------------------------------------------------

    def segment_specs(self) -> list[HtmlSegmentSpec]:
        """Group units into segments: consecutive text nodes between two block
        boundaries form one segment; every other unit is its own segment."""
        skip = {d.unit for d in self.data_uris}
        specs: list[HtmlSegmentSpec] = []
        flow: list[int] = []

        def close_flow() -> None:
            if flow:
                text = "".join(self.decoded(i).text for i in flow)
                if text.strip():
                    specs.append(HtmlSegmentSpec(tuple(flow), text, SegmentKind.BODY, "texte"))
                flow.clear()

        for unit in self.units:
            if unit.index in self._breaks:
                close_flow()
            if unit.kind == TEXT:
                flow.append(unit.index)
                continue
            if unit.kind != ATTR:
                close_flow()
            if unit.index in skip:
                continue
            text = self.decoded(unit.index).text
            if not text.strip():
                continue
            if unit.kind == ATTR:
                kind, label = SegmentKind.ATTRIBUTE, unit.name
            elif unit.kind == COMMENT:
                kind, label = SegmentKind.COMMENT, "commentaire"
            elif unit.kind == RCDATA:
                kind = SegmentKind.HEADER if unit.tag == "title" else SegmentKind.BODY
                label = "titre" if unit.tag == "title" else unit.tag
            else:
                kind, label = SegmentKind.HIDDEN, unit.name or "script"
            specs.append(HtmlSegmentSpec((unit.index,), text, kind, label))
        close_flow()
        return specs

    # -- rewriting -------------------------------------------------------------

    def rewrite(
        self,
        edits: Mapping[int, Sequence[tuple[int, int, str]]],
        values: Mapping[int, str] | None = None,
    ) -> str:
        """Return the source with ``edits`` (unit -> decoded ``(start, end, new)``)
        applied and the whole value of the units in ``values`` replaced (already
        decoded text, escaped here)."""
        patches: list[tuple[int, int, str]] = []
        for index, unit_edits in edits.items():
            unit = self.units[index]
            decoded = self.decoded(index)
            for start, end, new in unit_edits:
                if start >= end or end > len(decoded.text):
                    continue
                patches.append(
                    (decoded.span(start)[0], decoded.span(end - 1)[1], self._encode(unit, new))
                )
        for index, value in (values or {}).items():
            unit = self.units[index]
            patches.append((unit.start, unit.end, self._encode(unit, value, url=False)))
        patches.sort(key=lambda p: (p[0], p[1]))
        out: list[str] = []
        cursor = 0
        for start, end, new in patches:
            if start < cursor:  # shares a character reference with the previous patch
                continue
            out.append(self.text[cursor:start])
            out.append(new)
            cursor = end
        out.append(self.text[cursor:])
        return "".join(out)

    @staticmethod
    def _encode(unit: Unit, new: str, *, url: bool = True) -> str:
        if unit.kind in (RAW, COMMENT):
            return new
        if unit.kind == ATTR and url and unit.name in _URL_ATTRS:
            # Non-ASCII and spaces are percent-encoded; the reader decodes them back.
            new = quote(new, safe="/:@!$&'()*+,;=?#[]~%")
        out = new.replace("&", "&amp;")
        if unit.kind != ATTR:
            return out.replace("<", "&lt;").replace(">", "&gt;")
        if unit.quote == '"':
            return out.replace('"', "&quot;")
        if unit.quote == "'":
            return out.replace("'", "&#39;")
        return re.sub(r"""[\s"'=<>`]""", lambda m: f"&#{ord(m.group(0))};", out)


def _decode_refs(src: str, start: int, end: int) -> Decoded:
    if src.find("&", start, end) == -1:
        return Decoded(src[start:end], None, start)
    chars: list[str] = []
    spans: list[tuple[int, int]] = []
    pos = start
    for match in _CHARREF.finditer(src, start, end):
        for i in range(pos, match.start()):
            chars.append(src[i])
            spans.append((i, i + 1))
        ref = match.group(0)
        value = html.unescape(ref)
        if value == ref:
            for i in range(match.start(), match.end()):
                chars.append(src[i])
                spans.append((i, i + 1))
        else:
            for ch in value:
                chars.append(ch)
                spans.append((match.start(), match.end()))
        pos = match.end()
    for i in range(pos, end):
        chars.append(src[i])
        spans.append((i, i + 1))
    return Decoded("".join(chars), tuple(spans))


def _utf8_length(lead: int) -> int:
    if lead < 0x80:
        return 1
    if 0xC2 <= lead < 0xE0:
        return 2
    if 0xE0 <= lead < 0xF0:
        return 3
    if 0xF0 <= lead < 0xF5:
        return 4
    return 0


_PERCENT = re.compile(r"%[0-9A-Fa-f]{2}")


def _decode_percent(decoded: Decoded) -> Decoded:
    """Decode ``%XX`` UTF-8 sequences, merging the source spans of their bytes."""
    text = decoded.text
    if "%" not in text:
        return decoded
    chars: list[str] = []
    spans: list[tuple[int, int]] = []
    i = 0
    n = len(text)
    while i < n:
        if _PERCENT.match(text, i):
            lead = int(text[i + 1 : i + 3], 16)
            length = _utf8_length(lead)
            if length:
                raw = bytearray()
                j = i
                for _ in range(length):
                    if not _PERCENT.match(text, j):
                        break
                    raw.append(int(text[j + 1 : j + 3], 16))
                    j += 3
                try:
                    ch = bytes(raw).decode("utf-8") if len(raw) == length else ""
                except UnicodeDecodeError:
                    ch = ""
                if len(ch) == 1:
                    chars.append(ch)
                    spans.append((decoded.span(i)[0], decoded.span(j - 1)[1]))
                    i = j
                    continue
        chars.append(text[i])
        spans.append(decoded.span(i))
        i += 1
    return Decoded("".join(chars), tuple(spans))


# ---------------------------------------------------------------------------
# Segment helpers shared by the html and mail plugins
# ---------------------------------------------------------------------------


def build_segments(source: HtmlSource, id_prefix: str, label_prefix: str = "") -> list[Segment]:
    segments: list[Segment] = []
    for i, spec in enumerate(source.segment_specs()):
        segments.append(
            Segment(
                id=f"{id_prefix}{i}",
                text=spec.text,
                locator=("html", *spec.units),
                context=SegmentContext(spec.kind, label=f"{label_prefix}{spec.label}"),
            )
        )
    return segments


def plan_edits(
    source: HtmlSource, segments: Iterable[Segment], plan: ReplacementPlan
) -> dict[int, list[tuple[int, int, str]]]:
    """Distribute the replacements of each segment over its units."""
    edits: dict[int, list[tuple[int, int, str]]] = {}
    for segment in segments:
        replacements = plan.for_segment(segment.id)
        if not replacements or not segment.locator or segment.locator[0] != "html":
            continue
        units = [int(u) for u in segment.locator[1:]]
        _distribute(source, units, replacements, edits)
    for unit_edits in edits.values():
        unit_edits.sort()
    return edits


def _distribute(
    source: HtmlSource,
    units: Sequence[int],
    replacements: Sequence[Replacement],
    edits: dict[int, list[tuple[int, int, str]]],
) -> None:
    offsets: list[tuple[int, int, int]] = []  # (unit, seg start, seg end)
    cursor = 0
    for index in units:
        length = len(source.decoded(index).text)
        offsets.append((index, cursor, cursor + length))
        cursor += length
    for rep in replacements:
        for index, start, end in offsets:
            if end <= rep.start or start >= rep.end:
                continue
            local_start = max(rep.start, start) - start
            local_end = min(rep.end, end) - start
            new = rep.substitute if start <= rep.start < end else ""
            edits.setdefault(index, []).append((local_start, local_end, new))


def data_uri_value(mime: str, data: bytes) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def extension_for(mime: str) -> str:
    if mime in ("image/jpeg", "image/jpg", "image/pjpeg"):
        return ".jpg"
    ext = mimetypes.guess_extension(mime or "", strict=False)
    return ext or ".bin"


def mime_for(name: str) -> str:
    guessed, _ = mimetypes.guess_type(name, strict=False)
    return guessed or "application/octet-stream"


def sniff_charset(data: bytes) -> str | None:
    """Charset declared by ``<meta charset>`` / ``http-equiv`` in the first 4 KiB."""
    match = _META_CHARSET.search(data[:4096])
    if not match:
        return None
    name = match.group(1).decode("ascii", "replace").lower()
    try:
        "".encode(name)
    except LookupError:
        return None
    return name
