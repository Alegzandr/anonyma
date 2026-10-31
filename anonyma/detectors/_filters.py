"""Precision filters shared by the detectors (helpers, not a plugin).

* PDF content-stream syntax (``0 0 0 rg /Helv 0 Tf``) is never personal data.
* File names are fed to the ML detectors as words: separators and the extension
  become spaces (offsets unchanged), so ``message-joint-2.eml`` reads
  ``message joint 2``.
* Model spans are checked for plausibility per type: no lone punctuation, no
  technical tokens (fonts, software, placeholders), no single common word for
  name-like types, identifiers that look like identifiers, numbers with digits.
"""

from __future__ import annotations

import bisect
import ipaddress
import re
from functools import lru_cache

from anonyma.data import first_name, is_common_word, lexicon
from anonyma.models import EntityType, Segment, SegmentKind
from anonyma.names import civility_of
from anonyma.textutils import normalize

_PDF_TOKEN = re.compile(
    r"^(?:[-+]?\d*\.?\d+|/[^\s/\[\]()<>]+|\[|\]|<<|>>|"
    r"(?:b\*?|B\*?|BDC|BI|BMC|BT|BX|c|cm|CS|cs|d|d0|d1|Do|DP|EI|EMC|ET|EX|f\*?|F|g|G|gs|h|i|"
    r"ID|j|J|k|K|l|m|M|MP|n|q|Q|re|RG|rg|ri|s|S|SC|sc|SCN|scn|sh|T\*|Tc|Td|TD|Tf|Tj|TJ|TL|"
    r"Tm|Tr|Ts|Tw|Tz|v|w|W\*?|y|R|obj|endobj))$"
)


def is_pdf_operators(text: str) -> bool:
    """True for PDF content-stream / default-appearance syntax."""
    tokens = text.split()
    if len(tokens) < 3:
        return False
    hits = sum(1 for t in tokens if _PDF_TOKEN.match(t))
    return hits / len(tokens) >= 0.8


def skip_segment(segment: Segment) -> bool:
    """Segments no detector should look at."""
    label = normalize(segment.context.label)
    return "objet pdf" in label and is_pdf_operators(segment.text)


_SEPARATORS = re.compile(r"[/\\_\-.\s]+")
_EXTENSION = re.compile(r"\.[A-Za-z0-9]{1,6}$")


def filename_as_text(name: str) -> str:
    """Same length as ``name``: separators and the extension replaced by spaces."""
    chars = list(name)
    match = _EXTENSION.search(name)
    if match:
        for i in range(match.start(), match.end()):
            chars[i] = " "
    for m in _SEPARATORS.finditer(name):
        for i in range(m.start(), m.end()):
            chars[i] = " "
    return "".join(chars)


def ml_text(segment: Segment) -> str:
    """Text given to the ML detectors (file names rewritten as words)."""
    if segment.context.kind is SegmentKind.FILENAME:
        return filename_as_text(segment.text)
    return segment.text


#: Normalized tokens that are never personal data on their own.
TECHNICAL = frozenset(
    normalize(w)
    for w in [
        "Calibri",
        "Cambria",
        "Arial",
        "Helvetica",
        "Helv",
        "Times",
        "Courier",
        "Verdana",
        "Tahoma",
        "Georgia",
        "Garamond",
        "Consolas",
        "Segoe",
        "Symbol",
        "Wingdings",
        "Webdings",
        "Liberation",
        "DejaVu",
        "Noto",
        "Roboto",
        "OpenSans",
        "Montserrat",
        "Lato",
        "Aptos",
        "Candara",
        "Corbel",
        "Constantia",
        "Trebuchet",
        "Impact",
        "Comic",
        "Sans",
        "Serif",
        "Mono",
        "Monospace",
        "ZapfDingbats",
        "Unknown",
        "Inconnu",
        "Anonyme",
        "Anonymous",
        "User",
        "Utilisateur",
        "Admin",
        "Administrator",
        "Administrateur",
        "Root",
        "Guest",
        "Invite",
        "Person",
        "Personne",
        "Owner",
        "Author",
        "Auteur",
        "Default",
        "Normal",
        "Standard",
        "Heading",
        "Title",
        "Titre",
        "Subtitle",
        "Untitled",
        "Microsoft",
        "Office",
        "Word",
        "Excel",
        "PowerPoint",
        "Outlook",
        "Windows",
        "LibreOffice",
        "OpenOffice",
        "Writer",
        "Calc",
        "Impress",
        "Adobe",
        "Acrobat",
        "Distiller",
        "MuPDF",
        "PDFium",
        "Ghostscript",
        "pdfmake",
        "iText",
        "iTextSharp",
        "wkhtmltopdf",
        "PDFsharp",
        "ReportLab",
        "TCPDF",
        "FPDF",
        "dompdf",
        "mPDF",
        "PDFKit",
        "pdfTeX",
        "LuaTeX",
        "XeTeX",
        "Aspose",
        "Skia/PDF",
        "PDFlib",
        "PDFCreator",
        "PScript5",
        "Quartz",
        "PrinterName",
        "Printer",
        "Imprimante",
        "Google",
        "Docs",
        "Sheets",
        "Apple",
        "Keynote",
        "Mozilla",
        "Thunderbird",
        "Firefox",
        "Chrome",
        "Gmail",
        "Exchange",
        "Python",
        "Java",
        "JavaScript",
        "HTML",
        "CSS",
        "XML",
        "JSON",
        "UTF",
        "ASCII",
        "Unicode",
        "Arial-BoldMT",
        "TimesNewRoman",
        "TimesNewRomanPSMT",
        "Scanner",
        "Scan",
        "Numériseur",
        "Copieur",
    ]
)

