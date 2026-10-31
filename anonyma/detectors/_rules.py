"""Declarative regex rules of the French LIGHT detector (helpers, not a plugin).

Each :class:`Rule` becomes a Presidio ``PatternRecognizer``; validation (checksums),
context boosting and span adjustments are applied afterwards by
:mod:`anonyma.detectors.regex_fr`, so that a checksum failure can still be kept at a
low score when strong context words are present (recall first).

Regexes use the ``regex`` module syntax (variable-length look-behinds).
"""

from __future__ import annotations

import ipaddress
from collections.abc import Callable
from dataclasses import dataclass, field

import phonenumbers
import regex

from anonyma import checksums
from anonyma.models import EntityType, SegmentKind

#: Separators allowed inside numbers (space, tab, dot, hyphen, no-break spaces).
SEP = r"[ \t.\-\u00a0\u202f]"
SP = r"[ \t\u00a0\u202f]"
#: Separators seen inside IBANs (also inside RIB-style groupings).
IBAN_SEP = r"[ \t.\-\u00a0\u202f]"
#: Classic IBAN layout (groups of four): bounds a checksum-invalid IBAN candidate.
IBAN_STRICT = regex.compile(
    r"[A-Z]{2}\d{2}(?:" + SP + r"?[A-Z0-9]{4}){2,7}(?:" + SP + r"?[A-Z0-9]{1,4})?(?![A-Za-z0-9])",
    regex.IGNORECASE,
)

Validator = Callable[[str], bool]


@dataclass(frozen=True, slots=True)
class Rule:
    name: str
    entity_type: str
    pattern: str
    score: float
    #: Normalized (accent-free, lower-case) words or phrases looked up around the match.
    context: tuple[str, ...] = ()
    #: Score when a context word is found (defaults to max(score, 0.95)).
    context_score: float | None = None
    #: Drop matches without context.
    needs_context: bool = False
    validator: Validator | None = None
    #: Score kept when the validator fails but context is present (None = drop).
    invalid_score: float | None = None
    flags: int = regex.MULTILINE
    #: Segment kinds where the rule is skipped.
    skip_kinds: frozenset[SegmentKind] = field(default_factory=frozenset)
    #: Context window sizes (characters before / after the match).
    before: int = 48
    after: int = 16
    #: Strip trailing punctuation from the match (URLs).
    trim_trailing: bool = False
    #: Pattern (same flags) found in every text the rule can match: a cheap necessary
    #: condition checked first, so that slow keyword-led rules skip most segments.
    prefilter: str = ""


# -- validators -----------------------------------------------------------------


def _fr_phone(value: str) -> bool:
    d = checksums.digits(value.replace("(0)", ""))
    if d.startswith("0033"):
        d = "0" + d[4:]
    elif d.startswith("33") and len(d) == 11:
        d = "0" + d[2:]
    return len(d) == 10 and d[0] == "0" and d[1] != "0"


def _intl_number(value: str) -> phonenumbers.PhoneNumber | None:
    d = checksums.digits(value.replace("(0)", ""))
    if value.lstrip().startswith("00"):
        d = d[2:]
    if d.startswith("33"):
        return None  # handled by the French rule
    try:
        return phonenumbers.parse("+" + d, None)
    except phonenumbers.NumberParseException:
        return None


def _intl_phone(value: str) -> bool:
    number = _intl_number(value)
    return number is not None and bool(phonenumbers.is_valid_number(number))


def _intl_phone_possible(value: str) -> bool:
    """Right length for its country but outside allocated ranges (reserved, fiction...)."""
    number = _intl_number(value)
    return (
        number is not None
        and not phonenumbers.is_valid_number(number)
        and bool(phonenumbers.is_possible_number(number))
        and len(str(number.national_number)) >= 8
    )


def _card(value: str) -> bool:
    d = checksums.digits(value)
    return 13 <= len(d) <= 19 and d[0] in "123456" and checksums.luhn_valid(d)


def _card_not_siret(value: str) -> bool:
    return _card(value) and len(checksums.digits(value)) != 14


def _ipv4(value: str) -> bool:
    try:
        ip = ipaddress.IPv4Address(value)
    except ValueError:
        return False
    return not (ip.is_unspecified or ip.is_loopback or str(ip) == "255.255.255.255")


def _ipv6(value: str) -> bool:
    if value.count(":") < 2:
        return False
    try:
        ip = ipaddress.IPv6Address(value.split("%", maxsplit=1)[0])
    except ValueError:
        return False
    return not (ip.is_unspecified or ip.is_loopback)


def _nir_with_key(value: str) -> bool:
    return checksums.nir_valid(value)


def _has_digit(value: str) -> bool:
    return any(c.isdigit() for c in value)


def _bic(value: str) -> bool:
    return value[4:6] in BIC_COUNTRIES


