"""Realistic fake values for the REALISTIC replacement mode.

Every generator is deterministic for a given ``random.Random`` and preserves the
shape of the original (case, separators, grouping, prefixes). Numbers carry valid
check digits (Luhn, mod-97, RIB and NIR keys); phone numbers use the ARCEP ranges
reserved for fiction when possible; IP addresses use documentation ranges. Domains
keep their public suffix with a plausible label (see :func:`fake_domain` for why no
reserved suffix), places are real communes with a real postcode, organisations keep
their designators (« Groupe », « SAS ») around a new core.
"""

from __future__ import annotations

import ipaddress
import random
import re
import string
from dataclasses import dataclass
from datetime import date, timedelta
from functools import lru_cache

from faker import Faker

from anonyma import checksums
from anonyma.data import first_name, first_names
from anonyma.data.communes import communes
from anonyma.detectors._rules import ADDRESS_COMPLEMENT
from anonyma.names import PersonName, initials_of, is_initial, login_of, login_parts

PUBLIC_MAIL_DOMAINS = frozenset(
    [
        "gmail.com",
        "googlemail.com",
        "outlook.com",
        "outlook.fr",
        "hotmail.com",
        "hotmail.fr",
        "live.com",
        "live.fr",
        "msn.com",
        "yahoo.com",
        "yahoo.fr",
        "icloud.com",
        "me.com",
        "orange.fr",
        "wanadoo.fr",
        "free.fr",
        "sfr.fr",
        "neuf.fr",
        "laposte.net",
        "aol.com",
        "gmx.fr",
        "gmx.com",
        "protonmail.com",
        "proton.me",
        "bbox.fr",
        "numericable.fr",
        "club-internet.fr",
        "aliceadsl.fr",
        "cegetel.net",
    ]
)
#: ARCEP numbers reserved for audiovisual fiction: first 6 national digits.
FICTION_PREFIXES = {"1": "019900", "2": "026191", "3": "035301", "4": "046571", "5": "053649",
                    "6": "063998"}  # fmt: skip
SIV_LETTERS = "ABCDEFGHJKLMNPQRSTVWXYZ"

MONTHS_FR = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
             "septembre", "octobre", "novembre", "décembre"]  # fmt: skip
MONTHS_FR_ABBR = ["janv", "févr", "mars", "avr", "mai", "juin", "juil", "août", "sept", "oct",
                  "nov", "déc"]  # fmt: skip
MONTHS_EN = ["january", "february", "march", "april", "may", "june", "july", "august",
             "september", "october", "november", "december"]  # fmt: skip


@lru_cache(maxsize=1)
def faker_fr() -> Faker:
    return Faker("fr_FR")


@lru_cache(maxsize=1)
def _given_pool() -> dict[str, list[str]]:
    pool: dict[str, list[str]] = {"m": [], "f": []}
    for record in first_names().values():
        if record.gender in pool and record.count >= 2000 and "-" not in record.display:
            pool[record.gender].append(record.display)
    for names in pool.values():
        names.sort()
    return pool


def mirror_case(original: str, fake: str) -> str:
    """Apply the case style of ``original`` (UPPER, lower, Title) to ``fake``."""
    letters = [c for c in original if c.isalpha()]
    if not letters:
        return fake
    if all(c.isupper() for c in letters) and len(letters) > 1:
        return fake.upper()
    if all(c.islower() for c in letters):
        return fake.lower()
    return fake


_RUN = re.compile(r"[^\W\d_]+")


def _case_like(original: str, word: str) -> str:
    if original.isupper() and (len(original) > 1 or len(word) == 1):
        return word.upper()
    if original.islower():
        return word.lower()
    if original[:1].isupper() and original[1:].islower():
        return word[:1].upper() + word[1:].lower()
    return word


def match_case(original: str, fake: str) -> str:
    """Case of each letter run of ``original`` applied to the matching run of ``fake``
    (``Claire FONTAINE`` + ``Paul Martin`` -> ``Paul MARTIN``). When the run counts
    differ (``Claire FONTAINE`` + ``Paul Da Silva``), the last run of ``fake`` follows
    the last run of ``original`` and so do any extra runs: ``Paul DA SILVA``."""
    runs = _RUN.findall(original)
    targets = list(_RUN.finditer(fake))
    if not runs or not targets:
        return mirror_case(original, fake)
    out: list[str] = []
    pos = 0
    last = len(runs) - 1
    for i, m in enumerate(targets):
        source = runs[last] if i == len(targets) - 1 else runs[min(i, last)]
        out.append(fake[pos : m.start()])
        out.append(_case_like(source, m.group()))
        pos = m.end()
    out.append(fake[pos:])
    return "".join(out)


def reshape(original: str, fake_chars: str) -> str:
    """Write ``fake_chars`` into the alphanumeric positions of ``original``.

    An original character is never kept: positions left once ``fake_chars`` is
    exhausted get ``0`` (digits) or ``X`` (letters)."""
    out: list[str] = []
    it = iter(fake_chars)
    for ch in original:
        if ch.isalnum():
            nxt = next(it, None)
            if nxt is None:
                nxt = "0" if ch.isdigit() else "X"
            out.append(nxt.lower() if ch.islower() else nxt)
        else:
            out.append(ch)
    return "".join(out)


