"""Shared definitions of the benchmark: paths, entity types and text normalisation.

The ground truth stores, for every sensitive value, a list of *renderings*: the
strings whose survival anywhere in the output tree means the value leaked. Each
rendering has a matching mode:

``text``
    accent- and case-insensitive, every non alphanumeric run is a separator, the
    rendering must match whole words (``"14/02/1979"`` also matches
    ``"14.02.1979"`` and ``"14 02 1979"``).
``word``
    same as ``text`` but case-sensitive on the capitalised / upper-case forms only:
    used for surnames that are also ordinary words (``Petit``, ``Boucher``...),
    whose lower-case spelling in a sentence is not a rendering of the person.
``raw``
    accent- and case-insensitive substring with alphanumeric boundaries
    (e-mail addresses, URLs).
``compact``
    every non alphanumeric character removed (``FR76 3000-6000`` == ``fr763000
    6000``); all-digit values must not be glued to other digits, except for a short
    prefix (trunk / country code) on the left.
``ip``
    exact IPv4/IPv6 address with numeric boundaries.
``gps``
    the image still carries GPS coordinates in its EXIF block.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CORPUS = ROOT / "corpus"
TRUTH = ROOT / "ground_truth"
RESULTS = ROOT / "results"

#: Sending / transport dates of mails: ground truth for precision only (masked by the
#: date policy, not a detection target of the recall rates).
DATE_SENT = "DATE_ENVOI"

DIRECT_TYPES = (
    "PERSONNE",
    "EMAIL",
    "TELEPHONE",
    "IBAN",
    "CARTE",
    "NIR",
    "ADRESSE",
    "PLAQUE",
    "IP",
    "DATE_NAISSANCE",
    "IDENTIFIANT",
)
INDIRECT_TYPES = (
    "ORGANISATION",
    "LIEU",
    "FONCTION",
    "SIREN",
    "SIRET",
    "TVA",
    "URL",
    "NUMERO_DOSSIER",
    "GPS",
)

_SPACES = re.compile(r"[^0-9a-zA-Z]+")
_QUOTES = str.maketrans({"’": "'", "‘": "'", "ʼ": "'", "«": '"', "»": '"'})
_LIGATURES = str.maketrans({"œ": "oe", "Œ": "OE", "æ": "ae", "Æ": "AE", "ß": "ss"})


def strip_accents(text: str) -> str:
    text = text.translate(_LIGATURES)
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def norm_word(text: str) -> str:
    """Accent-free, separators collapsed to one space, padded; case kept."""
    return " " + _SPACES.sub(" ", strip_accents(text.translate(_QUOTES))).strip() + " "


def norm_text(text: str) -> str:
    return norm_word(text).lower()


def norm_raw(text: str) -> str:
    return strip_accents(text.translate(_QUOTES)).lower()


def norm_compact(text: str) -> str:
    return _SPACES.sub("", strip_accents(text)).lower()


_NUMBER_SEPARATORS = re.compile(r"[ \t.\-()_\u00a0\u202f]+")


def norm_light(text: str) -> str:
    """Only the separators used inside numbers removed (keeps ``,`` ``;`` ``/`` and newlines)."""
    return _NUMBER_SEPARATORS.sub("", strip_accents(text)).lower()


def word_forms(rendering: str) -> list[str]:
    """Capitalised and upper-case forms of a ``word`` rendering (normalised)."""
    base = norm_word(rendering).strip()
    capitalised = " ".join(w[:1].upper() + w[1:] for w in base.split(" "))
    return [f" {capitalised} ", f" {base.upper()} "]


def tokens(text: str) -> list[str]:
    """Normalised word tokens, as used for the precision diff."""
    return norm_text(text).split()
