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


# -- validators -----------------------------------------------------------------


def _fr_phone(value: str) -> bool:
    d = checksums.digits(value.replace("(0)", ""))
    if d.startswith("0033"):
        d = "0" + d[4:]
    elif d.startswith("33") and len(d) == 11:
        d = "0" + d[2:]
    return len(d) == 10 and d[0] == "0" and d[1] != "0"


def _intl_phone(value: str) -> bool:
    d = checksums.digits(value.replace("(0)", ""))
    if value.lstrip().startswith("00"):
        d = d[2:]
    if d.startswith("33"):
        return False  # handled by the French rule
    try:
        number = phonenumbers.parse("+" + d, None)
    except phonenumbers.NumberParseException:
        return False
    return bool(phonenumbers.is_valid_number(number))


def _card(value: str) -> bool:
    d = checksums.digits(value)
    return 13 <= len(d) <= 19 and d[0] in "23456" and checksums.luhn_valid(d)


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
    "ne le", "nee le", "ne(e) le", "ne(e)", "ne (e)", "nee", "ne a", "nee a", "ne en",
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
        r"(?<![\w.%+\-'])[\w.%+\-']{1,64}@(?:[\w\-]+\.)+[A-Za-z]{2,24}(?![\w\-])",
        1.0,
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
    # Banking.
    Rule(
        "iban",
        EntityType.IBAN,
        r"(?<![A-Za-z0-9])[A-Z]{2}\d{2}(?:"
        + SP
        + r"?[A-Z0-9]{4}){2,7}(?:"
        + SP
        + r"?[A-Z0-9]{1,4})?(?![A-Za-z0-9])",
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
    # French identifiers.
    Rule(
        "nir",
        EntityType.NIR,
        r"(?<![\dA-Za-z])[1-478]"
        + SP
        + r"?\d{2}"
        + SP
        + r"?(?:0[1-9]|1[0-2]|[2-9]\d)"
        + SP
        + r"?(?:\d{2}|2[AaBb])"
        + SP
        + r"?\d{3}"
        + SP
        + r"?\d{3}"
        + SP
        + r"?\d{2}(?![\dA-Za-z])",
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
        + SP
        + r"?\d{2}"
        + SP
        + r"?(?:0[1-9]|1[0-2]|[2-9]\d)"
        + SP
        + r"?(?:\d{2}|2[AaBb])"
        + SP
        + r"?\d{3}"
        + SP
        + r"?\d{3}(?!"
        + SP
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
    # Network.
    Rule(
        "ipv4",
        EntityType.IP_ADDRESS,
        r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2})?(?![\w]|\.\d)",
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
    ),
    Rule(
        "identifiant_chemin_windows",
        EntityType.USERNAME,
        r"(?<=(?:[A-Za-z]:)?\\\\?(?:Users|Utilisateurs|Documents and Settings|home)\\\\?)"
        r"[^\\/\s:*?\"<>|]{2,64}",
        0.9,
        flags=regex.MULTILINE | regex.IGNORECASE,
    ),
    Rule(
        "identifiant_chemin_unix",
        EntityType.USERNAME,
        r"(?<=/(?:home|Users|users|utilisateurs)/)[^/\s:*?\"<>|]{2,64}",
        0.9,
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
        r"(?<![\w@./+\-])@[A-Za-z0-9_](?:[A-Za-z0-9_.]{1,28}[A-Za-z0-9_])(?![\w@]|\.\w)",
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
    ),
)

DATE_RULES: tuple[Rule, ...] = (
    Rule(
        "date_numerique",
        EntityType.DATE,
        r"(?<![\d/.\-])(?:0?[1-9]|[12]\d|3[01])([/.\-])(?:0?[1-9]|1[0-2])\1(?:19|20)?\d{2}"
        r"(?![\d/\-]|\.\d)",
        0.65,
    ),
    Rule(
        "date_iso",
        EntityType.DATE,
        r"(?<![\d\-])(?:19|20)\d{2}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])(?![\d\-])",
        0.65,
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
        + r"+(?:19|20)\d{2}(?![\w])",
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
        + r"+(?:19|20)\d{2})(?![\w])",
        0.6,
        flags=regex.MULTILINE | regex.IGNORECASE,
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

__all__ = ["DATE_RULES", "DOB_CONTEXT", "LABEL_TYPES", "RULES", "Rule"]