def random_like(original: str, rng: random.Random) -> str:
    """Same shape: digits -> digits, letters -> letters (case kept), rest kept. A
    hexadecimal value (a GUID) stays hexadecimal: its letters are taken from a-f."""
    alnum = [ch for ch in original if ch.isalnum()]
    hexadecimal = any(ch.isalpha() for ch in alnum) and all(ch in string.hexdigits for ch in alnum)
    letters = "ABCDEF" if hexadecimal else string.ascii_uppercase
    out: list[str] = []
    for ch in original:
        if ch.isdigit():
            out.append(rng.choice(string.digits))
        elif ch.isalpha():
            letter = rng.choice(letters)
            out.append(letter if ch.isupper() else letter.lower())
        else:
            out.append(ch)
    return "".join(out)


# -- persons ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FakePerson:
    given: tuple[str, ...]  # one entry per given name of the original (compounds joined)
    surname: str
    gender: str


def fake_person(person: PersonName | None, gender: str | None, rng: random.Random) -> FakePerson:
    fake = faker_fr()
    fake.seed_instance(rng.getrandbits(32))
    gender = gender if gender in ("m", "f") else rng.choice("mf")
    pool = _given_pool()[gender] or (
        fake.first_names_male if gender == "m" else fake.first_names_female
    )
    given: list[str] = []
    originals = [g for g in (person.given if person else ()) if g] or ["x"]
    for token in originals:
        parts = token.split("-") if not is_initial(token) else [token]
        names = [rng.choice(pool) for _ in parts]
        given.append("-".join(names))
    surname = fake.last_name()
    if person is not None and len([s for s in person.surname if len(s) > 3]) > 1:
        surname = f"{surname}-{fake.last_name()}"
    return FakePerson(tuple(given), surname, gender)


def _initial_like(original: str, fake: str) -> str:
    """``J.`` -> ``P.``; ``J.-P.`` -> ``P.-H.``; ``JP`` -> ``PH``."""
    letters = [p[:1].upper() for p in fake.split("-") if p]
    n = sum(1 for c in original if c.isalpha())
    letters = (letters + letters)[: max(n, 1)]
    if "." in original:
        joiner = "-" if "-" in original else ""
        return joiner.join(f"{c}." for c in letters)
    return "".join(letters)


def render_person(person: PersonName, fake: FakePerson) -> str:
    """Render ``fake`` with the structure, order and case of ``person``."""
    given: list[str] = []
    for i, token in enumerate(person.given):
        fake_given = fake.given[min(i, len(fake.given) - 1)]
        if is_initial(token):
            given.append(_initial_like(token, fake_given))
        else:
            given.append(mirror_case(token, fake_given))
    surname: list[str] = []
    if person.surname:
        particles = [s for s in person.surname[:-1] if s.islower()]
        surname = [*particles, mirror_case(person.surname[-1], fake.surname)]
    parts = [*surname, *given] if person.surname_first else [*given, *surname]
    name = " ".join(parts)
    return f"{person.civility} {name}".strip() if person.civility else name


def login_like(template: str, original: str, fake: FakePerson) -> str:
    """``fake`` written like ``original`` (template of :func:`names.login_forms`)."""
    person = PersonName("", fake.given, (fake.surname,), False, fake.gender)
    form = login_of(template, person) or login_of("g.s", person)
    if form is None:  # never the original: it is the value being hidden
        return "utilisateur"
    return match_case(original, form)


def _plain(text: str) -> list[str]:
    from anonyma.textutils import strip_accents

    return [w for w in re.split(r"[^a-z0-9]+", strip_accents(text).casefold()) if w]


def local_like(original: str, person: PersonName, fake: FakePerson) -> str:
    """A local part of ``fake`` shaped like ``original``, an address of ``person`` that
    follows no login template: its given names alone (``thi-lan``), its surname
    alone, else ``given.surname``."""
    words = _plain(original)
    separator = next((c for c in original if c in ".-_"), "")
    given = [w for g in person.given if not is_initial(g) for w in _plain(g)]
    surname = [w for s in person.surname for w in _plain(s)]
    if words and all(w in given for w in words):
        parts = [w for g in fake.given for w in _plain(g)]
    elif words and all(w in surname for w in words):
        parts = _plain(fake.surname)
    else:
        return login_like("g.s", original, fake)
    form = separator.join(parts) or login_like("g.s", original, fake)
    return match_case(original, form)


def fake_initials(fake: FakePerson) -> str:
    return "".join(initials_of(g) for g in fake.given)


# -- contact data -----------------------------------------------------------------


#: Public suffixes of more than one label, kept whole (``cabinet.co.uk`` -> ``co.uk``).
#: ``gouv.fr`` is no suffix a substitute may claim: a fake ministry would read as one.
_MULTI_SUFFIXES = frozenset(
    ["co.uk", "org.uk", "ac.uk", "com.au", "co.jp", "com.br", "co.nz", "asso.fr", "com.fr",
     "nom.fr", "tm.fr", "presse.fr"]
)  # fmt: skip
_SUFFIX_REWRITES = {"gouv.fr": "fr"}
#: Sub-domain labels that name nothing (kept: ``mail.sorbel.fr`` -> ``mail.courtois.fr``).
_GENERIC_SUBDOMAINS = frozenset(
    ["www", "mail", "mail2", "smtp", "mx", "webmail", "extranet", "intranet", "portail", "app",
     "api", "cloud", "fr", "en", "news", "info", "contact", "email", "e", "m", "secure"]
)  # fmt: skip
_DOMAIN_TAILS = ("conseil", "groupe", "partenaires", "services", "solutions", "industrie")