#: Top-level domains that real addresses use (generic, sponsored and the most common
#: new gTLDs): needed beyond :data:`MAX_FREE_TLD` letters.
LONG_TLDS = frozenset(
    {
        "aero", "agency", "app", "archi", "art", "asia", "associates", "attorney", "auction",
        "audio", "auto", "band", "bank", "bar", "beer", "best", "bike", "bio", "biz", "blog",
        "boutique", "build", "business", "cab", "cafe", "camp", "care", "careers", "cash", "center",
        "chat", "cheap", "church", "city", "claims", "cleaning", "click", "clinic", "clothing",
        "cloud", "club", "coach", "codes", "coffee", "college", "community", "company", "computer",
        "construction", "consulting", "contact", "contractors", "cooking", "coop", "corsica",
        "country", "coupons", "credit", "creditcard", "cruises", "dance", "dating", "deals",
        "degree", "delivery", "dental", "design", "dev", "diamonds", "digital", "direct",
        "directory", "discount", "domains", "education", "email", "energy", "engineering",
        "enterprises", "equipment", "estate", "events", "exchange", "expert", "exposed", "express",
        "fail", "family", "farm", "fashion", "finance", "financial", "fish", "fitness", "flights",
        "florist", "football", "foundation", "fund", "furniture", "futbol", "gallery", "games",
        "garden", "gift", "gifts", "gives", "glass", "global", "gmbh", "gold", "golf", "gouv",
        "graphics", "gratis", "green", "gripe", "group", "guide", "guru", "health", "healthcare",
        "help", "hockey", "holdings", "holiday", "homes", "hospital", "host", "house", "immo",
        "immobilien", "industries", "info", "institute", "insure", "international", "investments",
        "jobs", "kitchen", "land", "lawyer", "lease", "legal", "life", "lighting", "limited",
        "limo", "link", "live", "loans", "ltd", "luxury", "management", "market", "marketing",
        "mba", "media", "memorial", "menu", "mobi", "moda", "money", "mortgage", "museum", "name",
        "network", "news", "ninja", "online", "paris", "partners", "parts", "photo", "photography",
        "photos", "pics", "pictures", "pizza", "place", "plumbing", "plus", "press", "pro",
        "productions", "properties", "property", "pub", "recipes", "red", "rehab", "reise",
        "reisen", "rentals", "repair", "report", "rest", "restaurant", "review", "reviews", "rocks",
        "run", "sale", "salon", "sarl", "school", "schule", "science", "services", "shoes", "shop",
        "shopping", "show", "singles", "site", "soccer", "social", "software", "solar", "solutions",
        "space", "store", "studio", "style", "supplies", "supply", "support", "surgery", "systems",
        "tattoo", "tax", "taxi", "team", "tech", "technology", "tennis", "test", "theater",
        "tienda", "tips", "tires", "today", "tools", "tours", "town", "toys", "trade", "trading",
        "training", "travel", "tube", "university", "vacations", "ventures", "vet", "viajes",
        "video", "villas", "vin", "vision", "voyage", "wang", "watch", "website", "wedding", "wiki",
        "win", "wine", "work", "works", "world", "wtf", "xyz", "yoga", "zone", "alsace", "bzh"
    }
)  # fmt: skip
#: An unknown top-level domain is accepted up to this length (``.test``, ``.local``).
MAX_FREE_TLD = 6
#: File extensions that end ``name@2x.png``-like tokens, never an address.
_FILE_TLDS = frozenset(
    {
        "png", "jpg", "jpeg", "gif", "svg", "webp", "bmp", "ico", "tif", "tiff", "css", "js", "mjs",
        "json", "xml", "htm", "html", "php", "asp", "aspx", "jsp", "pdf", "doc", "docx", "xls",
        "xlsx", "ppt", "pptx", "odt", "ods", "txt", "csv", "log", "tmp", "bak", "dll", "exe", "msg",
        "eml"
    }
)  # fmt: skip
_HOST_LABEL = regex.compile(r"^[^\W_](?:[\w\-]{0,61}[^\W_])?$")


def _camel_case(word: str) -> bool:
    """``FormattedValue``, ``nextLink``, ``OData``: a capital after a lower-case
    letter, or a capitals run followed by lower case."""
    letters = "".join(c for c in word if c.isalpha())
    return not (letters.islower() or letters.isupper() or letters[1:].islower())


def plausible_email(value: str) -> bool:
    """A host name after the ``@``: labels shaped like host names, ending with a
    plausible top-level domain (a country code, a short one, a known longer one; one
    case, never CamelCase). Dotted identifiers such as the OData annotations
    ``statuscode@OData.Community.Display.V1.FormattedValue`` or
    ``_ownerid_value@Microsoft.Dynamics.CRM.lookuplogicalname`` are not addresses."""
    domain = value.rsplit("@", 1)[-1]
    labels = domain.split(".")
    if len(labels) < 2 or not all(_HOST_LABEL.match(label) for label in labels):
        return False
    tld = labels[-1]
    key = tld.casefold()
    if key.startswith("xn--"):
        return True  # internationalized top-level domain (punycode)
    if key in _FILE_TLDS or _camel_case(tld):
        return False
    return len(key) <= MAX_FREE_TLD or key in LONG_TLDS


