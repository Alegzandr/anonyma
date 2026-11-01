"""Atomic identifiers: no entity may start or end inside a GUID, a hash or a token.

A GUID (``34be8221-5b2a-4e8a-b93c-e8b15d765216``), a long hexadecimal run (a
conversation index, a hash) or a base64 token is one value. A detector reading a
fragment of it as a name, a phone number or initials (``a-b``, ``127924553``) would
have the replacer rewrite a few characters inside it and break the identifier
(``…-4e8SS93c-…``), whatever ``Config.mask_opaque_ids`` says. Spans are therefore
checked centrally, for every detector, propagation and the verifier alike:

* a span covering whole tokens (the token itself as ``IDENTIFIANT_TECHNIQUE``, an
  e-mail address or a URL around it) is kept;
* a span lying inside a token is dropped;
* a span overlapping a token and text outside it is trimmed to the outside part
  (recall: ``Jean Dupont`` before an identifier stays masked);
* a span made of tokens only is an identifier or nothing: kept as a technical
  identifier, an address, a URL or a message id, never as a person, a street
  address or a user name (a model reading a GUID as « 116 avenue de Germain »).

Tokens are the opaque identifiers of :func:`anonyma.detectors.opaque.opaque_spans`
(UUIDs, hexadecimal runs, fingerprints, base64 and generated tokens).
"""

from __future__ import annotations

from collections.abc import Sequence

from anonyma.detectors.opaque import opaque_spans
from anonyma.models import EntityType

#: Texts shorter than this hold no token (the shortest rule needs 10 characters).
_MIN_TEXT = 10


def atomic_spans(text: str) -> list[tuple[int, int]]:
    """Sorted, non-overlapping ``(start, end)`` of the atomic tokens of ``text``."""
    if len(text) < _MIN_TEXT:
        return []
    return sorted((s, e) for s, e, _ in opaque_spans(text))


def trim_span(start: int, end: int, tokens: Sequence[tuple[int, int]]) -> tuple[int, int] | None:
    """``[start, end)`` cut so that it starts and ends outside every token (or on its
    edges); None when nothing of it lies outside the tokens it cuts."""
    for s, e in tokens:
        if e <= start or s >= end or (start <= s and e <= end):
            continue  # disjoint, or the whole token is covered
        if s <= start and end <= e:
            return None  # inside the token
        if s < start < e:
            start = e  # starts inside: keep what follows the token
        if s < end < e:
            end = s  # ends inside: keep what precedes it
        if start >= end:
            return None
    return start, end


def trimmed(
    text: str, start: int, end: int, tokens: Sequence[tuple[int, int]]
) -> tuple[int, int] | None:
    """Like :func:`trim_span`, then without the separators left at the edges; None
    when no letter or digit remains."""
    span = trim_span(start, end, tokens)
    if span is None:
        return None
    lo, hi = span
    if (lo, hi) != (start, end):
        while lo < hi and not text[lo].isalnum():
            lo += 1
        while hi > lo and not text[hi - 1].isalnum():
            hi -= 1
    if lo >= hi or not any(c.isalnum() for c in text[lo:hi]):
        return None
    return lo, hi


#: Types a span made of atomic tokens only may have (and the custom ones).
TOKEN_TYPES = frozenset(
    {
        EntityType.OPAQUE_ID,
        EntityType.URL,
        EntityType.EMAIL,
        EntityType.MESSAGE_ID,
        EntityType.BARCODE,
        EntityType.THREAD_INDEX,
    }
)
_BUILTIN_TYPES = frozenset(t.value for t in EntityType)


def only_tokens(text: str, start: int, end: int, tokens: Sequence[tuple[int, int]]) -> bool:
    """Every letter and digit of ``text[start:end]`` lies in a token."""
    if not tokens:
        return False
    for i in range(start, end):
        if text[i].isalnum() and not any(s <= i < e for s, e in tokens):
            return False
    return True


class AtomicIndex:
    """Atomic tokens of segment texts, computed once per text."""

    def __init__(self) -> None:
        self._cache: dict[str, list[tuple[int, int]]] = {}

    def tokens(self, text: str) -> list[tuple[int, int]]:
        found = self._cache.get(text)
        if found is None:
            found = atomic_spans(text)
            self._cache[text] = found
        return found

    def fit(self, text: str, start: int, end: int) -> tuple[int, int] | None:
        """The span to keep for ``text[start:end]`` (see :func:`trimmed`)."""
        tokens = self.tokens(text)
        if not tokens:
            return start, end
        return trimmed(text, start, end, tokens)

    def fit_typed(
        self, text: str, start: int, end: int, entity_type: str
    ) -> tuple[int, int] | None:
        """:meth:`fit`, and None for a span of tokens only typed as anything but an
        identifier (:data:`TOKEN_TYPES`, custom types)."""
        span = self.fit(text, start, end)
        if span is None or entity_type in TOKEN_TYPES or entity_type not in _BUILTIN_TYPES:
            return span
        if only_tokens(text, span[0], span[1], self.tokens(text)):
            return None
        return span


__all__ = ["TOKEN_TYPES", "AtomicIndex", "atomic_spans", "only_tokens", "trim_span", "trimmed"]