def slug(text: str) -> str:
    """``Groupe Courtois`` -> ``groupe-courtois`` (ASCII, lower case, hyphens)."""
    from anonyma.textutils import strip_accents

    return "-".join(_plain(strip_accents(text).replace("œ", "oe").replace("æ", "ae")))


def compact_key(text: str) -> str:
    """``Groupe-Sorbel`` / ``groupe sorbel`` / ``groupesorbel`` -> ``groupesorbel``."""
    return slug(text).replace("-", "")


def split_domain(host: str) -> tuple[list[str], str, str]:
    """``mail.groupe-sorbel.co.uk`` -> (``["mail"]``, ``groupe-sorbel``, ``co.uk``);
    a host without a dot is a bare label with no suffix."""
    labels = [p for p in host.casefold().strip(".").split(".") if p]
    if len(labels) < 2:
        return [], labels[0] if labels else "", ""
    tail = ".".join(labels[-2:])
    multi = tail in _MULTI_SUFFIXES or tail in _SUFFIX_REWRITES
    width = 2 if len(labels) >= 3 and multi else 1
    suffix = ".".join(labels[-width:])
    return labels[: -width - 1], labels[-width - 1], suffix


def fake_domain(original: str, rng: random.Random, label: str | None = None) -> str:
    """A plausible domain in the style of ``original``: the same public suffix
    (``.fr``, ``.com``, ``.co.uk``), generic sub-domains kept, and ``label`` (a
    substitute organisation, made by the caller) or an invented surname-like brand.

    SAFETY decision: substitutes keep the original top-level domain rather than a
    reserved one (``.example``, RFC 2606). A reserved suffix is safe but gives every
    address away as fake, which defeats the realistic mode; the cost is that a
    substitute such as ``courtois.fr`` may be registered by someone. That is accepted
    because a substitute never carries original data (nothing confidential can reach
    that domain), and the mode is documented as unfit for corpora fed to systems that
    send mail (REFERENCE.md: use the tag mode there). Phone numbers and IP addresses
    stay in their reserved ranges: those have realistic reserved values, domains do not.
    """
    prefix, original_label, suffix = split_domain(original)
    suffix = _SUFFIX_REWRITES.get(suffix, suffix)
    if label is None:
        label = slug(fake_org_word(rng))
        if rng.random() < 0.3:
            label = f"{label}-{rng.choice(_DOMAIN_TAILS)}"
        if "-" not in original_label:
            label = label.replace("-", "")
    kept = [p for p in prefix if p in _GENERIC_SUBDOMAINS]
    return ".".join([*kept, label, *([suffix] if suffix else [])])


def fake_local_part(rng: random.Random) -> str:
    person = fake_person(None, None, rng)
    parts = login_parts(PersonName("", person.given, (person.surname,), False, person.gender))
    return f"{parts[0]}.{parts[2]}" if parts else f"utilisateur{rng.randint(1, 9999)}"


def fake_phone_national(national: str, rng: random.Random) -> str:
    """Fake 10-digit French number of the same kind (fixed/mobile) as ``national``."""
    kind = national[1] if len(national) >= 2 else "6"
    prefix = FICTION_PREFIXES.get(kind)
    if prefix:
        return prefix + "".join(rng.choice(string.digits) for _ in range(4))
    return "0" + kind + "".join(rng.choice(string.digits) for _ in range(8))


def render_phone(original: str, national: str, rng: random.Random) -> str:
    """Write a fake number into the shape of ``original`` (country prefix kept)."""
    digits = checksums.digits(original)
    if len(national) == 10 and len(digits) >= 9:
        subscriber = national[1:]
        fake_digits = digits[: len(digits) - 9] + subscriber
    else:
        fake_digits = digits[:3] + "".join(rng.choice(string.digits) for _ in digits[3:])
    out: list[str] = []
    it = iter(fake_digits)
    for ch in original:
        out.append(next(it, ch) if ch.isdigit() else ch)
    return "".join(out)


# -- banking and identifiers --------------------------------------------------------


def fake_iban(original: str, rng: random.Random) -> str:
    compact = checksums.compact(original)
    country = compact[:2] if compact[:2].isalpha() else "FR"
    length = checksums.IBAN_LENGTHS.get(country, len(compact))
    if country == "FR" and length == 27:
        bank = "".join(rng.choice(string.digits) for _ in range(5))
        branch = "".join(rng.choice(string.digits) for _ in range(5))
        account = "".join(rng.choice(string.digits) for _ in range(11))
        bban = bank + branch + account + checksums.rib_key(bank, branch, account)
    else:
        bban = "".join(
            rng.choice(string.digits) if c.isdigit() else rng.choice(string.ascii_uppercase)
            for c in compact[4:length]
        ).ljust(length - 4, "0")
    iban = country + checksums.iban_check_digits(country, bban) + bban
    return reshape(original, iban)


def fake_luhn(original: str, rng: random.Random, *, keep_prefix: int = 1) -> str:
    number = checksums.digits(original)
    prefix = number[:keep_prefix]
    body = prefix + "".join(rng.choice(string.digits) for _ in range(len(number) - 1 - keep_prefix))
    return reshape(original, body + checksums.luhn_check_digit(body))


def fake_siren(rng: random.Random) -> str:
    body = str(rng.randint(1, 9)) + "".join(rng.choice(string.digits) for _ in range(7))
    return body + checksums.luhn_check_digit(body)


def fake_siret(siren: str, rng: random.Random) -> str:
    body = siren + "".join(rng.choice(string.digits) for _ in range(4))
    return body + checksums.luhn_check_digit(body)


