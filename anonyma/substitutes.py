"""Realistic fake values for the REALISTIC replacement mode.

Every generator is deterministic for a given ``random.Random`` and preserves the
shape of the original (case, separators, grouping, prefixes). Numbers carry valid
check digits (Luhn, mod-97, RIB and NIR keys); phone numbers use the ARCEP ranges
reserved for fiction when possible; IP addresses use documentation ranges.
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
from anonyma.data import first_names
from anonyma.names import LOGIN_TEMPLATES as _LOGIN_TEMPLATES
from anonyma.names import PersonName, initials_of, is_initial, login_parts

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
    """Write ``fake_chars`` into the alphanumeric positions of ``original``."""
    out: list[str] = []
    it = iter(fake_chars)
    for ch in original:
        if ch.isalnum():
            nxt = next(it, None)
            if nxt is None:
                out.append(ch)
                continue
            out.append(nxt.lower() if ch.islower() else nxt)
        else:
            out.append(ch)
    return "".join(out)


def random_like(original: str, rng: random.Random) -> str:
    """Same shape: digits -> digits, letters -> letters (case kept), rest kept."""
    out: list[str] = []
    for ch in original:
        if ch.isdigit():
            out.append(rng.choice(string.digits))
        elif ch.isalpha():
            letter = rng.choice(string.ascii_uppercase)
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
    parts = login_parts(
        PersonName("", fake.given, (fake.surname,), False, fake.gender)  # given + surname
    )
    if parts is None:
        return original
    return match_case(original, _LOGIN_TEMPLATES[template](*parts))


def fake_initials(fake: FakePerson) -> str:
    return "".join(initials_of(g) for g in fake.given)


# -- contact data -----------------------------------------------------------------


def fake_domain(rng: random.Random) -> str:
    fake = faker_fr()
    fake.seed_instance(rng.getrandbits(32))
    word = re.sub(r"[^a-z0-9]", "", checksums.compact(fake.domain_word()).lower()) or "exemple"
    return f"{word}.example"


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


def fake_nir(original: str, rng: random.Random) -> str:
    compact = checksums.compact(original)
    sex = compact[:1] if compact[:1] in "12378" else "1"
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


def shift_date(original: str, days: int) -> str | None:
    """``original`` shifted by ``days``, in the same format; None if not parseable."""
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
        day_out = (
            "1er"
            if new.day == 1 and day_s in ("1er", "1re")
            else (f"{new.day:02d}" if len(day_s) == 2 and day_s.isdigit() else str(new.day))
        )
        month_out = _month_name(new.month - 1, found[1], month_s)
        dot_out = dot if found[1].endswith("abbr") else ""
        return original.replace(text, f"{day_out}{sp1}{month_out}{dot_out}{sp2}{new.year}")
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


# -- places and organisations --------------------------------------------------------

_CP_CITY = re.compile(
    r"(?P<cp>\b(?:\d{5}|\d{2}\s\d{3}|2[AB]\d{3})\b)(?P<sp>\s+)(?P<city>.+)$", re.S
)
_STREET = re.compile(
    r"^(?P<num>\d{1,4}(?:\s?(?:bis|ter|quater|[a-dA-D])\b)?)(?P<sep>\s*,?\s*)(?P<rest>.+)$", re.S
)


def fake_city(rng: random.Random) -> str:
    fake = faker_fr()
    fake.seed_instance(rng.getrandbits(32))
    return str(fake.city())


def fake_postcode(rng: random.Random) -> str:
    fake = faker_fr()
    fake.seed_instance(rng.getrandbits(32))
    return str(fake.postcode())


def fake_address(original: str, rng: random.Random) -> str:
    """Street (number + street name) and/or postcode + city, separators kept."""
    fake = faker_fr()
    fake.seed_instance(rng.getrandbits(32))
    text = original
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
        city = mirror_case(cp_city.group("city"), str(fake.city()))
        out += f"{fake.postcode()}{cp_city.group('sp')}{city}"
    return out or str(fake.street_address())


def fake_company(original: str, rng: random.Random) -> str:
    fake = faker_fr()
    fake.seed_instance(rng.getrandbits(32))
    return mirror_case(original, str(fake.company()))


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
    "fake_person",
    "fake_phone_national",
    "fake_plate",
    "fake_postcode",
    "fake_siren",
    "fake_siret",
    "fake_url",
    "fake_username",
    "fake_vat_fr",
    "login_like",
    "match_case",
    "mirror_case",
    "random_like",
    "render_person",
    "render_phone",
    "reshape",
    "shift_date",
]
