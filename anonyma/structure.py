"""Structural names: JSON keys, CSV headers and XML names that are schema, not data.

A Dataverse export is keyed by field names such as ``createdon``, ``_ownerid_value``,
``@odata.etag`` or ``directioncode@OData.Community.Display.V1.FormattedValue``.
Exposed to detection, such a key reads as an e-mail address or a name; rewritten,
it breaks the schema the export is re-imported with, and the "original value" then
turns up in every other file of the batch (``X-D365-DirectionCode``) as a residue.

A name is **structural** when it is an ASCII identifier (letters, digits, ``_``,
``.``, ``@``, ``$``, ``:``, ``-``; no space; not starting with a digit) that does not
look like personal data: no e-mail address with a plain lower-case domain, no run of
five digits or more (phone, account), no GUID or token, no word that is a given name
and not a common word. Structural names are not exposed to detection and never
rewritten; any other name (``Zéphyrin Quintavalle``, ``jean.dupont@exemple.fr``,
``Nom du client``) stays exposed (recall over precision).

Extractors record the structural names they saw in ``Document.properties`` under
:data:`PROPERTY` (lower case, whole names and their words): the raw backstop of the
verification never searches the batch's own structure for original values.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from functools import lru_cache

from anonyma.atomic import atomic_spans
from anonyma.data import first_name, is_common_word

#: Document property: frozenset of lower-case structural names and their words.
PROPERTY = "structure"
MAX_NAME_LENGTH = 128

_IDENTIFIER = re.compile(r"[A-Za-z_@$][A-Za-z0-9_.@$:\-]*")
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@(?:[a-z0-9\-]+\.)+[a-z]{2,6}")
_DIGIT_RUN = re.compile(r"\d{5,}")
_WORD = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+")
_SEPARATORS = re.compile(r"[_.@$:\-]+")
#: OData / Dataverse annotations: ``name@OData.Community...``, ``@odata.etag``,
#: ``_x_value@Microsoft.Dynamics.CRM.lookuplogicalname``.
_ANNOTATION = re.compile(r"(?:^|@)(?:OData|odata|Microsoft)\.", re.ASCII)


def words(name: str) -> list[str]:
    """Words of an identifier (``_ownerid_value`` -> ``ownerid``, ``value``;
    ``FormattedValue`` -> ``Formatted``, ``Value``)."""
    out: list[str] = []
    for part in _SEPARATORS.split(name):
        out.extend(_WORD.findall(part))
    return out


@lru_cache(maxsize=65536)
def is_structural(name: str) -> bool:
    """Whether a key / header / element name is schema rather than data."""
    if not name or len(name) > MAX_NAME_LENGTH or not name.isascii():
        return False
    if not _IDENTIFIER.fullmatch(name):
        return False
    if _ANNOTATION.search(name):
        return True
    if _EMAIL.fullmatch(name) or _DIGIT_RUN.search(name) or atomic_spans(name):
        return False
    return not any(
        len(w) >= 3 and first_name(w) is not None and not is_common_word(w) for w in words(name)
    )


def is_structural_token(value: str) -> bool:
    """A value that can only be structure (an OData annotation, ``_x_value``,
    ``@odata.etag``, ``$metadata``): never searched as an original value."""
    value = value.strip()
    if not value or " " in value or not _IDENTIFIER.fullmatch(value):
        return False
    return bool(_ANNOTATION.search(value)) or value[0] in "_@$"


def vocabulary(names: Iterable[str]) -> frozenset[str]:
    """Lower-case structural names and their words of at least three letters."""
    out: set[str] = set()
    for name in names:
        out.add(name.lower())
        out.update(w.lower() for w in words(name) if len(w) >= 3)
        out.update(p.lower() for p in _SEPARATORS.split(name) if len(p) >= 3)
    return frozenset(out)


__all__ = ["PROPERTY", "is_structural", "is_structural_token", "vocabulary", "words"]
