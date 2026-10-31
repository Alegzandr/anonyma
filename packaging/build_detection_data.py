"""Build the compact data files shipped in ``anonyma/data/`` (run once, offline-able).

NOT part of the application and NOT a project dependency. Run it in a separate
throw-away environment::

    uv venv lexenv && VIRTUAL_ENV=lexenv uv pip install wordfreq spylls
    lexenv/Scripts/python packaging/build_detection_data.py \
        --insee nat2022.csv --dicts path/to/hunspell anonyma/data

Inputs:

* INSEE « Fichier des prénoms » (national file, ``nat20XX.csv``), Licence Ouverte /
  Etalab 2.0: https://www.insee.fr/fr/statistiques/7633685
* Hunspell dictionaries from the LibreOffice ``dictionaries`` repository:
  ``fr.dic``/``fr.aff`` (Grammalecte, MPL-2.0) and ``en_US.dic``/``en_US.aff``
  (SCOWL, permissive).
* ``wordfreq`` frequency lists (code Apache-2.0, data CC BY-SA 4.0) only *select*
  the most frequent words; every kept word must also be accepted, in lower case,
  by the Hunspell dictionary of its language (which rejects proper nouns written in
  lower case: "paris", "dupont"...).

Outputs (UTF-8, gzip):

* ``prenoms.tsv.gz``: ``prénom<TAB>genre<TAB>effectif`` with genre ``m``/``f``/``x``
  (x = mixed, under 80 % for one sex).
* ``lexique.txt.gz``: one normalized (accent-free, casefolded) common word per line.
"""

from __future__ import annotations

import argparse
import collections
import csv
import gzip
import re
import unicodedata
from pathlib import Path

MIN_FIRST_NAME_COUNT = 60
FR_WORDS = 60_000
EN_WORDS = 40_000
WORD = re.compile(r"^[a-zàâäéèêëîïôöùûüÿçœæ]+(?:['’-][a-zàâäéèêëîïôöùûüÿçœæ]+)*$")


def normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold().strip()


def build_first_names(insee: Path) -> dict[str, tuple[str, int]]:
    totals: collections.Counter[str] = collections.Counter()
    by_sex: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)
    with insee.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh, delimiter=";"):
            name = row["preusuel"].strip()
            if name.startswith("_") or len(name) < 2:
                continue
            count = int(row["nombre"])
            totals[name] += count
            by_sex[name][row["sexe"]] += count
    names: dict[str, tuple[str, int]] = {}
    for name, total in totals.items():
        if total < MIN_FIRST_NAME_COUNT:
            continue
        male = by_sex[name]["1"] / total
        gender = "m" if male >= 0.8 else "f" if male <= 0.2 else "x"
        display = "-".join(part.capitalize() for part in name.lower().split("-"))
        names[display] = (gender, total)
    return names


def build_lexicon(dicts: Path) -> list[str]:
    from spylls.hunspell import Dictionary
    from wordfreq import top_n_list

    keep: set[str] = set()
    for lang, dic, size in (("fr", "fr", FR_WORDS), ("en", "en_US", EN_WORDS)):
        dictionary = Dictionary.from_files(str(dicts / dic))
        for raw in top_n_list(lang, size):
            word = raw.replace("’", "'")
            if len(word) < 2 or not WORD.match(word):
                continue
            if not dictionary.lookup(word):
                continue
            keep.add(normalize(word))
    return sorted(keep)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--insee", type=Path, required=True)
    parser.add_argument("--dicts", type=Path, required=True)
    parser.add_argument("out", type=Path)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    names = build_first_names(args.insee)
    lines = [f"{n}\t{g}\t{c}" for n, (g, c) in sorted(names.items(), key=lambda kv: -kv[1][1])]
    with gzip.open(args.out / "prenoms.tsv.gz", "wt", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")

    words = build_lexicon(args.dicts)
    with gzip.open(args.out / "lexique.txt.gz", "wt", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(words) + "\n")
    print(f"{len(names)} first names, {len(words)} lexicon words")


if __name__ == "__main__":
    main()
