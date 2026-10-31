"""Check-digit algorithms for French and international identifiers.

Used by the recognizers (validation) and by the realistic replacer (generation of
well-formed fake values). Functions take raw strings and ignore separators.
"""

from __future__ import annotations

import re

_NON_ALNUM = re.compile(r"[^0-9A-Za-z]")

#: IBAN length per country (ISO 13616 registry, SEPA and common others).
IBAN_LENGTHS: dict[str, int] = {
    "AD": 24, "AE": 23, "AL": 28, "AT": 20, "AZ": 28, "BA": 20, "BE": 16, "BG": 22,
    "BH": 22, "BR": 29, "BY": 28, "CH": 21, "CR": 22, "CY": 28, "CZ": 24, "DE": 22,
    "DK": 18, "DO": 28, "EE": 20, "EG": 29, "ES": 24, "FI": 18, "FO": 18, "FR": 27,
    "GB": 22, "GE": 22, "GI": 23, "GL": 18, "GR": 27, "GT": 28, "HR": 21, "HU": 28,
    "IE": 22, "IL": 23, "IQ": 23, "IS": 26, "IT": 27, "JO": 30, "KW": 30, "KZ": 20,
    "LB": 28, "LC": 32, "LI": 21, "LT": 20, "LU": 20, "LV": 21, "MC": 27, "MD": 24,
    "ME": 22, "MK": 19, "MR": 27, "MT": 31, "MU": 30, "NL": 18, "NO": 15, "PK": 24,
    "PL": 28, "PS": 29, "PT": 25, "QA": 29, "RO": 24, "RS": 22, "SA": 24, "SC": 31,
    "SE": 24, "SI": 19, "SK": 24, "SM": 27, "ST": 25, "SV": 28, "TL": 23, "TN": 24,
    "TR": 26, "UA": 29, "VA": 22, "VG": 24, "XK": 20,
}  # fmt: skip


def compact(value: str) -> str:
    """Upper-case alphanumeric characters only."""
    return _NON_ALNUM.sub("", value).upper()


def digits(value: str) -> str:
    return "".join(c for c in value if c.isdigit())


# -- Luhn ---------------------------------------------------------------------


def luhn_sum(number: str) -> int:
    total = 0
    for i, ch in enumerate(reversed(number)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total


def luhn_valid(value: str) -> bool:
    number = digits(value)
    return len(number) >= 2 and luhn_sum(number) % 10 == 0


def luhn_check_digit(partial: str) -> str:
    """Digit to append to ``partial`` so that the result is Luhn-valid."""
    total = luhn_sum(partial + "0")
    return str((10 - total % 10) % 10)


# -- IBAN / RIB ---------------------------------------------------------------


def _alpha_to_digits(value: str) -> str:
    return "".join(str(int(c, 36)) for c in value)


def iban_valid(value: str) -> bool:
    iban = compact(value)
    if len(iban) < 15 or not iban[:2].isalpha() or not iban[2:4].isdigit():
        return False
    expected = IBAN_LENGTHS.get(iban[:2])
    if expected is not None and len(iban) != expected:
        return False
    if len(iban) > 34:
        return False
    return int(_alpha_to_digits(iban[4:] + iban[:4])) % 97 == 1


def iban_check_digits(country: str, bban: str) -> str:
    number = int(_alpha_to_digits(compact(bban) + country.upper() + "00"))
    return f"{98 - number % 97:02d}"


def _rib_letters(value: str) -> str:
    table = {c: str((i % 9) + 1) for i, c in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ")}
    # RIB letter conversion: A,J=1 B,K,S=2 C,L,T=3 D,M,U=4 E,N,V=5 F,O,W=6 G,P,X=7 H,Q,Y=8
    # I,R,Z=9
    table.update({"S": "2", "T": "3", "U": "4", "V": "5", "W": "6", "X": "7", "Y": "8", "Z": "9"})
    return "".join(table.get(c, c) for c in value.upper())


def rib_key(bank: str, branch: str, account: str) -> str:
    b, g, c = int(_rib_letters(bank)), int(_rib_letters(branch)), int(_rib_letters(account))
    return f"{97 - ((89 * b + 15 * g + 3 * c) % 97):02d}"


# -- French identifiers --------------------------------------------------------


def nir_number(value: str) -> int | None:
    """Numeric value of the first 13 characters of a NIR (Corsica handled)."""
    nir = compact(value)[:13]
    if len(nir) != 13:
        return None
    if nir[5:7] == "2A":
        nir = nir[:5] + "19" + nir[7:]
    elif nir[5:7] == "2B":
        nir = nir[:5] + "18" + nir[7:]
    return int(nir) if nir.isdigit() else None


def nir_key(value: str) -> str | None:
    number = nir_number(value)
    return None if number is None else f"{97 - number % 97:02d}"


def nir_valid(value: str) -> bool:
    nir = compact(value)
    if len(nir) != 15 or not nir[13:].isdigit():
        return False
    return nir_key(nir) == nir[13:]


LA_POSTE_SIREN = "356000000"


def siren_valid(value: str) -> bool:
    number = digits(value)
    return len(number) == 9 and (luhn_valid(number) or number == LA_POSTE_SIREN)


def siret_valid(value: str) -> bool:
    number = digits(value)
    if len(number) != 14:
        return False
    if number.startswith(LA_POSTE_SIREN):
        return sum(int(c) for c in number) % 5 == 0
    return luhn_valid(number)


def vat_fr_key(siren: str) -> str:
    return f"{(12 + 3 * (int(siren) % 97)) % 97:02d}"


def vat_fr_valid(value: str) -> bool:
    vat = compact(value)
    if len(vat) != 13 or not vat.startswith("FR") or not vat[4:].isdigit():
        return False
    key, siren = vat[2:4], vat[4:]
    if key.isdigit():
        return vat_fr_key(siren) == key
    return True  # new-style alphanumeric keys cannot be checked offline


__all__ = [
    "IBAN_LENGTHS",
    "compact",
    "digits",
    "iban_check_digits",
    "iban_valid",
    "luhn_check_digit",
    "luhn_valid",
    "nir_key",
    "nir_valid",
    "rib_key",
    "siren_valid",
    "siret_valid",
    "vat_fr_key",
    "vat_fr_valid",
]
