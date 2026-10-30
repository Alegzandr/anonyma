"""Mapping store for the pseudonymization regime.

Phase 0: in-memory implementation of ``models.MappingStore``. The infrastructure
workstream adds the encrypted SQLite store (Argon2id + AES-GCM).
"""

from __future__ import annotations


class InMemoryMappingStore:
    def __init__(self) -> None:
        self._data: dict[tuple[str, str], tuple[str, str]] = {}

    def lookup(self, entity_type: str, key: str) -> str | None:
        entry = self._data.get((entity_type, key))
        return entry[1] if entry else None

    def save(self, entity_type: str, key: str, original: str, substitute: str) -> None:
        self._data[(entity_type, key)] = (original, substitute)

    def reverse(self, substitute: str) -> str | None:
        for original, sub in self._data.values():
            if sub == substitute:
                return original
        return None

    def used_substitutes(self) -> set[str]:
        return {sub for _, sub in self._data.values()}