_LONE_PUNCT = re.compile(r"^\W+$")
_IDENT = re.compile(r"^@?[\w.\-+]{2,64}$")
_DOMAIN = re.compile(r"^(?:[\w\-]+\.)+[a-z]{2,6}$", re.IGNORECASE)
_FILE = re.compile(r"\.[A-Za-z0-9]{2,5}$")
_PLATE = re.compile(r"^(?:[A-Z]{2}[- ]?\d{3}[- ]?[A-Z]{2}|\d{1,4}[- ]?[A-Z]{1,3}[- ]?\d{2,3})$")

#: Minimum score per type for model spans (recall first: low, but not noise level).
MIN_SCORE: dict[str, float] = {
    EntityType.JOB_TITLE: 0.6,
    EntityType.USERNAME: 0.55,
    EntityType.ORGANIZATION: 0.4,
    EntityType.LOCATION: 0.4,
    EntityType.ADDRESS: 0.4,
}
#: Single common words are only kept for name-like types above this score
#: (or when they are frequent given names for persons).
COMMON_WORD_SCORE = 0.85
NAME_LIKE = frozenset(
    {EntityType.PERSON, EntityType.ORGANIZATION, EntityType.LOCATION, EntityType.ADDRESS}
)
NUMERIC_MIN_DIGITS: dict[str, int] = {
    EntityType.PHONE: 8,
    EntityType.IBAN: 12,
    EntityType.NIR: 13,
    EntityType.CREDIT_CARD: 12,
    EntityType.CASE_NUMBER: 2,
}


def _words(value: str) -> list[str]:
    return re.findall(r"[^\W\d_]+(?:['’][^\W\d_]+)*", value)


#: Grammatical words that never carry a name on their own.
FUNCTION_WORDS = frozenset(
    [
        "le",
        "la",
        "les",
        "l",
        "un",
        "une",
        "des",
        "du",
        "de",
        "d",
        "au",
        "aux",
        "et",
        "ou",
        "en",
        "a",
        "the",
        "of",
        "and",
        "an",
    ]
)


def _function_word(word: str) -> bool:
    return normalize(word) in FUNCTION_WORDS or civility_of(word) is not None


def _all_common(value: str) -> bool:
    """Every word is a common word or a grammatical one (« Bien à vous »)."""
    words = _words(value)
    return bool(words) and all(is_common_word(w) or normalize(w) in FUNCTION_WORDS for w in words)


#: Weekdays and months: never a name, a place or an organisation on their own.
CALENDAR_WORDS = frozenset(
    {"lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche", "janvier",
     "fevrier", "mars", "avril", "mai", "juin", "juillet", "aout", "septembre", "octobre",
     "novembre", "decembre"}
)  # fmt: skip


@lru_cache(maxsize=1)
def _sorted_lexicon() -> list[str]:
    return sorted(lexicon())


def truncated_common_word(word: str) -> bool:
    """``Courrie`` (OCR cut of ``courrier``): a common word missing 1-2 letters."""
    key = normalize(word)
    if len(key) < 5:
        return False
    words = _sorted_lexicon()
    i = bisect.bisect_left(words, key)
    while i < len(words) and words[i].startswith(key):
        if 0 < len(words[i]) - len(key) <= 2:
            return True
        i += 1
    return False


