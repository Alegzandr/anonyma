"""Mapping stores for the pseudonymization regime (``models.MappingStore``).

Phase 0: in-memory implementation plus the API of the encrypted SQLite store,
owned by the infrastructure workstream.
"""

from __future__ import annotations

from pathlib import Path


class StoreError(Exception):
    """``str(exc)`` is a French user-facing message."""


class WrongPassphraseError(StoreError):
    pass


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


class EncryptedMappingStore:
    """SQLite file; originals encrypted with AES-GCM under an Argon2id-derived key,
    lookups through keyed hashes. Implements ``models.MappingStore``."""

    @classmethod
    def open(cls, path: Path, passphrase: str, *, create: bool = True) -> EncryptedMappingStore:
        """Open (or create) a store. Raises WrongPassphraseError / StoreError."""
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    def lookup(self, entity_type: str, key: str) -> str | None:
        raise NotImplementedError

    def save(self, entity_type: str, key: str, original: str, substitute: str) -> None:
        raise NotImplementedError

    def reverse(self, substitute: str) -> str | None:
        raise NotImplementedError

    def used_substitutes(self) -> set[str]:
        raise NotImplementedError
