"""Generate the anonymisation benchmark corpus: ``uv run python -m tests.benchmark.generate``.

Every document lives in its own folder ``corpus/<slug>/`` and has a ground truth
file ``ground_truth/<slug>.json`` listing each sensitive value with its type, where
it appears, the literal forms written in the document (``surfaces``) and the
renderings whose survival in the output means a leak (see ``common.py``).

Everything is synthetic: invented people, ``exemple.test`` / ``.example`` domains,
ARCEP / Ofcom / NANP fiction phone ranges, documentation IP ranges, and IBAN, card,
NIR, SIREN/SIRET and VAT numbers generated here with valid checksums. The output is
deterministic for a given seed (7z archives embed the generation time).
"""

from __future__ import annotations

import io
import json
import random
import shutil
import struct
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import formataddr
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape, quoteattr

from PIL import Image, ImageDraw
from PIL.PngImagePlugin import PngInfo

from tests.benchmark.common import CORPUS, DIRECT_TYPES, TRUTH
from tests.fixtures.generators.mail_archive_image import _add_props, font, one_off
from tests.fixtures.generators.office import (
    NS_A,
    ODF_DECL,
    ODF_NS,
    PML,
    SML,
    WML,
    XML_DECL,
    content_types,
    odf_manifest,
    p_doc,
    p_shape,
    package_rels,
    rels,
    s_doc,
    w_doc,
    write_zip,
)

SEED = 20261031
RNG = random.Random(SEED)

MOIS = [
    "",
    "janvier",
    "février",
    "mars",
    "avril",
    "mai",
    "juin",
    "juillet",
    "août",
    "septembre",
    "octobre",
    "novembre",
    "décembre",
]
MONTHS = [
    "",
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
]

# Surname tokens that are also ordinary French words or places: only their
# capitalised / upper-case spelling counts as a rendering of the person.
COMMON_WORDS = {
    "martin",
    "petit",
    "boucher",
    "marchand",
    "blanc",
    "fontaine",
    "chevalier",
    "vienne",
    "mercier",
    "lebrun",
}
PARTICLES = {"de", "du", "des", "le", "la", "da", "van", "von", "d", "di"}


def x(text: str) -> str:
    """XML-escape element text."""
    return escape(text)


def xa(text: str) -> str:
    """XML-escape an attribute value (with its quotes)."""
    return quoteattr(text)


# ---------------------------------------------------------------------------
# Checksummed numbers
# ---------------------------------------------------------------------------


def digits(n: int) -> str:
    return "".join(str(RNG.randrange(10)) for _ in range(n))


def luhn_digit(payload: str) -> str:
    total = 0
    for i, ch in enumerate(reversed(payload)):
        d = int(ch)
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return str((10 - total % 10) % 10)


def luhn_ok(number: str) -> bool:
    return luhn_digit(number[:-1]) == number[-1]


def iban_from_bban(country: str, bban: str) -> str:
    rearranged = bban + country + "00"
    numeric = int("".join(str(int(c, 36)) for c in rearranged))
    return f"{country}{98 - numeric % 97:02d}{bban}"


def iban_fr() -> str:
    bank, branch, account = digits(5), digits(5), digits(11)
    key = 97 - ((89 * int(bank) + 15 * int(branch) + 3 * int(account)) % 97)
    return iban_from_bban("FR", f"{bank}{branch}{account}{key:02d}")


def iban_de() -> str:
    return iban_from_bban("DE", digits(8) + digits(10))


def iban_be() -> str:
    first = digits(10)
    check = int(first) % 97 or 97
    return iban_from_bban("BE", f"{first}{check:02d}")


def card_number(prefix: str = "4970") -> str:
    payload = prefix + digits(15 - len(prefix))
    return payload + luhn_digit(payload)


def nir(gender: str, born: date, dept: str) -> str:
    base = f"{1 if gender == 'M' else 2}{born.year % 100:02d}{born.month:02d}{dept}"
    base += digits(3) + digits(3)
    return f"{base}{97 - int(base) % 97:02d}"


def siren() -> str:
    payload = str(RNG.randrange(1, 10)) + digits(7)
    return payload + luhn_digit(payload)


def siret(siren_number: str) -> str:
    payload = siren_number + digits(4)
    return payload + luhn_digit(payload)


def vat_fr(siren_number: str) -> str:
    return f"FR{(12 + 3 * (int(siren_number) % 97)) % 97:02d}{siren_number}"


# ARCEP numbers reserved for fiction (01 99 00, 02 61 91, ... 06 39 98).
FICTION_PREFIXES = ("19900", "26191", "35301", "46571", "53649", "63998")


def phone_fr(prefix: str = "63998") -> str:
    """National significant number (9 digits, without the leading 0)."""
    return prefix + digits(4)


def fmt_phone(core: str, style: str) -> str:
    pairs = [core[1:3], core[3:5], core[5:7], core[7:9]]
    first = "0" + core[0]
    nbsp = "\u00a0"
    styles = {
        "spaces": " ".join([first, *pairs]),
        "dots": ".".join([first, *pairs]),
        "dashes": "-".join([first, *pairs]),
        "glued": first + "".join(pairs),
        "intl": "+33 " + core[0] + " " + " ".join(pairs),
        "intl0": "+33 (0)" + core[0] + " " + " ".join(pairs),
        "intl00": "0033 " + core[0] + " " + " ".join(pairs),
        "intlglued": "+33" + core,
        "nbsp": nbsp.join([first, *pairs]),
        "odd": f"{first} {core[1:5]} {core[5:9]}",
        "slashes": "/".join([first, *pairs]),
        "double": f"{first}  {pairs[0]} {pairs[1]}  {pairs[2]} {pairs[3]}",
    }
    return styles[style]


def fmt_iban(iban: str, style: str) -> str:
    groups = [iban[i : i + 4] for i in range(0, len(iban), 4)]
    if style == "spaces":
        return " ".join(groups)
    if style == "glued":
        return iban
    if style == "dashes":
        return "-".join(groups)
    if style == "nbsp":
        return "\u00a0".join(groups)
    if style == "lower":
        return " ".join(groups).lower()
    if style == "rib":  # FR RIB-like grouping 4/5/5/11/2
        return f"{iban[:4]} {iban[4:9]} {iban[9:14]} {iban[14:25]} {iban[25:]}"
    if style == "double":
        return "  ".join(groups)
    raise ValueError(style)


def fmt_card(card: str, style: str) -> str:
    groups = [card[i : i + 4] for i in range(0, 16, 4)]
    return {"spaces": " ".join(groups), "dashes": "-".join(groups), "glued": card}[style]


def fmt_nir(n: str, style: str) -> str:
    spaced = f"{n[0]} {n[1:3]} {n[3:5]} {n[5:7]} {n[7:10]} {n[10:13]} {n[13:]}"
    return {
        "spaces": spaced,
        "glued": n,
        "key": f"{n[:13]} {n[13:]}",
        "dots": spaced.replace(" ", "."),
    }[style]


def fmt_date(d: date, style: str) -> str:
    day = "1er" if d.day == 1 else str(d.day)
    return {
        "slash": f"{d.day:02d}/{d.month:02d}/{d.year}",
        "dot": f"{d.day:02d}.{d.month:02d}.{d.year}",
        "iso": d.isoformat(),
        "long": f"{day} {MOIS[d.month]} {d.year}",
        "short": f"{d.day:02d}/{d.month:02d}/{d.year % 100:02d}",
        "us": f"{d.month:02d}/{d.day:02d}/{d.year}",
        "en": f"{MONTHS[d.month]} {d.day}, {d.year}",
        "dash": f"{d.day:02d}-{d.month:02d}-{d.year}",
    }[style]


def date_renderings(d: date) -> list[dict[str, str]]:
    forms = {fmt_date(d, s) for s in ("slash", "iso", "long", "short", "us", "en")}
    forms.add(f"{d.day} {MOIS[d.month]} {d.year}")
    forms.add(f"{d.day} {MONTHS[d.month]} {d.year}")
    forms.add(f"{d.day:02d}{d.month:02d}{d.year}")
    forms.add(f"{d.day}/{d.month}/{d.year}")
    return [{"mode": "text", "text": f} for f in sorted(forms)]


# ---------------------------------------------------------------------------
# People
# ---------------------------------------------------------------------------


@dataclass
class Address:
    number: str
    street: str
    postcode: str
    city: str
    extra: str = ""

    @property
    def line1(self) -> str:
        return f"{self.number} {self.street}"

    @property
    def line2(self) -> str:
        return f"{self.postcode} {self.city}"

    @property
    def one_line(self) -> str:
        parts = [self.extra, self.line1, self.line2] if self.extra else [self.line1, self.line2]
        return ", ".join(parts)

    @property
    def value(self) -> str:
        return f"{self.line1}, {self.line2}"


@dataclass
class Person:
    first: str
    last: str
    gender: str
    born: date
    address: Address
    email: str = ""
    username: str = ""
    phone: str = ""
    nir: str = ""
    iban: str = ""
    card: str = ""
    plate: str = ""
    ip: str = ""
    job: str = ""

    @property
    def full(self) -> str:
        return f"{self.first} {self.last}"

    @property
    def civ(self) -> str:
        return "Mme" if self.gender == "F" else "M."

    @property
    def upper(self) -> str:
        return f"{self.last.upper()} {self.first}"

    @property
    def initials(self) -> str:
        firsts = "".join(f"{p[0]}." for p in self.first.replace("-", " ").split())
        return firsts + "".join(f"{p[0].upper()}." for p in self.surname_tokens())

    @property
    def initial_last(self) -> str:
        return f"{self.first[0]}. {self.last}"

    def surname_tokens(self) -> list[str]:
        parts = self.last.replace("-", " ").replace("'", " ").split()
        return [p for p in parts if p.lower() not in PARTICLES and len(p) >= 3]

    def renderings(self) -> list[dict[str, str]]:
        out = [
            {"mode": "text", "text": self.full},
            {"mode": "text", "text": f"{self.last} {self.first}"},
            {"mode": "text", "text": self.last},
        ]
        for tok in self.surname_tokens():
            mode = "word" if tok.lower() in COMMON_WORDS else "text"
            out.append({"mode": mode, "text": tok})
        if self.last.lower() in COMMON_WORDS:
            out[2]["mode"] = "word"
        unique: list[dict[str, str]] = []
        for r in out:
            if r not in unique:
                unique.append(r)
        return unique


def make_person(
    first: str,
    last: str,
    gender: str,
    born: date,
    address: Address,
    *,
    email: str | None = None,
    username: str | None = None,
    job: str = "",
    dept: str | None = None,
) -> Person:
    from tests.benchmark.common import strip_accents

    slug_first = strip_accents(first).lower().replace(" ", "-")
    slug_last = strip_accents(last).lower().replace(" ", "").replace("'", "")
    p = Person(first, last, gender, born, address, job=job)
    p.email = email or f"{slug_first}.{slug_last}@exemple.test"
    p.username = username or f"{slug_first[0]}{slug_last}"
    p.phone = phone_fr(RNG.choice(FICTION_PREFIXES[-1:]))
    if dept is None:
        dept = address.postcode[:2] if address.postcode.isdigit() else "99"
    p.nir = nir(gender, born, dept)
    p.iban = iban_fr()
    p.card = card_number(RNG.choice(["4970", "5132", "4561"]))
    letters = "ABCDEFGHJKLMNPQRSTVWXYZ"
    p.plate = (
        f"{RNG.choice(letters)}{RNG.choice(letters)}-{RNG.randrange(100, 999)}-"
        f"{RNG.choice(letters)}{RNG.choice(letters)}"
    )
    p.ip = f"{RNG.choice(['192.0.2', '198.51.100', '203.0.113'])}.{RNG.randrange(2, 254)}"
    return p


def build_people() -> dict[str, Person]:
    a = Address
    people = {
        "garnier": make_person(
            "Hélène",
            "Garnier",
            "F",
            date(1979, 2, 14),
            a("12", "rue des Glycines", "44000", "Nantes", "Bât. C, appartement 34"),
            job="directrice administrative et financière",
        ),
        "lefebvre": make_person(
            "Benoît",
            "Lefebvre",
            "M",
            date(1983, 11, 2),
            a("7", "impasse du Grand-Verger", "35000", "Rennes"),
            job="conseiller clientèle",
        ),
        "martin": make_person(
            "Claire",
            "Martin",
            "F",
            date(1990, 6, 9),
            a("3", "allée des Peupliers", "69003", "Lyon"),
            job="responsable communication",
        ),
        "petit": make_person(
            "Julien",
            "Petit",
            "M",
            date(1975, 9, 30),
            a("41", "boulevard Marcel-Brasseur", "59000", "Lille"),
            job="chef de projet",
        ),
        "boucher": make_person(
            "Sophie",
            "Boucher",
            "F",
            date(1986, 4, 17),
            a("18", "rue du Vieux-Pressoir", "21000", "Dijon"),
            job="responsable RH",
        ),
        "vienne": make_person(
            "Lorraine",
            "Vienne",
            "F",
            date(1968, 12, 1),
            a("5", "chemin des Écoliers", "86000", "Poitiers"),
            job="comptable",
        ),
        "kowalczyk": make_person(
            "Jean-Baptiste",
            "Kowalczyk",
            "M",
            date(1988, 7, 23),
            a("27", "rue Jules-Ferry-Prolongée", "57000", "Metz", "Résidence Les Mésanges"),
            email="jb.kowalczyk@exemple.test",
            username="jbkowal88",
            job="chauffeur poids lourd",
        ),
        "legoff": make_person(
            "Maëlle",
            "Le Goff",
            "F",
            date(1992, 3, 5),
            a("9", "venelle de Kerbriant", "29200", "Brest"),
            job="experte sinistres",
        ),
        "nguyen": make_person(
            "Thi Lan",
            "Nguyen",
            "F",
            date(1981, 10, 12),
            a("66", "avenue des Cerisiers-du-Japon", "13008", "Marseille"),
            email="tl.nguyen@exemple.test",
            job="pharmacienne",
        ),
        "valombreuse": make_person(
            "Aymeric",
            "de Valombreuse",
            "M",
            date(1959, 1, 28),
            a("2", "place du Pilori-Vieux", "37000", "Tours"),
            email="a.devalombreuse@exemple.test",
            username="aymeric.dv",
            job="notaire",
        ),
        "benali": make_person(
            "Inès",
            "Benali",
            "F",
            date(1995, 8, 19),
            a("110", "rue de la Tannerie-Basse", "31000", "Toulouse"),
            job="agente d'accueil",
        ),
        "delacroix": make_person(
            "Firmin",
            "Delacroix-Vasseur",
            "M",
            date(1972, 5, 6),
            a("4", "sentier des Grives", "76000", "Rouen"),
            email="firmin.dv@exemple.test",
            username="fdelacroixv",
            job="ingénieur méthodes",
        ),
        "marchand": make_person(
            "Océane",
            "Marchand",
            "F",
            date(1999, 2, 27),
            a("15", "rue des Tisserands-Réunis", "67000", "Strasbourg"),
            job="technicienne",
        ),
        "blanc": make_person(
            "Lucas",
            "Blanc",
            "M",
            date(1984, 12, 24),
            a("8", "quai des Mariniers-d'Antan", "33000", "Bordeaux"),
            job="artisan",
        ),
        "rousseau": make_person(
            "Agathe",
            "Rousseau",
            "F",
            date(1977, 7, 7),
            a("22", "rue de l'Ancienne-Gabelle", "45000", "Orléans"),
            job="avocate",
        ),
        "fontaine": make_person(
            "Rémi",
            "Fontaine",
            "M",
            date(1980, 1, 15),
            a("31", "rue des Charrons", "51100", "Reims"),
            job="gestionnaire de paie",
        ),
        "brunet": make_person(
            "Yannick",
            "Brunet",
            "M",
            date(1970, 3, 3),
            a("6", "allée Camille-Desrosiers", "63000", "Clermont-Ferrand"),
            job="auditeur",
        ),
        "chevalier": make_person(
            "Solène",
            "Chevalier",
            "F",
            date(1993, 11, 11),
            a("13", "rue Basse-des-Moulins", "80000", "Amiens"),
            job="juriste",
        ),
        "perrin": make_person(
            "Anaïs",
            "Perrin",
            "F",
            date(1987, 5, 20),
            a("19", "cours des Arbalétriers", "25000", "Besançon"),
            job="associée",
        ),
        "morel": make_person(
            "Quentin",
            "Morel",
            "M",
            date(1991, 9, 9),
            a("48", "rue du Four-à-Chaux", "87000", "Limoges"),
            job="manager",
        ),
        "girard": make_person(
            "Capucine",
            "Girard",
            "F",
            date(1996, 6, 1),
            a("3 bis", "rue des Pénitents-Gris", "84000", "Avignon"),
            job="assistante",
        ),
        "lambert": make_person(
            "Mathis",
            "Lambert",
            "M",
            date(2001, 4, 4),
            a("71", "route de la Garenne-Haute", "14000", "Caen"),
            job="apprenti",
        ),
        "faure": make_person(
            "Noémie",
            "Faure",
            "F",
            date(1982, 8, 8),
            a("17", "rue des Cordiers", "38000", "Grenoble"),
            job="gestionnaire RH",
        ),
        "mercier": make_person(
            "Gaëtan",
            "Mercier",
            "M",
            date(1978, 10, 31),
            a("25", "rue du Moulin-Tourneur", "49000", "Angers"),
            job="magasinier",
        ),
        "gauthier": make_person(
            "Ophélie",
            "Gauthier",
            "F",
            date(1989, 12, 12),
            a("1", "square des Hortensias-Bleus", "06000", "Nice"),
            job="cheffe de produit",
        ),
        "dumoulin": make_person(
            "Baptiste",
            "Dumoulin",
            "M",
            date(1985, 2, 2),
            a("90", "avenue du Haut-Fourneau", "42000", "Saint-Étienne"),
            job="commercial",
        ),
        "vidal": make_person(
            "Eléonore",
            "Vidal",
            "F",
            date(1974, 6, 21),
            a("14", "rue des Échevins", "34000", "Montpellier"),
            job="présidente",
        ),
        "durand": make_person(
            "Marc",
            "Durand",
            "M",
            date(1966, 9, 14),
            a("55", "rue de la Poterne", "54000", "Nancy"),
            job="trésorier",
        ),
        "pemberton": make_person(
            "Oliver",
            "Pemberton",
            "M",
            date(1985, 3, 14),
            a("221", "Larkspur Crescent", "BS1 4XE", "Bristol"),
            email="oliver.pemberton@company.example",
            username="opemberton",
            job="site manager",
        ),
        "ocallaghan": make_person(
            "Siobhan",
            "O'Callaghan",
            "F",
            date(1990, 11, 23),
            a("14", "Wexford Lane", "D04 X2Y1", "Dublin"),
            email="siobhan.ocallaghan@company.example",
            username="socallaghan",
            job="HR lead",
        ),
        "muller": make_person(
            "Lukas",
            "Müller",
            "M",
            date(1979, 7, 2),
            a("8", "Lindenweg", "10115", "Berlin"),
            email="lukas.mueller@firma.example",
            username="lmueller",
            job="Einkäufer",
        ),
    }
    people["muller"].iban = iban_de()
    people["pemberton"].iban = iban_be()
    return people


