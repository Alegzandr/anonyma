"""RTF tokenizer and document model shared by the RTF extractor and writer.

The source is read as latin-1 so that one character is one byte: every text
piece keeps its exact span and the writer splices replacements in place,
leaving formatting untouched.

Units (segments) are paragraphs of the body and of each destination (headers,
footnotes, annotations, field codes, document information, shape texts...).
Text written with ``\\deleted`` (tracked deletion) goes to separate units and is
removed by the writer, as are revision marks, rsids, the ``\\info`` group, user
properties and embedded OLE object data. Pictures become children.
"""

from __future__ import annotations

import codecs
import re
from dataclasses import dataclass, field

from anonyma.models import SegmentKind

TOKEN = re.compile(
    rb"\\([a-zA-Z]{1,32})(-?\d{1,10})? ?"  # control word (+ delimiter space)
    rb"|\\'([0-9a-fA-F]{2})"  # hex character
    rb"|\\(.)"  # control symbol
    rb"|([{}])"  # group
    rb"|([\r\n]+)"  # ignored line breaks
    rb"|([^\\{}\r\n]+)",  # text
    re.S,
)

CHARSETS = {
    0: "cp1252",
    1: "cp1252",
    2: "cp1252",
    77: "mac_roman",
    128: "cp932",
    129: "cp949",
    134: "cp936",
    136: "cp950",
    161: "cp1253",
    162: "cp1254",
    163: "cp1258",
    177: "cp1255",
    178: "cp1256",
    186: "cp1257",
    204: "cp1251",
    222: "cp874",
    238: "cp1250",
    254: "cp437",
}

PARAGRAPH_BREAKS = frozenset({"par", "sect", "page", "cell", "row", "nestcell", "nestrow"})
VIRTUAL_WORDS = {"tab": "\t", "line": "\n"}
CHAR_WORDS = {
    "emdash": "—",
    "endash": "–",
    "emspace": " ",
    "enspace": " ",
    "qmspace": " ",
    "bullet": "•",
    "lquote": "‘",
    "rquote": "’",
    "ldblquote": "“",
    "rdblquote": "”",
}
CHAR_SYMBOLS = {"~": "\u00a0", "_": "\u2011", "-": ""}

#: Destinations whose content is never text (fonts, binary data, tables...).
SKIP_DESTS = frozenset(
    {
        "colortbl",
        "stylesheet",
        "listtable",
        "listoverridetable",
        "rsidtbl",
        "objdata",
        "datastore",
        "themedata",
        "colorschememapping",
        "datafield",
        "blipuid",
        "fontemb",
        "fontfile",
        "panose",
        "generator",
        "xmlnstbl",
        "mmathPr",
        "latentstyles",
        "wgrffmtfilter",
        "pgdsctbl",
        "defchp",
        "defpap",
        "oldcprops",
        "oldpprops",
        "oldsprops",
        "oldtprops",
        "ftnsep",
        "ftnsepc",
        "aftnsep",
        "aftnsepc",
        "filetbl",
        "sn",
        "atnref",
        "atndate",
        "atntime",
        "bkmkend",
        "passwordhash",
        "falt",
        "listpicture",
        "pnseclvl",
    }
)
METADATA_DESTS = frozenset(
    {
        "title",
        "subject",
        "author",
        "manager",
        "company",
        "operator",
        "category",
        "keywords",
        "comment",
        "doccomm",
        "hlinkbase",
        "template",
        "propname",
        "staticval",
        "atnauthor",
        "atnid",
        "revtbl",
        "userprops",
    }
)
HEADER_DESTS = {
    "header": "en-tête",
    "headerl": "en-tête",
    "headerr": "en-tête",
    "headerf": "en-tête",
    "footer": "pied de page",
    "footerl": "pied de page",
    "footerr": "pied de page",
    "footerf": "pied de page",
}
CONTAINER_DESTS = frozenset(
    {"info", "object", "shp", "shpinst", "sp", "field", "result", "shppict"}
)
#: Groups replaced wholesale by the writer (metadata purge).
PURGE_GROUPS = {
    "info": b"{\\info}",
    "userprops": b"",
    "revtbl": b"{\\*\\revtbl {Unknown;}}",
    "atnauthor": b"{\\*\\atnauthor Anonyme}",
    "atnid": b"{\\*\\atnid A}",
    "atndate": b"",
    "atntime": b"",
    "rsidtbl": b"",
    "docvar": b"",
    "oldcprops": b"",
    "oldpprops": b"",
    "oldsprops": b"",
    "oldtprops": b"",
    "objdata": b"",
    "template": b"",
}
#: Control words removed by the writer (revision marks and rsids).
STRIP_WORDS = frozenset(
    {
        "revised",
        "revauth",
        "revdttm",
        "revauthdel",
        "revdttmdel",
        "crauth",
        "crdate",
        "deleted",
        "rsidroot",
    }
)
BLIPS = {
    "pngblip": ".png",
    "jpegblip": ".jpg",
    "emfblip": ".emf",
    "wmetafile": ".wmf",
    "dibitmap": ".bmp",
    "wbitmap": ".bmp",
    "macpict": ".pict",
}
BLIP_WORD = {
    ".png": b"\\pngblip",
    ".jpg": b"\\jpegblip",
    ".jpeg": b"\\jpegblip",
    ".emf": b"\\emfblip",
}
BLIP_WORD[".wmf"] = b"\\wmetafile8"


