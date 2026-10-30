"""Generate plain-text contract fixtures: ``python -m tests.fixtures.generators.text``."""

from __future__ import annotations

from pathlib import Path

from tests.canary import CANARY_EMAIL, CANARY_PERSON

OUT = Path(__file__).resolve().parent.parent / "contract"


def main() -> None:
    body = (
        f"Compte rendu rédigé par {CANARY_PERSON}.\r\n"
        f"Contact : {CANARY_EMAIL}\r\n\r\n"
        f"M. {CANARY_PERSON.upper()} a validé le dossier.\r\n"
    )
    (OUT / "note.txt").write_bytes(body.encode("cp1252"))
    (OUT / "journal.log").write_text(
        f"2026-01-01 INFO login user={CANARY_EMAIL}\n", encoding="utf-8"
    )
    (OUT / "lisezmoi.md").write_text(
        f"# Projet\n\nResponsable : **{CANARY_PERSON}** ({CANARY_EMAIL})\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