FILLER_FIRST = [
    ("Éloïse", "F"),
    ("Bastien", "M"),
    ("Margaux", "F"),
    ("Thibault", "M"),
    ("Léonie", "F"),
    ("Corentin", "M"),
    ("Apolline", "F"),
    ("Maxence", "M"),
    ("Clémence", "F"),
    ("Hugo", "M"),
    ("Jeanne", "F"),
    ("Valentin", "M"),
]
FILLER_LAST = [
    "Ravier",
    "Tessier",
    "Lebrun",
    "Charpentier",
    "Guillot",
    "Poirier",
    "Barbier",
    "Leclerc",
    "Hamelin",
    "Joubert",
    "Royer",
    "Lemaire",
]
STREETS = [
    "rue des Tilleuls-Argentés",
    "allée des Sorbiers",
    "rue du Pont-Levis",
    "avenue des Acacias",
    "impasse des Ajoncs",
    "rue de la Forge-Neuve",
    "chemin du Bois-Joli",
    "rue des Halles-Basses",
    "place de la Halle-aux-Grains",
    "rue des Lavandières",
    "boulevard des Platanes",
    "rue Saint-Roch-des-Champs",
]
CITIES = [
    ("72000", "Le Mans"),
    ("17000", "La Rochelle"),
    ("56100", "Lorient"),
    ("64000", "Pau"),
    ("66000", "Perpignan"),
    ("68100", "Mulhouse"),
    ("03100", "Montluçon"),
    ("41000", "Blois"),
    ("62000", "Arras"),
    ("16000", "Angoulême"),
    ("71100", "Chalon-sur-Saône"),
    ("18000", "Bourges"),
]


def filler_people() -> list[Person]:
    out = []
    for i, ((first, gender), last) in enumerate(zip(FILLER_FIRST, FILLER_LAST, strict=True)):
        postcode, city = CITIES[i]
        born = date(RNG.randrange(1950, 2004), RNG.randrange(1, 13), RNG.randrange(1, 29))
        addr = Address(str(RNG.randrange(1, 120)), STREETS[i], postcode, city)
        out.append(make_person(first, last, gender, born, addr))
    return out


# ---------------------------------------------------------------------------
# Ground truth model
# ---------------------------------------------------------------------------


def renderings_for(kind: str, value: str) -> list[dict[str, str]]:
    from tests.benchmark.common import norm_compact

    if kind == "EMAIL":
        local = value.split("@", maxsplit=1)[0]
        return [{"mode": "raw", "text": value}, {"mode": "raw", "text": local + "@"}]
    if kind in {"IBAN", "CARTE", "NIR", "PLAQUE", "SIREN", "SIRET", "TVA", "NUMERO_DOSSIER"}:
        out = [{"mode": "compact", "text": norm_compact(value)}]
        if kind == "IBAN":
            out.append({"mode": "compact", "text": norm_compact(value)[4:]})
        if kind == "NIR":
            out.append({"mode": "compact", "text": norm_compact(value)[:13]})
        return out
    if kind == "IP":
        return [{"mode": "ip", "text": value}]
    if kind == "URL":
        return [{"mode": "raw", "text": value.split("://", 1)[-1]}]
    if kind == "DATE_NAISSANCE":
        return date_renderings(date.fromisoformat(value))
    if kind == "GPS":
        return [{"mode": "gps", "text": value}]
    return [{"mode": "text", "text": value}]


@dataclass
class Doc:
    slug: str
    fmt: str
    description: str
    entries: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    keep: list[str] = field(default_factory=list)
    images: dict[str, dict[str, Any]] = field(default_factory=dict)
    pending: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)

    @property
    def dir(self) -> Path:
        return OUTPUT["corpus"] / self.slug

    def add(
        self,
        kind: str,
        value: str,
        where: str,
        *surfaces: str,
        renderings: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        key = (kind, value)
        entry = self.entries.get(key)
        if entry is None:
            entry = {
                "id": f"{self.slug.split('_')[0]}-{len(self.entries) + 1:03d}",
                "type": kind,
                "direct": kind in DIRECT_TYPES,
                "value": value,
                "where": [],
                "surfaces": [],
                "renderings": renderings or renderings_for(kind, value),
                "partial": [],
                "regions": [],
            }
            self.entries[key] = entry
        if where not in entry["where"]:
            entry["where"].append(where)
        for s in surfaces or (value,):
            if s not in entry["surfaces"]:
                entry["surfaces"].append(s)
        return entry

    def person(self, p: Person, where: str, *surfaces: str, first_only: bool = False) -> None:
        """Register a person; the first name alone is a partial rendering (reported apart)."""
        entry = self.add(
            "PERSONNE", p.full, where, *(surfaces or (p.full,)), renderings=p.renderings()
        )
        if p.first not in entry["surfaces"]:
            entry["surfaces"].append(p.first)
        partial = {"mode": "word", "text": p.first}
        if partial not in entry["partial"]:
            entry["partial"].append(partial)
        if first_only:
            entry["first_name_alone"] = True

    def phone(self, core: str, where: str, *surfaces: str) -> None:
        self.add(
            "TELEPHONE",
            "+33" + core if len(core) == 9 else core,
            where,
            *surfaces,
            renderings=[{"mode": "compact", "text": core}],
        )

    def intl_phone(self, e164: str, national: str, where: str, *surfaces: str) -> None:
        self.add(
            "TELEPHONE", e164, where, *surfaces, renderings=[{"mode": "compact", "text": national}]
        )

    def dob(self, d: date, where: str, *surfaces: str) -> None:
        self.add("DATE_NAISSANCE", d.isoformat(), where, *surfaces)

    def address(self, a: Address, where: str, *surfaces: str) -> None:
        rend = [{"mode": "text", "text": a.line1}, {"mode": "text", "text": a.street}]
        self.add("ADRESSE", a.value, where, *(surfaces or (a.line1, a.line2)), renderings=rend)

    def region(self, kind: str, value: str, image: str, bbox: tuple[float, ...]) -> None:
        self.pending.append((kind, value, {"image": image, "bbox": [round(v, 4) for v in bbox]}))

    def save(self) -> None:
        for kind, value, region in self.pending:
            self.entries[(kind, value)]["regions"].append(region)
        data = {
            "slug": self.slug,
            "format": self.fmt,
            "description": self.description,
            "files": sorted(
                p.relative_to(self.dir).as_posix() for p in self.dir.rglob("*") if p.is_file()
            ),
            "entries": list(self.entries.values()),
            "keep": self.keep,
            "images": self.images,
        }
        truth = OUTPUT["truth"]
        truth.mkdir(parents=True, exist_ok=True)
        (truth / f"{self.slug}.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n"
        )


# Ordinary sentences that must stay readable (precision / negative controls).
NEGATIVES = [
    "La réunion budgétaire du comité de direction est reportée au trimestre prochain.",
    "Le petit-déjeuner sera servi à partir de sept heures dans la salle du rez-de-chaussée.",
    "Un martin-pêcheur a été aperçu près de la fontaine du parc municipal.",
    "Le boucher du marché couvert propose désormais des produits bio.",
    "Merci de noter que le chevalier blanc reste une figure de style dans ce rapport.",
    "Le montant total s'élève à 1 250,00 euros hors taxes pour l'ensemble des prestations.",
    "Conformément à l'article L. 1234-5 du Code du travail, le préavis est de deux mois.",
    "La version 2.3.1 du logiciel corrige le problème signalé à la page 12 du manuel.",
    "Le marchand de journaux ferme le dimanche après-midi.",
    "Le comité se réunira le 12 mars 2026 à 14 h 30 en salle Bleue.",
]


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------


class Canvas:
    def __init__(self, width: int, height: int, background: str = "white") -> None:
        self.image = Image.new("RGB", (width, height), background)
        self.draw = ImageDraw.Draw(self.image)
        self.lines: list[tuple[float, float, str, int]] = []

    def text(self, xy: tuple[float, float], line: str, size: int = 30, fill: str = "black") -> None:
        self.draw.text(xy, line, fill=fill, font=font(size))
        self.lines.append((xy[0], xy[1], line, size))

    def bbox(self, sub: str) -> tuple[float, float, float, float]:
        """Fractional bounding box of the first drawn occurrence of ``sub``."""
        for x0, y0, line, size in self.lines:
            idx = line.find(sub)
            if idx < 0:
                continue
            f = font(size)
            left = x0 + f.getlength(line[:idx])
            right = x0 + f.getlength(line[: idx + len(sub)])
            _, top, _, bottom = self.draw.textbbox((x0, y0), line, font=f)
            w, h = self.image.size
            return (
                max(0.0, (left - 2) / w),
                max(0.0, (top - 2) / h),
                min(1.0, (right + 2) / w),
                min(1.0, (bottom + 2) / h),
            )
        raise KeyError(sub)

    def png(self, info: PngInfo | None = None) -> bytes:
        buffer = io.BytesIO()
        self.image.save(buffer, "PNG", pnginfo=info)
        return buffer.getvalue()

    def jpeg(self, quality: int = 90, exif: bytes | None = None) -> bytes:
        buffer = io.BytesIO()
        if exif is None:
            self.image.save(buffer, "JPEG", quality=quality)
        else:
            self.image.save(buffer, "JPEG", quality=quality, exif=exif)
        return buffer.getvalue()


def mark(doc: Doc, canvas: Canvas, image: str, kind: str, value: str, sub: str) -> None:
    doc.region(kind, value, image, canvas.bbox(sub))


# ---------------------------------------------------------------------------
# Office builders (raw XML, like tests/fixtures/generators/office.py)
# ---------------------------------------------------------------------------


def zip_entry(zf: zipfile.ZipFile, name: str, data: str | bytes) -> None:
    """Archive member with a fixed date (deterministic archives)."""
    info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    zf.writestr(info, data.encode("utf-8") if isinstance(data, str) else data)


def ooxml_core(title: str, creator: str, last_by: str, keywords: str = "") -> str:
    return (
        XML_DECL
        + '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/'
        'core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:dcterms="http://purl.org/dc/terms/" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        f"<dc:title>{x(title)}</dc:title><dc:creator>{x(creator)}</dc:creator>"
        f"<cp:lastModifiedBy>{x(last_by)}</cp:lastModifiedBy>"
        f"<cp:keywords>{x(keywords)}</cp:keywords>"
        '<dcterms:created xsi:type="dcterms:W3CDTF">2026-01-01T00:00:00Z</dcterms:created>'
        "</cp:coreProperties>"
    )


def ooxml_app(application: str, company: str, manager: str) -> str:
    return (
        XML_DECL + '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/'
        'extended-properties" xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/'
        'docPropsVTypes">'
        f"<Application>{application}</Application><Company>{x(company)}</Company>"
        f"<Manager>{x(manager)}</Manager></Properties>"
    )


def ooxml_custom(name: str, value: str) -> str:
    return (
        XML_DECL + '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/'
        'custom-properties" xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/'
        'docPropsVTypes">'
        f'<property fmtid="{{D5CDD505-2E9C-101B-9397-08002B2CF9AE}}" pid="2" name={xa(name)}>'
        f"<vt:lpwstr>{x(value)}</vt:lpwstr></property></Properties>"
    )


def wp(*runs: str, style: str = "") -> str:
    """A WordprocessingML paragraph from runs (strings are plain runs)."""
    ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    body = "".join(r if r.startswith("<") else wr(r) for r in runs)
    return f"<w:p>{ppr}{body}</w:p>"


def wr(text: str, *, bold: bool = False, italic: bool = False) -> str:
    rpr = ""
    if bold or italic:
        rpr = "<w:rPr>" + ("<w:b/>" if bold else "") + ("<w:i/>" if italic else "") + "</w:rPr>"
    return f'<w:r>{rpr}<w:t xml:space="preserve">{x(text)}</w:t></w:r>'


def docx_package(
    body: str,
    *,
    header: str,
    footer: str,
    comments: str = "",
    footnotes: str = "",
    core: str,
    app: str,
    custom: str,
    extra_rels: list[tuple[str, str, str, bool]] | None = None,
    media: dict[str, bytes] | None = None,
) -> bytes:
    sect = (
        '<w:sectPr><w:headerReference w:type="default" r:id="rIdH"/>'
        '<w:footerReference w:type="default" r:id="rIdF"/></w:sectPr>'
    )
    doc_rels = [
        ("rIdH", "header", "header1.xml", False),
        ("rIdF", "footer", "footer1.xml", False),
        ("rIdS", "settings", "settings.xml", False),
        *(extra_rels or []),
    ]
    overrides = {
        "word/document.xml": f"{WML}.document.main+xml",
        "word/header1.xml": f"{WML}.header+xml",
        "word/footer1.xml": f"{WML}.footer+xml",
        "word/settings.xml": f"{WML}.settings+xml",
    }
    entries: dict[str, str | bytes] = {}
    if comments:
        doc_rels.append(("rIdC", "comments", "comments.xml", False))
        overrides["word/comments.xml"] = f"{WML}.comments+xml"
        entries["word/comments.xml"] = w_doc("comments", comments)
    if footnotes:
        doc_rels.append(("rIdFn", "footnotes", "footnotes.xml", False))
        overrides["word/footnotes.xml"] = f"{WML}.footnotes+xml"
        entries["word/footnotes.xml"] = w_doc("footnotes", footnotes)
    entries.update(
        {
            "[Content_Types].xml": content_types(overrides),
            "_rels/.rels": package_rels("word/document.xml"),
            "word/document.xml": w_doc("document", f"<w:body>{body}{sect}</w:body>"),
            "word/_rels/document.xml.rels": rels(doc_rels),
            "word/header1.xml": w_doc("hdr", header),
            "word/footer1.xml": w_doc("ftr", footer),
            "word/settings.xml": w_doc("settings", '<w:zoom w:percent="100"/>'),
            "docProps/core.xml": core,
            "docProps/app.xml": app,
            "docProps/custom.xml": custom,
        }
    )
    for name, data in (media or {}).items():
        entries[f"word/media/{name}"] = data
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in entries.items():
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, content.encode("utf-8") if isinstance(content, str) else content)
    return buffer.getvalue()


def docx_image_paragraph(rid: str, descr: str) -> str:
    return (
        '<w:p><w:r><w:drawing><wp:inline xmlns:wp="http://schemas.openxmlformats.org/'
        'drawingml/2006/wordprocessingDrawing">'
        f'<wp:extent cx="4572000" cy="1524000"/><wp:docPr id="1" name="Image 1" descr={xa(descr)}/>'
        f'<a:graphic xmlns:a="{NS_A}"><a:graphicData uri="http://schemas.openxmlformats.org/'
        'drawingml/2006/picture"><pic:pic xmlns:pic="http://schemas.openxmlformats.org/'
        'drawingml/2006/picture"><pic:nvPicPr><pic:cNvPr id="1" name="Image 1"/>'
        f'<pic:cNvPicPr/></pic:nvPicPr><pic:blipFill><a:blip r:embed="{rid}"/></pic:blipFill>'
        "<pic:spPr/></pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>"
    )


def simple_docx(paragraphs: list[str], *, title: str, author: str) -> bytes:
    body = "".join(wp(p) for p in paragraphs)
    return docx_package(
        body,
        header=wp(""),
        footer=wp(""),
        core=ooxml_core(title, author, author),
        app=ooxml_app("Microsoft Office Word", "", ""),
        custom=ooxml_custom("Source", "export"),
    )


