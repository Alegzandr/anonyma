"""Empty input files of formats that cannot be empty (containers, binary formats)."""

from __future__ import annotations

from pathlib import Path

from anonyma.models import Document, Severity

EMPTY_CODE = "extract.empty"


def empty_file(source: Path, document: Document) -> bool:
    """True (and an error recorded) when ``source`` has 0 bytes: a docx, a zip, a
    PDF... of 0 bytes holds nothing, and the reader's own error would be cryptic."""
    try:
        if source.stat().st_size:
            return False
    except OSError:
        return False
    document.add_issue(
        Severity.ERROR,
        EMPTY_CODE,
        "Fichier vide (0 octet) : il ne contient rien à lire ni à anonymiser.",
    )
    return True
