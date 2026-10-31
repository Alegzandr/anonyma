from __future__ import annotations

import re
import sqlite3
import threading
from pathlib import Path

import pytest

from anonyma.config import Config
from anonyma.models import MappingStore, Profile, Regime
from anonyma.pipeline import Pipeline
from anonyma.store import (
    EncryptedMappingStore,
    InMemoryMappingStore,
    KdfParams,
    StoreError,
    WrongPassphraseError,
)
from tests.canary import CANARY_EMAIL, CANARY_PERSON, CANARY_TERMS

FAST = KdfParams(memory_cost=8 * 1024, iterations=1, lanes=1)
SECRET = "correct cheval batterie agrafe"


def _open(path: Path, passphrase: str = SECRET, **kw: object) -> EncryptedMappingStore:
    return EncryptedMappingStore.open(path, passphrase, kdf=FAST, **kw)  # type: ignore[arg-type]


def test_implements_protocol(tmp_path: Path) -> None:
    with _open(tmp_path / "t.db") as store:
        assert isinstance(store, MappingStore)
    assert isinstance(InMemoryMappingStore(), MappingStore)


def test_roundtrip_and_persistence(tmp_path: Path) -> None:
    path = tmp_path / "t.db"
    with _open(path) as store:
        assert store.lookup("PERSONNE", "zephyrin quintavalle") is None
        store.save("PERSONNE", "zephyrin quintavalle", CANARY_PERSON, "[PERSONNE_001]")
        store.save("EMAIL", CANARY_EMAIL, CANARY_EMAIL, "[EMAIL_001]")
        assert store.lookup("PERSONNE", "zephyrin quintavalle") == "[PERSONNE_001]"
        # Same key, other type: independent entry.
        assert store.lookup("ORGANISATION", "zephyrin quintavalle") is None
    with _open(path) as store:
        assert store.lookup("PERSONNE", "zephyrin quintavalle") == "[PERSONNE_001]"
        assert store.reverse("[PERSONNE_001]") == CANARY_PERSON
        assert store.reverse("[PERSONNE_999]") is None
        assert store.used_substitutes() == {"[PERSONNE_001]", "[EMAIL_001]"}
        assert store.count() == 2
        assert {(e.entity_type, e.original) for e in store.items()} == {
            ("PERSONNE", CANARY_PERSON),
            ("EMAIL", CANARY_EMAIL),
        }
        assert CANARY_PERSON not in repr(next(store.items()))


def test_save_overwrites(tmp_path: Path) -> None:
    with _open(tmp_path / "t.db") as store:
        store.save("PERSONNE", "k", "Original", "[PERSONNE_001]")
        store.save("PERSONNE", "k", "Original", "[PERSONNE_002]")
        assert store.lookup("PERSONNE", "k") == "[PERSONNE_002]"
        assert store.reverse("[PERSONNE_001]") is None
        assert store.count() == 1


def test_wrong_passphrase(tmp_path: Path) -> None:
    path = tmp_path / "t.db"
    _open(path).close()
    with pytest.raises(WrongPassphraseError, match="incorrecte"):
        _open(path, "mauvaise phrase")


def test_passphrase_unicode_normalization(tmp_path: Path) -> None:
    path = tmp_path / "t.db"
    _open(path, "été").close()  # decomposed
    _open(path, "été").close()  # precomposed: same passphrase


def test_errors(tmp_path: Path) -> None:
    with pytest.raises(StoreError):
        _open(tmp_path / "absent.db", create=False)
    with pytest.raises(StoreError):
        _open(tmp_path / "t.db", "")
    garbage = tmp_path / "garbage.db"
    garbage.write_bytes(b"not a database" * 100)
    with pytest.raises(StoreError):
        _open(garbage)
    other = tmp_path / "other.db"
    with sqlite3.connect(other) as db:
        db.execute("CREATE TABLE x (a)")
    with pytest.raises(StoreError, match="n'est pas"):
        _open(other)
    store = _open(tmp_path / "t.db")
    store.close()
    with pytest.raises(StoreError):
        store.lookup("PERSONNE", "x")


def test_newer_schema_refused(tmp_path: Path) -> None:
    path = tmp_path / "t.db"
    _open(path).close()
    with sqlite3.connect(path) as db:
        db.execute("UPDATE meta SET value = ? WHERE name = 'schema'", (b"99",))
    with pytest.raises(StoreError, match="plus récente"):
        _open(path)


def test_tampered_row_detected(tmp_path: Path) -> None:
    path = tmp_path / "t.db"
    with _open(path) as store:
        store.save("PERSONNE", "a", "Alpha", "[PERSONNE_001]")
        store.save("PERSONNE", "b", "Bravo", "[PERSONNE_002]")
    with sqlite3.connect(path) as db:
        rows = db.execute("SELECT lookup, substitute FROM mapping").fetchall()
        # Swap the encrypted substitutes between rows: AAD binding must catch it.
        db.execute("UPDATE mapping SET substitute = ? WHERE lookup = ?", (rows[1][1], rows[0][0]))
    with _open(path) as store, pytest.raises(StoreError, match="endommagée"):
        for key in ("a", "b"):
            store.lookup("PERSONNE", key)


def test_pragmas(tmp_path: Path) -> None:
    path = tmp_path / "t.db"
    with _open(path) as store:
        assert store._execute("PRAGMA journal_mode") == [("delete",)]
        assert store._execute("PRAGMA secure_delete") == [(1,)]
    assert not (tmp_path / "t.db-wal").exists()