@dataclass(slots=True)
class RtfPiece:
    start: int
    end: int
    text: str = field(repr=False)
    editable: bool = True
    uc: int = 1


@dataclass(slots=True)
class RtfUnit:
    kind: SegmentKind
    label: str
    pieces: list[RtfPiece] = field(default_factory=list)
    deleted: bool = False

    @property
    def text(self) -> str:
        return "".join(p.text for p in self.pieces)


@dataclass(slots=True)
class RtfPicture:
    start: int
    end: int = 0
    ext: str = ".bin"
    controls: list[bytes] = field(default_factory=list)
    hex_chunks: list[bytes] = field(default_factory=list, repr=False)
    binary: bytearray = field(default_factory=bytearray, repr=False)

    def data(self) -> bytes:
        if self.binary:
            return bytes(self.binary)
        hexdata = re.sub(rb"[^0-9a-fA-F]", b"", b"".join(self.hex_chunks))
        if len(hexdata) % 2:
            hexdata = hexdata[:-1]
        return bytes.fromhex(hexdata.decode("ascii"))


@dataclass(slots=True)
class RtfDocument:
    units: list[RtfUnit]
    pictures: list[RtfPicture]
    #: Writer edits for the metadata purge: ``(start, end, replacement)``.
    purge: list[tuple[int, int, bytes]]
    objects: int


@dataclass(slots=True)
class _State:
    inst: int
    dest: str
    kind: SegmentKind
    label: str
    uc: int = 1
    font: int = -1
    deleted: bool = False
    skip: bool = False
    intbl: bool = False
    fresh: bool = True
    star: bool = False
    start: int = 0
    picture: RtfPicture | None = None
    in_fonttbl: bool = False

    def child(self, start: int) -> _State:
        return _State(
            inst=self.inst,
            dest=self.dest,
            kind=self.kind,
            label=self.label,
            uc=self.uc,
            font=self.font,
            deleted=self.deleted,
            skip=self.skip,
            intbl=self.intbl,
            fresh=True,
            star=False,
            start=start,
            picture=self.picture,
            in_fonttbl=self.in_fonttbl,
        )


def _signed(value: int) -> int:
    return value + 65536 if value < 0 else value


