from __future__ import annotations

import os
from pathlib import Path

from anonyma import outputs
from anonyma.pipeline import quarantine_dir


def test_only_unchanged_recorded_files_are_removed(tmp_path: Path, portable_home: Path) -> None:
    out = tmp_path / "sortie"
    (out / "dossier").mkdir(parents=True)
    ours = out / "dossier" / "a.txt"
    ours.write_text("produit", encoding="utf-8")
    edited = out / "b.txt"
    edited.write_text("produit", encoding="utf-8")
    quarantined = quarantine_dir(out) / "c.txt"
    quarantined.parent.mkdir()
    quarantined.write_text("produit", encoding="utf-8")
    outputs.record(out, [out / "dossier", edited, quarantined])
    assert outputs.manifest_path(out).is_relative_to(portable_home)  # never in the output

    foreign = out / "dossier" / "perso.txt"
    foreign.write_text("à moi", encoding="utf-8")
    edited.write_text("modifié par quelqu'un", encoding="utf-8")
    os.utime(edited, ns=(1, 1))

    cleanup = outputs.remove_previous(out)
    assert (cleanup.removed, cleanup.kept) == (2, 1)
    assert not ours.exists() and not quarantined.exists()
    assert not quarantined.parent.exists()  # emptied quarantine folder removed
    assert foreign.exists() and edited.exists() and out.is_dir()
    # A second pass only reports the modified file again; nothing else is touched.
    assert outputs.remove_previous(out) == outputs.Cleanup(0, 1)


def test_record_ignores_paths_outside_the_output(tmp_path: Path) -> None:
    out = tmp_path / "sortie"
    out.mkdir()
    elsewhere = tmp_path / "ailleurs.txt"
    elsewhere.write_text("x", encoding="utf-8")
    outputs.record(out, [elsewhere])
    assert outputs.remove_previous(out) == outputs.Cleanup(0, 0)
    assert elsewhere.exists()
