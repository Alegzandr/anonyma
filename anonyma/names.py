"""Person-name parsing, civilities and derived surface forms.

Shared by the resolver (variant linking), propagation (login-style forms in file
names and URLs), the replacer (structure-preserving substitutes) and the verifier.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache

from anonyma.data import first_name, gender_of, is_common_word
from anonyma.textutils import normalize, strip_accents

#: Normalized civility -> gender ("m", "f" or "" when unknown).
CIVILITIES: dict[str, str] = {
    "m": "m", "m.": "m", "mr": "m", "mr.": "m", "monsieur": "m", "mm": "m", "mm.": "m",
    "messieurs": "m", "sir": "m",
    "mme": "f", "mme.": "f", "mmes": "f", "madame": "f", "mlle": "f", "mlle.": "f",
    "melle": "f", "mademoiselle": "f", "mrs": "f", "mrs.": "f", "ms": "f", "ms.": "f",
    "miss": "f",
    "dr": "", "dr.": "", "docteur": "", "pr": "", "pr.": "", "professeur": "",
    "me": "", "me.": "", "maitre": "", "mgr": "m", "monseigneur": "m", "pere": "m",
    "soeur": "f", "frere": "m",
}  # fmt: skip

#: Lower-case name particles kept inside surnames.
PARTICLES = frozenset(
    {"de", "du", "des", "d'", "la", "le", "les", "van", "von", "der", "den", "ter", "di",
     "da", "del", "della", "dos", "das", "do", "ben", "bin", "ibn", "el", "al", "y", "zu",
     "st", "saint", "sainte", "mac", "mc", "o'"}
)  # fmt: skip

_TOKEN = re.compile(r"[^\s,;:()\"«»<>]+")
#: File-name style: ``_`` between the words (``zephyrin_QUINTAVALLE``).
_UNDERSCORE_TOKEN = re.compile(r"[^\s,;:()\"«»<>_]+")
_INITIAL = re.compile(r"^(?:[A-Za-zÀ-ÿ]\.?)(?:-?[A-Za-zÀ-ÿ]\.?)?$")


def is_initial(token: str) -> bool:
    """``J.``, ``J``, ``J.-P.``, ``JP.``; not a two-letter word without dot."""
    t = token.strip()
    if not t:
        return False
    letters = [c for c in t if c.isalpha()]
    if len(letters) == 1:
        return True
    return len(letters) == 2 and ("." in t or "-" in t) and bool(_INITIAL.match(t))


def initials_of(token: str) -> str:
    """Normalized initials of a given-name token: ``Jean-Pierre`` -> ``jp``."""
    if is_initial(token):
        return normalize("".join(c for c in token if c.isalpha()))
    return "".join(normalize(part)[:1] for part in token.split("-") if part)


def civility_of(token: str) -> str | None:
    """Gender ("m"/"f"/"") if ``token`` is a civility, else None."""
    return CIVILITIES.get(normalize(token))


def _is_upper_word(token: str) -> bool:
    letters = [c for c in token if c.isalpha()]
    return len(letters) >= 2 and all(c.isupper() for c in letters)


@dataclass(frozen=True, slots=True)
class PersonName:
    """A parsed person mention. Tokens keep their original spelling."""

    civility: str
    given: tuple[str, ...]
    surname: tuple[str, ...]
    surname_first: bool
    gender: str | None

    @property
    def given_key(self) -> str:
        return "-".join(normalize(g) for g in self.given if not is_initial(g))

    @property
    def initials(self) -> str:
        return "".join(initials_of(g) for g in self.given)

    @property
    def surname_key(self) -> str:
        return " ".join(normalize(s) for s in self.surname)

    @property
    def has_full_given(self) -> bool:
        return any(not is_initial(g) for g in self.given)

    @property
    def is_full(self) -> bool:
        return self.has_full_given and bool(self.surname)

    def identity_key(self) -> str:
        """Clustering key, independent of case, accents, order and civility."""
        if self.is_full:
            return f"p:{self.given_key}|{self.surname_key}"
        if self.surname and self.given:
            return f"i:{self.initials}|{self.surname_key}"
        if self.surname:
            return f"s:{self.surname_key}"
        if self.given:
            return f"g:{self.given_key or self.initials}"
        return ""


def tokens_of(value: str) -> list[str]:
    return _TOKEN.findall(value)


@lru_cache(maxsize=65536)
def parse_person(value: str) -> PersonName | None:
    """Parse ``value`` as a person name; None if it holds no name token."""
    tokens = tokens_of(value)
    if "_" in value:
        # ``zephyrin_QUINTAVALLE`` reads as two words when the case or a known given
        # name tells the order; otherwise (``Durand_Zephyrin``) the order is a guess
        # and the value stays one surname-like token, as before.
        split = _UNDERSCORE_TOKEN.findall(value)
        words = [w for w in split if any(c.isalpha() for c in w)]
        upper = [_is_upper_word(w) for w in words]
        if (any(upper) and not all(upper)) or any(given_like(w) for w in words):
            tokens = split
    civility: list[str] = []
    gender: str | None = None
    while tokens and civility_of(tokens[0]) is not None and len(tokens) > 1:
        tok = tokens.pop(0)
        civility.append(tok)
        gender = civility_of(tok) or gender
    words = [t for t in tokens if any(c.isalpha() for c in t)]
    if not words:
        return None
    given: list[str] = []
    surname: list[str] = []
    surname_first = False
    if len(words) == 1:
        word = words[0]
        record = first_name(word)
        if is_initial(word) or (record is not None and not civility and record.count >= 500):
            given = [word]
        else:
            surname = [word]
    else:
        upper = [_is_upper_word(w) for w in words]
        if any(upper) and not all(upper):
            # French convention: SURNAME in capitals, given names in title case.
            first_upper = upper.index(True)
            if first_upper == 0:
                surname_first = True
                k = 0
                while k < len(words) and (upper[k] or normalize(words[k]) in PARTICLES):
                    k += 1
                surname, given = words[:k], words[k:]
            else:
                given, surname = words[:first_upper], words[first_upper:]
        else:
            first_is_name = given_like(words[0])
            last_is_name = given_like(words[-1])
            if not first_is_name and last_is_name and not is_initial(words[0]):
                surname_first = True
                k = len(words) - 1
                while k > 1 and given_like(words[k - 1]):
                    k -= 1
                surname, given = words[:k], words[k:]
            else:
                k = 1
                while k < len(words) - 1 and (given_like(words[k]) or is_initial(words[k])):
                    if normalize(words[k]) in PARTICLES:
                        break
                    k += 1
                given, surname = words[:k], words[k:]
    if gender is None:
        for g in given:
            if not is_initial(g):
                gender = gender_of(g)
                break
    return PersonName(" ".join(civility), tuple(given), tuple(surname), surname_first, gender)


def given_like(token: str) -> bool:
    """An initial or a given name of the lexicon (any case, compounds included)."""
    if is_initial(token):
        return True
    record = first_name(token)
    if record is None and "-" in token:
        parts = token.split("-")
        return all(first_name(p) is not None for p in parts if p)
    return record is not None and record.count >= 200


# -- derived forms ---------------------------------------------------------------


def _ascii(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", strip_accents(text).casefold())


Template = Callable[[str, str, str], str]

#: Login / file-name style templates: (given, initials, surname) -> form.
LOGIN_TEMPLATES: dict[str, Template] = {
    "g.s": lambda g, i, s: f"{g}.{s}",
    "g-s": lambda g, i, s: f"{g}-{s}",
    "g_s": lambda g, i, s: f"{g}_{s}",
    "gs": lambda g, i, s: f"{g}{s}",
    "s.g": lambda g, i, s: f"{s}.{g}",
    "s-g": lambda g, i, s: f"{s}-{g}",
    "s_g": lambda g, i, s: f"{s}_{g}",
    "sg": lambda g, i, s: f"{s}{g}",
    "is": lambda g, i, s: f"{i}{s}",
    "i.s": lambda g, i, s: f"{i}.{s}",
    "i-s": lambda g, i, s: f"{i}-{s}",
    "i_s": lambda g, i, s: f"{i}_{s}",
    "si": lambda g, i, s: f"{s}{i}",
    "s.i": lambda g, i, s: f"{s}.{i}",
    "s_i": lambda g, i, s: f"{s}_{i}",
}


def login_parts(person: PersonName) -> tuple[str, str, str] | None:
    """ASCII (given, initials, surname) used by the login templates."""
    if not person.is_full:
        return None
    given = _ascii(person.given_key.replace("-", ""))
    surname = _ascii(person.surname_key)
    initials = _ascii(person.initials)
    if len(surname) < 2 or not given:
        return None
    return given, initials, surname


def login_forms(person: PersonName) -> dict[str, str]:
    return dict(_login_forms(person))


@lru_cache(maxsize=65536)
def _login_forms(person: PersonName) -> dict[str, str]:
    """Template name -> derived form (``jdupont``, ``jean.dupont``, ``dupont_jean``...)."""
    parts = login_parts(person)
    if parts is None:
        return {}
    forms: dict[str, str] = {}
    for name, template in LOGIN_TEMPLATES.items():
        form = template(*parts)
        if len(form) >= 5 and form not in forms.values():
            forms[name] = form
    # Hyphens kept (``jean-baptiste.kerguelen``, ``berenice.lagarde-moreau``): the
    # template names carry an ``h`` (``gh.s``, ``g.sh``); without it they are the
    # plain templates.
    given, initials, surname = parts
    hyphen_given = "-".join(_ascii(g) for g in person.given_key.split("-") if g)
    hyphen_surname = "-".join(
        w for w in (_ascii(p) for p in re.split(r"[-\s]+", person.surname_key)) if w
    )
    if "-" in hyphen_given:
        forms.setdefault("gh.s", f"{hyphen_given}.{surname}")
        forms.setdefault("gh-s", f"{hyphen_given}-{surname}")
    if "-" in hyphen_surname:
        forms.setdefault("g.sh", f"{given}.{hyphen_surname}")
        forms.setdefault("g_sh", f"{given}_{hyphen_surname}")
        forms.setdefault("i.sh", f"{initials}.{hyphen_surname}")
        forms.setdefault("ish", f"{initials}{hyphen_surname}")
        forms.setdefault("sh.g", f"{hyphen_surname}.{given}")
        if "-" in hyphen_given:
            forms.setdefault("gh.sh", f"{hyphen_given}.{hyphen_surname}")
    return forms


def login_of(template: str, person: PersonName) -> str | None:
    """Form of ``person`` for a template of :func:`login_forms`; a hyphen template
    (``g.sh``) falls back to the plain one when ``person`` has no hyphen."""
    forms = login_forms(person)
    return forms.get(template) or forms.get(template.replace("h", ""))


def match_login_template(local: str, person: PersonName) -> str | None:
    """Name of the template that turns ``person`` into ``local`` (case-insensitive)."""
    target = local.casefold()
    for name, form in login_forms(person).items():
        if form == target:
            return name
    return None


def _dotted(token: str, *, spaced_parts: bool) -> str:
    """``Jean-Baptiste`` -> ``J.-B.``; ``Delacroix-Vasseur`` -> ``D.-V.`` (or ``D.V.``)."""
    parts = [p for p in token.replace("'", "-").split("-") if p and p[0].isalpha()]
    return ("-" if spaced_parts else "").join(f"{p[0].upper()}." for p in parts)


def initials_forms(person: PersonName) -> list[str]:
    """Initials-only signatures: ``J.D.``, ``J. D.``, ``J.-B. K.``, ``F.D.V.``.

    Only dotted forms with at least two letters: they cannot be ordinary words.
    """
    if not person.is_full:
        return []
    surname = [s for s in person.surname if normalize(s) not in PARTICLES]
    if not surname:
        return []
    forms: set[str] = set()
    for hyphen in (True, False):
        given = "".join(_dotted(g, spaced_parts=hyphen) for g in person.given)
        family = "".join(_dotted(s, spaced_parts=hyphen) for s in surname)
        if len([c for c in given + family if c.isalpha()]) < 2:
            continue
        forms.add(given + family)
        forms.add(f"{given} {family}")
    return sorted(forms)


def text_forms(person: PersonName) -> list[str]:
    """Readable forms worth propagating: full name in both orders, initial forms."""
    forms: list[str] = []
    given = " ".join(person.given)
    surname = " ".join(person.surname)
    if person.is_full:
        forms += [f"{given} {surname}", f"{surname} {given}"]
        first = person.given[0]
        initial = initials_of(first)[:1].upper()
        if initial:
            forms += [f"{initial}. {surname}", f"{surname} {initial}."]
        # Every given name as initials: ``J.-B. Kowalczyk``, ``J.B. Kowalczyk``.
        for hyphen in (True, False):
            dotted = "".join(_dotted(g, spaced_parts=hyphen) for g in person.given)
            if len([c for c in dotted if c.isalpha()]) >= 2:
                forms += [f"{dotted} {surname}", f"{surname} {dotted}"]
        forms += initials_forms(person)
    return list(dict.fromkeys(forms))


def needs_capital(word: str) -> bool:
    """Single words that are also common words only match when capitalized."""
    return " " not in word.strip() and is_common_word(word)


# -- functional names --------------------------------------------------------------

#: Words naming a function, a team, a mailbox or a channel rather than a person
#: (normalized): « Service Client Entreprises », « Protection des données »,
#: ``facturation@``, ``offres@``.
ROLE_WORDS = frozenset(
    {
        "accueil", "achat", "achats", "admin", "administratif", "administration", "agence",
        "alerte", "alertes", "alerts", "assistance", "billing", "boutique", "bureau",
        "cabinet", "care", "careers", "centre", "clientele", "client", "clients", "commande",
        "commandes", "commercial", "commerciale", "commerciaux", "communication", "compta",
        "comptabilite", "conformite", "conseil", "contact", "contacts", "contentieux",
        "contrats", "customer", "desk", "direction", "donnees", "dpo", "entreprise",
        "entreprises", "equipe", "expedition", "facturation", "factures", "gestion", "help",
        "helpdesk", "hotline", "info", "infos", "informatique", "juridique", "litiges",
        "livraison", "logistique", "mail", "maintenance", "marketing", "newsletter",
        "noreply", "notification", "notifications", "offres", "office", "operations", "paie",
        "partenaires", "particuliers", "planning", "pole", "postmaster", "presse",
        "professionnels", "projet", "projets", "promotions", "protection", "qualite",
        "reception", "reclamation", "reclamations", "recouvrement", "recrutement",
        "relation", "relations", "rgpd", "rh", "sales", "sav", "secretariat", "securite",
        "service", "services", "siege", "social", "standard", "support", "systeme",
        "team", "technique", "ventes", "webmaster",
    }
)  # fmt: skip
#: Capitals-only tokens read as acronyms inside a functional name (« Projets IRVE »).
_ACRONYM_MAX = 5
_VERSION = re.compile(r"^v?\d+(?:[.,]\d+)*$", re.IGNORECASE)


def is_role_word(word: str) -> bool:
    return normalize(word.strip(".,;:()")) in ROLE_WORDS


def is_functional_name(value: str, local_part: str = "") -> bool:
    """A display name or field value that names a team, a mailbox or a piece of
    software, not a person: every word is an ordinary word, a particle, an acronym or
    a version number, at least one names a function (:data:`ROLE_WORDS`) and none
    is a given name (« Service Client Entreprises », « Protection des données »,
    « Projets IRVE », « Hexagone Facturation 4.2 »; never « Pierre Boulanger »).

    ``local_part`` (the address next to a display name) vetoes it when it reads as
    a person's login (``jean.boulanger``)."""
    words = [w.strip(".,;:()\"'«»") for w in value.split()]
    words = [w for w in words if w and w not in ("&", "-", "–", "|", "/")]
    if not words:
        return False
    role = version = False
    for word in words:
        key = normalize(word)
        if _VERSION.match(word):
            version = True
            continue
        if not any(c.isalpha() for c in word):
            return False
        if given_like(word) or civility_of(word) is not None:
            return False
        if key in ROLE_WORDS:
            role = True
        elif key in PARTICLES or key in ("et", "a", "aux", "au", "pour", "en", "sur"):
            continue
        elif word.isupper() and len(word) <= _ACRONYM_MAX:
            continue  # « IRVE », « DSI »: an acronym, given a role word elsewhere
        elif not is_common_word(word):
            return False
    generic_box = bool(local_part) and all(
        is_role_word(p) for p in re.split(r"[._\-+]", local_part) if p
    )
    if not (role or (version and len(words) > 1) or generic_box):
        return False  # « Promo <offres@…> »: ordinary words over a function mailbox
    if local_part:
        parts = [p for p in re.split(r"[._\-+]", local_part) if p]
        if any(given_like(p) and not is_initial(p) and not is_role_word(p) for p in parts):
            return False
    return True


__all__ = [
    "CIVILITIES",
    "LOGIN_TEMPLATES",
    "PARTICLES",
    "ROLE_WORDS",
    "PersonName",
    "civility_of",
    "given_like",
    "initials_forms",
    "initials_of",
    "is_functional_name",
    "is_initial",
    "is_role_word",
    "login_forms",
    "login_of",
    "login_parts",
    "match_login_template",
    "needs_capital",
    "parse_person",
    "text_forms",
    "tokens_of",
]