def fake_vat_fr(siren: str) -> str:
    return "FR" + checksums.vat_fr_key(siren) + siren


def fake_nir(original: str, rng: random.Random, *, birth: tuple[int, int] | None = None) -> str:
    """Fake NIR of the same sex; ``birth`` (year, month) of the substitute birth date
    keeps the number consistent with it."""
    compact = checksums.compact(original)
    sex = compact[:1] if compact[:1] in "12378" else "1"
    if birth is not None:
        year, month = f"{birth[0] % 100:02d}", f"{birth[1]:02d}"
    else:
        year = f"{rng.randint(40, 99):02d}"
        month = f"{rng.randint(1, 12):02d}"
    dept = f"{rng.choice([d for d in range(1, 96) if d != 20]):02d}"
    rest = "".join(rng.choice(string.digits) for _ in range(6))
    body = sex + year + month + dept + rest
    key = checksums.nir_key(body) or "00"
    return reshape(original, (body + key)[: len(compact)] if len(compact) < 15 else body + key)


def fake_plate(original: str, rng: random.Random) -> str:
    out: list[str] = []
    for ch in original:
        if ch.isdigit():
            out.append(rng.choice(string.digits))
        elif ch.isalpha():
            letter = rng.choice(SIV_LETTERS)
            out.append(letter if ch.isupper() else letter.lower())
        else:
            out.append(ch)
    return "".join(out)


def fake_bic(original: str, rng: random.Random) -> str:
    compact = checksums.compact(original)
    bank = "".join(rng.choice(string.ascii_uppercase) for _ in range(4))
    tail = "".join(rng.choice(string.ascii_uppercase + string.digits) for _ in compact[6:])
    return reshape(original, bank + compact[4:6] + tail)


def fake_ip(original: str, rng: random.Random) -> str:
    value = original.strip()
    suffix = ""
    if "/" in value:
        value, suffix = value.split("/", 1)
        suffix = "/" + suffix
    try:
        ip = ipaddress.ip_address(value.split("%")[0])
    except ValueError:
        return random_like(original, rng)
    if ip.version == 4:
        net = rng.choice(["192.0.2", "198.51.100", "203.0.113"])
        return f"{net}.{rng.randint(1, 254)}{suffix}"
    groups = ":".join(f"{rng.getrandbits(16):x}" for _ in range(4))
    return f"2001:db8::{groups}{suffix}"


def fake_mac(original: str, rng: random.Random) -> str:
    out = []
    for ch in original:
        out.append(rng.choice("0123456789abcdef") if ch.isalnum() else ch)
    text = "".join(out)
    return text.upper() if original.isupper() else text


# -- dates ------------------------------------------------------------------------

_NUMERIC_DATE = re.compile(r"^(\d{1,2})([/.\-])(\d{1,2})\2(\d{2}|\d{4})$")
_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_TEXT_DATE = re.compile(r"^(1er|1re|\d{1,2})(\s+)([^\W\d_]+)(\.?)(\s+)(\d{4})$", re.UNICODE)
_TEXT_DAY_MONTH = re.compile(r"^(1er|1re|\d{1,2})(\s+)([^\W\d_]+)(\.?)$", re.UNICODE)
_NUMERIC_DAY_MONTH = re.compile(r"^(\d{1,2})/(\d{1,2})$")
_EN_DATE = re.compile(r"^([^\W\d_]+)(\.?)(\s+)(\d{1,2})(st|nd|rd|th)?(,?)(\s+)(\d{4})$")


def _month_index(word: str) -> tuple[int, str] | None:
    from anonyma.textutils import normalize

    w = normalize(word)
    for table, style in ((MONTHS_FR, "fr"), (MONTHS_FR_ABBR, "fr_abbr"), (MONTHS_EN, "en")):
        for i, m in enumerate(table):
            if normalize(m) == w:
                return i, style
    for i, m in enumerate(MONTHS_EN):
        if m.startswith(w) and len(w) >= 3:
            return i, "en_abbr"
    for i, m in enumerate(MONTHS_FR):
        if normalize(m).startswith(w) and len(w) >= 3:
            return i, "fr_abbr"
    return None


def _month_name(index: int, style: str, original: str) -> str:
    if style == "fr":
        name = MONTHS_FR[index]
    elif style == "fr_abbr":
        name = MONTHS_FR_ABBR[index]
    elif style == "en":
        name = MONTHS_EN[index]
    else:
        name = MONTHS_EN[index][:3]
    if original[:1].isupper():
        name = name.upper() if original.isupper() and len(original) > 1 else name.capitalize()
    return name


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_date(original: str) -> date | None:
    """The date written in ``original`` (formats of :func:`shift_date`), or None."""
    shifted = shift_date(original, 0)
    if shifted is None:
        return None
    text = shifted.strip()
    m = _NUMERIC_DATE.match(text)
    if m:
        d_s, _sep, m_s, y_s = m.groups()
        year = int(y_s) + (
            1900 if len(y_s) == 2 and int(y_s) > 30 else 2000 if len(y_s) == 2 else 0
        )
        return _safe_date(year, int(m_s), int(d_s))
    m = _ISO_DATE.match(text)
    if m:
        return _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = _TEXT_DATE.match(text)
    if m:
        found = _month_index(m.group(3))
        day = 1 if m.group(1) in ("1er", "1re") else int(m.group(1))
        return _safe_date(int(m.group(6)), found[0] + 1, day) if found else None
    m = _EN_DATE.match(text)
    if m:
        found = _month_index(m.group(1))
        return _safe_date(int(m.group(8)), found[0] + 1, int(m.group(4))) if found else None
    return None


