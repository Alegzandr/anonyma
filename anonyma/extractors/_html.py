"""HTML tokenizer working on source offsets, shared by the html and mail plugins.

The markup is never re-serialised: the writer copies the source verbatim and only
substitutes the source spans that hold replaced characters. Every piece of text
(text nodes, attribute values, comments, script/style contents, ``<title>``) is a
:class:`Unit` whose decoded text keeps, for each character, the source span it
comes from, so that character references (``&eacute;``) and percent-encoded URLs
are handled in both directions.

Nothing in the markup is copied unseen. A ``<`` that does not open a real element
(``<marc.dupont@exemple.test>``, ``<Jean Dupont>``: not an HTML, SVG or MathML
element name) is text, written back as ``&lt;``. What a browser hides but a reader
of the raw file sees (processing instructions, bogus comments and doctypes, end-tag
attributes, unknown attribute and element names) is a ``MARKUP`` unit, exposed as a
hidden segment; when a replacement touches one, the writer drops the whole construct
(:attr:`Unit.outer`) instead of patching it.
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
#: Markup a browser hides (PI, doctype, bogus comment, end-tag junk, unknown names):
#: verbatim, dropped as a whole (``Unit.outer``) when a replacement touches it.
MARKUP = "markup"

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

#: Element names a browser parses as tags (HTML, obsolete HTML, SVG, MathML), lower
#: case. Any other ``<name`` is text, except custom elements (``<mon-widget>``) and
#: namespaced ones (``<o:p>``, ``<st1:PersonName>`` in Office HTML).
_ELEMENTS = frozenset(
    str.split(
        """
    a abbr acronym address applet area article aside audio b base basefont bdi bdo
    bgsound big blink blockquote body br button canvas caption center cite code col
    colgroup command content data datalist dd del details dfn dialog dir div dl dt
    element em embed fieldset figcaption figure font footer form frame frameset h1 h2
    h3 h4 h5 h6 head header hgroup hr html i iframe image img input ins isindex kbd
    keygen label layer legend li link listing main map mark marquee menu menuitem meta
    meter multicol nav nextid nobr noembed noframes nolayer noscript object ol optgroup
    option output p param picture plaintext portal pre progress q rb rp rt rtc ruby s
    samp script search section select shadow slot small source spacer span strike
    strong style sub summary sup table tbody td template textarea tfoot th thead time
    title tr track tt u ul var video wbr xmp
    svg altglyph altglyphdef altglyphitem animate animatecolor animatemotion
    animatetransform circle clippath color-profile cursor defs desc discard ellipse
    feblend fecolormatrix fecomponenttransfer fecomposite feconvolvematrix
    fediffuselighting fedisplacementmap fedistantlight fedropshadow feflood fefunca
    fefuncb fefuncg fefuncr fegaussianblur feimage femerge femergenode femorphology
    feoffset fepointlight fespecularlighting fespotlight fetile feturbulence filter
    font-face font-face-format font-face-name font-face-src font-face-uri
    foreignobject g glyph glyphref hatch hatchpath hkern line lineargradient marker
    mask mesh meshgradient meshpatch meshrow metadata missing-glyph mpath path pattern
    polygon polyline radialgradient rect set solidcolor stop switch symbol text
    textpath tref tspan unknown use view vkern
    math maction annotation annotation-xml menclose merror mfenced mfrac mglyph mi
    mlabeledtr mmultiscripts mn mo mover mpadded mphantom mprescripts mroot mrow ms
    mspace msqrt mstyle msub msubsup msup mtable mtd mtext mtr munder munderover none
    semantics
    """
    )
)
_CUSTOM_ELEMENT = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)+")
_NAMESPACED = re.compile(r"([A-Za-z][A-Za-z0-9]*):[A-Za-z][A-Za-z0-9_.-]*")
#: Office HTML namespaces (``<o:p>``, ``v:shape``, ``w:st``...): fixed vocabularies,
#: their element and attribute names are never exposed.
_OFFICE_PREFIXES = re.compile(r"(?:o|v|w|m|x|dt|st\d+|xml|xmlns|xlink|xsl|html|svg)", re.I)
#: Attribute names of the HTML, SVG and MathML vocabularies (others are exposed).
_KNOWN_ATTRS = frozenset(
    str.split(
        """
    abbr accept accept-charset accesskey action align alink allow allowfullscreen alt
    archive async autocapitalize autocomplete autofocus autoplay axis background
    bgcolor border bordercolor cellpadding cellspacing char charoff charset checked
    cite class classid clear code codebase codetype color cols colspan compact content
    contenteditable controls coords crossorigin data datetime declare decoding default
    defer dir dirname disabled download draggable enctype enterkeyhint face for form
    formaction formenctype formmethod formnovalidate formtarget frame frameborder
    headers height hidden high href hreflang hspace http-equiv id inert inputmode
    integrity is ismap itemid itemprop itemref itemscope itemtype kind label lang
    language leftmargin link list loading longdesc loop low marginheight marginwidth max
    maxlength media method min minlength multiple muted name nohref nomodule nonce
    noresize noshade novalidate nowrap open optimum pattern ping placeholder playsinline
    popover poster preload profile readonly referrerpolicy rel required rev reversed
    role rows rowspan rules sandbox scheme scope scrolling selected shape size sizes
    slot span spellcheck src srcdoc srclang srcset standby start step style summary
    tabindex target text title topmargin translate type usemap valign value valuetype
    version vlink vspace width wrap xmlns
    d x y z cx cy r rx ry x1 x2 y1 y2 dx dy fx fy fr points transform viewbox
    preserveaspectratio fill fill-opacity fill-rule stroke stroke-width stroke-opacity
    stroke-linecap stroke-linejoin stroke-dasharray stroke-dashoffset stroke-miterlimit
    opacity offset stop-color stop-opacity gradientunits gradienttransform
    patternunits patterntransform clip-path clip-rule clippathunits mask maskunits filter
    font-family font-size font-style font-weight text-anchor dominant-baseline
    baseline-shift letter-spacing word-spacing text-decoration visibility display
    overflow color-interpolation-filters flood-color flood-opacity lighting-color
    in in2 result stddeviation operator k1 k2 k3 k4 mode values keytimes keysplines
    calcmode attributename attributetype begin dur end repeatcount repeatdur from to by
    additive accumulate rotate pathlength marker-start marker-mid marker-end markerwidth
    markerheight markerunits refx refy orient baseprofile focusable vector-effect
    shape-rendering image-rendering text-rendering mathvariant mathcolor mathsize
    mathbackground displaystyle scriptlevel stretchy symmetric fence separator lspace
    rspace largeop movablelimits accent accentunder linethickness columnalign rowalign
    notation encoding definitionurl
    """
    )
)
_KNOWN_ATTR_PATTERN = re.compile(r"aria-[a-z-]+|on[a-z]+")
#: Doctypes of the W3C/IETF vocabularies (``<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML
#: 1.0 Transitional//EN" "http://www.w3.org/...">``): nothing in them to expose.
_STANDARD_DOCTYPE = re.compile(
    r"doctype\s+html(?:\s+public\s+([\"'])-//(?:w3c|ietf)//[^\"']*\1"
    r"(?:\s+([\"'])https?://www\.w3\.org/[^\"']*\2)?"
    r"|\s+system\s+([\"'])about:legacy-compat\3)?\s*",
    re.I,
)

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
    #: MARKUP units: the source range removed when a replacement touches the unit.
    outer: tuple[int, int] = (0, 0)


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
        #: Source offsets of the ``<`` that open no element (text, escaped on write).
        self.text_lts: list[int] = []
        self._tokenize()

    # -- tokenizer -------------------------------------------------------------

    def _add(
        self, kind: str, start: int, end: int, tag: str, name: str = "", quote: str = ""
    ) -> None:
        self.units.append(Unit(len(self.units), kind, start, end, tag, name, quote))

    def _markup(self, start: int, end: int, outer: tuple[int, int], label: str) -> None:
        if self.text[start:end].strip():
            self.units.append(Unit(len(self.units), MARKUP, start, end, "", label, "", outer))

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
                # Doctype, processing instruction or bogus comment: ends at the first
                # '>' (as browsers do), hidden on screen but present in the file.
                flush(lt)
                end = src.find(">", lt + 2)
                stop = n if end == -1 else end
                pos = n if end == -1 else end + 1
                if not _STANDARD_DOCTYPE.fullmatch(src, lt + 2, stop):
                    self._markup(lt + 2, stop, (lt, pos), "balisage")
                text_start = pos
            elif nxt == "/" and lt + 2 < n and src[lt + 2].isascii() and src[lt + 2].isalpha():
                match = _TAG_NAME.match(src, lt + 2)
                assert match is not None
                if not _element_name(match.group(0)):
                    self.text_lts.append(lt)
                    pos = lt + 1
                    continue
                flush(lt)
                tag = match.group(0).lower()
                end = self._attributes(tag, match.end(), collect=False)
                if tag in _BLOCK_TAGS:
                    self._break()
                pos = min(end + 1, n)
                if _exposed_name(match.group(0)):
                    self._markup(lt + 2, match.end(), (lt, pos), "balise")
                else:
                    # Anything after the name of an end tag is ignored by browsers.
                    self._markup(match.end(), end, (match.end(), end), "balise")
                text_start = pos
            elif nxt.isascii() and nxt.isalpha():
                match = _TAG_NAME.match(src, lt + 1)
                assert match is not None
                if not _element_name(match.group(0)):
                    self.text_lts.append(lt)
                    pos = lt + 1
                    continue
                flush(lt)
                pos = self._start_tag(lt)
                text_start = pos
            else:
                pos = lt + 1
        flush(n)

    def _attributes(self, tag: str, pos: int, *, collect: bool = True) -> int:
        """Scan the attributes of a tag from ``pos``; returns the offset of its
        closing ``>`` (or the end of the text). ``collect``: add the attribute units
        (start tags; browsers ignore those of end tags)."""
        src = self.text
        n = len(src)
        while pos < n:
            while pos < n and (src[pos] in _SPACE or src[pos] == "/"):
                pos += 1
            if pos >= n or src[pos] == ">":
                break
            name_start = pos
            pos += 1  # the first character may be '='
            while pos < n and src[pos] not in _SPACE and src[pos] not in "/>=":
                pos += 1
            name_end = pos
            name = src[name_start:name_end].lower()
            scan = pos
            while scan < n and src[scan] in _SPACE:
                scan += 1
            value: tuple[int, int, str] | None = None
            if scan < n and src[scan] == "=":
                scan += 1
                while scan < n and src[scan] in _SPACE:
                    scan += 1
                if scan < n and src[scan] in "\"'":
                    quote = src[scan]
                    close = src.find(quote, scan + 1)
                    close = n if close == -1 else close
                    value = (scan + 1, close, quote)
                    pos = min(close + 1, n)
                else:
                    value_start = scan
                    while scan < n and src[scan] not in _SPACE and src[scan] != ">":
                        scan += 1
                    value = (value_start, scan, "")
                    pos = scan
            if collect:
                if value is not None:
                    self._attribute(tag, name, *value)
                if not _known_attribute(name):
                    self._markup(name_start, name_end, (name_start, pos), "nom d'attribut")
        return pos

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
        pos = min(self._attributes(tag, match.end()) + 1, n)  # skip '>'
        if _exposed_name(match.group(0)):
            # Dropping the whole tag wins over the patches of its attributes.
            self._markup(lt + 1, match.end(), (lt, pos), "balise")
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
            if unit.kind in (RAW, COMMENT, MARKUP):
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
            if unit.kind not in (ATTR, MARKUP):
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
            elif unit.kind == MARKUP:
                kind, label = SegmentKind.HIDDEN, unit.name
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
        patches: list[tuple[int, int, str]] = [(lt, lt + 1, "&lt;") for lt in self.text_lts]
        for index, unit_edits in edits.items():
            unit = self.units[index]
            if unit.kind == MARKUP:
                # Never patched: hidden markup holding a value goes away as a whole.
                patches.append((*unit.outer, ""))
                continue
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
        # Outer ranges first: a dropped construct swallows the patches inside it.
        patches.sort(key=lambda p: (p[0], -p[1]))
        out: list[str] = []
        cursor = 0
        for start, end, new in patches:
            if start < cursor:  # inside the previous patch (or shares a character reference)
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


def _element_name(name: str) -> bool:
    """True if a browser parses ``<name`` as an element (otherwise it is text)."""
    lower = name.lower()
    if lower in _ELEMENTS or _NAMESPACED.fullmatch(name):
        return True
    # Custom elements are written in lower case: ``<Jean-Pierre>`` is text.
    return name == lower and _CUSTOM_ELEMENT.fullmatch(name) is not None


def _exposed_name(name: str) -> bool:
    """An element name outside the known vocabularies (exposed for detection)."""
    if name.lower() in _ELEMENTS:
        return False
    namespaced = _NAMESPACED.fullmatch(name)
    return not (namespaced and _OFFICE_PREFIXES.fullmatch(namespaced.group(1)))


def _known_attribute(name: str) -> bool:
    if name in _KNOWN_ATTRS or _KNOWN_ATTR_PATTERN.fullmatch(name):
        return True
    namespaced = _NAMESPACED.fullmatch(name)
    return namespaced is not None and _OFFICE_PREFIXES.fullmatch(namespaced.group(1)) is not None


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