class _Parser:
    def __init__(self, raw: bytes) -> None:
        self.raw = raw
        self.units: dict[tuple[int, int, bool], RtfUnit] = {}
        self.paragraph: dict[int, int] = {}
        self.pictures: list[RtfPicture] = []
        self.purge: list[tuple[int, int, bytes]] = []
        self.objects = 0
        self.next_inst = 1
        self.codepage = "cp1252"
        self.fonts: dict[int, str] = {}
        self.font_being_defined = -1
        self.default_font = -1
        self.decoder: codecs.IncrementalDecoder | None = None
        self.decoder_cp = ""
        self.pending_start = -1
        self.skip_fallback = 0
        self.high_surrogate: tuple[int, int] | None = None

    # -- text output ----------------------------------------------------------

    def unit(self, state: _State) -> RtfUnit:
        para = self.paragraph.get(state.inst, 0)
        key = (state.inst, para, state.deleted)
        unit = self.units.get(key)
        if unit is None:
            kind, label = state.kind, state.label
            if state.deleted:
                kind, label = SegmentKind.COMMENT, "texte supprimé (révision)"
            elif kind is SegmentKind.BODY and state.intbl and state.dest == "":
                kind, label = SegmentKind.CELL, "tableau"
            unit = self.units[key] = RtfUnit(kind, label, deleted=state.deleted)
        return unit

    def emit(self, state: _State, start: int, end: int, text: str, editable: bool = True) -> None:
        if state.skip or state.picture is not None or state.in_fonttbl:
            return
        self.unit(state).pieces.append(RtfPiece(start, end, text, editable, state.uc))

    def codepage_for(self, state: _State) -> str:
        font = state.font if state.font >= 0 else self.default_font
        return self.fonts.get(font, self.codepage)

    def flush_bytes(self) -> None:
        self.decoder = None
        self.pending_start = -1

    def emit_byte(self, state: _State, start: int, end: int, value: int) -> None:
        cp = self.codepage_for(state)
        if self.decoder is None or self.decoder_cp != cp:
            try:
                self.decoder = codecs.getincrementaldecoder(cp)(errors="replace")
            except LookupError:
                self.decoder = codecs.getincrementaldecoder("cp1252")(errors="replace")
            self.decoder_cp = cp
            self.pending_start = -1
        if self.pending_start < 0:
            self.pending_start = start
        text = self.decoder.decode(bytes([value]))
        if text:
            self.emit(state, self.pending_start, end, text)
            self.pending_start = -1

    def break_paragraph(self, state: _State) -> None:
        self.paragraph[state.inst] = self.paragraph.get(state.inst, 0) + 1

    # -- destinations ---------------------------------------------------------

    def open_destination(self, state: _State, word: str) -> None:
        state.dest = word
        if word == "pict":
            state.picture = RtfPicture(start=state.start)
            return
        if word in SKIP_DESTS:
            state.skip = True
            if word == "objdata":
                self.objects += 1
            return
        if word == "fonttbl":
            state.in_fonttbl = True
            return
        if word in CONTAINER_DESTS or word == "fldrslt":
            if word == "info":
                state.kind, state.label = SegmentKind.METADATA, "info"
            return
        state.inst = self.next_inst
        self.next_inst += 1
        if word in METADATA_DESTS:
            state.kind, state.label = SegmentKind.METADATA, word
        elif word in HEADER_DESTS:
            state.kind, state.label = SegmentKind.HEADER, HEADER_DESTS[word]
        elif word == "footnote":
            state.kind, state.label = SegmentKind.NOTE, "note de bas de page"
        elif word == "annotation":
            state.kind, state.label = SegmentKind.COMMENT, "commentaire"
        elif word == "fldinst":
            state.kind, state.label = SegmentKind.ATTRIBUTE, "code de champ"
        elif word == "shptxt":
            state.kind, state.label = SegmentKind.BODY, "zone de texte"
        elif word == "sv":
            state.kind, state.label = SegmentKind.ATTRIBUTE, "propriété de forme"
        elif word == "bkmkstart":
            state.kind, state.label = SegmentKind.HIDDEN, "signet"
        elif word in ("listtext", "pntext", "pntxta", "pntxtb"):
            state.kind, state.label = SegmentKind.HIDDEN, "numérotation"
        else:
            state.kind, state.label = SegmentKind.HIDDEN, word

    # -- main loop ------------------------------------------------------------

    def parse(self) -> RtfDocument:
        raw = self.raw
        n = len(raw)
        stack: list[_State] = []
        state = _State(inst=0, dest="", kind=SegmentKind.BODY, label="corps")
        pos = 0
        while pos < n:
            match = TOKEN.match(raw, pos)
            if match is None:  # lone backslash at EOF
                break
            start, end = match.start(), match.end()
            pos = end
            word, param, hexchar, symbol, group, newline, text = match.groups()
            if newline is not None:
                continue
            if hexchar is None:
                self.flush_bytes()
            if group == b"{":
                stack.append(state)
                state = state.child(start)
                continue
            if group == b"}":
                self.close_group(state, end)
                if stack:
                    state = stack.pop()
                continue
            if word is not None:
                pos = self.control_word(state, word.decode(), param, start, end, pos)
                continue
            if symbol is not None:
                self.control_symbol(state, symbol.decode("latin-1"), start, end)
                continue
            if hexchar is not None:
                state.fresh = False
                if self.consume_fallback(1):
                    self.extend_last(state, end)
                    continue
                if state.picture is not None:
                    continue
                self.emit_byte(state, start, end, int(hexchar, 16))
                continue
            if text is not None:
                state.fresh = False
                self.text(state, text, start, end)
        units = [u for u in self.units.values() if u.text.strip()]
        return RtfDocument(units, self.pictures, self.purge, self.objects)

    def consume_fallback(self, count: int) -> bool:
        if self.skip_fallback <= 0:
            return False
        self.skip_fallback -= count
        return True

    def extend_last(self, state: _State, end: int) -> None:
        """Include a \\u fallback character in the span of the preceding piece."""
        if state.skip or state.picture is not None or state.in_fonttbl:
            return
        unit = self.unit(state)
        if unit.pieces:
            unit.pieces[-1].end = end

    def text(self, state: _State, data: bytes, start: int, end: int) -> None:
        if state.picture is not None:
            if state.dest == "pict":
                state.picture.hex_chunks.append(data)
            return
        if state.in_fonttbl:
            return
        offset = 0
        while self.skip_fallback > 0 and offset < len(data):
            self.skip_fallback -= 1
            offset += 1
        if offset:
            self.extend_last(state, start + offset)
            data = data[offset:]
            start += offset
            if not data:
                return
        cp = self.codepage_for(state)
        try:
            decoded = data.decode(cp, errors="replace")
        except LookupError:
            decoded = data.decode("cp1252", errors="replace")
        self.emit(state, start, end, decoded)

    def control_symbol(self, state: _State, symbol: str, start: int, end: int) -> None:
        if symbol == "*":
            state.star = True
            return
        state.fresh = False
        if self.consume_fallback(1):
            self.extend_last(state, end)
            return
        if symbol in ("\\", "{", "}"):
            self.emit(state, start, end, symbol)
        elif symbol in CHAR_SYMBOLS:
            if CHAR_SYMBOLS[symbol]:
                self.emit(state, start, end, CHAR_SYMBOLS[symbol])
        elif symbol in ("\n", "\r"):
            self.break_paragraph(state)

    def control_word(
        self, state: _State, word: str, param: bytes | None, start: int, end: int, pos: int
    ) -> int:
        value = int(param) if param is not None else None
        if state.fresh:
            state.fresh = False
            if (
                state.star
                or word in HEADER_DESTS
                or word in METADATA_DESTS
                or word
                in (
                    SKIP_DESTS
                    | CONTAINER_DESTS
                    | {
                        "pict",
                        "footnote",
                        "annotation",
                        "fldinst",
                        "fldrslt",
                        "shptxt",
                        "sv",
                        "info",
                    }
                    | {"listtext", "pntext", "pntxta", "pntxtb", "fonttbl", "bkmkstart", "docvar"}
                )
            ):
                self.open_destination(state, word)
        if word == "bin" and value is not None:
            data = self.raw[pos : pos + max(value, 0)]
            if state.picture is not None:
                state.picture.binary.extend(data)
            return pos + max(value, 0)
        if state.picture is not None and state.dest == "pict":
            if word in BLIPS:
                state.picture.ext = BLIPS[word]
            elif not word.startswith("bliptag"):
                state.picture.controls.append(self.raw[start:end])
            return pos
        if word == "u" and value is not None:
            self.unicode_char(state, _signed(value), start, end)
            return pos
        self.skip_fallback = min(self.skip_fallback, 0)
        if word in STRIP_WORDS or "rsid" in word:
            self.purge.append((start, end, b""))
        if word == "deleted":
            state.deleted = value != 0
        elif word == "plain":
            state.deleted = False
        elif word == "uc" and value is not None:
            state.uc = max(0, value)
        elif word == "ansicpg" and value is not None:
            self.codepage = f"cp{value}"
        elif word == "mac":
            self.codepage = "mac_roman"
        elif word == "pc":
            self.codepage = "cp437"
        elif word == "pca":
            self.codepage = "cp850"
        elif word == "deff" and value is not None:
            self.default_font = value
        elif word == "f" and value is not None:
            if state.in_fonttbl:
                self.font_being_defined = value
            else:
                state.font = value
        elif word == "fcharset" and value is not None and state.in_fonttbl:
            self.fonts[self.font_being_defined] = CHARSETS.get(value, self.codepage)
        elif word == "intbl":
            state.intbl = True
        elif word == "pard":
            state.intbl = False
        elif word in PARAGRAPH_BREAKS:
            self.break_paragraph(state)
        elif word in VIRTUAL_WORDS:
            self.emit(state, start, end, VIRTUAL_WORDS[word], editable=False)
        elif word in CHAR_WORDS:
            self.emit(state, start, end, CHAR_WORDS[word])
        return pos

    def unicode_char(self, state: _State, code: int, start: int, end: int) -> None:
        self.skip_fallback = state.uc
        if 0xD800 <= code < 0xDC00:
            self.high_surrogate = (code, start)
            self.emit(state, start, end, "")
            return
        if 0xDC00 <= code < 0xE000 and self.high_surrogate is not None:
            high, high_start = self.high_surrogate
            self.high_surrogate = None
            char = chr(0x10000 + ((high - 0xD800) << 10) + (code - 0xDC00))
            if not (state.skip or state.picture is not None or state.in_fonttbl):
                unit = self.unit(state)
                if unit.pieces and unit.pieces[-1].start == high_start:
                    unit.pieces[-1].text = char
                    unit.pieces[-1].end = end
                    return
            self.emit(state, start, end, char)
            return
        self.high_surrogate = None
        self.emit(state, start, end, chr(code))

    def close_group(self, state: _State, end: int) -> None:
        self.flush_bytes()
        if state.picture is not None and state.dest == "pict":
            state.picture.end = end
            self.pictures.append(state.picture)
        if state.dest in PURGE_GROUPS:
            self.purge.append((state.start, end, PURGE_GROUPS[state.dest]))


def parse_rtf(raw: bytes) -> RtfDocument:
    return _Parser(raw).parse()


def encode_text(text: str, uc: int) -> bytes:
    """RTF-escape ``text``; non-ASCII as ``\\uN`` with ``uc`` fallback characters."""
    out: list[str] = []
    for ch in text:
        code = ord(ch)
        if ch in "\\{}":
            out.append("\\" + ch)
        elif ch == "\t":
            out.append("\\tab ")
        elif ch == "\n":
            out.append("\\line ")
        elif 32 <= code < 128:
            out.append(ch)
        else:
            units = (
                [code]
                if code <= 0xFFFF
                else [0xD800 + ((code - 0x10000) >> 10), 0xDC00 + ((code - 0x10000) & 0x3FF)]
            )
            for unit in units:
                signed = unit - 65536 if unit > 32767 else unit
                out.append(f"\\u{signed}" + ("?" * uc if uc else " "))
    return "".join(out).encode("ascii")