_EN_ABBR = frozenset(m[:3] for m in MONTHS_EN)


def _text_day_month(new: date, day_s: str, month_s: str, dot: str, style: str) -> tuple[str, str]:
    """Day and month of a written date (« 4 octobre », « 1er mai », « 3 sept. »): no
    leading zero (« 04 octobre » is not French), the abbreviation dot only on a
    month that is really abbreviated (never « mai. »)."""
    from anonyma.textutils import normalize

    day_out = "1er" if new.day == 1 and day_s in ("1er", "1re") else str(new.day)
    # A leading zero written by the author is kept, and so is the fixed width of a
    # capitalized abbreviation (« 01 Nov 2026 » of a mail header); a French date in
    # prose (« 14 octobre ») never gets one (« 04 octobre »).
    header = month_s[:1].isupper() and not dot and len(month_s) == 3
    if day_s.isdigit() and len(day_s) == 2 and (day_s.startswith("0") or header):
        day_out = f"{new.day:02d}"
    if header and style == "fr_abbr" and month_s.casefold() in _EN_ABBR:
        # « 02 Oct 2026 » of a mail header is English: « 22 Sept » would not parse.
        style = "en_abbr"
    month_out = _month_name(new.month - 1, style, month_s)
    full = style == "fr" or normalize(month_out) == normalize(MONTHS_FR[new.month - 1])
    if style.endswith("abbr") and dot and not full:
        month_out += dot
    return day_out, month_out


def shift_date(original: str, days: int, year: int | None = None) -> str | None:
    """``original`` shifted by ``days``, in the same format; None if not parseable.

    A day and month without a year (« 9 octobre », « 9/10 ») is shifted as a date of
    ``year`` (default: the current one) and written back without it."""
    text = original.strip()
    m = _NUMERIC_DATE.match(text)
    if m:
        d_s, sep, m_s, y_s = m.groups()
        year = int(y_s) + (
            1900 if len(y_s) == 2 and int(y_s) > 30 else 2000 if len(y_s) == 2 else 0
        )
        parsed = _safe_date(year, int(m_s), int(d_s))
        if parsed is None:
            return None
        new = parsed + timedelta(days=days)
        day = f"{new.day:02d}" if len(d_s) == 2 else str(new.day)
        month = f"{new.month:02d}" if len(m_s) == 2 else str(new.month)
        year_s = f"{new.year % 100:02d}" if len(y_s) == 2 else str(new.year)
        return original.replace(text, f"{day}{sep}{month}{sep}{year_s}")
    m = _ISO_DATE.match(text)
    if m:
        parsed = _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if parsed is None:
            return None
        return original.replace(text, (parsed + timedelta(days=days)).isoformat())
    m = _TEXT_DATE.match(text)
    if m:
        day_s, sp1, month_s, dot, sp2, year_s = m.groups()
        found = _month_index(month_s)
        day_num = 1 if day_s in ("1er", "1re") else int(day_s)
        if found is None:
            return None
        parsed = _safe_date(int(year_s), found[0] + 1, day_num)
        if parsed is None:
            return None
        new = parsed + timedelta(days=days)
        day_out, month_out = _text_day_month(new, day_s, month_s, dot, found[1])
        return original.replace(text, f"{day_out}{sp1}{month_out}{sp2}{new.year}")
    m = _TEXT_DAY_MONTH.match(text)
    if m:
        day_s, sp1, month_s, dot = m.groups()
        found = _month_index(month_s)
        if found is None:
            return None
        day_num = 1 if day_s in ("1er", "1re") else int(day_s)
        parsed = _safe_date(year or date.today().year, found[0] + 1, day_num)
        if parsed is None:
            return None
        day_out, month_out = _text_day_month(
            parsed + timedelta(days=days), day_s, month_s, dot, found[1]
        )
        return original.replace(text, f"{day_out}{sp1}{month_out}")
    m = _NUMERIC_DAY_MONTH.match(text)
    if m:
        d_s, m_s = m.groups()
        parsed = _safe_date(year or date.today().year, int(m_s), int(d_s))
        if parsed is None:
            return None
        new = parsed + timedelta(days=days)
        day = f"{new.day:02d}" if len(d_s) == 2 else str(new.day)
        month = f"{new.month:02d}" if len(m_s) == 2 else str(new.month)
        return original.replace(text, f"{day}/{month}")
    m = _EN_DATE.match(text)
    if m:
        month_s, dot, sp1, day_s, suffix, comma, sp2, year_s = m.groups()
        found = _month_index(month_s)
        if found is None:
            return None
        parsed = _safe_date(int(year_s), found[0] + 1, int(day_s))
        if parsed is None:
            return None
        new = parsed + timedelta(days=days)
        suffix_out = ""
        if suffix:
            suffix_out = {1: "st", 2: "nd", 3: "rd"}.get(
                new.day % 10 if new.day not in (11, 12, 13) else 0, "th"
            )
        month_out = _month_name(new.month - 1, found[1], month_s)
        return original.replace(
            text, f"{month_out}{dot}{sp1}{new.day}{suffix_out}{comma}{sp2}{new.year}"
        )
    return None


