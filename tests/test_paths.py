from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from anonyma import paths

_HOLDER = """
import sys
from pathlib import Path
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