_SENTENCE_END = '.!?:;\n\r•·-–—*>"«(['


def mid_sentence(text: str, start: int) -> bool:
    """True if ``text[start]`` is preceded by a word, not by a sentence start."""
    i = start - 1
    while i >= 0 and text[i] in " \t\u00a0\u202f":
        i -= 1
    return i >= 0 and text[i] not in _SENTENCE_END


def plausible(
    entity_type: str,
    value: str,
    score: float,
    *,
    filename: bool = False,
    ocr: bool = False,
    capitalized_mid_sentence: bool = False,
) -> bool:
    """Whether a model span of ``entity_type`` is worth keeping."""
    stripped = value.strip()
    if len(stripped) < 2 or _LONE_PUNCT.match(stripped):
        return False
    words_ = _words(stripped)
    if (
        ocr
        and len(words_) == 1
        and entity_type in NAME_LIKE
        and first_name(words_[0]) is None
        and truncated_common_word(words_[0])
        and score < COMMON_WORD_SCORE
    ):
        return False
    if score < MIN_SCORE.get(entity_type, 0.0):
        return False
    key = normalize(stripped)
    words = _words(stripped)
    if key in TECHNICAL or (words and all(normalize(w) in TECHNICAL for w in words)):
        return False
    if (
        entity_type in NAME_LIKE
        and words
        and all(normalize(w) in CALENDAR_WORDS for w in words)
        and not (entity_type == EntityType.PERSON and any(first_name(w) for w in words))
    ):
        return False  # « Lundi », « Mars 2024 » (« Avril » may still be a first name)
    if entity_type in NAME_LIKE or entity_type == EntityType.JOB_TITLE:
        if not words:
            return False
        # Words that carry the name: determiners, prepositions and civilities left out.
        content = [w for w in words if not _function_word(w)]
        lowercase_common = bool(content) and all(
            not w[0].isupper() and is_common_word(w) for w in content
        )
        if entity_type == EntityType.PERSON:
            if not content:
                return False  # "Mme", "Madame, Monsieur": civilities alone
            if lowercase_common:
                return False  # "Un martin-pêcheur": ordinary words, whatever the score
            if not any(w[0].isupper() for w in content) and score < COMMON_WORD_SCORE:
                return False  # "vu zephyrin" lower-case fragments need high confidence
            if _all_common(stripped) and not any(
                (r := first_name(w)) is not None and r.count >= 2000 for w in words
            ):
                # "à Berger, qui...": a capitalized common word inside a sentence is
                # most likely a surname; at a sentence start it needs high confidence.
                if capitalized_mid_sentence and stripped[0].isupper():
                    return True
                return score >= COMMON_WORD_SCORE and len(words) > 1
        elif entity_type == EntityType.JOB_TITLE:
            if len(words) == 1 and not stripped[0].islower() and score < COMMON_WORD_SCORE:
                return False  # "Rappeler", "Signature": capitalized single words
        elif entity_type == EntityType.LOCATION and len(words) == 1 and stripped[0].isupper():
            pass  # cities are often common words too (Nice, Tours, Quimper...)
        elif entity_type in (EntityType.ORGANIZATION, EntityType.LOCATION) and lowercase_common:
            return False  # "comité de direction", "salle du rez-de-chaussée": not proper names
        elif _all_common(stripped) and score < COMMON_WORD_SCORE:
            return False
    if entity_type == EntityType.USERNAME:
        if filename or " " in stripped or "\\" in stripped or "/" in stripped:
            return False
        if not _IDENT.match(stripped) or _DOMAIN.match(stripped) or _FILE.search(stripped):
            return False
        if is_common_word(stripped):
            return False
    if entity_type == EntityType.IP_ADDRESS:
        try:
            ipaddress.ip_address(stripped.split("/")[0].split("%")[0])
        except ValueError:
            return False
    if entity_type == EntityType.EMAIL and "@" not in stripped:
        return False
    if entity_type == EntityType.LICENSE_PLATE and not _PLATE.match(stripped.upper()):
        return False
    minimum = NUMERIC_MIN_DIGITS.get(entity_type)
    return minimum is None or sum(c.isdigit() for c in stripped) >= minimum


__all__ = [
    "CALENDAR_WORDS",
    "TECHNICAL",
    "filename_as_text",
    "is_pdf_operators",
    "mid_sentence",
    "ml_text",
    "plausible",
    "skip_segment",
]