BIC_COUNTRIES = frozenset(checksums.IBAN_LENGTHS) | frozenset(
    [
        "US",
        "CA",
        "JP",
        "CN",
        "HK",
        "SG",
        "AU",
        "NZ",
        "IN",
        "ZA",
        "MA",
        "DZ",
        "TN",
        "SN",
        "CI",
        "CM",
        "BR",
        "MX",
        "AR",
        "CL",
        "RU",
        "KR",
        "TW",
        "TH",
        "VN",
        "ID",
        "MY",
        "PH",
    ]
)


# -- vocabularies ---------------------------------------------------------------

STREET_TYPES = (
    r"rue|ruelle|avenue|av|boulevard|bd|blvd|bvd|place|pl|impasse|imp|all[ée]e|chemin|ch|"
    r"route|rte|quai|cours|square|sq|passage|pass|r[ée]sidence|r[ée]s|lotissement|lot|"
    r"lieu[- ]dit|ld|hameau|voie|sentier|rond[- ]point|faubourg|fbg|faub|parvis|esplanade|"
    r"promenade|prom|villa|cit[ée]|quartier|mont[ée]e|traverse|venelle|carrefour|domaine|"
    r"chauss[ée]e|zone|za|zi|zac|parc|port|mail|cour|digue|lev[ée]e|côte|cote|corniche|"
    r"sente|vc|chem|street|st|road|rd|lane|ln|drive|dr"
)
#: Street types not ambiguous enough to be used without a house number.
STRICT_STREET_TYPES = (
    r"rue|avenue|boulevard|impasse|all[ée]e|chemin|quai|square|r[ée]sidence|lotissement|"
    r"lieu[- ]dit|hameau|faubourg|esplanade|promenade|villa|venelle|chauss[ée]e"
)

MONTHS_FR = (
    r"janvier|f[ée]vrier|mars|avril|mai|juin|juillet|ao[uû]t|septembre|octobre|novembre|"
    r"d[ée]cembre|janv|f[ée]vr?|avr|juil|sept|oct|nov|d[ée]c"
)
MONTHS_EN = (
    r"january|february|march|april|may|june|july|august|september|october|november|"
    r"december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec"
)

DOB_CONTEXT = (
    "ne le", "nee le", "ne(e) le", "ne(e)", "ne (e)", "nee", "ne a", "nee a", "ne en", "nele",
    "neele",
    "nee en", "date de naissance", "naissance", "naiss", "ddn", "d.d.n", "dob", "d.o.b",
    "date of birth", "birth date", "birthdate", "born", "birthday", "anniversaire",
)  # fmt: skip

# -- rules ------------------------------------------------------------------------

_NUM_START = r"(?<![\w+])"
_NUM_END = r"(?!\d)"