#: Date part at the start of a timestamp (``31/10/2026 à 14:22``, ``2026-10-31T14:22Z``).
_DATE_PREFIX = re.compile(
    r"^\s*(?:\d{1,2}([/.\-])\d{1,2}\1(?:\d{4}|\d{2})|\d{4}-\d{2}-\d{2}"
    r"|(?:1er|1re|\d{1,2})\s+[^\W\d_]+\.?\s+\d{4}|[^\W\d_]+\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4})"
    r"(?=$|[^\w]|T\d)",
    re.UNICODE,
)
_TIME_PART = re.compile(r"(?<!\d)(\d{1,2})(\s*[:hH]\s*)(\d{2})(?:(\s*[:mM]\s*)(\d{2}))?(?!\d)")
_TIME_ZONE_PART = re.compile(r"(?:UTC|GMT)?\s*[+\-]\d{1,2}(?:[:']?\d{2}'?)?")
_PDF_TIMESTAMP = re.compile(r"^(\s*D:)(\d{4})(\d{2})(\d{2})(\d{6}|\d{4}|\d{2})?(.*)$", re.DOTALL)


def shift_timestamp(original: str, days: int) -> str | None:
    """A date with a time of day (or a time alone): the date shifted by ``days``,
    the time of day and the time zone kept, so that every rendering of one instant
    (``2026-10-31T13:22:05Z``, ``31/10/2026 14:22``, ``Sat, 31 Oct 2026 13:22:05
    +0000``) moves by the same whole number of days and the order and intervals of
    the events are preserved.

    Handles ``31/10/2026 à 14:22``, ``2026-10-31T14:22:05.123+01:00``, ``14h22``,
    PDF-style ``D:20261031142205+01'00'``. None when ``original`` holds no time or a
    date this function cannot shift (the caller then randomizes the whole value).
    """
    m = _PDF_TIMESTAMP.match(original)
    if m:
        prefix, year, month, day, clock, rest = m.groups()
        parsed = _safe_date(int(year), int(month), int(day))
        if parsed is None:
            return None
        new = parsed + timedelta(days=days)
        return f"{prefix}{new.year:04d}{new.month:02d}{new.day:02d}{clock or ''}{rest}"
    m = _DATE_PREFIX.match(original)
    if m:
        shifted = shift_date(m.group(), days)
        if shifted is None:
            return None
        return shifted + original[m.end() :]
    # A time alone (with its zone) stays: any other number is a date this function
    # cannot shift (``2026.10.31 14:22``).
    leftover = _TIME_ZONE_PART.sub("", _TIME_PART.sub("", original))
    if any(c.isdigit() for c in leftover) or not _TIME_PART.search(original):
        return None
    return original


# -- places and organisations --------------------------------------------------------

_CP_CITY = re.compile(
    r"(?P<cp>\b(?:\d{5}|\d{2}\s\d{3}|2[AB]\d{3})\b)(?P<sp>\s+)(?P<city>.+)$", re.S
)
_STREET = re.compile(
    r"^(?P<num>\d{1,4}(?:\s?(?:bis|ter|quater|[a-dA-D])\b)?)(?P<sep>\s*,?\s*)(?P<rest>.+)$", re.S
)


def real_commune(rng: random.Random) -> tuple[str, str]:
    """A real ``(postcode, commune)`` pair (:mod:`anonyma.data.communes`): an
    invented commune or postcode gives a substitute address away."""
    return rng.choice(communes())


def fake_city(rng: random.Random) -> str:
    return real_commune(rng)[1]


def fake_postcode(rng: random.Random) -> str:
    return real_commune(rng)[0]


def _postcode_like(original: str, postcode: str) -> str:
    """``postcode`` written like ``original`` (``75 008`` -> ``69 001``)."""
    return reshape(original, postcode) if any(c.isspace() for c in original) else postcode


_COMPLEMENT = re.compile(rf"\s*{ADDRESS_COMPLEMENT}\s*")
_COMPLEMENT_CODE = re.compile(r"\w*\d\w*|(?<!\w)[A-Z](?!\w)")


def fake_complement(original: str, rng: random.Random) -> str:
    """An address complement keeps its words and gets other codes: digits and capital
    letters of each code change, ordinal suffixes and keywords stay."""

    def code(m: re.Match[str]) -> str:
        text = m.group()
        out = []
        for i, c in enumerate(text):
            if c.isdigit():
                # A number never gains a leading zero (a floor, a door number).
                lead = c != "0" and (i == 0 or not text[i - 1].isdigit())
                out.append(rng.choice(string.digits[1:] if lead else string.digits))
            else:
                out.append(rng.choice(string.ascii_uppercase) if c.isupper() else c)
        return "".join(out)

    for _ in range(8):
        fake = _COMPLEMENT_CODE.sub(code, original)
        if fake != original:
            return fake
    return fake


