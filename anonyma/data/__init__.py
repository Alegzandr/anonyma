"""Packaged linguistic data used by detection and substitution.

* ``prenoms.tsv.gz``: French given names with gender, from the INSEE « Fichier des
  prénoms » (Licence Ouverte / Etalab 2.0).
* ``lexique.txt.gz``: common French and English words (normalized), selected by
  frequency with wordfreq (data CC BY-SA 4.0) and validated against the Hunspell
  dictionaries (Grammalecte, MPL-2.0; SCOWL). Rebuilt with
  ``packaging/build_detection_data.py``.
* :mod:`anonyma.data.communes`: real French communes with one of their postcodes,
  hand-curated, for realistic substitute addresses.
"""

from __future__ import annotations

import gzip
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources

from anonyma.textutils import normalize


@dataclass(frozen=True, slots=True)
class FirstName:
    display: str
    gender: str  # "m", "f" or "x" (mixed)
    count: int


def _read_gz(name: str) -> str:
    raw = resources.files(__name__).joinpath(name).read_bytes()
    return gzip.decompress(raw).decode("utf-8")


@lru_cache(maxsize=1)
def first_names() -> dict[str, FirstName]:
    """Normalized given name -> :class:`FirstName`."""
    table: dict[str, FirstName] = {}
    for line in _read_gz("prenoms.tsv.gz").splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        key = normalize(parts[0])
        if key and key not in table:
            table[key] = FirstName(parts[0], parts[1], int(parts[2]))
    return table


@lru_cache(maxsize=1)
def lexicon() -> frozenset[str]:
    """Normalized common words (French and English)."""
    return frozenset(w for w in _read_gz("lexique.txt.gz").splitlines() if w)


def first_name(token: str) -> FirstName | None:
    """The given-name record of ``token`` (compound names checked as a whole)."""
    return first_names().get(normalize(token))


def is_common_word(token: str) -> bool:
    return normalize(token) in lexicon()


def gender_of(given: str) -> str | None:
    """``"m"``/``"f"`` for a given name (first part of a compound), else None."""
    record = first_name(given)
    if record is None and "-" in given:
        record = first_name(given.split("-", maxsplit=1)[0])
    if record is None or record.gender == "x":
        return None
    return record.gender


__all__ = ["FirstName", "first_name", "first_names", "gender_of", "is_common_word", "lexicon"]