RULES: tuple[Rule, ...] = (
    # E-mail addresses (plain and obfuscated).
    Rule(
        "email",
        EntityType.EMAIL,
        r"(?<![\w.%+\-'])[\w.%+\-']{1,64}@(?:[\w\-]+\.)+(?:\p{L}{2,24}|xn--[A-Za-z0-9\-]{2,59})"
        # Never a prefix of a longer dotted name (``x@a.CRM.lookuplogicalname``).
        r"(?![\w\-]|\.[\w\-])",
        1.0,
        validator=plausible_email,
    ),
    Rule(
        "email_obfusque",
        EntityType.EMAIL,
        r"(?<![\w.])[\w.%+\-]{1,64}"
        + SP
        + r"?(?:\(at\)|\[at\]|\{at\}|"
        + SP
        + r"(?:at|arobase)"
        + SP
        + r")"
        + SP
        + r"?(?:[\w\-]+"
        + SP
        + r"?(?:\.|\(dot\)|\[dot\]|"
        + SP
        + r"(?:dot|point)"
        + SP
        + r")"
        + SP
        + r"?)+[A-Za-z]{2,24}(?![\w])",
        0.75,
        flags=regex.MULTILINE | regex.IGNORECASE,
        prefilter=r"[(\[{]at[)\]}]|" + SP + r"(?:at|arobase)" + SP,
    ),
    # Phone numbers.
    Rule(
        "telephone_fr",
        EntityType.PHONE,
        _NUM_START + r"(?:(?:\+|00)" + SP + r"?33" + SEP + r"?(?:\(0\)" + SEP + r"?)?|0)"
        r"[1-9](?:" + SEP + r"{0,2}\d){8}" + _NUM_END,
        0.9,
        context=(
            "tel",
            "telephone",
            "portable",
            "mobile",
            "fixe",
            "fax",
            "gsm",
            "phone",
            "appeler",
            "joindre",
            "contact",
            "numero",
            "standard",
            "ligne",
            "cell",
        ),
        context_score=1.0,
        validator=_fr_phone,
    ),
    Rule(
        # Pairs separated by slashes (``06/12/34/56/78``), the same separator throughout.
        "telephone_fr_barres",
        EntityType.PHONE,
        r"(?<![\w/])0[1-9](?:/\d{2}){4}(?![\w/])",
        0.85,
        context=("tel", "telephone", "portable", "mobile", "fixe", "fax", "gsm", "phone"),
        context_score=1.0,
    ),
    Rule(
        # Phone stored as a number (spreadsheets, JSON): the leading 0 is lost, the
        # country code may remain (``33612345678``).
        "telephone_sans_zero",
        EntityType.PHONE,
        r"(?<![\d+.,])(?:33)?[1-9](?:" + SEP + r"?\d){8}(?![\d.,]\d|\d)",
        0.3,
        context=("tel", "telephone", "portable", "mobile", "fixe", "fax", "gsm", "phone"),
        context_score=0.85,
        needs_context=True,
    ),
    Rule(
        "telephone_outremer",
        EntityType.PHONE,
        _NUM_START
        + r"(?:\+|00)"
        + SP
        + r"?(?:262|269|508|590|594|596|681|687|689)"
        + SEP
        + r"?(?:\(0\)"
        + SEP
        + r"?)?(?:"
        + SEP
        + r"{0,2}\d){9}"
        + _NUM_END,
        0.9,
    ),
    Rule(
        "telephone_international",
        EntityType.PHONE,
        _NUM_START
        + r"(?:\+|00)"
        + SP
        + r"?[1-9]\d{0,2}(?:"
        + SEP
        + r"{0,2}(?:\(\d{1,4}\)|\d)){6,13}"
        + _NUM_END,
        0.85,
        context=("tel", "telephone", "portable", "mobile", "fax", "phone", "whatsapp", "gsm"),
        context_score=1.0,
        validator=_intl_phone,
    ),
    Rule(
        # ``+44 7700 900123``: a possible but unallocated number (reserved or fiction
        # ranges, recently opened blocks): still written as a phone number.
        "telephone_international_possible",
        EntityType.PHONE,
        _NUM_START
        + r"(?:\+|00)"
        + SP
        + r"?[1-9]\d{0,2}(?:"
        + SEP
        + r"{0,2}(?:\(\d{1,4}\)|\d)){6,13}"
        + _NUM_END,
        0.6,
        context=("tel", "telephone", "portable", "mobile", "fax", "phone", "whatsapp", "gsm"),
        context_score=0.9,
        validator=_intl_phone_possible,
    ),
    # Banking.
    Rule(
        # Any grouping (4 by 4, RIB-like 5/5/11/2, glued) and separator (spaces, tabs,
        # hyphens, dots, doubled spaces): the checksum decides, the detector shrinks
        # the match to the longest valid prefix (see ``IBAN_STRICT`` for invalid ones).
        "iban",
        EntityType.IBAN,
        r"(?<![A-Za-z0-9])[A-Z]{2}\d{2}(?:" + IBAN_SEP + r"{0,2}[A-Z0-9]){10,30}(?![A-Za-z0-9])",
        1.0,
        context=("iban", "rib", "compte", "bancaire", "virement", "banque", "bank", "account"),
        validator=checksums.iban_valid,
        invalid_score=0.45,
        flags=regex.MULTILINE | regex.IGNORECASE,
    ),
    Rule(
        "bic",
        EntityType.BIC,
        r"(?<![A-Za-z0-9])[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}(?:[A-Z0-9]{3})?(?![A-Za-z0-9])",
        0.4,
        context=("bic", "swift", "code banque", "bank code"),
        context_score=0.9,
        needs_context=True,
        validator=_bic,
        before=64,
    ),
    Rule(
        "carte_bancaire",
        EntityType.CREDIT_CARD,
        r"(?<![\d\-])(?:\d[ \-\u00a0]?){12,18}\d(?![\d\-])",
        0.9,
        context=(
            "carte",
            "cb",
            "visa",
            "mastercard",
            "amex",
            "card",
            "credit",
            "paiement",
            "expire",
            "cvv",
            "cryptogramme",
        ),
        context_score=1.0,
        validator=_card_not_siret,
        invalid_score=0.4,
    ),
    Rule(
        # 12-digit Maestro numbers: too common a length to be trusted without context.
        "carte_bancaire_courte",
        EntityType.CREDIT_CARD,
        r"(?<![\d\-])(?:\d[ \- ]?){11}\d(?![\d\-])",
        0.35,
        context=("carte", "cb", "maestro", "card", "paiement", "expire", "cvv"),
        context_score=0.9,
        validator=lambda v: checksums.luhn_valid(v) and checksums.digits(v)[0] in "56",
    ),
    # French identifiers.
    Rule(
        # Separators: spaces, dots or hyphens. The key makes the match safe, so a word
        # glued on the left (OCR: ``sociale185...``) does not prevent it.
        "nir",
        EntityType.NIR,
        r"(?<!\d)(?<!\d"
        + SEP
        + r")[1-478]"
        + SEP
        + r"?\d{2}"
        + SEP
        + r"?(?:0[1-9]|1[0-2]|[2-9]\d)"
        + SEP
        + r"?(?:\d{2}|2[AaBb])"
        + SEP
        + r"?\d{3}"
        + SEP
        + r"?\d{3}"
        + SEP
        + r"?\d{2}(?![\dA-Za-z])(?!"
        + SEP
        + r"\d)",
        1.0,
        context=(
            "securite sociale",
            "secu",
            "nir",
            "n° ss",
            "no ss",
            "num ss",
            "insee",
            "assure",
            "immatriculation",
            "carte vitale",
            "ss :",
            "nss",
        ),
        validator=_nir_with_key,
        invalid_score=0.45,
    ),
    Rule(
        "nir_sans_cle",
        EntityType.NIR,
        r"(?<![\dA-Za-z])[1-478]"
        + SEP
        + r"?\d{2}"
        + SEP
        + r"?(?:0[1-9]|1[0-2]|[2-9]\d)"
        + SEP
        + r"?(?:\d{2}|2[AaBb])"
        + SEP
        + r"?\d{3}"
        + SEP
        + r"?\d{3}(?!"
        + SEP
        + r"?\d)(?![A-Za-z])",
        0.45,
        context=(
            "securite sociale",
            "secu",
            "nir",
            "n° ss",
            "no ss",
            "num ss",
            "insee",
            "assure",
            "carte vitale",
            "nss",
        ),
        context_score=0.9,
    ),
    Rule(
        "siret",
        EntityType.SIRET,
        r"(?<![\d])\d{3}" + SP + r"?\d{3}" + SP + r"?\d{3}" + SP + r"?\d{5}(?![\d])",
        0.85,
        context=("siret", "etablissement", "n° siret"),
        context_score=1.0,
        validator=checksums.siret_valid,
        invalid_score=0.45,
    ),
    Rule(
        "siren",
        EntityType.SIREN,
        r"(?<![\d.])\d{3}[ \t.\u00a0\u202f]?\d{3}[ \t.\u00a0\u202f]?\d{3}(?![\d]|[.,]\d)",
        0.35,
        context=(
            "siren",
            "rcs",
            "r.c.s",
            "societe",
            "entreprise",
            "immatricul*",
            "sas",
            "sarl",
            "registre du commerce",
        ),
        context_score=0.95,
        validator=checksums.siren_valid,
        invalid_score=0.4,
    ),
    Rule(
        "tva_fr",
        EntityType.VAT_NUMBER,
        r"(?<![A-Za-z0-9])FR"
        + SP
        + r"?[0-9A-HJ-NP-Z]{2}"
        + SP
        + r"?\d{3}"
        + SP
        + r"?\d{3}"
        + SP
        + r"?\d{3}(?!\d)",
        1.0,
        context=("tva", "vat", "intracom", "intra-communautaire", "intracommunautaire"),
        validator=checksums.vat_fr_valid,
        invalid_score=0.45,
    ),
    Rule(
        "tva_ue",
        EntityType.VAT_NUMBER,
        r"(?<![A-Za-z0-9])(?:DE\d{9}|BE[01]\d{9}|IT\d{11}|ES[A-Z0-9]\d{7}[A-Z0-9]|NL\d{9}B\d{2}"
        r"|LU\d{8}|ATU\d{8}|PT\d{9}|IE\d{7}[A-W][AH]?|IE\d[A-Z+*]\d{5}[A-W]|PL\d{10}|SE\d{12}"
        r"|DK\d{8}|FI\d{8}|CZ\d{8,10}|SK\d{10}|HU\d{8}|RO\d{2,10}|BG\d{9,10}|HR\d{11}"
        r"|SI\d{8}|EE\d{9}|LV\d{11}|LT(?:\d{9}|\d{12})|CY\d{8}[A-Z]|MT\d{8}|EL\d{9}"
        r"|CHE[- ]?\d{3}\.?\d{3}\.?\d{3})(?:" + SP + r"?(?:TVA|MWST|IVA))?(?![A-Za-z0-9])",
        0.6,
        context=("tva", "vat", "intracom", "ust", "btw", "iva", "mwst", "partita"),
    ),
    # Licence plates.
    Rule(
        "plaque_siv",
        EntityType.LICENSE_PLATE,
        r"(?<![A-Za-z0-9\-])[A-HJ-NP-TV-Z]{2}-\d{3}-[A-HJ-NP-TV-Z]{2}(?![A-Za-z0-9\-])",
        0.85,
        context=("immatricul*", "plaque", "vehicule", "voiture", "carte grise", "auto", "moto"),
    ),
    Rule(
        # Same shape with letters SIV never uses (I, O, U): typos, foreign or fake plates.
        "plaque_siv_large",
        EntityType.LICENSE_PLATE,
        r"(?<![A-Za-z0-9\-])[A-Z]{2}-\d{3}-[A-Z]{2}(?![A-Za-z0-9\-])",
        0.6,
        context=("immatricul*", "plaque", "vehicule", "voiture", "carte grise", "auto", "moto"),
        context_score=0.9,
    ),
    Rule(
        # Old FNI numbers written with hyphens (``431-USL-85``) are distinctive.
        "plaque_fni_tirets",
        EntityType.LICENSE_PLATE,
        r"(?<![A-Za-z0-9\-])\d{1,4}-[A-Z]{1,3}-(?:\d{2}|2A|2B|97[1-6])(?![A-Za-z0-9\-])",
        0.6,
        context=("immatricul*", "plaque", "vehicule", "voiture", "carte grise"),
        context_score=0.9,
    ),
    Rule(
        "plaque_siv_compacte",
        EntityType.LICENSE_PLATE,
        r"(?<![A-Za-z0-9\-])[A-HJ-NP-TV-Z]{2}"
        + SP
        + r"?\d{3}"
        + SP
        + r"?[A-HJ-NP-TV-Z]{2}(?![A-Za-z0-9\-])",
        0.5,
        context=("immatricul*", "plaque", "vehicule", "voiture", "carte grise", "auto", "moto"),
    ),
    Rule(
        "plaque_fni",
        EntityType.LICENSE_PLATE,
        r"(?<![A-Za-z0-9])\d{1,4}[ \-]?[A-Z]{1,3}[ \-]?(?:0[1-9]|[1-8]\d|9[0-5]|2A|2B|97[1-6])"
        r"(?![A-Za-z0-9])",
        0.3,
        context=("immatricul*", "plaque", "vehicule", "voiture", "carte grise"),
        context_score=0.85,
        needs_context=True,
    ),
    # Postal code alone (with context).
    Rule(
        "code_postal",
        EntityType.POSTAL_CODE,
        r"(?<![\d])(?:0[1-9]|[1-8]\d|9[0-8])" + SP + r"?\d{3}(?![\d])",
        0.3,
        context=("code postal", "cp", "c.p", "postal", "zip", "cedex"),
        context_score=0.85,
        needs_context=True,
        before=24,
    ),
    # Network. IPv4 not after "Name/": a software version (LibreOffice/24.2.7.2, written
    # again by LibreOffice in every legacy file it saves); "//" (URL host) still matches.
    Rule(
        "ipv4",
        EntityType.IP_ADDRESS,
        r"(?<![\w.])(?<![A-Za-z]/)(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2})?(?![\w]|\.\d)",
        0.85,
        context=("ip", "adresse ip", "serveur", "server", "host", "hote", "connexion"),
        context_score=1.0,
        validator=lambda v: _ipv4(v.split("/")[0]),
    ),
    Rule(
        "ipv6",
        EntityType.IP_ADDRESS,
        r"(?<![\w:.])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?:%[\w.]+)?(?![\w:])",
        0.85,
        validator=_ipv6,
    ),
    Rule(
        "adresse_mac",
        EntityType.IP_ADDRESS,
        r"(?<![\w:\-])[0-9A-Fa-f]{2}(?:([:\-])[0-9A-Fa-f]{2})(?:\1[0-9A-Fa-f]{2}){4}(?![\w:\-])",
        0.7,
        context=("mac", "ethernet", "wifi", "adresse physique"),
        context_score=0.9,
    ),
    Rule(
        "url",
        EntityType.URL,
        r"(?<![\w@])(?:(?:https?|ftps?|sftp|file|smb|webdav)://|www\.)[^\s<>\"'«»\[\]{}|\\^`]+",
        0.9,
        flags=regex.MULTILINE | regex.IGNORECASE,
        trim_trailing=True,
    ),
    Rule(
        "domaine",
        EntityType.URL,
        r"(?<![\w@.\-/\\])(?:[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?\.)+"
        r"(?:fr|com|org|net|eu|io|info|biz|be|ch|lu|de|uk|es|it|nl|ca|us|gouv\.fr|asso\.fr|"
        r"tech|app|dev|online|site|store|cloud|ai|me|tv|pro|bzh|paris|corsica|re|gp|mq|gf|"
        r"yt|nc|pf|ma|tn|dz|sn|ci|cm|immo|avocat|notaire|legal|law)"
        r"(?:/[^\s<>\"'«»]*)?(?![\w\-@])",
        0.6,
        context=("site", "web", "internet", "domaine", "url", "lien", "voir", "consulter"),
        context_score=0.85,
        flags=regex.MULTILINE | regex.IGNORECASE,
        skip_kinds=frozenset({SegmentKind.FILENAME}),
        trim_trailing=True,
    ),
    # Accounts and identifiers.
    Rule(
        "identifiant_contexte",
        EntityType.USERNAME,
        r"(?<=\b(?:login|identifiant|utilisateur|user(?:name|[ _]?id)?|nom d'utilisateur|"
        r"pseudo|pseudonyme|compte|account|uid|id utilisateur|id client|handle|skype|"
        r"teams|slack|github|gitlab|discord|telegram)"
        + SP
        + r"{0,3}[:=]"
        + SP
        + r"{0,3})[^\s,;\"'<>()\[\]]{2,64}",
        0.85,
        flags=regex.MULTILINE | regex.IGNORECASE,
        # The last word of every keyword above, then the separator.
        prefilter=r"(?:login|identifiant|utilisateur|user|name|pseudo|pseudonyme|compte|account"
        r"|id|client|handle|skype|teams|slack|github|gitlab|discord|telegram)" + SP + r"{0,3}[:=]",
    ),
    Rule(
        "identifiant_chemin_windows",
        EntityType.USERNAME,
        r"(?<=(?:[A-Za-z]:)?\\\\?(?:Users|Utilisateurs|Documents and Settings|home)\\\\?)"
        # A profile folder may hold spaces (« C:\Users\Jean Dupont\ »).
        r"(?:[^\\/:*?\"<>|\r\n\t]{2,64}(?=\\)|[^\\/\s:*?\"<>|]{2,64})",
        0.9,
        flags=regex.MULTILINE | regex.IGNORECASE,
        prefilter=r"users|utilisateurs|documents and settings|home",
    ),
    Rule(
        "identifiant_chemin_unix",
        EntityType.USERNAME,
        r"(?<=/(?:home|Users|users|utilisateurs)/)[^/\s:*?\"<>|]{2,64}",
        0.9,
        prefilter=r"/(?:home|Users|users|utilisateurs)/",
    ),
    Rule(
        "identifiant_domaine",
        EntityType.USERNAME,
        r"(?<![\w\\])[A-Z][A-Z0-9\-]{1,14}\\[A-Za-z][\w.\-]{1,30}(?![\w\\])",
        0.6,
    ),
    Rule(
        "pseudo_arobase",
        EntityType.USERNAME,
        # Hyphens inside a handle (« @jean-baptiste ») belong to it.
        r"(?<![\w@./+\-])@\w(?:[\w.\-]{1,28}\w)(?![\w@\-]|\.\w)",
        0.6,
        context=(
            "twitter",
            "instagram",
            "x.com",
            "tiktok",
            "suivre",
            "compte",
            "pseudo",
            "telegram",
            "mastodon",
            "linkedin",
        ),
        context_score=0.9,
    ),
    Rule(
        "id_message_corps",
        EntityType.MESSAGE_ID,
        r"<[^<>\s@]{6,}@[^<>\s@]+>",
        0.6,
        context=("message-id", "in-reply-to", "references", "message id"),
        context_score=0.95,
        validator=_has_digit,
    ),
    # Case / file / contract numbers introduced by a keyword.
    Rule(
        "numero_dossier_contexte",
        EntityType.CASE_NUMBER,
        r"(?<=\b(?:dossier|affaire|r[ée]f[ée]rence|r[ée]f|rg|ticket|contrat|police|sinistre|"
        r"client|adh[ée]rent|matricule|passeport|passport|cni|carte d'identit[ée]|permis|"
        r"titre de s[ée]jour|facture|commande|devis|patient|ipp|nda|"
        r"n°"
        + SP
        + r"?(?:de"
        + SP
        + r")?(?:dossier|client|contrat|police|adh[ée]rent))(?![\w])"
        # « mon numéro client est le HX-C-941260 »
        + r"(?:"
        + SP
        + r"+(?:est|[ée]tait)(?:"
        + SP
        + r"+(?:le|la))?)?"
        + SP
        + r"{0,3}(?:n[°ºo]\.?"
        + SP
        + r"{0,2}|num[ée]ro"
        + SP
        + r"{0,2}|:"
        + SP
        + r"{0,2}|#"
        + SP
        + r"?){0,2})[A-Z0-9][A-Z0-9/\-_.]{2,30}(?<![.\-/_])",
        0.7,
        validator=_has_digit,
        flags=regex.MULTILINE | regex.IGNORECASE,
        prefilter=r"dossier|affaire|r[ée]f|rg|ticket|contrat|police|sinistre|client|adh[ée]rent"
        r"|matricule|passeport|passport|cni|carte d'identit|permis|titre de|facture|commande"
        r"|devis|patient|ipp|nda",
    ),
)