def fake_address(original: str, rng: random.Random) -> str:
    """Street (number + street name) and/or postcode + city, separators kept. The
    postcode and city are a real pair; a postcode alone becomes a real postcode; a
    complement alone (building, door...) keeps its words with other codes."""
    if _COMPLEMENT.fullmatch(original):
        return fake_complement(original, rng)
    fake = faker_fr()
    fake.seed_instance(rng.getrandbits(32))
    text = original
    if re.fullmatch(r"\s*(?:\d{5}|\d{2}\s\d{3})\s*", text):
        core = text.strip()
        return text.replace(core, _postcode_like(core, fake_postcode(rng)))
    cp_city = _CP_CITY.search(text)
    street_part = text[: cp_city.start()] if cp_city else text
    out = ""
    if street_part.strip():
        sm = _STREET.match(street_part.strip())
        street = str(fake.street_name())
        lead = street_part[: len(street_part) - len(street_part.lstrip())]
        trail_match = re.search(r"[\s,]*$", street_part)
        trail = trail_match.group() if trail_match else ""
        if sm:
            number = str(rng.randint(1, 120))
            if not sm.group("num").isdigit():
                number += sm.group("num").lstrip("0123456789")
            out = f"{lead}{number}{sm.group('sep') or ' '}{mirror_case(sm.group('rest'), street)}"
        else:
            out = lead + mirror_case(street_part.strip(), street)
        out += trail
    if cp_city:
        postcode, commune = real_commune(rng)
        city = cp_city.group("city")
        trail = city[len(city.rstrip()) :]
        out += (
            f"{_postcode_like(cp_city.group('cp'), postcode)}{cp_city.group('sp')}"
            f"{mirror_case(city, commune)}{trail}"
        )
    return out or str(fake.street_address())


# Organisations keep their shape: the designator in front (« Groupe », « Projet »)
# and the legal form behind (« SAS », « & Associés ») stay, only the distinctive core
# changes, so that « Groupe Sorbel » becomes « Groupe Courtois », « Sorbel SAS »
# « Courtois SAS » and a bare « Colibri » one word. Keys are :func:`compact_key` forms.
ORG_PREFIXES = frozenset(
    ["groupe", "group", "societe", "ste", "cabinet", "banque", "laboratoires", "laboratoire",
     "labo", "association", "asso", "fondation", "institut", "agence", "compagnie", "cie",
     "entreprise", "entreprises", "etablissements", "ets", "maison", "atelier", "ateliers",
     "studio", "clinique", "editions", "transports", "assurances", "mutuelle", "holding",
     "federation", "syndicat", "collectif", "projet", "programme", "operation", "chantier",
     "mission", "project", "program"]
)  # fmt: skip
#: Designators that make the name a project's: its core gets a code name.
PROJECT_PREFIXES = frozenset(["projet", "programme", "operation", "chantier", "mission",
                              "project", "program"])  # fmt: skip
_ORG_LINKS = frozenset(["de", "du", "des", "la", "le", "les"])
ORG_SUFFIXES = frozenset(
    ["sa", "sas", "sasu", "sarl", "eurl", "snc", "sci", "scp", "scop", "selarl", "selas", "sca",
     "gie", "gmbh", "ltd", "limited", "inc", "llc", "plc", "ag", "bv", "nv", "spa", "srl",
     "corp", "co", "associes", "associe", "avocats", "conseil", "conseils", "consulting",
     "partners", "partenaires", "group", "groupe", "france", "international", "holding",
     "industries", "industrie", "services", "solutions", "technologies", "freres", "fils",
     "cie"]
)  # fmt: skip
_SUFFIX_LINKS = frozenset(["&", "et", "and"])

#: Neutral code names for projects (birds, stones, stars, trees, winds): never a
#: person's or a company's name. Given names among them are dropped at load time.
PROJECT_WORDS = (
    "Héron", "Aigrette", "Mésange", "Pluvier", "Sterne", "Loriot", "Martinet", "Bouvreuil",
    "Grèbe", "Fauvette", "Bergeronnette", "Courlis", "Vanneau", "Guillemot", "Macareux",
    "Cormoran", "Albatros", "Pélican", "Flamant", "Ibis", "Toucan", "Épervier", "Gerfaut",
    "Busard", "Harfang", "Goéland", "Verdier", "Roitelet", "Sittelle", "Grimpereau",
    "Alouette", "Hirondelle", "Cigogne", "Bécasse", "Perdrix", "Onyx", "Agate", "Quartz",
    "Basalte", "Granit", "Obsidienne", "Jaspe", "Malachite", "Lapis", "Rubis", "Topaze",
    "Émeraude", "Grenat", "Péridot", "Béryl", "Calcite", "Pyrite", "Ardoise", "Porphyre",
    "Silex", "Tourmaline", "Cobalt", "Mica", "Deneb", "Rigel", "Bételgeuse", "Antarès",
    "Aldébaran", "Canopus", "Arcturus", "Pollux", "Castor", "Pégase", "Andromède", "Persée",
    "Céphée", "Hydre", "Mistral", "Sirocco", "Tramontane", "Noroît", "Mélèze", "Séquoia",
    "Cèdre", "Érable", "Bouleau", "Tilleul", "Frêne", "Platane", "Cyprès", "Genévrier",
    "Sorbier", "Aulne", "Saule",
)  # fmt: skip


def _token_key(token: str) -> str:
    return "&" if token == "&" else compact_key(token)


def split_organization(text: str) -> tuple[str, str, str]:
    """``Groupe Sorbel`` -> (``Groupe ``, ``Sorbel``, ``""``); ``Sorbel SAS`` ->
    (``""``, ``Sorbel``, `` SAS``); ``DocuSign, Inc.`` -> (``""``, ``DocuSign``,
    ``, Inc.``). The core always keeps at least one word."""
    tokens = list(re.finditer(r"\S+", text))
    if not tokens:
        return "", text, ""
    first, last = 0, len(tokens)
    while first < last - 1 and _token_key(tokens[first].group()) in ORG_PREFIXES:
        first += 1
        while first < last - 1 and _token_key(tokens[first].group()) in _ORG_LINKS:
            first += 1
    stripped = False
    while last - 1 > first:
        key = _token_key(tokens[last - 1].group())
        if key in ORG_SUFFIXES or (stripped and key in _SUFFIX_LINKS):
            last -= 1
            stripped = True
        else:
            break
    start, end = tokens[first].start(), tokens[last - 1].end()
    head, dash, rest = tokens[first].group().partition("-")
    if first == last - 1 and rest and _token_key(head) in ORG_PREFIXES:
        start += len(head) + len(dash)  # « PROJET-X »: the designator is joined
    while end > start + 1 and not text[end - 1].isalnum():
        end -= 1
    return text[:start], text[start:end], text[end:]