def odf_package_bytes(mimetype: str, body: str, *, meta: str, header: str, footer: str) -> bytes:
    content = (
        ODF_DECL + f'<office:document-content {ODF_NS} office:version="1.3"><office:body>{body}'
        "</office:body></office:document-content>"
    )
    styles = (
        ODF_DECL + f'<office:document-styles {ODF_NS} office:version="1.3"><office:master-styles>'
        '<style:master-page style:name="Standard">'
        f"<style:header><text:p>{header}</text:p></style:header>"
        f"<style:footer><text:p>{footer}</text:p></style:footer>"
        "</style:master-page></office:master-styles></office:document-styles>"
    )
    entries: dict[str, str | bytes] = {
        "mimetype": mimetype,
        "content.xml": content,
        "styles.xml": styles,
        "meta.xml": ODF_DECL
        + f'<office:document-meta {ODF_NS} office:version="1.3"><office:meta>{meta}'
        "</office:meta></office:document-meta>",
        "META-INF/manifest.xml": odf_manifest(mimetype, ()),
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED if name == "mimetype" else zipfile.ZIP_DEFLATED
            zf.writestr(info, data.encode("utf-8") if isinstance(data, str) else data)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Document builders
# ---------------------------------------------------------------------------

P: dict[str, Person] = {}
COMPANY: dict[str, tuple[str, str]] = {}
#: Output folders (``main(root)`` may redirect them, e.g. to check determinism).
OUTPUT = {"corpus": CORPUS, "truth": TRUTH}


def build_companies() -> dict[str, tuple[str, str]]:
    return {
        "valmont": ("Transports Valmont SAS", siren()),
        "lerouge": ("Cabinet Lerouge & Associés", siren()),
        "hauts": ("Mutuelle des Hauts-Plateaux", siren()),
        "bellerive": ("Assurances Bellerive", siren()),
        "orme": ("Clinique de l'Orme-Rond", siren()),
    }


def company(doc: Doc, key: str, where: str, *, with_ids: bool = False) -> tuple[str, str, str, str]:
    name, sn = COMPANY[key]
    st = siret(sn)
    vat = vat_fr(sn)
    doc.add("ORGANISATION", name, where)
    if with_ids:
        doc.add("SIREN", sn, where, f"{sn[:3]} {sn[3:6]} {sn[6:]}")
        doc.add("SIRET", st, where, f"{st[:3]} {st[3:6]} {st[6:9]} {st[9:]}")
        doc.add("TVA", vat, where, vat)
    return name, sn, st, vat


def doc_txt_lettre() -> Doc:
    doc = Doc("01_txt_lettre", "txt", "Lettre de réclamation (texte brut UTF-8)")
    h, b = P["garnier"], P["lefebvre"]
    org, sn, st, vat = company(doc, "valmont", "corps", with_ids=True)
    url = f"https://www.transports-valmont.example/suivi?client={h.username}"
    text = f"""{h.first} {h.last.upper()}
{h.address.extra}
{h.address.line1}
{h.address.line2}
Tél. : {fmt_phone(h.phone, "spaces")} / {fmt_phone(P["garnier"].phone[:5] + "1234", "intl0")}
Courriel : {h.email}

{org} — Service client
SIREN {sn[:3]} {sn[3:6]} {sn[6:]} — SIRET {st[:3]} {st[3:6]} {st[6:9]} {st[9:]}
TVA intracommunautaire : {vat}

Nantes, le 4 mars 2026

Objet : réclamation, dossier n° SIN-2024-008812

Madame, Monsieur,

Je soussignée {h.civ} {h.full}, née le {fmt_date(h.born, "slash")} à Rennes, numéro de
sécurité sociale {fmt_nir(h.nir, "spaces")}, {h.job} de profession, conteste le
prélèvement effectué sur mon compte {fmt_iban(h.iban, "spaces")}.

Ma carte bancaire {fmt_card(h.card, "spaces")} a également été débitée deux fois.
Mon véhicule immatriculé {h.plate} n'a jamais quitté le dépôt de {h.address.city}.
Votre site indique une connexion depuis l'adresse {h.ip} avec l'identifiant {h.username}
(lien de suivi : {url}).

Votre conseiller, {b.civ} {b.full}, m'avait assuré que le dossier serait clos. {b.civ} {b.last}
ne répond plus à mes appels au {fmt_phone(b.phone, "dots")} ni à ses courriels ({b.email}).

{NEGATIVES[0]}
{NEGATIVES[5]}

Je vous prie d'agréer, Madame, Monsieur, mes salutations distinguées.

{h.initial_last}
P.-S. : mon second numéro est le {fmt_phone(h.phone, "nbsp")} ; ancien RIB {fmt_iban(b.iban, "rib")}
(compte joint ouvert par {b.first} {b.last.upper()}).
"""
    doc.dir.mkdir(parents=True)
    (doc.dir / "lettre_reclamation.txt").write_text(text, encoding="utf-8", newline="\n")
    doc.person(h, "corps, signature", f"{h.first} {h.last.upper()}", h.full, h.initial_last)
    doc.address(h.address, "en-tête (sur trois lignes)", h.address.line1, h.address.line2)
    doc.add(
        "ADRESSE",
        h.address.extra,
        "en-tête",
        renderings=[{"mode": "text", "text": h.address.extra}],
    )
    doc.phone(
        h.phone, "en-tête, post-scriptum", fmt_phone(h.phone, "spaces"), fmt_phone(h.phone, "nbsp")
    )
    second = h.phone[:5] + "1234"
    doc.phone(second, "en-tête", fmt_phone(second, "intl0"))
    doc.add("EMAIL", h.email, "en-tête")
    doc.add("NUMERO_DOSSIER", "SIN-2024-008812", "objet")
    doc.dob(h.born, "corps", fmt_date(h.born, "slash"))
    doc.add("LIEU", "Rennes", "corps")
    doc.add("LIEU", "Nantes", "date")
    doc.add("NIR", h.nir, "corps", fmt_nir(h.nir, "spaces"))
    doc.add("FONCTION", h.job, "corps")
    doc.add("IBAN", h.iban, "corps", fmt_iban(h.iban, "spaces"))
    doc.add("CARTE", h.card, "corps", fmt_card(h.card, "spaces"))
    doc.add("PLAQUE", h.plate, "corps")
    doc.add("IP", h.ip, "corps")
    doc.add("IDENTIFIANT", h.username, "corps, URL")
    doc.add("URL", url, "corps")
    doc.person(b, "corps", f"{b.civ} {b.full}", f"{b.civ} {b.last}", f"{b.first} {b.last.upper()}")
    doc.phone(b.phone, "corps", fmt_phone(b.phone, "dots"))
    doc.add("EMAIL", b.email, "corps")
    doc.add("IBAN", b.iban, "post-scriptum", fmt_iban(b.iban, "rib"))
    doc.keep += [NEGATIVES[0], NEGATIVES[5]]
    return doc


def doc_csv() -> Doc:
    doc = Doc("02_csv_clients", "csv", "Export clients (CSV ; UTF-8 BOM, champs multilignes)")
    styles = ["spaces", "dots", "glued", "intl", "dashes", "intl00", "odd", "slashes"]
    istyles = ["spaces", "glued", "dashes", "lower", "rib", "double"]
    rows = [
        "id;civilite;nom;prenom;naissance;courriel;telephone;iban;adresse;cp;ville;"
        "immatriculation;commentaire"
    ]
    for i, p in enumerate(filler_people()):
        accentless = i % 3 == 0
        first = p.first
        if accentless:
            from tests.benchmark.common import strip_accents

            first = strip_accents(first)
        tel = fmt_phone(p.phone, styles[i % len(styles)])
        iban = fmt_iban(p.iban, istyles[i % len(istyles)])
        born = fmt_date(p.born, "slash" if i % 2 else "iso")
        addr = p.address.line1
        if i % 4 == 1:
            addr = f'"{p.address.line1}\nRésidence du Parc"'
        comment = NEGATIVES[i % len(NEGATIVES)] if i % 3 == 2 else f"Rappeler {p.civ} {p.last}"
        rows.append(
            f"{i + 1};{p.civ};{p.last.upper()};{first};{born};{p.email};{tel};{iban};{addr};"
            f'{p.address.postcode};{p.address.city};{p.plate};"{comment}"'
        )
        p_surfaces = [p.last.upper(), first, f"{p.civ} {p.last}"]
        doc.person(p, f"ligne {i + 1}", *p_surfaces)
        doc.dob(p.born, f"ligne {i + 1}", born)
        doc.add("EMAIL", p.email, f"ligne {i + 1}")
        doc.phone(p.phone, f"ligne {i + 1}", tel)
        doc.add("IBAN", p.iban, f"ligne {i + 1}", iban)
        doc.address(p.address, f"ligne {i + 1}", p.address.line1)
        doc.add("LIEU", p.address.city, f"ligne {i + 1}")
        doc.add("PLAQUE", p.plate, f"ligne {i + 1}")
        if i % 3 == 2:
            doc.keep.append(comment)
    doc.dir.mkdir(parents=True)
    (doc.dir / "clients.csv").write_bytes(("\r\n".join(rows) + "\r\n").encode("utf-8-sig"))
    return doc


def doc_json() -> Doc:
    doc = Doc("03_json_crm", "json", "Export CRM imbriqué (clés nominatives, échappements \\u)")
    k, n = P["kowalczyk"], P["nguyen"]
    data = {
        "export": {"date": "2026-02-01", "source": "crm", "version": "4.2"},
        "utilisateurs": [
            {
                "login": k.username,
                "nom_complet": k.full,
                "courriel": k.email,
                "telephone": fmt_phone(k.phone, "intl"),
                "naissance": k.born.isoformat(),
                "ip_derniere_connexion": k.ip,
                "iban": fmt_iban(k.iban, "glued"),
                "adresse": {
                    "rue": k.address.line1,
                    "complement": k.address.extra,
                    "cp": k.address.postcode,
                    "ville": k.address.city,
                },
                "vehicule": {"immatriculation": k.plate, "type": "porteur 19 t"},
                "notes": [f"Appelé par {n.full} le 3 février", None, True, 3.5],
            },
            {
                "login": n.username,
                "nom_complet": n.last.upper() + " " + n.first,
                "courriel": n.email,
                "telephone": int("33" + n.phone),
                "naissance": fmt_date(n.born, "slash"),
                "carte": fmt_card(n.card, "glued"),
                "ip_derniere_connexion": "2001:db8::4a:1f",
                "fonction": n.job,
            },
        ],
        f"{k.last} {k.first}": {"commentaire": "fiche nominative", "nir": k.nir},
        "remarque": NEGATIVES[7],
    }
    text = json.dumps(data, ensure_ascii=False, indent=2)
    escaped = json.dumps(f"Appelé par {n.full} le 3 février", ensure_ascii=True)
    text = text.replace(
        json.dumps(f"Appelé par {n.full} le 3 février", ensure_ascii=False), escaped
    )
    doc.dir.mkdir(parents=True)
    (doc.dir / "crm_export.json").write_text(text + "\n", encoding="utf-8", newline="\n")
    doc.person(k, "valeur, clé", k.full, f"{k.last} {k.first}")
    doc.add("IDENTIFIANT", k.username, "valeur login")
    doc.add("EMAIL", k.email, "valeur")
    doc.phone(k.phone, "valeur", fmt_phone(k.phone, "intl"))
    doc.dob(k.born, "valeur", k.born.isoformat())
    doc.add("IP", k.ip, "valeur")
    doc.add("IBAN", k.iban, "valeur")
    doc.address(k.address, "objet adresse", k.address.line1)
    doc.add(
        "ADRESSE",
        k.address.extra,
        "objet adresse",
        renderings=[{"mode": "text", "text": k.address.extra}],
    )
    doc.add("LIEU", k.address.city, "objet adresse")
    doc.add("PLAQUE", k.plate, "valeur")
    doc.add("NIR", k.nir, "valeur sous clé nominative")
    doc.person(n, "valeur, note échappée \\u", n.full, n.last.upper() + " " + n.first)
    doc.add("IDENTIFIANT", n.username, "valeur login")
    doc.add("EMAIL", n.email, "valeur")
    doc.phone(n.phone, "nombre JSON", str(int("33" + n.phone)))
    doc.dob(n.born, "valeur", fmt_date(n.born, "slash"))
    doc.add("CARTE", n.card, "valeur")
    doc.add("IP", "2001:db8::4a:1f", "valeur")
    doc.add("FONCTION", n.job, "valeur")
    doc.keep.append(NEGATIVES[7])
    return doc


def doc_xml() -> Doc:
    doc = Doc("04_xml_sinistre", "xml", "Déclaration de sinistre XML ISO-8859-1 (attributs, CDATA)")
    m, k = P["legoff"], P["kowalczyk"]
    org, *_ = company(doc, "bellerive", "élément assureur")
    text = (
        '<?xml version="1.0" encoding="ISO-8859-1"?>\n'
        f"<!-- Saisie effectuée par {m.full} ({m.username}) -->\n"
        f'<sinistre reference="RG 23/04567" gestionnaire={xa(m.full)} assureur={xa(org)}>\n'
        f'  <declarant civilite="{m.civ}" nom={xa(m.last.upper())} prenom={xa(m.first)}>\n'
        f"    <naissance>{fmt_date(m.born, 'dot')}</naissance>\n"
        f"    <adresse>{x(m.address.line1)}\n      {x(m.address.line2)}</adresse>\n"
        f'    <contact type="mobile">{fmt_phone(m.phone, "dashes")}</contact>\n'
        f'    <contact type="courriel">{m.email}</contact>\n'
        "  </declarant>\n"
        f'  <tiers nom={xa(k.full)} immatriculation="{k.plate.replace("-", " ")}"/>\n'
        f"  <circonstances><![CDATA[Le véhicule de <b>{k.first} {k.last}</b> a heurté "
        f"le portail. {NEGATIVES[3]}]]></circonstances>\n"
        f"  <?validation par={xa(m.last)}?>\n"
        f"  <lieu>Brest</lieu>\n"
        "</sinistre>\n"
    )
    doc.dir.mkdir(parents=True)
    (doc.dir / "declaration_sinistre.xml").write_bytes(text.encode("iso-8859-1"))
    doc.person(
        m,
        "commentaire, attributs, instruction de traitement",
        m.full,
        m.last.upper(),
        m.first,
        m.last,
    )
    doc.add("IDENTIFIANT", m.username, "commentaire XML")
    doc.add("NUMERO_DOSSIER", "RG 23/04567", "attribut")
    doc.dob(m.born, "élément", fmt_date(m.born, "dot"))
    doc.address(m.address, "élément (saut de ligne)", m.address.line1, m.address.line2)
    doc.phone(m.phone, "élément", fmt_phone(m.phone, "dashes"))
    doc.add("EMAIL", m.email, "élément")
    doc.person(k, "attribut, CDATA", k.full)
    doc.add("PLAQUE", k.plate, "attribut", k.plate.replace("-", " "))
    doc.add("LIEU", "Brest", "élément")
    doc.keep.append(NEGATIVES[3])
    return doc


def doc_html() -> Doc:
    doc = Doc("05_html_equipe", "html", "Page « équipe » HTML (href, alt, title, entités, JSON-LD)")
    c, j = P["martin"], P["petit"]
    curl = f"https://intranet.exemple.test/equipe/{c.first.lower()}-{c.last.lower()}"
    jurl = f"https://reseau.example/in/julien-petit-{j.born.year}"
    text = f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<title>Équipe communication — {c.full}</title>
<meta name="author" content="{c.full}">
<script type="application/ld+json">{{"@type": "Person", "name": "{j.full}", \
"telephone": "{fmt_phone(j.phone, "intlglued")}"}}</script>
</head>
<body>
<!-- Page maintenue par {j.username} -->
<h1>Notre équipe</h1>
<div class="carte" data-contact="{c.email}">
  <img src="photos/{c.last.lower()}.jpg" alt="Portrait de {c.full}" \
title="{c.first} {c.last}, {c.job}">
  <p><b>Cl</b>aire <span class="nom">MARTIN</span>, {c.job}</p>
  <p><a href="mailto:{c.email.replace("@", "%40")}?subject=Contact%20{c.first}%20{c.last}">\
&eacute;crire &agrave; Claire</a> — <a href="{curl}">fiche</a></p>
  <address>{c.address.line1}<br>{c.address.line2}</address>
</div>
<div class="carte">
  <p>{j.civ} J. Petit, {j.job} — né le {fmt_date(j.born, "long")}</p>
  <p>Profil : <a href="{jurl}" title="Profil de Julien Petit">{jurl}</a></p>
  <p>Tél. <a href="tel:{fmt_phone(j.phone, "intlglued")}">{fmt_phone(j.phone, "spaces")}</a></p>
</div>
<p>{NEGATIVES[2]}</p>
<p>{NEGATIVES[1]}</p>
</body>
</html>
"""
    doc.dir.mkdir(parents=True)
    (doc.dir / "equipe.html").write_text(text, encoding="utf-8", newline="\n")
    doc.person(
        c,
        "title, meta, alt, title, gras coupé, nom de fichier image",
        c.full,
        "MARTIN",
        f"{c.first} {c.last}",
    )
    doc.add("EMAIL", c.email, "attribut data, mailto encodé %40")
    doc.add("URL", curl, "href")
    doc.add("FONCTION", c.job, "texte, attribut title")
    doc.address(c.address, "balise address avec <br>", c.address.line1, c.address.line2)
    doc.add("LIEU", c.address.city, "balise address")
    doc.person(j, "JSON-LD, texte, title", j.full, "J. Petit", "Julien Petit")
    doc.add("IDENTIFIANT", j.username, "commentaire HTML")
    doc.dob(j.born, "texte", fmt_date(j.born, "long"))
    doc.add("URL", jurl, "href et texte")
    doc.phone(
        j.phone,
        "JSON-LD, href tel:, texte",
        fmt_phone(j.phone, "spaces"),
        fmt_phone(j.phone, "intlglued"),
    )
    doc.add("FONCTION", j.job, "texte")
    doc.keep += [NEGATIVES[2], NEGATIVES[1]]
    return doc


def signature_image(doc: Doc, image_id: str, p: Person, extra: str) -> bytes:
    canvas = Canvas(900, 220)
    canvas.text((30, 30), f"{p.first} {p.last.upper()}", 40)
    canvas.text((30, 95), f"{p.job} — {extra}", 26)
    canvas.text((30, 145), f"Mobile : {fmt_phone(p.phone, 'spaces')}", 30)
    mark(doc, canvas, image_id, "PERSONNE", p.full, p.last.upper())
    mark(doc, canvas, image_id, "TELEPHONE", "+33" + p.phone, fmt_phone(p.phone, "spaces"))
    return canvas.png()


def doc_eml_thread() -> Doc:
    doc = Doc(
        "06_eml_fil",
        "eml",
        "Fil de discussion profond : citations >, >>, >>>, transfert, HTML+texte, image en "
        "ligne, pièces jointes docx et pdf, message joint contenant un message joint",
    )
    s, r, k, i = P["boucher"], P["fontaine"], P["kowalczyk"], P["benali"]
    org, *_ = company(doc, "valmont", "signature")
    random.seed(SEED)
    profile = f"https://intranet.exemple.test/annuaire/{s.first.lower()}.{s.last.lower()}"
    plain = f"""Bonjour Rémi,

Je te transmets les éléments demandés pour le dossier de {k.civ} {k.last.upper()}.
Son IBAN est bien {fmt_iban(k.iban, "spaces")} et sa date de naissance le {fmt_date(k.born, "dot")}.
{NEGATIVES[4]}

Sophie

--
{s.first} {s.last.upper()} | {s.job} | {org}
Tél. direct : {fmt_phone("199002781", "spaces")} – Mob. {fmt_phone(s.phone, "dots")}
Fiche : {profile}

> Le 3 mars 2026 à 10:12, {r.full} <{r.email}> a écrit :
> Peux-tu me renvoyer l'IBAN de M. Kowalczyk ? Je n'arrive pas à joindre
> {r.first} du service paie.
>
>> Le 2 mars 2026, {k.full} <{k.email}> a écrit :
>> Bonjour,
>> Voici mon nouvel IBAN : {fmt_iban(k.iban, "glued")}
>> et mon numéro de sécurité sociale : {fmt_nir(k.nir, "key")}.
>> Je reste joignable au {fmt_phone(k.phone, "intl")}.
>> J.-B. K.
>>
>>> ---------- Message transféré ---------
>>> De : {i.full} <{i.email}>
>>> Date : dim. 1 mars 2026 à 18:40
>>> Objet : Badge et parking
>>> À : {k.email}
>>>
>>> Le badge de J.-B. Kowalczyk est prêt. Plaque enregistrée : {k.plate}.
>>> Merci de passer à l'accueil, bâtiment B. {i.first}
"""
    html = f"""<html><body>
<p>Bonjour R&eacute;mi,</p>
<p>Je te transmets les &eacute;l&eacute;ments demand&eacute;s pour le dossier de
{k.civ} <b>{k.last.upper()}</b>.<br>Son IBAN est bien {fmt_iban(k.iban, "spaces")} et sa date de
naissance le {fmt_date(k.born, "dot")}.</p>
<p>{NEGATIVES[4]}</p>
<p>Sophie</p>
<table><tr><td><img src="cid:sig001@exemple.test" alt="Signature {s.full}" title="{s.first} \
{s.last}"></td>
<td><a href="{profile}" title="Fiche de {s.full}">{s.first} {s.last.upper()}</a><br>
{s.job} | {org}<br>Mob. <a href="tel:{fmt_phone(s.phone, "intlglued")}">\
{fmt_phone(s.phone, "dots")}</a></td></tr></table>
<blockquote type="cite"><p>Le 3 mars 2026 &agrave; 10:12, {r.full} &lt;<a href="mailto:{r.email}">\
{r.email}</a>&gt; a &eacute;crit :</p>
<p>Peux-tu me renvoyer l'IBAN de M. Kowalczyk ?</p>
<blockquote type="cite"><p>Le 2 mars 2026, <a href="mailto:{k.email}" title="{k.full}">{k.full}</a>
a &eacute;crit :</p><p>Voici mon nouvel IBAN : {fmt_iban(k.iban, "glued")}<br>
NIR {fmt_nir(k.nir, "key")}</p>
<blockquote type="cite"><p>---------- Message transf&eacute;r&eacute; ---------<br>
De : {i.full} &lt;{i.email}&gt;<br>Le badge de J.-B. Kowalczyk est pr&ecirc;t. Plaque : {k.plate}.
</p></blockquote></blockquote></blockquote>
</body></html>"""
    msg = EmailMessage(policy=SMTP)
    msg["Received"] = (
        f"from poste-{s.username}.exemple.test (poste.exemple.test [{s.ip}]) "
        f"by mx.exemple.test with ESMTPS for <{r.email}>; Wed, 4 Mar 2026 09:00:00 +0100"
    )
    msg["Return-Path"] = f"<{s.email}>"
    msg["From"] = formataddr((s.full, s.email))
    msg["To"] = formataddr((r.full, r.email))
    msg["Cc"] = formataddr((f"{k.last.upper()} {k.first}", k.email))
    msg["Subject"] = f"RE: TR: Dossier {k.last} – pièces justificatives"
    msg["Date"] = "Wed, 04 Mar 2026 09:00:00 +0100"
    msg["Message-ID"] = "<fil-0042@exemple.test>"
    msg["In-Reply-To"] = "<fil-0041@exemple.test>"
    msg["Thread-Topic"] = f"Dossier {k.last}"
    msg.set_content(plain, cte="quoted-printable")
    msg.add_alternative(html, subtype="html", cte="base64")
    msg.get_payload()[1].add_related(
        signature_image(doc, "sig", s, org),
        maintype="image",
        subtype="png",
        cid="<sig001@exemple.test>",
        filename="signature.png",
    )
    attestation = simple_docx(
        [
            f"Attestation d'emploi de {k.full}",
            f"Né le {fmt_date(k.born, 'long')}, demeurant {k.address.one_line}.",
            f"Numéro de sécurité sociale : {fmt_nir(k.nir, 'spaces')}",
            f"Fait par {s.full}, {s.job}.",
        ],
        title=f"Attestation {k.last}",
        author=s.full,
    )
    msg.add_attachment(
        attestation,
        maintype="application",
        subtype="vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=f"Attestation_{k.last}.docx",
    )
    pdf = pdf_simple(
        [f"Demande de badge — {k.full}", f"Plaque : {k.plate}", f"Visa : {i.full}"],
        author=i.full,
    )
    msg.add_attachment(
        pdf, maintype="application", subtype="pdf", filename=f"Badge {k.first} {k.last}.pdf"
    )
    inner = EmailMessage(policy=SMTP)
    inner["From"] = formataddr((i.full, i.email))
    inner["To"] = formataddr((k.full, k.email))
    inner["Subject"] = "Badge et parking"
    inner["Date"] = "Sun, 01 Mar 2026 18:40:00 +0100"
    inner.set_content(
        f"Bonjour {k.first},\nVotre badge est prêt.\n{i.full}\n{fmt_phone(i.phone, 'odd')}\n"
    )
    innermost = EmailMessage(policy=SMTP)
    innermost["From"] = formataddr(("Accueil", "accueil@exemple.test"))
    innermost["To"] = i.email
    innermost["Subject"] = f"Réservation parking {k.plate}"
    innermost.set_content(
        f"Place 12 attribuée à {k.full} (né le {fmt_date(k.born, 'slash')}).\n{NEGATIVES[8]}\n"
    )
    inner.add_attachment(innermost)
    msg.add_attachment(inner)
    doc.dir.mkdir(parents=True)
    (doc.dir / "fil_discussion.eml").write_bytes(msg.as_bytes(policy=SMTP))
    doc.images["sig"] = {"kind": "mime_image", "file": "fil_discussion.eml", "index": 0}

    doc.person(
        s,
        "From, Return-Path, signature texte et HTML, alt/title, URL, docx joint",
        s.full,
        f"{s.first} {s.last.upper()}",
        first_only=True,
    )
    doc.add("EMAIL", s.email, "From, Return-Path")
    doc.add("IP", s.ip, "en-tête Received")
    doc.add("IDENTIFIANT", s.username, "en-tête Received (nom d'hôte)")
    doc.add("URL", profile, "signature, href")
    doc.add("FONCTION", s.job, "signature")
    doc.phone(s.phone, "signature texte/HTML, image", fmt_phone(s.phone, "dots"))
    doc.phone("199002781", "signature", fmt_phone("199002781", "spaces"))
    doc.person(r, "To, citation niveau 1", r.full, first_only=True)
    doc.add("EMAIL", r.email, "To, Received, citation")
    doc.person(
        k,
        "Cc, objet, citations niveaux 1-3, pièces jointes, noms de fichiers",
        k.full,
        f"{k.civ} {k.last.upper()}",
        f"{k.last.upper()} {k.first}",
        "M. Kowalczyk",
        "J.-B. Kowalczyk",
        first_only=True,
    )
    doc.add(
        "PERSONNE",
        "J.-B. K.",
        "signature citée niveau 2",
        "J.-B. K.",
        renderings=[{"mode": "text", "text": "J.-B. K."}],
    )
    doc.add("EMAIL", k.email, "Cc, citations")
    doc.add(
        "IBAN",
        k.iban,
        "corps, citation niveau 2",
        fmt_iban(k.iban, "spaces"),
        fmt_iban(k.iban, "glued"),
    )
    doc.dob(
        k.born,
        "corps, docx joint, message joint niveau 2",
        fmt_date(k.born, "dot"),
        fmt_date(k.born, "long"),
        fmt_date(k.born, "slash"),
    )
    doc.add(
        "NIR",
        k.nir,
        "citation niveau 2, docx joint",
        fmt_nir(k.nir, "key"),
        fmt_nir(k.nir, "spaces"),
    )
    doc.phone(k.phone, "citation niveau 2", fmt_phone(k.phone, "intl"))
    doc.add("PLAQUE", k.plate, "citation niveau 3, pdf joint, objet du message joint niveau 2")
    doc.address(k.address, "docx joint", k.address.line1)
    doc.add(
        "ADRESSE",
        k.address.extra,
        "docx joint",
        renderings=[{"mode": "text", "text": k.address.extra}],
    )
    doc.person(
        i, "citation niveau 3 (transfert), pdf joint, message joint", i.full, first_only=True
    )
    doc.add("EMAIL", i.email, "citation transfert, message joint")
    doc.phone(i.phone, "message joint", fmt_phone(i.phone, "odd"))
    doc.keep += [NEGATIVES[4], NEGATIVES[8]]
    return doc


def pdf_simple(lines: list[str], *, author: str) -> bytes:
    import pymupdf

    pdf = pymupdf.open()
    page = pdf.new_page()
    for n, line in enumerate(lines):
        page.insert_text((72, 90 + 20 * n), line, fontsize=11)
    pdf.set_metadata({"author": author, "title": lines[0], "creationDate": "D:20260101000000"})
    data = bytes(pdf.tobytes(garbage=3, deflate=True, no_new_id=True))
    pdf.close()
    return data


def doc_eml_encoded() -> Doc:
    doc = Doc(
        "07_eml_encode",
        "eml",
        "Courriel HTML seul, en-têtes RFC 2047 (Q et B), "
        "noms d'affichage inversés, archive zip jointe",
    )
    m, g = P["legoff"], P["girard"]
    random.seed(SEED + 7)
    html = (
        f"<html><body><p>Bonjour {g.first},</p><p>Suite &agrave; notre appel, voici les "
        f"coordonn&eacute;es de l'assur&eacute;e : <b>{m.first}</b> <i>{m.last}</i>, "
        f"{x(m.address.line1)}, {m.address.line2}.</p>"
        f'<p>Joignable au <span style="white-space:nowrap">{fmt_phone(m.phone, "intl00")}'
        f"</span>.</p><p>{NEGATIVES[6]}</p>"
        f'<p><img src="https://suivi.exemple.test/pixel.gif?u={m.username}" alt="" width="1"></p>'
        "</body></html>"
    )
    raw_headers = (
        "From: =?utf-8?q?Ma=C3=ABlle_Le_Goff?= <" + m.email + ">\r\n"
        f'To: "{g.last.upper()} {g.first}" <{g.email}>\r\n'
        "Subject: =?utf-8?b?" + base64_text(f"Coordonnées de {m.full}") + "?=\r\n"
        "Date: Thu, 05 Mar 2026 11:00:00 +0100\r\n"
        "Message-ID: <enc-0007@exemple.test>\r\n"
        "MIME-Version: 1.0\r\n"
        'Content-Type: multipart/mixed; boundary="==limite-0007=="\r\n\r\n'
    )
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as zf:
        zip_entry(
            zf,
            f"{m.last} - constat.txt",
            f"Constat amiable signé par {m.full} le 4 mars 2026.\r\n"
            f"Véhicule adverse : {P['kowalczyk'].plate}\r\n",
        )
    import base64

    body = (
        "--==limite-0007==\r\nContent-Type: text/html; charset=utf-8\r\n"
        "Content-Transfer-Encoding: base64\r\n\r\n"
        + base64.encodebytes(html.encode()).decode().replace("\n", "\r\n")
        + "\r\n--==limite-0007==\r\nContent-Type: application/zip\r\n"
        "Content-Transfer-Encoding: base64\r\n"
        'Content-Disposition: attachment; filename="=?utf-8?q?pi=C3=A8ces_'
        + m.last.replace(" ", "_")
        + '.zip?="\r\n\r\n'
        + base64.encodebytes(zbuf.getvalue()).decode().replace("\n", "\r\n")
        + "\r\n--==limite-0007==--\r\n"
    )
    doc.dir.mkdir(parents=True)
    (doc.dir / "coordonnees.eml").write_bytes((raw_headers + body).encode("ascii"))
    doc.person(
        m,
        "From (RFC 2047 Q), Subject (RFC 2047 B), corps HTML coupé, zip joint",
        m.full,
        m.first,
        m.last,
    )
    doc.add("EMAIL", m.email, "From")
    doc.address(m.address, "corps HTML", m.address.line1, m.address.line2)
    doc.phone(m.phone, "corps HTML", fmt_phone(m.phone, "intl00"))
    doc.add("IDENTIFIANT", m.username, "URL du pixel")
    doc.person(g, "To (NOM Prénom), salutation", f"{g.last.upper()} {g.first}", first_only=True)
    doc.add("EMAIL", g.email, "To")
    doc.add("PLAQUE", P["kowalczyk"].plate, "zip joint")
    doc.keep.append(NEGATIVES[6])
    return doc


def base64_text(text: str) -> str:
    import base64

    return base64.b64encode(text.encode()).decode()


def doc_mbox() -> Doc:
    doc = Doc(
        "08_mbox_boite", "mbox", "Boîte mbox de trois messages (QP, base64, pièce jointe CSV)"
    )
    a, q, n = P["rousseau"], P["morel"], P["faure"]
    random.seed(SEED + 8)
    m1 = EmailMessage(policy=SMTP)
    m1["From"] = formataddr((a.full, a.email))
    m1["To"] = formataddr((q.full, q.email))
    m1["Subject"] = "Honoraires"
    m1["Date"] = "Mon, 09 Mar 2026 08:00:00 +0100"
    m1.set_content(
        f"Bonjour Quentin,\n\nMerci de régler la note sur le compte {fmt_iban(a.iban, 'nbsp')}.\n"
        f"From the desk of {a.full}\nMaître {a.last}, {a.job}\n",
        cte="quoted-printable",
    )
    m2 = EmailMessage(policy=SMTP)
    m2["From"] = formataddr((q.full, q.email))
    m2["To"] = formataddr((n.full, n.email))
    m2["Subject"] = "Liste des nouveaux arrivants"
    m2["Date"] = "Tue, 10 Mar 2026 08:00:00 +0100"
    m2.set_content(f"Noémie,\nci-joint la liste.\n{NEGATIVES[9]}\nQ. Morel\n", cte="base64")
    lam, mer = P["lambert"], P["mercier"]
    csv_text = (
        "nom,prenom,naissance,telephone\n"
        f"{lam.last},{lam.first},{fmt_date(lam.born, 'slash')},{fmt_phone(lam.phone, 'glued')}\n"
        f"{mer.last},{mer.first},{fmt_date(mer.born, 'slash')},{fmt_phone(mer.phone, 'spaces')}\n"
    )
    m2.add_attachment(csv_text.encode(), maintype="text", subtype="csv", filename="arrivants.csv")
    m3 = EmailMessage(policy=SMTP)
    m3["From"] = formataddr((n.full, n.email))
    m3["To"] = q.email
    m3["Subject"] = f"RE: Liste des nouveaux arrivants — {lam.full}"
    m3["Date"] = "Tue, 10 Mar 2026 09:00:00 +0100"
    m3.set_content(
        f"Bien reçu. {lam.first} {lam.last.upper()} commence lundi ; "
        f"son login sera {lam.username}.\n"
        f"From now on, contact me at {fmt_phone(n.phone, 'intl')}.\n\n"
        f"> ci-joint la liste.\n> Q. Morel\n"
    )
    out = b""
    for sender, message in ((a.email, m1), (q.email, m2), (n.email, m3)):
        raw = message.as_bytes(policy=SMTP.clone(linesep="\n"))
        raw = raw.replace(b"\nFrom ", b"\n>From ")
        out += f"From {sender} Mon Mar  9 08:00:00 2026\n".encode() + raw.rstrip(b"\n") + b"\n\n"
    doc.dir.mkdir(parents=True)
    (doc.dir / "boite.mbox").write_bytes(out)
    doc.person(
        a,
        "From, ligne From mbox, corps (« From the desk of », ligne échappée >From)",
        a.full,
        f"Maître {a.last}",
    )
    doc.add("EMAIL", a.email, "From, séparateur mbox")
    doc.add("IBAN", a.iban, "corps QP", fmt_iban(a.iban, "nbsp"))
    doc.add("FONCTION", a.job, "corps")
    doc.person(q, "To, From, signature initiale", q.full, "Q. Morel", first_only=True)
    doc.add("EMAIL", q.email, "To, From, séparateur mbox")
    doc.person(n, "To, From", n.full, first_only=True)
    doc.add("EMAIL", n.email, "From, séparateur mbox")
    doc.phone(n.phone, "corps", fmt_phone(n.phone, "intl"))
    doc.person(
        lam,
        "CSV joint (base64), objet, corps",
        lam.last,
        lam.first,
        lam.full,
        f"{lam.first} {lam.last.upper()}",
    )
    doc.dob(lam.born, "CSV joint", fmt_date(lam.born, "slash"))
    doc.phone(lam.phone, "CSV joint", fmt_phone(lam.phone, "glued"))
    doc.add("IDENTIFIANT", lam.username, "corps")
    doc.person(mer, "CSV joint", mer.last, mer.first)
    doc.dob(mer.born, "CSV joint", fmt_date(mer.born, "slash"))
    doc.phone(mer.phone, "CSV joint", fmt_phone(mer.phone, "spaces"))
    doc.keep.append(NEGATIVES[9])
    return doc


PT_LONG = 0x0003
PT_SYSTIME = 0x0040


def doc_msg() -> Doc:
    from extract_msg.ole_writer import OleWriter

    doc = Doc(
        "09_msg_outlook",
        "msg",
        "Message Outlook .msg (propriétés MAPI, RTF compressé, destinataires, pièce jointe texte)",
    )
    a, b = P["rousseau"], P["dumoulin"]
    import compressed_rtf

    writer = OleWriter()
    body = (
        f"Bonjour Baptiste,\r\nVoici les coordonnées de {a.civ} {a.full} : "
        f"{fmt_phone(a.phone, 'dots')}, née le {fmt_date(a.born, 'long')}.\r\n"
        f"Carte de paiement : {fmt_card(a.card, 'dashes')}\r\n{NEGATIVES[0]}\r\n"
    )
    html = (
        f"<html><body><p>Bonjour Baptiste,</p><p>Coordonnées de <b>{a.full}</b> : "
        f"{fmt_phone(a.phone, 'dots')}</p></body></html>"
    )
    rtf = (
        "{\\rtf1\\ansi\\ansicpg1252 Bonjour, coordonn\\'e9es de Agathe Rousseau "
        f"({fmt_phone(a.phone, 'dots')}).\\par}}"
    ).encode("ascii")
    headers = (
        f"Received: from poste ([{b.ip}]) by mx.exemple.test; Mon, 9 Mar 2026 10:00:00 +0100\r\n"
        f"From: {b.full} <{b.email}>\r\nTo: {a.full} <{a.email}>\r\n"
        f"Subject: Coordonnées {a.last}\r\nMessage-ID: <msg-0009@exemple.test>\r\n"
    )
    _add_props(
        writer,
        "",
        b"\0" * 8 + struct.pack("<IIII", 1, 1, 1, 1) + b"\0" * 8,
        {0x0039: (PT_SYSTIME, 134117136000000000)},
        {
            0x001A: "IPM.Note",
            0x0037: f"Coordonnées {a.last}",
            0x0C1A: b.full,
            0x5D01: b.email,
            0x0C1F: b.email,
            0x0E04: a.full,
            0x1000: body,
            0x007D: headers,
            0x0070: f"Coordonnées {a.last}",
        },
        {
            0x1013: html.encode("utf-8"),
            0x1009: compressed_rtf.compress(rtf, compressed=True),
            0x0C19: one_off(b.full, b.email),
        },
    )
    _add_props(
        writer,
        "__recip_version1.0_#00000000/",
        b"\0" * 8,
        {0x0C15: (PT_LONG, 1), 0x3000: (PT_LONG, 0)},
        {0x3001: a.full, 0x39FE: a.email, 0x3003: a.email, 0x3002: "SMTP"},
        {0x0FFF: one_off(a.full, a.email)},
    )
    _add_props(
        writer,
        "__attach_version1.0_#00000000/",
        b"\0" * 8,
        {0x3705: (PT_LONG, 1), 0x0E21: (PT_LONG, 0)},
        {0x3707: f"Relevé {a.last}.txt", 0x3704: "Releve.txt", 0x370E: "text/plain"},
        {0x3701: f"Relevé de {a.full}\r\nIBAN {fmt_iban(a.iban, 'spaces')}\r\n".encode()},
    )
    for name in ("00020102", "00030102", "00040102"):
        writer.addEntry(f"__nameid_version1.0/__substg1.0_{name}", b"")
    buffer = io.BytesIO()
    writer.write(buffer)
    doc.dir.mkdir(parents=True)
    (doc.dir / "coordonnees.msg").write_bytes(buffer.getvalue())
    doc.person(
        a,
        "sujet, destinataire, corps texte/HTML/RTF, pièce jointe et son nom",
        a.full,
        f"{a.civ} {a.full}",
        a.last,
    )
    doc.add("EMAIL", a.email, "destinataire, en-têtes")
    doc.phone(a.phone, "corps texte/HTML/RTF", fmt_phone(a.phone, "dots"))
    doc.dob(a.born, "corps", fmt_date(a.born, "long"))
    doc.add("CARTE", a.card, "corps", fmt_card(a.card, "dashes"))
    doc.add("IBAN", a.iban, "pièce jointe", fmt_iban(a.iban, "spaces"))
    doc.person(b, "expéditeur, en-têtes", b.full, first_only=True)
    doc.add("EMAIL", b.email, "expéditeur")
    doc.add("IP", b.ip, "en-tête Received")
    doc.keep.append(NEGATIVES[0])
    return doc


def badge_image(doc: Doc, image_id: str, p: Person) -> bytes:
    canvas = Canvas(1000, 330, "#f4f4f4")
    canvas.text((40, 30), "BADGE VISITEUR", 34)
    canvas.text((40, 95), f"{p.first} {p.last.upper()}", 48)
    canvas.text((40, 175), f"Né le {fmt_date(p.born, 'slash')}", 32)
    canvas.text((40, 240), f"Véhicule : {p.plate}", 32)
    mark(doc, canvas, image_id, "PERSONNE", p.full, p.last.upper())
    mark(doc, canvas, image_id, "DATE_NAISSANCE", p.born.isoformat(), fmt_date(p.born, "slash"))
    mark(doc, canvas, image_id, "PLAQUE", p.plate, p.plate)
    return canvas.png()


def doc_docx() -> Doc:
    doc = Doc(
        "10_docx_audit",
        "docx",
        "Rapport d'audit Word : en-tête/pied, commentaires, "
        "révisions, noms coupés par la mise en forme, lien sur plusieurs runs, note de bas "
        "de page, métadonnées, image contenant du texte",
    )
    y, s, a, q, c, lv = (
        P[k] for k in ("brunet", "chevalier", "perrin", "morel", "girard", "vienne")
    )
    org, _, st, vat = company(doc, "lerouge", "pied de page, métadonnées", with_ids=True)
    body = "".join(
        [
            wp("Rapport d'audit interne", style="Titre"),
            # Name split by formatting inside one word.
            wp(
                "Mission conduite par ",
                wr("Yan", bold=True),
                wr("nick", italic=True),
                wr(f" {y.last.upper()}"),
                wr(f", {y.job}, né le {fmt_date(y.born, 'long')}."),
            ),
            # Hyperlink spanning two runs.
            wp(
                "Juriste référente : ",
                f'<w:hyperlink r:id="rIdLink" w:tooltip={xa("Fiche " + s.full)}>'
                + wr(f"{s.first} ")
                + wr(s.last, bold=True)
                + "</w:hyperlink>",
                wr(f" ({s.email})."),
                '<w:r><w:footnoteReference w:id="1"/></w:r>',
            ),
            wp(
                '<w:commentRangeStart w:id="0"/>',
                wr(f"Les remboursements versés à {lv.civ} {lv.full} sur le compte "),
                wr(fmt_iban(lv.iban, "spaces")),
                '<w:commentRangeEnd w:id="0"/><w:r><w:commentReference w:id="0"/></w:r>',
                wr(f" posent question. {lv.civ} Vienne n'a pas fourni de justificatif."),
            ),
            wp(
                wr("Validé par "),
                f'<w:ins w:id="10" w:author={xa(c.full)} w:date="2026-01-02T00:00:00Z">'
                + wr(q.full)
                + "</w:ins>",
                f'<w:del w:id="11" w:author={xa(c.full)} w:date="2026-01-02T00:00:00Z">'
                f'<w:r><w:delText xml:space="preserve"> et {x(a.full)} (IBAN '
                f"{fmt_iban(a.iban, 'glued')})</w:delText></w:r></w:del>",
            ),
            "<w:tbl><w:tr>"
            + "".join(f"<w:tc>{wp(h)}</w:tc>" for h in ("Salarié", "Naissance", "NIR", "Téléphone"))
            + "</w:tr><w:tr>"
            + "".join(
                f"<w:tc>{wp(v)}</w:tc>"
                for v in (
                    y.upper,
                    fmt_date(y.born, "slash"),
                    fmt_nir(y.nir, "glued"),
                    fmt_phone(y.phone, "spaces"),
                )
            )
            + "</w:tr></w:tbl>",
            docx_image_paragraph("rIdImg", f"Badge de {y.full}"),
            wp(NEGATIVES[6]),
            wp(NEGATIVES[3]),
            wp(f"Siège : {org}, SIRET {st}."),
        ]
    )
    comments = (
        f'<w:comment w:id="0" w:author={xa(q.full)} w:initials="QM" w:date="2026-01-02T00:00:00Z">'
        f"{wp(f'À vérifier avec {lv.first} {lv.last} au {fmt_phone(lv.phone, "spaces")}.')}"
        "</w:comment>"
    )
    footnotes = f'<w:footnote w:id="1">{wp(f"Désignée par {a.full}, associée.")}</w:footnote>'
    header = wp(f"Rapport confidentiel — préparé par {a.full}")
    footer = wp(f"{org} — TVA {vat} — {a.email} — {fmt_phone(a.phone, 'intl')}")
    data = docx_package(
        body,
        header=header,
        footer=footer,
        comments=comments,
        footnotes=footnotes,
        core=ooxml_core(f"Audit {y.last}", a.full, q.full, keywords=f"{y.full}; {s.email}"),
        app=ooxml_app("Microsoft Office Word", org, a.full),
        custom=ooxml_custom("Responsable", c.full),
        extra_rels=[
            ("rIdLink", "hyperlink", f"mailto:{s.email}", True),
            ("rIdImg", "image", "media/image1.png", False),
        ],
        media={"image1.png": badge_image(doc, "badge", y)},
    )
    doc.dir.mkdir(parents=True)
    (doc.dir / "rapport_audit.docx").write_bytes(data)
    doc.images["badge"] = {
        "kind": "zip_member",
        "file": "rapport_audit.docx",
        "member": "word/media/image1.png",
    }
    doc.person(
        y,
        "corps (gras/italique coupé), tableau, descr image, mots-clés, titre, image",
        "Yannick " + y.last.upper(),
        y.upper,
        y.full,
    )
    doc.dob(y.born, "corps, tableau, image", fmt_date(y.born, "long"), fmt_date(y.born, "slash"))
    doc.add("FONCTION", y.job, "corps")
    doc.add("NIR", y.nir, "tableau", fmt_nir(y.nir, "glued"))
    doc.phone(y.phone, "tableau", fmt_phone(y.phone, "spaces"))
    doc.add("PLAQUE", y.plate, "image")
    doc.person(s, "lien sur deux runs, info-bulle", s.full)
    doc.add("EMAIL", s.email, "corps, cible du lien, mots-clés")
    doc.person(
        lv,
        "corps, commentaire (personne au nom de ville)",
        f"{lv.civ} {lv.full}",
        f"{lv.civ} Vienne",
        f"{lv.first} {lv.last}",
    )
    doc.add("IBAN", lv.iban, "corps", fmt_iban(lv.iban, "spaces"))
    doc.phone(lv.phone, "commentaire", fmt_phone(lv.phone, "spaces"))
    doc.person(q, "révision insérée, auteur du commentaire, dernier modificateur", q.full)
    doc.person(c, "auteur des révisions, propriété personnalisée", c.full)
    doc.person(a, "révision supprimée, en-tête, note de bas de page, auteur, responsable", a.full)
    doc.add("IBAN", a.iban, "révision supprimée", fmt_iban(a.iban, "glued"))
    doc.add("EMAIL", a.email, "pied de page")
    doc.phone(a.phone, "pied de page", fmt_phone(a.phone, "intl"))
    doc.keep += [NEGATIVES[6], NEGATIVES[3]]
    return doc


def doc_xlsx() -> Doc:
    doc = Doc(
        "11_xlsx_registre",
        "xlsx",
        "Registre du personnel Excel : nom de feuille, "
        "commentaires de cellule, en-tête/pied, formules, métadonnées",
    )
    lam, nf, mer, gau = P["lambert"], P["faure"], P["mercier"], P["gauthier"]
    sheet = lam.full
    rows = [
        ("Nom", "Prénom", "Naissance", "Téléphone", "IBAN", "Login"),
        (
            lam.last.upper(),
            lam.first,
            fmt_date(lam.born, "slash"),
            fmt_phone(lam.phone, "dots"),
            fmt_iban(lam.iban, "double"),
            lam.username,
        ),
        (
            mer.last,
            mer.first,
            mer.born.isoformat(),
            fmt_phone(mer.phone, "intl"),
            fmt_iban(mer.iban, "spaces"),
            mer.username,
        ),
        (
            gau.last.upper(),
            gau.first,
            fmt_date(gau.born, "dot"),
            fmt_phone(gau.phone, "glued"),
            fmt_iban(gau.iban, "glued"),
            gau.username,
        ),
    ]
    strings: list[str] = []
    for row in rows:
        for v in row:
            if v not in strings:
                strings.append(v)
    strings.append(NEGATIVES[5])
    sst = "".join(f'<si><t xml:space="preserve">{x(s)}</t></si>' for s in strings)
    cols = "ABCDEF"
    sheet_rows = ""
    for r, row in enumerate(rows, start=1):
        cells = "".join(
            f'<c r="{cols[c]}{r}" t="s"><v>{strings.index(v)}</v></c>' for c, v in enumerate(row)
        )
        sheet_rows += f'<row r="{r}">{cells}</row>'
    sheet_rows += (
        f'<row r="6"><c r="A6" t="s"><v>{strings.index(NEGATIVES[5])}</v></c></row>'
        f'<row r="7"><c r="A7" t="inlineStr"><is><t>Référent : {x(nf.full)}</t></is></c>'
        f'<c r="B7"><v>{int("33" + nf.phone)}</v></c></row>'
    )
    quoted = f"'{sheet}'"
    sheet1 = (
        f"<sheetData>{sheet_rows}</sheetData>"
        '<pageMargins left="0.7" right="0.7" top="0.75" bottom="0.75" header="0.3" footer="0.3"/>'
        f"<headerFooter><oddHeader>&amp;LRegistre tenu par {x(nf.full)}</oddHeader>"
        f"<oddFooter>&amp;R{nf.email}</oddFooter></headerFooter>"
        '<legacyDrawing r:id="rIdV"/>'
    )
    sheet2 = (
        "<sheetData>"
        f'<row r="1"><c r="A1" t="str"><f>{x(quoted)}!A2</f><v>{x(lam.last.upper())}</v></c>'
        f'<c r="B1" t="inlineStr"><is><t>{x(NEGATIVES[8])}</t></is></c></row>'
        "</sheetData>"
    )
    comments = (
        f"<authors><author>{x(nf.full)}</author></authors><commentList>"
        f'<comment ref="A3" authorId="0"><text><r><rPr><b/></rPr><t>{x(nf.full)}:</t></r>'
        f'<r><t xml:space="preserve"> {x(mer.first)} {x(mer.last)} a changé d\'adresse : '
        f"{x(mer.address.one_line)}</t></r></text></comment></commentList>"
    )
    vml = (
        '<xml xmlns:v="urn:schemas-microsoft-com:vml" xmlns:o="urn:schemas-microsoft-com:'
        'office:office" xmlns:x="urn:schemas-microsoft-com:office:excel">'
        '<v:shape id="_x0000_s1025" type="#_x0000_t202"><v:textbox>'
        f"<div style='text-align:left'>{x(mer.first)} {x(mer.last)}<br></div></v:textbox>"
        '<x:ClientData ObjectType="Note"><x:Row>2</x:Row><x:Column>0</x:Column>'
        "</x:ClientData></v:shape></xml>"
    )
    entries: dict[str, str | bytes] = {
        "[Content_Types].xml": content_types(
            {
                "xl/workbook.xml": f"{SML}.sheet.main+xml",
                "xl/worksheets/sheet1.xml": f"{SML}.worksheet+xml",
                "xl/worksheets/sheet2.xml": f"{SML}.worksheet+xml",
                "xl/sharedStrings.xml": f"{SML}.sharedStrings+xml",
                "xl/comments1.xml": f"{SML}.comments+xml",
            }
        ).replace(
            '<Default Extension="png"',
            '<Default Extension="vml" ContentType="application/vnd.openxmlformats-'
            'officedocument.vmlDrawing"/><Default Extension="png"',
        ),
        "_rels/.rels": package_rels("xl/workbook.xml"),
        "xl/workbook.xml": s_doc(
            "workbook",
            f'<sheets><sheet name={xa(sheet)} sheetId="1" r:id="rId1"/>'
            '<sheet name="Synthèse" sheetId="2" r:id="rId2"/></sheets>'
            f'<definedNames><definedName name="Apprenti">{x(quoted)}!$A$2</definedName>'
            "</definedNames>",
        ),
        "xl/_rels/workbook.xml.rels": rels(
            [
                ("rId1", "worksheet", "worksheets/sheet1.xml", False),
                ("rId2", "worksheet", "worksheets/sheet2.xml", False),
                ("rId3", "sharedStrings", "sharedStrings.xml", False),
            ]
        ),
        "xl/sharedStrings.xml": XML_DECL
        + f'<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        f'count="{len(strings)}" uniqueCount="{len(strings)}">{sst}</sst>',
        "xl/worksheets/sheet1.xml": s_doc("worksheet", sheet1),
        "xl/worksheets/_rels/sheet1.xml.rels": rels(
            [
                ("rIdC", "comments", "../comments1.xml", False),
                ("rIdV", "vmlDrawing", "../drawings/vmlDrawing1.vml", False),
            ]
        ),
        "xl/worksheets/sheet2.xml": s_doc("worksheet", sheet2),
        "xl/comments1.xml": s_doc("comments", comments),
        "xl/drawings/vmlDrawing1.vml": vml,
        "docProps/core.xml": ooxml_core("Registre", nf.full, gau.full),
        "docProps/app.xml": ooxml_app("Microsoft Excel", COMPANY["hauts"][0], nf.full),
        "docProps/custom.xml": ooxml_custom("Contact", nf.email),
    }
    doc.dir.mkdir(parents=True)
    write_zip(doc.dir / "registre_personnel.xlsx", entries)
    doc.add("ORGANISATION", COMPANY["hauts"][0], "métadonnées (société)")
    doc.person(
        lam, "nom de feuille, nom défini, formule, cellules", lam.full, lam.last.upper(), lam.first
    )
    doc.dob(lam.born, "cellule", fmt_date(lam.born, "slash"))
    doc.phone(lam.phone, "cellule", fmt_phone(lam.phone, "dots"))
    doc.add("IBAN", lam.iban, "cellule (doubles espaces)", fmt_iban(lam.iban, "double"))
    doc.add("IDENTIFIANT", lam.username, "cellule")
    doc.person(
        mer,
        "cellules, commentaire de cellule, note VML",
        mer.last,
        mer.first,
        f"{mer.first} {mer.last}",
    )
    doc.dob(mer.born, "cellule", mer.born.isoformat())
    doc.phone(mer.phone, "cellule", fmt_phone(mer.phone, "intl"))
    doc.add("IBAN", mer.iban, "cellule", fmt_iban(mer.iban, "spaces"))
    doc.add("IDENTIFIANT", mer.username, "cellule")
    doc.address(mer.address, "commentaire de cellule", mer.address.line1, mer.address.line2)
    doc.person(gau, "cellules, dernier modificateur", gau.last.upper(), gau.first, gau.full)
    doc.dob(gau.born, "cellule", fmt_date(gau.born, "dot"))
    doc.phone(gau.phone, "cellule", fmt_phone(gau.phone, "glued"))
    doc.add("IBAN", gau.iban, "cellule", fmt_iban(gau.iban, "glued"))
    doc.add("IDENTIFIANT", gau.username, "cellule")
    doc.person(
        nf, "chaîne en ligne, en-tête, auteur du commentaire, créateur, responsable", nf.full
    )
    doc.phone(nf.phone, "cellule numérique", str(int("33" + nf.phone)))
    doc.add("EMAIL", nf.email, "pied de page, propriété personnalisée")
    doc.keep += [NEGATIVES[5], NEGATIVES[8]]
    return doc


def doc_pptx() -> Doc:
    doc = Doc(
        "12_pptx_projet",
        "pptx",
        "Présentation PowerPoint : titre coupé en runs, notes "
        "de l'orateur, commentaires, auteurs, masque et disposition, métadonnées",
    )
    o, b, e = P["gauthier"], P["dumoulin"], P["vidal"]
    title = (
        '<a:p><a:r><a:rPr lang="fr-FR" b="1"/><a:t>Projet Hermine — pilotage : Oph</a:t></a:r>'
        f'<a:r><a:rPr lang="fr-FR"/><a:t>élie {x(o.last)}</a:t></a:r></a:p>'
    )
    body = (
        f"<a:p><a:r><a:t>Commercial : {x(b.full)} ({b.email})</a:t></a:r></a:p>"
        f"<a:p><a:r><a:t>Budget validé par {x(e.civ)} {x(e.last.upper())}, {x(e.job)}</a:t>"
        "</a:r></a:p>"
        f"<a:p><a:r><a:t>{x(NEGATIVES[7])}</a:t></a:r></a:p>"
    )
    slide = p_doc(
        "sld",
        '<p:cSld><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/>'
        "</p:nvGrpSpPr><p:grpSpPr/>"
        + p_shape(2, title, descr=f"Titre {o.full}")
        + p_shape(3, body)
        + "</p:spTree></p:cSld>",
    )
    notes = p_doc(
        "notes",
        '<p:cSld><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/>'
        "</p:nvGrpSpPr><p:grpSpPr/>"
        + p_shape(
            2,
            f"<a:p><a:r><a:t>Appeler {x(b.first)} au {fmt_phone(b.phone, 'spaces')} avant la "
            f"réunion ; son adresse : {x(b.address.one_line)}.</a:t></a:r></a:p>"
            f"<a:p><a:r><a:t>Ne pas citer {x(e.full)} (née le {fmt_date(e.born, 'slash')}).</a:t>"
            "</a:r></a:p>",
        )
        + "</p:spTree></p:cSld>",
    )
    layout = p_doc(
        "sldLayout",
        '<p:cSld name="Titre"><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/>'
        "<p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/>"
        + p_shape(2, f"<a:p><a:r><a:t>Modèle de {x(o.full)}</a:t></a:r></a:p>")
        + "</p:spTree></p:cSld>",
    )
    master = p_doc(
        "sldMaster",
        '<p:cSld><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/>'
        "</p:nvGrpSpPr><p:grpSpPr/>"
        + p_shape(2, f"<a:p><a:r><a:t>{o.email}</a:t></a:r></a:p>")
        + '</p:spTree></p:cSld><p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/>'
        "</p:sldLayoutIdLst>",
    )
    entries: dict[str, str | bytes] = {
        "[Content_Types].xml": content_types(
            {
                "ppt/presentation.xml": f"{PML}.presentation.main+xml",
                "ppt/slides/slide1.xml": f"{PML}.slide+xml",
                "ppt/notesSlides/notesSlide1.xml": f"{PML}.notesSlide+xml",
                "ppt/slideLayouts/slideLayout1.xml": f"{PML}.slideLayout+xml",
                "ppt/slideMasters/slideMaster1.xml": f"{PML}.slideMaster+xml",
                "ppt/commentAuthors.xml": f"{PML}.commentAuthors+xml",
                "ppt/comments/comment1.xml": f"{PML}.comments+xml",
            }
        ),
        "_rels/.rels": package_rels("ppt/presentation.xml"),
        "ppt/presentation.xml": p_doc(
            "presentation",
            '<p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst>'
            '<p:sldIdLst><p:sldId id="256" r:id="rId2"/></p:sldIdLst>'
            '<p:sldSz cx="9144000" cy="6858000"/><p:notesSz cx="6858000" cy="9144000"/>',
        ),
        "ppt/_rels/presentation.xml.rels": rels(
            [
                ("rId1", "slideMaster", "slideMasters/slideMaster1.xml", False),
                ("rId2", "slide", "slides/slide1.xml", False),
                ("rId3", "commentAuthors", "commentAuthors.xml", False),
            ]
        ),
        "ppt/slides/slide1.xml": slide,
        "ppt/slides/_rels/slide1.xml.rels": rels(
            [
                ("rId1", "slideLayout", "../slideLayouts/slideLayout1.xml", False),
                ("rIdN", "notesSlide", "../notesSlides/notesSlide1.xml", False),
                ("rIdC", "comments", "../comments/comment1.xml", False),
            ]
        ),
        "ppt/notesSlides/notesSlide1.xml": notes,
        "ppt/notesSlides/_rels/notesSlide1.xml.rels": rels(
            [("rId1", "slide", "../slides/slide1.xml", False)]
        ),
        "ppt/slideLayouts/slideLayout1.xml": layout,
        "ppt/slideLayouts/_rels/slideLayout1.xml.rels": rels(
            [("rId1", "slideMaster", "../slideMasters/slideMaster1.xml", False)]
        ),
        "ppt/slideMasters/slideMaster1.xml": master,
        "ppt/slideMasters/_rels/slideMaster1.xml.rels": rels(
            [("rId1", "slideLayout", "../slideLayouts/slideLayout1.xml", False)]
        ),
        "ppt/commentAuthors.xml": p_doc(
            "cmAuthorLst",
            f'<p:cmAuthor id="0" name={xa(e.full)} initials="EV" lastIdx="1" clrIdx="0"/>',
        ),
        "ppt/comments/comment1.xml": p_doc(
            "cmLst",
            '<p:cm authorId="0" dt="2026-01-01T00:00:00.000" idx="1"><p:pos x="10" y="10"/>'
            f"<p:text>Vérifier le chiffre avec {x(b.full)} ({b.username})</p:text></p:cm>",
        ),
        "docProps/core.xml": ooxml_core("Projet Hermine", o.full, e.full),
        "docProps/app.xml": ooxml_app("Microsoft Office PowerPoint", COMPANY["orme"][0], e.full),
        "docProps/custom.xml": ooxml_custom("Commercial", b.email),
    }
    doc.dir.mkdir(parents=True)
    write_zip(doc.dir / "projet_hermine.pptx", entries)
    doc.add("ORGANISATION", COMPANY["orme"][0], "métadonnées")
    doc.person(
        o, "titre coupé en deux runs, descr, disposition, créateur", f"Ophélie {o.last}", o.full
    )
    doc.add("EMAIL", o.email, "masque")
    doc.person(b, "corps, notes, commentaire", b.full, first_only=True)
    doc.add("EMAIL", b.email, "corps, propriété personnalisée")
    doc.phone(b.phone, "notes de l'orateur", fmt_phone(b.phone, "spaces"))
    doc.address(b.address, "notes de l'orateur", b.address.line1, b.address.line2)
    doc.add("IDENTIFIANT", b.username, "commentaire")
    doc.person(
        e,
        "corps (civilité + NOM), notes, auteur de commentaire, modificateur",
        f"{e.civ} {e.last.upper()}",
        e.full,
    )
    doc.add("FONCTION", e.job, "corps")
    doc.dob(e.born, "notes", fmt_date(e.born, "slash"))
    doc.keep.append(NEGATIVES[7])
    return doc


def odf_annot(author: str, text: str) -> str:
    return (
        f"<office:annotation><dc:creator>{x(author)}</dc:creator><dc:date>2026-01-01T00:00:00"
        f"</dc:date><text:p>{x(text)}</text:p></office:annotation>"
    )


def doc_odt() -> Doc:
    doc = Doc(
        "13_odt_compte_rendu",
        "odt",
        "Compte rendu OpenDocument : métadonnées, "
        "annotation, modification suivie, en-tête/pied, nom coupé par un span, adresse "
        "avec saut de ligne",
    )
    e, md = P["vidal"], P["durand"]
    org = "Association des Riverains du Val-Fleuri"
    body = (
        "<office:text>"
        '<text:tracked-changes><text:changed-region text:id="ct1"><text:deletion>'
        f"<office:change-info><dc:creator>{x(md.full)}</dc:creator><dc:date>2026-01-01T00:00:00"
        f"</dc:date></office:change-info><text:p>Ancien trésorier joignable au "
        f"{fmt_phone(md.phone, 'spaces')}</text:p></text:deletion>"
        "</text:changed-region></text:tracked-changes>"
        f'<text:h text:outline-level="1">Assemblée générale — {x(org)}</text:h>'
        f'<text:p>Séance présidée par Elé<text:span text:style-name="T1">onore</text:span> '
        f'{x(e.last.upper())}, {x(e.job)}.<text:change text:change-id="ct1"/>'
        f"{odf_annot(md.full, f'Relu par {md.full}, {md.email}')}</text:p>"
        f"<text:p>Le trésorier, {x(md.civ)} {x(md.full)}, né le {fmt_date(md.born, 'long')}, "
        f"demeurant {x(md.address.line1)}<text:line-break/>{x(md.address.line2)}, présente les "
        "comptes.</text:p>"
        f"<text:p>Cotisations à verser sur le compte {fmt_iban(md.iban, 'spaces')}.</text:p>"
        f"<text:p>{x(NEGATIVES[2])}</text:p>"
        f"<text:p>{x(NEGATIVES[9])}</text:p>"
        f"<text:p>M. Durand quitte la séance à 16 h.</text:p>"
        "</office:text>"
    )
    meta = (
        "<meta:generator>Bench</meta:generator>"
        f"<dc:title>AG {x(org)}</dc:title>"
        f"<meta:initial-creator>{x(e.full)}</meta:initial-creator><dc:creator>{x(md.full)}"
        f"</dc:creator><meta:keyword>{md.email}</meta:keyword>"
    )
    data = odf_package_bytes(
        "application/vnd.oasis.opendocument.text",
        body,
        meta=meta,
        header=f"Compte rendu rédigé par {x(e.full)}",
        footer=f"Contact : {e.email} — {fmt_phone(e.phone, 'dots')}",
    )
    doc.dir.mkdir(parents=True)
    (doc.dir / "compte_rendu_ag.odt").write_bytes(data)
    doc.add("ORGANISATION", org, "titre, métadonnées")
    doc.person(e, "corps (span), en-tête, créatrice initiale", "Eléonore " + e.last.upper(), e.full)
    doc.add("FONCTION", e.job, "corps")
    doc.add("EMAIL", e.email, "pied de page")
    doc.phone(e.phone, "pied de page", fmt_phone(e.phone, "dots"))
    doc.person(
        md,
        "corps, annotation, modification suivie, créateur",
        md.full,
        f"{md.civ} {md.full}",
        "M. Durand",
    )
    doc.add("EMAIL", md.email, "annotation, mots-clés")
    doc.phone(md.phone, "modification suivie (suppression)", fmt_phone(md.phone, "spaces"))
    doc.dob(md.born, "corps", fmt_date(md.born, "long"))
    doc.address(md.address, "corps (saut de ligne)", md.address.line1, md.address.line2)
    doc.add("IBAN", md.iban, "corps", fmt_iban(md.iban, "spaces"))
    doc.keep += [NEGATIVES[2], NEGATIVES[9]]
    return doc


def doc_ods() -> Doc:
    doc = Doc(
        "14_ods_planning",
        "ods",
        "Planning OpenDocument tableur : nom de feuille, annotation, cellules",
    )
    bl, ch, ma = P["blanc"], P["chevalier"], P["marchand"]
    sheet = f"Astreintes {bl.last}"

    def cell(text: str, extra: str = "") -> str:
        return (
            f'<table:table-cell office:value-type="string"><text:p>{x(text)}</text:p>{extra}'
            "</table:table-cell>"
        )

    body = (
        f"<office:spreadsheet><table:table table:name={xa(sheet)}>"
        "<table:table-row>"
        + cell("Semaine")
        + cell("Agent")
        + cell("Téléphone")
        + "</table:table-row>"
        "<table:table-row>"
        + cell("S10")
        + cell(bl.upper, odf_annot(ma.full, f"Remplace {ch.full}"))
        + cell(fmt_phone(bl.phone, "spaces"))
        + "</table:table-row>"
        "<table:table-row>"
        + cell("S11")
        + cell(ch.full)
        + cell(fmt_phone(ch.phone, "intlglued"))
        + "</table:table-row>"
        "<table:table-row>"
        + cell("S12")
        + cell(f"{ma.first} {ma.last}")
        + cell(ma.email)
        + "</table:table-row>"
        "<table:table-row>" + cell(NEGATIVES[4]) + "</table:table-row>"
        "</table:table>"
        '<table:table table:name="Résumé"><table:table-row>'
        f'<table:table-cell table:formula="of:=[\'{x(sheet)}\'.B2]" office:value-type="string" '
        f"office:string-value={xa(bl.upper)}><text:p>{x(bl.upper)}</text:p></table:table-cell>"
        "</table:table-row></table:table></office:spreadsheet>"
    )
    meta = (
        f"<dc:creator>{x(ma.full)}</dc:creator>"
        f"<meta:initial-creator>{x(ma.full)}</meta:initial-creator>"
    )
    data = odf_package_bytes(
        "application/vnd.oasis.opendocument.spreadsheet",
        body,
        meta=meta,
        header=f"Planning — {x(ch.full)}",
        footer="Page 1",
    )
    doc.dir.mkdir(parents=True)
    (doc.dir / "planning_astreintes.ods").write_bytes(data)
    doc.person(bl, "nom de feuille, cellule, formule", bl.upper, f"{bl.last}")
    doc.phone(bl.phone, "cellule", fmt_phone(bl.phone, "spaces"))
    doc.person(ch, "cellule, annotation, en-tête", ch.full)
    doc.phone(ch.phone, "cellule", fmt_phone(ch.phone, "intlglued"))
    doc.person(ma, "cellule, auteur d'annotation, créatrice", f"{ma.first} {ma.last}", ma.full)
    doc.add("EMAIL", ma.email, "cellule")
    doc.keep.append(NEGATIVES[4])
    return doc


def rtf_esc(text: str) -> str:
    out = []
    for ch in text:
        if ord(ch) < 128:
            out.append(ch)
        elif ch in "éèàçêôûîëïù":
            out.append(f"\\'{ch.encode('cp1252')[0]:02x}")
        else:
            out.append(f"\\u{ord(ch)}?")
    return "".join(out)


def doc_rtf() -> Doc:
    doc = Doc(
        "15_rtf_attestation",
        "rtf",
        "Attestation RTF : \\info, en-tête/pied, échappements "
        "\\'e9 et \\u, nom coupé par un groupe gras",
    )
    n, f = P["nguyen"], P["delacroix"]
    text = (
        "{\\rtf1\\ansi\\ansicpg1252\\deff0{\\fonttbl{\\f0\\fswiss\\fcharset0 Arial;}}\r\n"
        f"{{\\info{{\\title Attestation {rtf_esc(n.last)}}}{{\\author {rtf_esc(f.full)}}}"
        f"{{\\operator {rtf_esc(f.full)}}}{{\\company {rtf_esc(COMPANY['orme'][0])}}}}}\r\n"
        f"{{\\header \\pard\\plain Pharmacie \\u8212? {rtf_esc(n.full)}\\par}}\r\n"
        f"{{\\footer \\pard\\plain {f.email} \\u8212? {fmt_phone(f.phone, 'dots')}\\par}}\r\n"
        "\\pard\\plain Je soussign\\'e9, "
        f"{{\\b Fir}}min {rtf_esc(f.last.upper())}, {rtf_esc(f.job)}, atteste que "
        f"{rtf_esc(n.civ)} {rtf_esc(n.first)} {{\\i {rtf_esc(n.last)}}}, n\\'e9e le "
        f"{rtf_esc(fmt_date(n.born, 'long'))}, num\\'e9ro de s\\'e9curit\\'e9 sociale "
        f"{fmt_nir(n.nir, 'dots')}, demeure au {rtf_esc(n.address.line1)}\\line "
        f"{rtf_esc(n.address.line2)}.\\par\r\n"
        f"{rtf_esc(NEGATIVES[1])}\\par\r\n"
        f"Fait \\'e0 Rouen. {rtf_esc(f.initials)}\\par\r\n"
        "}"
    )
    doc.dir.mkdir(parents=True)
    (doc.dir / "attestation.rtf").write_bytes(text.encode("ascii"))
    doc.add("ORGANISATION", COMPANY["orme"][0], "\\info company")
    doc.person(n, "titre, en-tête, corps (italique)", n.full, f"{n.first} {n.last}")
    doc.dob(n.born, "corps", fmt_date(n.born, "long"))
    doc.add("NIR", n.nir, "corps", fmt_nir(n.nir, "dots"))
    doc.address(n.address, "corps (\\line)", n.address.line1, n.address.line2)
    doc.person(f, "auteur, corps (gras coupé), signature", f"Firmin {f.last.upper()}", f.full)
    doc.add(
        "PERSONNE",
        f.initials,
        "signature (initiales)",
        f.initials,
        renderings=[{"mode": "text", "text": f.initials}],
    )
    doc.add("EMAIL", f.email, "pied de page")
    doc.phone(f.phone, "pied de page", fmt_phone(f.phone, "dots"))
    doc.add("FONCTION", f.job, "corps")
    doc.add("LIEU", "Rouen", "corps")
    doc.keep.append(NEGATIVES[1])
    return doc


def doc_pdf_text() -> Doc:
    import pymupdf

    doc = Doc(
        "16_pdf_releve",
        "pdf",
        "Relevé PDF texte : deux pages, métadonnées, XMP, "
        "annotation, signets, nom coupé sur deux lignes et sur deux polices",
    )
    bl, n = P["blanc"], P["nguyen"]
    pdf = pymupdf.open()
    page = pdf.new_page()
    page.insert_text((72, 80), "RELEVÉ D'IDENTITÉ BANCAIRE", fontsize=14)
    page.insert_text((72, 110), f"Titulaire : {bl.civ} {bl.last.upper()} {bl.first}", fontsize=11)
    page.insert_text((72, 130), f"IBAN : {fmt_iban(bl.iban, 'spaces')}", fontsize=11)
    page.insert_text((72, 150), f"Carte : {fmt_card(bl.card, 'spaces')}", fontsize=11)
    xpos = 72 + pymupdf.get_text_length("Conseillère : ", fontsize=11)
    page.insert_text((72, 170), "Conseillère : ", fontsize=11)
    page.insert_text((xpos, 170), "Thi ", fontsize=11, fontname="hebo")
    xpos += pymupdf.get_text_length("Thi ", fontname="hebo", fontsize=11)
    page.insert_text((xpos, 170), f"Lan {n.last} — {n.email}", fontsize=11)
    page.insert_textbox(
        pymupdf.Rect(72, 190, 200, 260),
        f"Adresse du titulaire : {bl.address.line1}, {bl.address.line2}",
        fontsize=11,
    )
    page.insert_text((72, 290), NEGATIVES[0][:70], fontsize=10)
    annot = page.add_text_annot((450, 80), f"Vérifié avec {n.full}")
    annot.set_info(title=n.full, subject=f"Client {bl.full}")
    annot.update()
    page.insert_link(
        {
            "kind": pymupdf.LINK_URI,
            "from": pymupdf.Rect(72, 300, 300, 315),
            "uri": f"mailto:{n.email}",
        }
    )
    page2 = pdf.new_page()
    page2.insert_text(
        (72, 80),
        f"Né le {fmt_date(bl.born, 'slash')} — tél. {fmt_phone(bl.phone, 'intl')}",
        fontsize=11,
    )
    page2.insert_text((72, 100), f"Véhicule de service : {bl.plate}", fontsize=11)
    page2.insert_text(
        (72, 120), f"M. BLANC est client depuis 2019. {NEGATIVES[9][:40]}", fontsize=11
    )
    pdf.set_metadata(
        {
            "title": f"RIB {bl.full}",
            "author": n.full,
            "subject": f"Client {bl.last}",
            "keywords": n.email,
            "creator": "Bench",
            "producer": "Bench",
            "creationDate": "D:20260101000000",
            "modDate": "D:20260101000000",
        }
    )
    pdf.set_xml_metadata(
        '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/'
        '22-rdf-syntax-ns#"><rdf:Description xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f"<dc:creator>{x(n.full)}</dc:creator></rdf:Description></rdf:RDF></x:xmpmeta>"
    )
    pdf.set_toc([[1, f"Relevé {bl.full}", 1], [1, "Annexe", 2]])
    doc.dir.mkdir(parents=True)
    (doc.dir / "releve_identite.pdf").write_bytes(
        pdf.tobytes(garbage=3, deflate=True, no_new_id=True)
    )
    pdf.close()
    doc.person(
        bl,
        "corps (NOM Prénom), adresse, titre, sujet, signet, page 2",
        bl.upper,
        "M. BLANC",
        bl.full,
    )
    doc.add("IBAN", bl.iban, "corps", fmt_iban(bl.iban, "spaces"))
    doc.add("CARTE", bl.card, "corps", fmt_card(bl.card, "spaces"))
    doc.address(bl.address, "zone de texte sur deux lignes", bl.address.line1, bl.address.line2)
    doc.dob(bl.born, "page 2", fmt_date(bl.born, "slash"))
    doc.phone(bl.phone, "page 2", fmt_phone(bl.phone, "intl"))
    doc.add("PLAQUE", bl.plate, "page 2")
    doc.person(n, "deux polices sur une ligne, annotation, auteur, XMP", n.full)
    doc.add("EMAIL", n.email, "corps, lien, mots-clés")
    return doc


def letter_canvas(doc: Doc, image_id: str, p: Person, lines_extra: list[str]) -> Canvas:
    canvas = Canvas(1240, 1754)
    y = 120
    lines = [
        f"{p.civ} {p.first} {p.last.upper()}",
        p.address.line1,
        p.address.line2,
        f"Tél. : {fmt_phone(p.phone, 'spaces')}",
        "",
        "Objet : demande de remboursement",
        "",
        f"Je soussigné {p.full}, né le {fmt_date(p.born, 'slash')},",
        f"numéro de sécurité sociale {fmt_nir(p.nir, 'spaces')},",
        f"demande le virement sur le compte {fmt_iban(p.iban, 'spaces')}.",
        *lines_extra,
        "",
        "Veuillez agréer mes salutations distinguées.",
    ]
    for line in lines:
        if line:
            canvas.text((110, y), line, 32)
        y += 58
    mark(doc, canvas, image_id, "PERSONNE", p.full, p.last.upper())
    mark(doc, canvas, image_id, "ADRESSE", p.address.value, p.address.line1)
    mark(doc, canvas, image_id, "TELEPHONE", "+33" + p.phone, fmt_phone(p.phone, "spaces"))
    mark(doc, canvas, image_id, "DATE_NAISSANCE", p.born.isoformat(), fmt_date(p.born, "slash"))
    mark(doc, canvas, image_id, "NIR", p.nir, fmt_nir(p.nir, "spaces"))
    mark(doc, canvas, image_id, "IBAN", p.iban, fmt_iban(p.iban, "spaces"))
    return canvas


def doc_pdf_signed() -> Doc:
    """The e-signed PDF incident (``tests/fixtures/generators/signed_pdf.py``)."""
    from tests.benchmark.common import norm_compact
    from tests.fixtures.generators import signed_pdf as sig

    doc = Doc(
        "25_pdf_signe",
        "pdf",
        "PDF signé électroniquement : tampons de signature en images (nom en casse "
        "mixte, horodatage vertical), référence hexadécimale en image et en QR code, "
        "identifiant de transaction en texte invisible, signature numérique, nom de "
        "fichier nominatif, éditeur en liste blanche (nom, URL, logo) à conserver",
    )
    doc.dir.mkdir(parents=True)
    (doc.dir / sig.PDF_NAME).write_bytes(sig.signed_pdf())
    doc.images["page2"] = {"kind": "pdf_page", "file": sig.PDF_NAME, "page": 1}
    regions = sig.cert_page_regions()
    entry = doc.add(
        "PERSONNE",
        sig.SIGNER,
        "corps, champ de formulaire, tampon image (casse mixte), signature numérique, "
        "métadonnées, XMP, nom de fichier",
        sig.SIGNER,
        sig.STAMP_NAME,
        renderings=[
            {"mode": "text", "text": sig.SIGNER},
            {"mode": "text", "text": f"{sig.SURNAME} {sig.GIVEN}"},
            {"mode": "text", "text": sig.SURNAME},
            {"mode": "text", "text": "Quintavalle"},
            {"mode": "text", "text": "Brezac"},
        ],
    )
    entry["partial"].append({"mode": "word", "text": sig.GIVEN})
    doc.region("PERSONNE", sig.SIGNER, "page2", regions["name"])
    doc.add("EMAIL", sig.SIGNER_EMAIL, "corps, certificat de signature")
    doc.add(
        "HORODATAGE",
        sig.TIMESTAMP,
        "tampon image (vertical), date de signature, XMP",
        renderings=[
            {"mode": "text", "text": sig.TIMESTAMP.removesuffix(" UTC")},
            {"mode": "compact", "text": "20260314104207"},
        ],
    )
    doc.region("HORODATAGE", sig.TIMESTAMP, "page2", regions["timestamp"])
    doc.add(
        "IDENTIFIANT_TECHNIQUE",
        sig.HEX_REF,
        "image, QR code, mots-clés",
        renderings=[{"mode": "compact", "text": sig.HEX_REF}],
    )
    doc.region("IDENTIFIANT_TECHNIQUE", sig.HEX_REF, "page2", regions["reference"])
    doc.region("IDENTIFIANT_TECHNIQUE", sig.HEX_REF, "page2", regions["qr"])
    doc.add(
        "IDENTIFIANT_TECHNIQUE",
        sig.TRANSACTION_ID,
        "texte invisible, mots-clés",
        renderings=[{"mode": "compact", "text": norm_compact(sig.TRANSACTION_ID)}],
    )
    doc.keep += [f"Document émis via {sig.PUBLISHER}", sig.PUBLISHER_URL]
    return doc


def doc_pdf_scan() -> Doc:
    import pymupdf

    doc = Doc("17_pdf_numerise", "pdf", "Courrier numérisé : page image JPEG sans couche texte")
    p = P["valombreuse"]
    doc.person(p, "image de la page", f"{p.civ} {p.first} {p.last.upper()}", p.full)
    doc.address(p.address, "image de la page", p.address.line1, p.address.line2)
    doc.phone(p.phone, "image de la page", fmt_phone(p.phone, "spaces"))
    doc.dob(p.born, "image de la page", fmt_date(p.born, "slash"))
    doc.add("NIR", p.nir, "image de la page", fmt_nir(p.nir, "spaces"))
    doc.add("IBAN", p.iban, "image de la page", fmt_iban(p.iban, "spaces"))
    doc.add("EMAIL", p.email, "image de la page")
    canvas = letter_canvas(doc, "page1", p, [f"Courriel : {p.email}"])
    mark(doc, canvas, "page1", "EMAIL", p.email, p.email)
    pdf = pymupdf.open()
    page = pdf.new_page(width=595, height=842)
    page.insert_image(page.rect, stream=canvas.jpeg(85))
    pdf.set_metadata(
        {"creator": "Scanner", "producer": "Scanner", "creationDate": "D:20260101000000"}
    )
    doc.dir.mkdir(parents=True)
    (doc.dir / "courrier_numerise.pdf").write_bytes(
        pdf.tobytes(garbage=3, deflate=True, no_new_id=True)
    )
    pdf.close()
    doc.images["page1"] = {"kind": "pdf_page", "file": "courrier_numerise.pdf", "page": 0}
    return doc


def doc_png() -> Doc:
    doc = Doc("18_png_capture", "png", "Capture d'écran d'un formulaire (PNG, métadonnées tEXt)")
    f = P["delacroix"]
    canvas = Canvas(1100, 560, "#eef1f5")
    canvas.draw.rectangle((20, 20, 1080, 540), outline="#888888", width=2)
    canvas.text((50, 45), "Fiche adhérent — espace client", 34)
    rows = [
        ("Nom", f"{f.first} {f.last}"),
        ("Courriel", f.email),
        ("Téléphone", fmt_phone(f.phone, "dots")),
        ("IBAN", fmt_iban(f.iban, "spaces")),
        ("Adresse IP", f.ip),
        ("Identifiant", f.username),
    ]
    for n, (label, value) in enumerate(rows):
        canvas.text((60, 120 + n * 65), f"{label} : {value}", 30)
    doc.person(f, "image, métadonnées PNG", f"{f.first} {f.last}", f.full)
    doc.add("EMAIL", f.email, "image")
    doc.phone(f.phone, "image", fmt_phone(f.phone, "dots"))
    doc.add("IBAN", f.iban, "image", fmt_iban(f.iban, "spaces"))
    doc.add("IP", f.ip, "image")
    doc.add("IDENTIFIANT", f.username, "image")
    mark(doc, canvas, "img", "PERSONNE", f.full, f.last)
    mark(doc, canvas, "img", "EMAIL", f.email, f.email)
    mark(doc, canvas, "img", "TELEPHONE", "+33" + f.phone, fmt_phone(f.phone, "dots"))
    mark(doc, canvas, "img", "IBAN", f.iban, fmt_iban(f.iban, "spaces"))
    mark(doc, canvas, "img", "IP", f.ip, f.ip)
    mark(doc, canvas, "img", "IDENTIFIANT", f.username, f.username)
    ma = P["marchand"]
    info = PngInfo()
    info.add_text("Author", ma.full)
    info.add_itxt("Description", f"Capture réalisée par {ma.full}", lang="fr")
    doc.person(ma, "métadonnées PNG (tEXt, iTXt)", ma.full)
    doc.dir.mkdir(parents=True)
    (doc.dir / "capture_formulaire.png").write_bytes(canvas.png(info))
    doc.images["img"] = {"kind": "file", "file": "capture_formulaire.png"}
    return doc


def gps_exif(artist: str, description: str) -> bytes:
    exif = Image.Exif()
    exif[0x013B] = artist
    exif[0x010E] = description
    exif[0x9C9D] = artist.encode("utf-16-le") + b"\0\0"
    exif_ifd = exif.get_ifd(0x8769)
    exif_ifd[0xA430] = artist
    gps = exif.get_ifd(0x8825)
    gps[1] = "N"
    gps[2] = (47.0, 13.0, 12.5)
    gps[3] = "W"
    gps[4] = (1.0, 33.0, 4.25)
    return exif.tobytes()


def doc_jpg() -> Doc:
    doc = Doc(
        "19_jpg_badge",
        "jpg",
        "Photo de badge JPEG : texte dans l'image, EXIF Artist, XPAuthor, propriétaire, GPS",
    )
    ma = P["marchand"]
    canvas = Canvas(1000, 330, "#f4f4f4")
    canvas.text((40, 30), "BADGE PERSONNEL", 34)
    canvas.text((40, 95), f"{ma.first} {ma.last.upper()}", 48)
    canvas.text((40, 175), f"Né(e) le {fmt_date(ma.born, 'slash')}", 32)
    canvas.text((40, 240), f"Matricule : {ma.username}", 32)
    doc.person(
        ma,
        "image, EXIF (Artist, XPAuthor, CameraOwnerName, description)",
        f"{ma.first} {ma.last.upper()}",
        ma.full,
    )
    doc.dob(ma.born, "image", fmt_date(ma.born, "slash"))
    doc.add("IDENTIFIANT", ma.username, "image")
    doc.add("GPS", "47.22,-1.55", "EXIF GPS")
    mark(doc, canvas, "img", "PERSONNE", ma.full, ma.last.upper())
    mark(doc, canvas, "img", "DATE_NAISSANCE", ma.born.isoformat(), fmt_date(ma.born, "slash"))
    mark(doc, canvas, "img", "IDENTIFIANT", ma.username, ma.username)
    doc.dir.mkdir(parents=True)
    (doc.dir / "photo_badge.jpg").write_bytes(
        canvas.jpeg(92, exif=gps_exif(ma.full, f"Badge de {ma.full}"))
    )
    doc.images["img"] = {"kind": "file", "file": "photo_badge.jpg"}
    return doc


def doc_zip_nested() -> Doc:
    doc = Doc(
        "20_zip_dossier",
        "zip",
        "Archive zip : dossier au nom d'une personne, fichiers "
        "nommés, courriel contenant une archive contenant un docx et un txt (archive dans "
        "un courriel dans une archive), commentaire d'archive",
    )
    f, i = P["delacroix"], P["benali"]
    random.seed(SEED + 20)
    folder = f"Dossier {f.first} {f.last}"
    inner_zip = io.BytesIO()
    with zipfile.ZipFile(inner_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        zip_entry(
            zf,
            "contrat.docx",
            simple_docx(
                [
                    f"Contrat de travail de {f.full}",
                    f"Né le {fmt_date(f.born, 'long')}",
                    f"NIR : {fmt_nir(f.nir, 'spaces')}",
                    NEGATIVES[7],
                ],
                title=f"Contrat {f.last}",
                author=i.full,
            ),
        )
        zip_entry(
            zf, f"RIB_{f.last}.txt", f"Titulaire : {f.full}\nIBAN : {fmt_iban(f.iban, 'dashes')}\n"
        )
    mail = EmailMessage(policy=SMTP)
    mail["From"] = formataddr((i.full, i.email))
    mail["To"] = formataddr((f.full, f.email))
    mail["Subject"] = f"Pièces du dossier {f.last}"
    mail["Date"] = "Fri, 06 Mar 2026 10:00:00 +0100"
    mail.set_content(
        f"Bonjour {f.first},\nVoici l'archive demandée.\n{i.first} {i.last}\n"
        f"{fmt_phone(i.phone, 'spaces')}\n"
    )
    mail.add_attachment(
        inner_zip.getvalue(), maintype="application", subtype="zip", filename="pieces.zip"
    )
    outer = io.BytesIO()
    with zipfile.ZipFile(outer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.comment = f"Archive constituée par {i.full}".encode()
        zip_entry(
            zf,
            f"{folder}/CV_{f.first}_{f.last}.txt",
            f"Curriculum vitae\n{f.full}\n{f.address.line1}\n{f.address.line2}\n"
            f"{f.email} — {fmt_phone(f.phone, 'intl')}\nNé le {fmt_date(f.born, 'slash')}\n"
            f"{NEGATIVES[6]}\n",
        )
        zip_entry(zf, f"{folder}/courriels/echange_{f.username}.eml", mail.as_bytes(policy=SMTP))
        zip_entry(
            zf,
            f"{folder}/notes.csv",
            f"date;auteur;note\n2026-03-01;{i.full};Entretien avec {f.last.upper()}\n",
        )
    doc.dir.mkdir(parents=True)
    (doc.dir / "dossier_complet.zip").write_bytes(outer.getvalue())
    doc.person(
        f,
        "nom de dossier, nom de fichier, CV, courriel, docx et txt imbriqués",
        folder,
        f"CV_{f.first}_{f.last}",
        f.full,
        f.last.upper(),
        first_only=True,
    )
    doc.add(
        "ADRESSE",
        f.address.value,
        "CV",
        f.address.line1,
        f.address.line2,
        renderings=[
            {"mode": "text", "text": f.address.line1},
            {"mode": "text", "text": f.address.street},
        ],
    )
    doc.add("EMAIL", f.email, "CV, courriel")
    doc.phone(f.phone, "CV", fmt_phone(f.phone, "intl"))
    doc.dob(f.born, "CV, docx imbriqué", fmt_date(f.born, "slash"), fmt_date(f.born, "long"))
    doc.add("IDENTIFIANT", f.username, "nom de fichier du courriel")
    doc.add("NIR", f.nir, "docx dans zip dans courriel dans zip", fmt_nir(f.nir, "spaces"))
    doc.add("IBAN", f.iban, "txt dans zip dans courriel dans zip", fmt_iban(f.iban, "dashes"))
    doc.person(
        i, "commentaire d'archive, courriel, auteur du docx, CSV", i.full, f"{i.first} {i.last}"
    )
    doc.add("EMAIL", i.email, "courriel")
    doc.phone(i.phone, "courriel", fmt_phone(i.phone, "spaces"))
    doc.keep += [NEGATIVES[6], NEGATIVES[7]]
    return doc


def doc_7z() -> Doc:
    import py7zr

    doc = Doc(
        "21_7z_sauvegarde",
        "7z",
        "Archive 7z contenant un zip contenant un courriel avec une pièce jointe CSV",
    )
    md, e = P["durand"], P["vidal"]
    random.seed(SEED + 21)
    mail = EmailMessage(policy=SMTP)
    mail["From"] = formataddr((md.full, md.email))
    mail["To"] = formataddr((e.full, e.email))
    mail["Subject"] = "Cotisations"
    mail.set_content(
        f"Eléonore,\nles cotisations sont à jour sauf pour {P['lambert'].full}.\n"
        f"Marc Durand — {fmt_phone(md.phone, 'intl')}\n"
    )
    mail.add_attachment(
        (
            f"nom;iban\n{md.last.upper()} {md.first};{fmt_iban(md.iban, 'spaces')}\n"
            f"{e.last.upper()} {e.first};{fmt_iban(e.iban, 'spaces')}\n"
        ).encode(),
        maintype="text",
        subtype="csv",
        filename="cotisations.csv",
    )
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w", zipfile.ZIP_DEFLATED) as zf:
        zip_entry(zf, "courriel_cotisations.eml", mail.as_bytes(policy=SMTP))
        zip_entry(zf, "lisezmoi.txt", NEGATIVES[0] + "\n")
    doc.dir.mkdir(parents=True)
    target = doc.dir / "sauvegarde.7z"
    with py7zr.SevenZipFile(target, "w") as sz:
        sz.writestr(inner.getvalue(), "export/export.zip")
        sz.writestr(f"Trésorier {md.full}\n".encode(), f"export/{md.last}.txt")
    doc.person(
        md,
        "courriel dans zip dans 7z, nom de fichier 7z, CSV",
        md.full,
        f"{md.last.upper()} {md.first}",
        md.last,
    )
    doc.add("EMAIL", md.email, "courriel")
    doc.phone(md.phone, "courriel", fmt_phone(md.phone, "intl"))
    doc.add("IBAN", md.iban, "CSV joint", fmt_iban(md.iban, "spaces"))
    doc.person(e, "courriel, CSV joint", e.full, f"{e.last.upper()} {e.first}", first_only=True)
    doc.add("EMAIL", e.email, "courriel")
    doc.add("IBAN", e.iban, "CSV joint", fmt_iban(e.iban, "spaces"))
    doc.person(P["lambert"], "courriel", P["lambert"].full)
    doc.keep.append(NEGATIVES[0])
    return doc


def doc_named_folders() -> Doc:
    doc = Doc(
        "22_dossiers_nommes",
        "txt",
        "Arborescence de fichiers : dossier et fichiers "
        "portant le nom de la personne, casse mixte et accents manquants",
    )
    o, g = P["gauthier"], P["girard"]
    folder = doc.dir / f"Salariés/{o.first} {o.last.upper()}"
    folder.mkdir(parents=True)
    (folder / f"CV {o.first} {o.last}.txt").write_text(
        f"ophelie gauthier\nOPHELIE GAUTHIER\n{o.address.line1}, {o.address.line2}\n"
        f"Née le {fmt_date(o.born, 'iso')}\n{NEGATIVES[3]}\n",
        encoding="utf-8",
        newline="\n",
    )
    (folder / f"{o.last.lower()}_{o.first[0].lower()}_entretien.csv").write_text(
        f"date;evaluateur;commentaire\n2026-02-02;{g.full};Entretien de Mme Gauthier\n",
        encoding="utf-8",
        newline="\n",
    )
    doc.person(
        o,
        "noms de dossier et de fichiers, minuscules et majuscules sans accent",
        f"{o.first} {o.last.upper()}",
        "ophelie gauthier",
        "OPHELIE GAUTHIER",
        "Mme Gauthier",
    )
    doc.address(o.address, "CV", o.address.line1, o.address.line2)
    doc.dob(o.born, "CV", fmt_date(o.born, "iso"))
    doc.person(g, "CSV", g.full)
    doc.keep.append(NEGATIVES[3])
    return doc


def doc_negatives() -> Doc:
    doc = Doc(
        "23_txt_temoins",
        "txt",
        "Témoin négatif : texte ordinaire avec mots courants "
        "homonymes de noms (petit, boucher, martin-pêcheur...), villes, dates de réunion",
    )
    text = "\n\n".join(
        [
            "Note interne — organisation du séminaire de printemps",
            *NEGATIVES,
            "Le séminaire aura lieu à Lyon puis à Vienne, en Isère, avant un retour à Paris.",
            "Prévoir un petit budget pour le traiteur et réserver la salle Fontaine.",
            "Le blanc d'œuf et le vin blanc sont commandés chez le marchand habituel.",
            "Référence de commande interne : BC-2026-0042, à rappeler sur chaque facture.",
            "Le chevalier servant de la pièce de théâtre sera joué par un comédien amateur.",
        ]
    )
    doc.dir.mkdir(parents=True)
    (doc.dir / "notes_internes.txt").write_text(text + "\n", encoding="utf-8", newline="\n")
    for city in ("Lyon", "Vienne", "Paris", "Isère"):
        doc.add("LIEU", city, "corps")
    doc.keep += [
        *NEGATIVES,
        "Prévoir un petit budget pour le traiteur",
        "Le blanc d'œuf et le vin blanc sont commandés chez le marchand habituel.",
        "Le chevalier servant de la pièce de théâtre sera joué par un comédien amateur.",
    ]
    return doc


def doc_english() -> Doc:
    doc = Doc(
        "24_txt_anglais",
        "txt",
        "Rapport d'incident en anglais : téléphones "
        "internationaux, IBAN DE/BE, IPv6, date au format US, apostrophe dans le nom",
    )
    o, s, lm = P["pemberton"], P["ocallaghan"], P["muller"]
    text = f"""INCIDENT REPORT #INC-2026-0315

Reported by: {o.full} ({o.job}), {o.email}
Phone: +44 20 7946 0123 / mobile +44 7700 900456
Date of birth: {fmt_date(o.born, "en")} — staff ID {o.username}
Home address: {o.address.line1}, {o.address.line2}

On 12 March 2026, {s.full} (HR lead, {s.email}, +353 1 555 0199) noticed repeated failed logins
for the account {s.username} from 2001:db8:85a3::8a2e:370:7334 and 198.51.100.77.
Ms O'Callaghan escalated to Lukas MÜLLER (Berlin office, +49 30 901820, {lm.email}).
Mr Müller confirmed the refund to IBAN {fmt_iban(lm.iban, "spaces")} and the payment card
{fmt_card(o.card, "dashes")} belonging to Mr Pemberton (date of birth {fmt_date(o.born, "us")}).
Belgian account on file: {fmt_iban(o.iban, "spaces")}.

The quarterly budget review is postponed until further notice.
The help desk is open from 8 am to 6 pm, Monday to Friday.
"""
    doc.dir.mkdir(parents=True)
    (doc.dir / "incident_report.txt").write_text(text, encoding="utf-8", newline="\n")
    doc.add("NUMERO_DOSSIER", "INC-2026-0315", "titre")
    doc.person(o, "corps", o.full, "Mr Pemberton")
    doc.add("FONCTION", o.job, "corps")
    doc.add("EMAIL", o.email, "corps")
    doc.intl_phone("+442079460123", "2079460123", "corps", "+44 20 7946 0123")
    doc.intl_phone("+447700900456", "7700900456", "corps", "+44 7700 900456")
    doc.dob(o.born, "corps", fmt_date(o.born, "en"), fmt_date(o.born, "us"))
    doc.add("IDENTIFIANT", o.username, "corps")
    doc.address(o.address, "corps", o.address.line1, o.address.line2)
    doc.add("LIEU", "Bristol", "adresse")
    doc.person(s, "corps (apostrophe)", s.full, "Ms O'Callaghan")
    doc.add("EMAIL", s.email, "corps")
    doc.intl_phone("+35315550199", "15550199", "corps", "+353 1 555 0199")
    doc.add("IDENTIFIANT", s.username, "corps")
    doc.add("IP", "2001:db8:85a3::8a2e:370:7334", "corps")
    doc.add("IP", "198.51.100.77", "corps")
    doc.person(lm, "corps (majuscules avec tréma)", "Lukas MÜLLER", "Mr Müller")
    doc.add("LIEU", "Berlin", "corps")
    doc.intl_phone("+4930901820", "30901820", "corps", "+49 30 901820")
    doc.add("EMAIL", lm.email, "corps")
    doc.add("IBAN", lm.iban, "corps", fmt_iban(lm.iban, "spaces"))
    doc.add("CARTE", o.card, "corps", fmt_card(o.card, "dashes"))
    doc.add("IBAN", o.iban, "corps", fmt_iban(o.iban, "spaces"))
    doc.keep += [
        "The quarterly budget review is postponed until further notice.",
        "The help desk is open from 8 am to 6 pm, Monday to Friday.",
    ]
    return doc


BUILDERS: list[Callable[[], Doc]] = [
    doc_txt_lettre,
    doc_csv,
    doc_json,
    doc_xml,
    doc_html,
    doc_eml_thread,
    doc_eml_encoded,
    doc_mbox,
    doc_msg,
    doc_docx,
    doc_xlsx,
    doc_pptx,
    doc_odt,
    doc_ods,
    doc_rtf,
    doc_pdf_text,
    doc_pdf_scan,
    doc_png,
    doc_jpg,
    doc_zip_nested,
    doc_7z,
    doc_named_folders,
    doc_negatives,
    doc_english,
    doc_pdf_signed,
]


def check_numbers() -> None:
    for p in P.values():
        assert luhn_ok(p.card), p.card
        assert int(p.nir[13:]) == 97 - int(p.nir[:13]) % 97, p.nir
    for _, sn in COMPANY.values():
        assert luhn_ok(sn)


def main(root: Path | None = None) -> None:
    """Write the corpus and its ground truth (to ``root`` instead of this folder)."""
    OUTPUT["corpus"] = root / "corpus" if root else CORPUS
    OUTPUT["truth"] = root / "ground_truth" if root else TRUTH
    RNG.seed(SEED)
    COMPANY.clear()
    COMPANY.update(build_companies())
    P.clear()
    P.update(build_people())
    check_numbers()
    for target in OUTPUT.values():
        if target.exists():
            shutil.rmtree(target)
    OUTPUT["corpus"].mkdir(parents=True)
    for build in BUILDERS:
        doc = build()
        doc.save()
    total = sum(f.stat().st_size for f in OUTPUT["corpus"].rglob("*") if f.is_file())
    print(f"{len(BUILDERS)} documents, {total / 1024:.0f} Kio")


if __name__ == "__main__":
    main()