#: Time zone after a time: ``Z``, ``UTC``, ``+01:00``, ``+0100``, ``(UTC+1)``, ``CEST``.
TIME_ZONE = (
    r"(?:Z(?![\w])|" + SP + r"?(?:\(?(?:UTC|GMT)(?:" + SP + r"?[+\-]" + SP
    + r"?\d{1,2}(?::?\d{2})?)?\)?|CEST|CET|HNEC|HAEC)(?![\w])|"
    + SP + r"?[+\-](?:0\d|1[0-4])(?::?[0-5]\d|'[0-5]\d'?)(?![\d]))"
)  # fmt: skip
#: Time of day: ``14:22``, ``14:22:05``, ``14:22:05,123``, ``14h22``, ``14 h 30``,
#: ``14h22min05``; seconds and time zone optional.
TIME_OF_DAY = (
    r"(?:[01]?\d|2[0-3])(?:" + SP + r"?:" + SP + r"?[0-5]\d(?::[0-5]\d(?:[.,]\d{1,6})?)?"
    r"|" + SP + r"?[hH]" + SP + r"?(?:[0-5]\d(?:" + SP + r"?(?:min|mn|m)?" + SP
    + r"?[0-5]\d(?:" + SP + r"?s)?(?![\w])|(?:" + SP + r"?(?:min|mn))?)?(?![\w]))?)"
    r"(?:" + TIME_ZONE + r")?"
)  # fmt: skip
#: Optional time glued to a date: ``31/10/2026 à 14:22``, ``31/10/2026, 14h22``,
#: ``31/10/2026 - 14:22:05 +01:00``, ``2026-10-31T14:22:05Z``.
TIME_AFTER_DATE = (
    r"(?:(?:T|,?" + SP + r"*(?:(?:à|a|at|@|-|–|,)" + SP + r"*)?|" + SP
    + r"+)(?=\d)" + TIME_OF_DAY + r"(?<=\d|[hH]|[a-zA-Z)']))?"
)  # fmt: skip