def test_nothing_sensitive_in_clear(tmp_path: Path) -> None:
    folder = tmp_path / "store"
    path = folder / "t.db"
    secrets = [CANARY_PERSON, CANARY_EMAIL, "Quintavalle", "PERSONNE", "[PERSONNE_001]", SECRET]
    with _open(path) as store:
        store.save("PERSONNE", "zephyrin quintavalle", CANARY_PERSON, "[PERSONNE_001]")
        store.save("EMAIL", CANARY_EMAIL, CANARY_EMAIL, "[EMAIL_001]")
        store.save("PERSONNE", "temp", "Supprimé Quintavalle", "[PERSONNE_002]")
        store.save("PERSONNE", "temp", "Autre", "[PERSONNE_003]")  # overwritten row
    for file in folder.iterdir():
        raw = file.read_bytes()
        for value in [*secrets, "Supprimé", "zephyrin", "quintavalle"]:
            for encoding in ("utf-8", "utf-16-le"):
                assert value.encode(encoding) not in raw, (file.name, value, encoding)


def test_concurrent_access(tmp_path: Path) -> None:
    with _open(tmp_path / "t.db") as store:

        def work(n: int) -> None:
            for i in range(20):
                store.save("PERSONNE", f"{n}-{i}", f"v{n}-{i}", f"[PERSONNE_{n}{i:03d}]")
                assert store.lookup("PERSONNE", f"{n}-{i}") == f"[PERSONNE_{n}{i:03d}]"

        threads = [threading.Thread(target=work, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert store.count() == 80


def test_default_kdf_cost(tmp_path: Path) -> None:
    path = tmp_path / "t.db"
    EncryptedMappingStore.open(path, SECRET).close()
    with sqlite3.connect(path) as db:
        meta = dict(db.execute("SELECT name, value FROM meta"))
    assert int(meta["memory_cost"]) >= 64 * 1024
    assert meta["kdf"] == b"argon2id"
    assert len(meta["salt"]) == 16


def test_pseudonymization_is_stable_across_batches(tmp_path: Path) -> None:
    config = Config(
        profile=Profile.LIGHT,
        workers=1,
        regime=Regime.PSEUDONYMIZATION,
        always_mask=list(CANARY_TERMS),
    )
    src = tmp_path / "in"
    src.mkdir()
    (src / "a.txt").write_text(f"Bonjour {CANARY_PERSON}.\n", encoding="utf-8")
    db = tmp_path / "table.db"
    with _open(db) as store:
        first = Pipeline(config, store=store).run([src], tmp_path / "out1")
    (src / "a.txt").write_text(f"Courriel {CANARY_EMAIL} de {CANARY_PERSON}.\n", encoding="utf-8")
    texts = [first.results[0].output_path.read_text(encoding="utf-8")]  # type: ignore[union-attr]
    # New values get random ids: sequential numbers would reveal the table size.
    person = re.fullmatch(r"Bonjour (\[PERSONNE_[A-Z2-9]{4}\])\.\n", texts[0])
    assert person is not None
    with _open(db) as store:
        second = Pipeline(config, store=store).run([src], tmp_path / "out2")
        assert store.reverse(person.group(1)) == CANARY_PERSON
    texts.append(second.results[0].output_path.read_text(encoding="utf-8"))  # type: ignore[union-attr]
    assert re.fullmatch(
        r"Courriel \[EMAIL_[A-Z2-9]{4}\] de " + re.escape(person.group(1)) + r"\.\n", texts[1]
    )


def test_neutral_filenames_encrypted(tmp_path: Path) -> None:
    path = tmp_path / "t.db"
    original = f"Dossier {CANARY_PERSON}/CV {CANARY_PERSON}.pdf"
    with _open(path) as store:
        store.save_filename("lot-1", "dossier_0001/document_0001.pdf", original)
        store.save_filename("lot-2", "document_0001.pdf", "autre.pdf")
        store.save_filename("lot-2", "document_0001.pdf", "remplace.pdf")
    data = path.read_bytes()
    assert CANARY_PERSON.encode() not in data and b"document_0001" not in data
    with _open(path) as store:
        entries = store.filenames()
        assert [(e.batch_id, e.output_name) for e in entries] == [
            ("lot-1", "dossier_0001/document_0001.pdf"),
            ("lot-2", "document_0001.pdf"),
        ]
        assert entries[0].original == original and entries[1].original == "remplace.pdf"
        assert [e.original for e in store.filenames("lot-2")] == ["remplace.pdf"]
        assert CANARY_PERSON not in repr(entries[0])
        assert store.count() == 0  # value mappings are separate
    memory = InMemoryMappingStore()
    memory.save_filename("lot-1", "document_0001.pdf", original)
    assert [e.original for e in memory.filenames("lot-1")] == [original]


def test_sqlite_failures_are_store_errors(tmp_path: Path) -> None:
    path = tmp_path / "t.db"
    with _open(path) as store:
        store._execute("DROP TABLE mapping")
        with pytest.raises(StoreError, match="Lecture impossible"):
            store.lookup("PERSONNE", "x")
        with pytest.raises(StoreError, match="Écriture impossible"):
            store.save("PERSONNE", "x", "X", "[PERSONNE_001]")