def is_project_name(text: str) -> bool:
    """« Projet Colibri », « Programme Héron »: named by a project designator."""
    prefix = split_organization(text)[0].split()
    return bool(prefix) and _token_key(prefix[0]) in PROJECT_PREFIXES


@lru_cache(maxsize=1)
def _org_word_pool() -> tuple[str, ...]:
    """Faker ``fr_FR`` surnames that read as a brand or a firm (« Courtois », «
    Lacroix »): one plain word, never a given name (« Groupe Jean » is not credible,
    « Groupe Martin » reads as a person)."""
    provider = faker_fr().provider("faker.providers.person")
    names = getattr(provider, "last_names", ()) if provider is not None else ()
    return tuple(
        sorted({str(n) for n in names if str(n).isalpha() and len(str(n)) >= 4
                and first_name(str(n)) is None})
    )  # fmt: skip


@lru_cache(maxsize=1)
def _project_pool() -> tuple[str, ...]:
    return tuple(w for w in PROJECT_WORDS if first_name(w) is None)


def fake_org_word(rng: random.Random) -> str:
    return rng.choice(_org_word_pool())


def fake_project_word(rng: random.Random) -> str:
    return rng.choice(_project_pool())


def fake_org_core(core: str, rng: random.Random, *, project: bool = False) -> str:
    """A substitute for the distinctive core of an organisation or project name: a
    code name for a project, letters for an acronym, else one surname-like word per
    word of the original (two at most), in the case of ``core``."""
    words = [w for w in core.split() if any(c.isalpha() for c in w)]
    if len(words) == 1 and core.isalpha() and core.isupper() and len(core) <= 4:
        return "".join(rng.choice(string.ascii_uppercase) for _ in core)
    if project:
        return mirror_case(core, fake_project_word(rng))
    picked = [fake_org_word(rng) for _ in range(min(max(len(words), 1), 2))]
    return mirror_case(core, " ".join(picked))


def render_organization(original: str, fake_core: str) -> str:
    """``original`` with its core replaced by ``fake_core`` (case of the core)."""
    prefix, core, suffix = split_organization(original)
    return f"{prefix}{mirror_case(core, fake_core)}{suffix}"


def fake_company(original: str, rng: random.Random) -> str:
    _, core, _ = split_organization(original)
    return render_organization(original, fake_org_core(core, rng))


def fake_job(original: str, rng: random.Random) -> str:
    fake = faker_fr()
    fake.seed_instance(rng.getrandbits(32))
    job = str(fake.job())
    if original[:1].isupper():
        job = job[:1].upper() + job[1:]
    return mirror_case(original, job)


def fake_last_name(original: str, rng: random.Random) -> str:
    fake = faker_fr()
    fake.seed_instance(rng.getrandbits(32))
    return mirror_case(original, str(fake.last_name()))


def fake_username(original: str, rng: random.Random) -> str:
    local = fake_local_part(rng)
    if any(c.isdigit() for c in original):
        local += str(rng.randint(1, 99))
    if original.startswith("@"):
        local = "@" + local.replace(".", "_")
    return local


def fake_url(original: str, domain: str, rng: random.Random) -> str:
    m = re.match(
        r"^(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]*://)?(?P<www>www\.)?(?P<host>[^/?#]+)(?P<rest>.*)$",
        original,
    )
    if not m:
        return f"https://www.{domain}/"
    scheme = m.group("scheme") or ""
    www = m.group("www") or ""
    rest = m.group("rest")
    path = ""
    if rest and rest != "/":
        path = "/" + "-".join(fake_local_part(rng).split(".")) if rest.startswith("/") else ""
    elif rest == "/":
        path = "/"
    return f"{scheme}{www}{domain}{path}"


def fake_message_id(original: str, domain: str, rng: random.Random) -> str:
    token = "".join(rng.choice("0123456789abcdef") for _ in range(24))
    core = f"{token}@{domain}"
    return f"<{core}>" if original.strip().startswith("<") else core


__all__ = [
    "PUBLIC_MAIL_DOMAINS",
    "FakePerson",
    "fake_address",
    "fake_bic",
    "fake_city",
    "fake_company",
    "fake_domain",
    "fake_iban",
    "fake_initials",
    "fake_ip",
    "fake_job",
    "fake_last_name",
    "fake_local_part",
    "fake_luhn",
    "fake_mac",
    "fake_message_id",
    "fake_nir",
    "fake_org_core",
    "fake_org_word",
    "fake_person",
    "fake_phone_national",
    "fake_plate",
    "fake_postcode",
    "fake_project_word",
    "fake_siren",
    "fake_siret",
    "fake_url",
    "fake_username",
    "fake_vat_fr",
    "local_like",
    "login_like",
    "match_case",
    "mirror_case",
    "random_like",
    "real_commune",
    "render_organization",
    "render_person",
    "render_phone",
    "reshape",
    "shift_date",
    "slug",
    "split_domain",
    "split_organization",
]