DATE_RULES: tuple[Rule, ...] = (
    Rule(
        "date_numerique",
        EntityType.DATE,
        r"(?<![\d/.\-])(?:0?[1-9]|[12]\d|3[01])([/.\-])(?:0?[1-9]|1[0-2])\1(?:19|20)?\d{2}"
        r"(?![\d/\-]|\.\d)" + TIME_AFTER_DATE,
        0.65,
        flags=regex.MULTILINE | regex.IGNORECASE,
    ),
    Rule(
        # US order with an unambiguous day (``03/14/1985``): the French rule rejects it.
        "date_us",
        EntityType.DATE,
        r"(?<![\d/.\-])(?:0?[1-9]|1[0-2])/(?:1[3-9]|2\d|3[01])/(?:19|20)\d{2}(?![\d/\-]|\.\d)"
        + TIME_AFTER_DATE,
        0.65,
        flags=regex.MULTILINE | regex.IGNORECASE,
    ),
    Rule(
        "date_iso",
        EntityType.DATE,
        r"(?<![\d\-])(?:19|20)\d{2}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])(?![\d\-])"
        + TIME_AFTER_DATE,
        0.65,
        flags=regex.MULTILINE | regex.IGNORECASE,
    ),
    Rule(
        "date_textuelle",
        EntityType.DATE,
        r"(?<![\w])(?:1er|1re|0?[1-9]|[12]\d|3[01])"
        + SP
        + r"+(?:"
        + MONTHS_FR
        + r")\.?"
        + SP
        + r"+(?:19|20)\d{2}(?![\w])"
        + TIME_AFTER_DATE,
        0.65,
        flags=regex.MULTILINE | regex.IGNORECASE,
    ),
    Rule(
        # Year first with dots or slashes (``2026.10.31``: signature appearances).
        "date_annee_points",
        EntityType.DATE,
        r"(?<![\d/.\-])(?:19|20)\d{2}([./])(?:0[1-9]|1[0-2])\1(?:0[1-9]|[12]\d|3[01])"
        r"(?![\d/]|\.\d)" + TIME_AFTER_DATE,
        0.65,
        flags=regex.MULTILINE | regex.IGNORECASE,
    ),
    Rule(
        # PDF date strings (signature dictionaries, visible signing panels).
        "horodatage_pdf",
        EntityType.DATE,
        r"(?<![\w])D:(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])(?:\d{2}){0,3}"
        r"(?:Z(?:00'?00'?)?|[+\-]\d{2}'?(?:\d{2}'?)?)?",
        0.65,
        skip_kinds=frozenset({SegmentKind.METADATA}),
    ),
    Rule(
        # A time alone: kept only next to a signature cue or a person (see the
        # detector), never as an ordinary date.
        "heure",
        EntityType.DATE,
        r"(?<![\w:.,/\-+])" + TIME_OF_DAY + r"(?<=\d|[hH]|[a-zA-Z)'])(?![\w:/]|[.,]\d)",
        0.65,
        flags=regex.MULTILINE | regex.IGNORECASE,
    ),
    Rule(
        "date_anglaise",
        EntityType.DATE,
        r"(?<![\w])(?:(?:"
        + MONTHS_EN
        + r")\.?"
        + SP
        + r"+(?:0?[1-9]|[12]\d|3[01])(?:st|nd|rd|th)?,?"
        + SP
        + r"+(?:19|20)\d{2}|(?:0?[1-9]|[12]\d|3[01])(?:st|nd|rd|th)?"
        + SP
        + r"+(?:"
        + MONTHS_EN
        + r")\.?,?"
        + SP
        + r"+(?:19|20)\d{2})(?![\w])"
        + TIME_AFTER_DATE,
        0.6,
        flags=regex.MULTILINE | regex.IGNORECASE,
        prefilter=r"(?<![\w])(?:" + MONTHS_EN + r")",
    ),
)

#: Segment labels (normalized substrings) that force or boost a type.
LABEL_TYPES: tuple[tuple[str, str], ...] = (
    ("message-id", EntityType.MESSAGE_ID),
    ("in-reply-to", EntityType.MESSAGE_ID),
    ("references", EntityType.MESSAGE_ID),
    ("naissance", EntityType.DATE_OF_BIRTH),
    ("birth", EntityType.DATE_OF_BIRTH),
)

__all__ = ["DATE_RULES", "DOB_CONTEXT", "LABEL_TYPES", "RULES", "Rule", "plausible_email"]
