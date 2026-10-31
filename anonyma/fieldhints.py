"""Field hints: what a table column or a JSON key says about its values.

A column headed ``Nom`` or a JSON key ``login`` tells what the values are even when
the values alone are ambiguous (``LEBRUN``, ``jbkowal88``). Extractors append the
hint to the segment label, e.g. ``C4 (colonne nom)`` or ``$.*[0].* (champ
identifiant)``; detectors read it back with :func:`hint_of`.

Labels must never hold values: a hint is always one word of the closed vocabulary
below (the canonical names of :data:`HINTS`), never the raw header or key.
"""

from __future__ import annotations

import re

from anonyma.textutils import normalize

#: Canonical hint -> normalized header / key spellings.
HINTS: dict[str, tuple[str, ...]] = {
    "nom": (
        "nom", "noms", "prenom", "prenoms", "nom de famille", "nom complet", "nom prenom",
        "prenom nom", "nom et prenom", "nom et prenoms", "nom usuel", "nom d usage",
        "nom de naissance", "nom de jeune fille", "patronyme", "name", "first name",
        "firstname", "given name", "last name", "lastname", "surname", "family name",
        "full name", "fullname", "display name", "titulaire", "beneficiaire", "salarie",
        "salariee", "employe", "employee", "collaborateur", "collaboratrice", "agent",
        "interlocuteur", "personne", "personne a contacter", "contact name", "patient",
        "assure", "adherent", "signataire", "redacteur", "auteur", "author", "evaluateur",
        "responsable", "referent", "gestionnaire", "conseiller", "destinataire", "expediteur",
    ),
    "identifiant": (
        "login", "identifiant", "identifiant de connexion", "username", "user name", "user",
        "utilisateur", "nom d utilisateur", "user id", "userid", "uid", "pseudo", "pseudonyme",
        "matricule", "handle", "account", "compte utilisateur", "samaccountname",
    ),
    "telephone": (
        "telephone", "tel", "tel fixe", "tel portable", "telephone portable", "portable",
        "mobile", "phone", "phone number", "gsm", "fax", "numero de telephone", "cellulaire",
    ),
    "naissance": (
        "naissance", "date de naissance", "date naissance", "ddn", "ne le", "nee le",
        "birth", "birthdate", "birthday", "date of birth", "dob", "birth date",
    ),
    "courriel": ("courriel", "email", "e mail", "mail", "adresse mail", "adresse email"),
    "adresse": (
        "adresse", "address", "adresse postale", "rue", "voie", "complement",
        "complement d adresse", "adresse 1", "adresse 2", "adresse ligne 1",
        "adresse ligne 2", "street", "domicile",
    ),
    "ville": ("ville", "commune", "city", "localite", "lieu de naissance", "town"),
    "iban": ("iban", "rib", "compte bancaire", "bank account"),
    "nir": ("nir", "numero de securite sociale", "securite sociale", "secu", "nss", "insee"),
    "fonction": ("fonction", "poste", "metier", "profession", "emploi", "job title"),
    "immatriculation": ("immatriculation", "plaque", "plaque d immatriculation", "vehicule"),
}  # fmt: skip

_LOOKUP: dict[str, str] = {
    spelling: hint for hint, spellings in HINTS.items() for spelling in spellings
}
_SEPARATORS = re.compile(r"[\s_.\-'’/:*#()]+")
_LABEL_HINT = re.compile(r"\((?:colonne|champ) ([a-z]+)\)\s*$")


def field_hint(name: str) -> str | None:
    """Canonical hint of a column header / key, or None (``"Prénom"`` -> ``"nom"``)."""
    key = _SEPARATORS.sub(" ", normalize(name)).strip()
    if not key or len(key) > 40:
        return None
    return _LOOKUP.get(key)


def with_column_hint(label: str, hint: str | None) -> str:
    return f"{label} (colonne {hint})" if hint else label


def with_key_hint(label: str, hint: str | None) -> str:
    return f"{label} (champ {hint})" if hint else label


def hint_of(label: str) -> str | None:
    """Hint carried by a segment label (normalized or not), or None."""
    match = _LABEL_HINT.search(normalize(label))
    return match.group(1) if match and match.group(1) in HINTS else None


_DATA_LIKE = re.compile(r"@|\d{4}")
#: Header cells such as ``id`` or ``#`` that carry no hint but are not data either.
_NEUTRAL = frozenset({"id", "#", "n", "no", "num", "numero", "ref", "code"})


def header_hints(rows: dict[int, dict[int, str]]) -> dict[int, str]:
    """Column index -> hint, from the header row of a table.

    ``rows`` maps row index -> column index -> text. The header row is the first
    non-empty row; it is trusted when no cell looks like data (an e-mail address, a
    run of four digits), at least two cells are known field names and they make at
    least half of the row. A vertical key/value layout (``Nom | Jean Dupont``) is
    therefore not taken for a header.
    """
    for row in sorted(rows):
        cells = {c: t for c, t in rows[row].items() if t.strip()}
        if not cells:
            continue
        if any(_DATA_LIKE.search(t) for t in cells.values()):
            return {}
        hints = {c: h for c, t in cells.items() if (h := field_hint(t)) is not None}
        neutral = sum(1 for t in cells.values() if normalize(t) in _NEUTRAL)
        if len(hints) >= 2 and 2 * len(hints) >= len(cells) - neutral:
            return hints
        return {}
    return {}


__all__ = [
    "HINTS",
    "field_hint",
    "header_hints",
    "hint_of",
    "with_column_hint",
    "with_key_hint",
]
