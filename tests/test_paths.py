from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from anonyma import paths

_HOLDER = """
import sys
from pathlib import Path
import pytest

from anonyma import paths
paths.claim(Path(sys.argv[1]))
print("ok", flush=True)
sys.stdin.readline()
"""


def _old(path: Path) -> None:
    past = time.time() - 2 * paths.STALE_SECONDS
    os.utime(path, (past, past))


def test_clean_tmp_spares_batches_of_live_instances() -> None:
    root = paths.tmp_dir()
    root.mkdir(parents=True, exist_ok=True)
    busy, dead, fresh, stale = (root / n for n in ("occupe", "abandonne", "neuf", "ancien"))
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(busy)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "ok"
        paths.claim(dead)
        paths.release(dead)  # its owner is gone: lock file left, nobody holds it
        fresh.mkdir()
        stale.mkdir()
        _old(stale)
        (root / "temp.bin").write_bytes(b"x")
        paths.clean_tmp()
        assert busy.is_dir() and fresh.is_dir() and (root / "temp.bin").exists()
        assert not dead.exists() and not stale.exists()
    finally:
        assert holder.stdin is not None
        holder.stdin.write("\n")
        holder.stdin.close()
        holder.wait(timeout=30)
    paths.clean_tmp()  # the instance ended: its batch goes
    assert not busy.exists()


def test_own_claims_survive_clean_tmp() -> None:
    folder = paths.tmp_dir() / "lot"
    paths.claim(folder)
    try:
        paths.clean_tmp()
        assert folder.is_dir()
    finally:
        paths.release(folder)


def test_remove_tree_waits_for_a_file_released_shortly(tmp_path: Path) -> None:
    import threading

    from anonyma.paths import remove_tree

    folder = tmp_path / "lot"
    (folder / "profil").mkdir(parents=True)
    held = (folder / "profil" / "registrymodifications.xcu").open("w", encoding="utf-8")
    # On Windows an open file cannot be deleted: the first attempts fail.
    threading.Timer(0.6, held.close).start()
    assert remove_tree(folder, retry_seconds=5.0)
    assert not folder.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows read-only attribute")
def test_remove_tree_clears_read_only_folders_and_files(tmp_path: Path) -> None:
    import shutil

    from anonyma.paths import remove_tree

    folder = tmp_path / "lot"
    documents = folder / "lo-1-2" / "Documents"
    documents.mkdir(parents=True)
    (folder / "lo-1-2" / "lecture_seule.xcu").write_text("x", encoding="utf-8")
    for entry in (documents, folder / "lo-1-2" / "lecture_seule.xcu"):
        subprocess.run(["attrib", "+R", str(entry)], check=True, capture_output=True)
    shutil.rmtree(folder, ignore_errors=True)
    assert documents.exists()  # what a plain removal leaves behind
    assert remove_tree(folder, retry_seconds=1.0)
    assert not folder.exists()
