"""Mapping stores for the pseudonymization regime (``models.MappingStore``).

``EncryptedMappingStore`` is a single SQLite file in which nothing sensitive is
stored in clear:

* a 32-byte master key is derived from the passphrase with Argon2id (random salt
  and cost parameters stored in the file), then split with HKDF-SHA256 into an
  encryption key and two independent index keys;
* entity type + original value and the substitute are each encrypted with
  AES-256-GCM (random 96-bit nonce, row index as associated data so rows cannot
  be swapped);
* lookups go through HMAC-SHA256 keyed indexes: ``HMAC(k_lookup, type || key)``
  for :meth:`lookup`/:meth:`save` and ``HMAC(k_substitute, substitute)`` for
  :meth:`reverse`;
* an HMAC check token detects a wrong passphrase (:class:`WrongPassphraseError`);
* rollback journal (no WAL), ``secure_delete`` on, temp store in memory, one
  commit per write so that a crash never loses a mapping already used in an
  output file.

Neutral output names (``Config.neutral_filenames``) are kept in a second table,
``filenames``: batch id, neutral path and original path encrypted together under the
same key, row addressed by ``HMAC(k_lookup, "filename" || batch || neutral path)``.
The table is created on first open, so older stores gain it transparently.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import os
import sqlite3
import threading
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.argon2 import Argon2id
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


class StoreError(Exception):
    """``str(exc)`` is a French user-facing message."""


class WrongPassphraseError(StoreError):
    pass


class InMemoryMappingStore:
    def __init__(self) -> None:
        self._data: dict[tuple[str, str], tuple[str, str]] = {}
        self._names: dict[tuple[str, str], str] = {}

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

    def items(self) -> Iterator[MappingEntry]:
        for (entity_type, _), (original, substitute) in self._data.items():
            yield MappingEntry(entity_type, original, substitute)

    def save_filename(self, batch_id: str, output_name: str, original: str) -> None:
        self._names[(batch_id, output_name)] = original

    def filenames(self, batch_id: str | None = None) -> list[FilenameEntry]:
        return sorted(
            FilenameEntry(batch, output, original)
            for (batch, output), original in self._names.items()
            if batch_id is None or batch == batch_id
        )

    def close(self) -> None:
        """Nothing to release (same interface as the encrypted store)."""


@dataclass(frozen=True, slots=True)
class MappingEntry:
    entity_type: str
    original: str
    substitute: str

    def __repr__(self) -> str:  # never show values
        return f"MappingEntry(entity_type={self.entity_type!r})"


@dataclass(frozen=True, slots=True, order=True)
class FilenameEntry:
    """A neutral output name and the original name it replaces."""

    batch_id: str
    output_name: str
    original: str

    def __repr__(self) -> str:  # never show the original name
        return f"FilenameEntry(batch_id={self.batch_id!r}, output_name={self.output_name!r})"


@dataclass(frozen=True, slots=True)
class KdfParams:
    """Argon2id cost. Defaults follow the RFC 9106 "second recommended option"
    with a larger memory cost (128 MiB): a fraction of a second on a laptop."""

    memory_cost: int = 128 * 1024  # KiB
    iterations: int = 3
    lanes: int = 4


#: Bounds of the KDF parameters read from a file (never trusted as such: a
#: tampered file must not make opening it exhaust memory or run for hours).
MAX_MEMORY_COST = 2 * 1024 * 1024  # KiB: 2 GiB
MAX_ITERATIONS = 64
MAX_LANES = 64
MIN_SALT = 8

SCHEMA_VERSION = 1
_MAGIC = b"anonyma-mapping-store"
_CHECK_LABEL = b"anonyma/check/v1"
_NONCE = 12
_SALT = 16
_READ_ERROR = "Lecture impossible dans la table de correspondance."
_WRITE_ERROR = "Écriture impossible dans la table de correspondance."


def _sane(params: KdfParams, salt: bytes) -> bool:
    """KDF parameters within :data:`MAX_MEMORY_COST`... and valid for Argon2id."""
    return (
        1 <= params.lanes <= MAX_LANES
        and 8 * params.lanes <= params.memory_cost <= MAX_MEMORY_COST
        and 1 <= params.iterations <= MAX_ITERATIONS
        and len(salt) >= MIN_SALT
    )


def _owner_only(path: Path, exists: bool) -> None:
    """POSIX: the file is created readable by its owner only (0600), and an existing
    one is narrowed to that when possible. SQLite gives its journal the same mode."""
    if os.name == "nt":
        return
    if exists:
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)
        return
    try:
        os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
        os.chmod(path, 0o600)  # whatever the umask
    except OSError as exc:
        raise StoreError("Impossible de créer la table de correspondance.") from exc


def _derive(passphrase: str, salt: bytes, params: KdfParams) -> tuple[bytes, bytes, bytes, bytes]:
    """(encryption key, lookup index key, substitute index key, check token)."""
    normalized = unicodedata.normalize("NFC", passphrase).encode("utf-8")
    master = Argon2id(
        salt=salt,
        length=32,
        iterations=params.iterations,
        lanes=params.lanes,
        memory_cost=params.memory_cost,
    ).derive(normalized)

    def expand(label: bytes) -> bytes:
        return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=label).derive(master)

    enc = expand(b"anonyma/enc/v1")
    idx = expand(b"anonyma/idx/v1")
    sub = expand(b"anonyma/sub/v1")
    check = hmac.new(expand(b"anonyma/chk/v1"), _CHECK_LABEL, hashlib.sha256).digest()
    return enc, idx, sub, check


class EncryptedMappingStore:
    """SQLite file; originals encrypted with AES-GCM under an Argon2id-derived key,
    lookups through keyed hashes. Implements ``models.MappingStore``.

    Thread-safe (one connection guarded by a lock). Use as a context manager or
    call :meth:`close`.
    """

    def __init__(
        self,
        connection: sqlite3.Connection,
        path: Path,
        enc_key: bytes,
        idx_key: bytes,
        sub_key: bytes,
    ) -> None:
        # Use :meth:`open`; the constructor is not part of the public API.
        self._db: sqlite3.Connection | None = connection
        self.path = path
        self._aead = AESGCM(enc_key)
        self._idx_key = idx_key
        self._sub_key = sub_key
        self._lock = threading.RLock()

    # -- opening -------------------------------------------------------------

    @classmethod
    def open(
        cls,
        path: Path,
        passphrase: str,
        *,
        create: bool = True,
        kdf: KdfParams | None = None,
    ) -> EncryptedMappingStore:
        """Open (or create) a store. Raises WrongPassphraseError / StoreError.

        ``kdf`` only applies when creating a new file; existing files keep the
        parameters they were created with.
        """
        if not passphrase:
            raise StoreError("La phrase secrète ne peut pas être vide.")
        path = Path(path)
        exists = path.is_file() and path.stat().st_size > 0
        if not exists and not create:
            raise StoreError("Table de correspondance introuvable.")
        if not exists:
            path.parent.mkdir(parents=True, exist_ok=True)
        _owner_only(path, exists)
        try:
            db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        except sqlite3.Error as exc:
            raise StoreError("Impossible d'ouvrir la table de correspondance.") from exc
        try:
            cls._configure(db)
            if exists:
                keys = cls._unlock(db, passphrase)
            else:
                keys = cls._initialize(db, passphrase, kdf or KdfParams())
            cls._ensure_filenames(db)
        except BaseException:
            db.close()
            raise
        return cls(db, path, *keys)

    @staticmethod
    def _configure(db: sqlite3.Connection) -> None:
        try:
            db.execute("PRAGMA journal_mode=DELETE")
            db.execute("PRAGMA secure_delete=ON")
            db.execute("PRAGMA temp_store=MEMORY")
            db.execute("PRAGMA synchronous=FULL")
        except sqlite3.DatabaseError as exc:
            raise StoreError("Ce fichier n'est pas une table de correspondance Anonyma.") from exc

    @staticmethod
    def _ensure_filenames(db: sqlite3.Connection) -> None:
        try:
            db.execute(
                "CREATE TABLE IF NOT EXISTS filenames (id BLOB PRIMARY KEY, entry BLOB NOT NULL)"
                " WITHOUT ROWID"
            )
        except sqlite3.Error as exc:
            raise StoreError("Impossible de préparer la table de correspondance.") from exc

    @staticmethod
    def _initialize(
        db: sqlite3.Connection, passphrase: str, params: KdfParams
    ) -> tuple[bytes, bytes, bytes]:
        salt = os.urandom(_SALT)
        enc, idx, sub, check = _derive(passphrase, salt, params)
        meta = {
            "magic": _MAGIC,
            "schema": str(SCHEMA_VERSION).encode(),
            "kdf": b"argon2id",
            "salt": salt,
            "memory_cost": str(params.memory_cost).encode(),
            "iterations": str(params.iterations).encode(),
            "lanes": str(params.lanes).encode(),
            "check": check,
        }
        try:
            db.execute("BEGIN")
            db.execute("CREATE TABLE meta (name TEXT PRIMARY KEY, value BLOB NOT NULL)")
            db.execute(
                "CREATE TABLE mapping ("
                " lookup BLOB PRIMARY KEY,"
                " sub_index BLOB NOT NULL,"
                " original BLOB NOT NULL,"
                " substitute BLOB NOT NULL"
                ") WITHOUT ROWID"
            )
            db.execute("CREATE INDEX mapping_sub ON mapping (sub_index)")
            db.executemany("INSERT INTO meta VALUES (?, ?)", meta.items())
            db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            db.execute("COMMIT")
        except sqlite3.Error as exc:
            with contextlib.suppress(sqlite3.Error):
                db.execute("ROLLBACK")
            raise StoreError("Impossible de créer la table de correspondance.") from exc
        return enc, idx, sub

    @staticmethod
    def _unlock(db: sqlite3.Connection, passphrase: str) -> tuple[bytes, bytes, bytes]:
        try:
            meta = {str(k): bytes(v) for k, v in db.execute("SELECT name, value FROM meta")}
        except sqlite3.Error as exc:
            raise StoreError("Ce fichier n'est pas une table de correspondance Anonyma.") from exc
        if meta.get("magic") != _MAGIC:
            raise StoreError("Ce fichier n'est pas une table de correspondance Anonyma.")
        try:
            schema = int(meta["schema"])
            params = KdfParams(
                memory_cost=int(meta["memory_cost"]),
                iterations=int(meta["iterations"]),
                lanes=int(meta["lanes"]),
            )
            salt, expected = meta["salt"], meta["check"]
        except (KeyError, ValueError) as exc:
            raise StoreError("Table de correspondance endommagée.") from exc
        if schema > SCHEMA_VERSION:
            raise StoreError(
                "Cette table de correspondance a été créée par une version plus récente d'Anonyma."
            )
        if meta.get("kdf") != b"argon2id" or not _sane(params, salt):
            raise StoreError("Table de correspondance endommagée.")
        enc, idx, sub, check = _derive(passphrase, salt, params)
        if not hmac.compare_digest(check, expected):
            raise WrongPassphraseError("Phrase secrète incorrecte.")
        return enc, idx, sub

    # -- lifecycle -----------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None

    @property
    def closed(self) -> bool:
        return self._db is None

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def _execute(self, sql: str, params: tuple[Any, ...] = (), *, write: bool = False) -> list[Any]:
        """Run one statement; every SQLite failure becomes a :class:`StoreError`."""
        with self._lock:
            if self._db is None:
                raise StoreError("La table de correspondance est fermée.")
            try:
                return self._db.execute(sql, params).fetchall()
            except sqlite3.Error as exc:
                raise StoreError(_WRITE_ERROR if write else _READ_ERROR) from exc

    # -- crypto helpers --------------------------------------------------------

    def _lookup_index(self, entity_type: str, key: str) -> bytes:
        data = entity_type.encode("utf-8") + b"\0" + key.encode("utf-8")
        return hmac.new(self._idx_key, data, hashlib.sha256).digest()

    def _sub_index(self, substitute: str) -> bytes:
        return hmac.new(self._sub_key, substitute.encode("utf-8"), hashlib.sha256).digest()

    def _seal(self, plaintext: str, aad: bytes) -> bytes:
        nonce = os.urandom(_NONCE)
        return nonce + self._aead.encrypt(nonce, plaintext.encode("utf-8"), aad)

    def _open(self, blob: bytes, aad: bytes) -> str:
        try:
            return self._aead.decrypt(blob[:_NONCE], blob[_NONCE:], aad).decode("utf-8")
        except (InvalidTag, ValueError) as exc:
            raise StoreError("Table de correspondance endommagée.") from exc

    def _decode_row(self, lookup: bytes, original: bytes, substitute: bytes) -> MappingEntry:
        typed = self._open(original, b"o" + lookup)
        entity_type, _, value = typed.partition("\0")
        return MappingEntry(entity_type, value, self._open(substitute, b"s" + lookup))

    # -- MappingStore ------------------------------------------------------------

    def lookup(self, entity_type: str, key: str) -> str | None:
        index = self._lookup_index(entity_type, key)
        rows = self._execute("SELECT substitute FROM mapping WHERE lookup = ?", (index,))
        return self._open(bytes(rows[0][0]), b"s" + index) if rows else None

    def save(self, entity_type: str, key: str, original: str, substitute: str) -> None:
        index = self._lookup_index(entity_type, key)
        row = (
            index,
            self._sub_index(substitute),
            self._seal(f"{entity_type}\0{original}", b"o" + index),
            self._seal(substitute, b"s" + index),
        )
        self._execute(
            "INSERT INTO mapping (lookup, sub_index, original, substitute)"
            " VALUES (?, ?, ?, ?) ON CONFLICT(lookup) DO UPDATE SET"
            " sub_index = excluded.sub_index, original = excluded.original,"
            " substitute = excluded.substitute",
            row,
            write=True,
        )

    def reverse(self, substitute: str) -> str | None:
        rows = self._execute(
            "SELECT lookup, original, substitute FROM mapping WHERE sub_index = ?",
            (self._sub_index(substitute),),
        )
        for lookup, original, sub in rows:
            entry = self._decode_row(bytes(lookup), bytes(original), bytes(sub))
            if entry.substitute == substitute:
                return entry.original
        return None

    def used_substitutes(self) -> set[str]:
        return {entry.substitute for entry in self.items()}

    # -- neutral file names ----------------------------------------------------

    def save_filename(self, batch_id: str, output_name: str, original: str) -> None:
        """Keep ``original`` behind the neutral ``output_name`` of a batch."""
        data = f"filename\0{batch_id}\0{output_name}".encode()
        index = hmac.new(self._idx_key, data, hashlib.sha256).digest()
        entry = self._seal(f"{batch_id}\0{output_name}\0{original}", b"f" + index)
        self._execute(
            "INSERT INTO filenames (id, entry) VALUES (?, ?)"
            " ON CONFLICT(id) DO UPDATE SET entry = excluded.entry",
            (index, entry),
            write=True,
        )

    def filenames(self, batch_id: str | None = None) -> list[FilenameEntry]:
        """Every neutral name of the store (of one batch when ``batch_id`` is given)."""
        entries = []
        for index, entry in self._execute("SELECT id, entry FROM filenames"):
            batch, output, original = self._open(bytes(entry), b"f" + bytes(index)).split("\0", 2)
            if batch_id is None or batch == batch_id:
                entries.append(FilenameEntry(batch, output, original))
        return sorted(entries)

    # -- extras ----------------------------------------------------------------

    def items(self) -> Iterator[MappingEntry]:
        """Every decrypted mapping (re-identification, export)."""
        rows = self._execute("SELECT lookup, original, substitute FROM mapping")
        for lookup, original, substitute in rows:
            yield self._decode_row(bytes(lookup), bytes(original), bytes(substitute))

    def count(self) -> int:
        """Number of mappings. (Deliberately not ``__len__``: callers test the store
        for truthiness, and an empty store must stay truthy.)"""
        return int(self._execute("SELECT COUNT(*) FROM mapping")[0][0])


__all__ = [
    "EncryptedMappingStore",
    "FilenameEntry",
    "InMemoryMappingStore",
    "KdfParams",
    "MappingEntry",
    "StoreError",
    "WrongPassphraseError",
]
