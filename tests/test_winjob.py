from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from anonyma import libreoffice

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows job objects")

#: A stand-in for soffice.exe: writes the converted file, starts a child that keeps
#: running (like a lingering soffice.bin) and returns at once.
_FAKE_SOFFICE = r"""
import subprocess, sys
from pathlib import Path
args = sys.argv[1:]
out = Path(args[args.index("--outdir") + 1])
source = Path(args[-1])
target = args[args.index("--convert-to") + 1].split(":")[0]
(out / f"{source.stem}.{target}").write_bytes(b"converti")
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
Path(sys.argv[0]).with_name("enfant.pid").write_text(str(child.pid))
"""


def _alive(pid: int) -> bool:
    found = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True, check=False
    )
    return str(pid) in found.stdout


def test_lingering_helper_process_is_ended_after_conversion(tmp_path: Path) -> None:
    script = tmp_path / "faux_soffice.py"
    script.write_text(_FAKE_SOFFICE, encoding="utf-8")
    launcher = tmp_path / "soffice.cmd"
    launcher.write_text(f'@"{sys.executable}" "{script}" %*\n', encoding="utf-8")
    source = tmp_path / "ancien.doc"
    source.write_bytes(b"doc")
    result = libreoffice.convert(
        source, "docx:MS Word 2007 XML", tmp_path / "w", launcher, timeout=60
    )
    assert result.read_bytes() == b"converti"
    pid = int((tmp_path / "enfant.pid").read_text())
    deadline = time.monotonic() + 10
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.2)
    assert not _alive(pid)
