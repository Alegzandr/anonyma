"""RTF writer: replacements spliced at token spans, metadata and revisions purged.

Purge: ``\\info`` emptied, user properties, document variables, rsids and
revision marks removed, tracked deletions removed (insertions kept), annotation
authors replaced by "Anonyme", embedded OLE object data removed (its preview
picture is kept, anonymized like every picture).
"""

from __future__ import annotations

import binascii
import re
from pathlib import Path

from anonyma.extractors._media import BLANKED_CODE, PLACEHOLDER_PNG, blanked_message
from anonyma.extractors._rtf import BLIP_WORD, RtfPicture, encode_text, parse_rtf
from anonyma.extractors._xmlscan import redistribute
from anonyma.models import (
    Document,
    Issue,
    ReplacementPlan,
    Severity,
    WriteContext,
    Writer,
)
from anonyma.registry import register_writer


def _picture_group(picture: RtfPicture, data: bytes, ext: str) -> bytes:
    hexdata = binascii.hexlify(data)
    lines = b"\r\n".join(hexdata[i : i + 128] for i in range(0, len(hexdata), 128))
    return b"{\\pict" + b"".join(picture.controls) + BLIP_WORD[ext] + b"\r\n" + lines + b"}"


_OPEN_WORD = re.compile(rb"\\[a-zA-Z]{1,32}-?\d{0,10}$")
_MERGES = re.compile(rb"[A-Za-z0-9 \-]")


class _Joiner:
    """Concatenates RTF chunks, adding the delimiter space a control word needs
    when an edit makes it adjacent to a letter, digit, space or hyphen."""

    def __init__(self) -> None:
        self.chunks: list[bytes] = []
        self.tail = b""

    def add(self, chunk: bytes) -> None:
        if not chunk:
            return
        if _MERGES.match(chunk[:1]) and _OPEN_WORD.search(self.tail):
            chunk = b" " + chunk
        self.chunks.append(chunk)
        self.tail = (self.tail + chunk)[-48:]

    def result(self) -> bytes:
        return b"".join(self.chunks)


def apply_edits(raw: bytes, edits: list[tuple[int, int, bytes]]) -> bytes:
    """Apply span edits; an edit inside another (kept) edit is dropped."""
    edits.sort(key=lambda e: (e[0], -e[1]))
    out = _Joiner()
    cursor = 0
    for start, end, replacement in edits:
        if start < cursor:
            continue
        out.add(raw[cursor:start])
        out.add(replacement)
        cursor = end
    out.add(raw[cursor:])
    return out.result()


@register_writer
class RtfWriter(Writer):
    name = "rtf"
    formats = frozenset({"rtf"})

    def write(
        self,
        document: Document,
        plan: ReplacementPlan,
        destination: Path,
        ctx: WriteContext,
    ) -> list[Issue]:
        raw = document.source.read_bytes()
        parsed = parse_rtf(raw)
        edits: list[tuple[int, int, bytes]] = list(parsed.purge)
        for i, unit in enumerate(parsed.units):
            if unit.deleted:
                edits.extend((p.start, p.end, b"") for p in unit.pieces)
                continue
            reps = plan.for_segment(f"u{i}")
            if not reps:
                continue
            new = redistribute([(p.text, p.editable) for p in unit.pieces], reps)
            for piece, text in zip(unit.pieces, new, strict=True):
                if piece.editable and text != piece.text:
                    edits.append((piece.start, piece.end, encode_text(text, piece.uc)))
        issues: list[Issue] = []
        blanked = 0
        by_index = {
            index: child_id
            for child_id, index in document.properties.get("rtf_pictures", {}).items()
        }
        for index, picture in enumerate(parsed.pictures):
            child_id = by_index.get(index)
            if child_id is None:
                continue
            output = ctx.child_outputs.get(child_id)
            if output is not None and output.path is not None:
                ext = output.path.suffix.lower()
                if ext in BLIP_WORD:
                    group = _picture_group(picture, output.path.read_bytes(), ext)
                    edits.append((picture.start, picture.end, group))
                    continue
            blanked += 1
            edits.append(
                (picture.start, picture.end, _picture_group(picture, PLACEHOLDER_PNG, ".png"))
            )
        if blanked:
            issues.append(Issue(Severity.WARNING, BLANKED_CODE, blanked_message(blanked)))
        if parsed.objects:
            issues.append(
                Issue(
                    Severity.WARNING,
                    "rtf.object_removed",
                    f"{parsed.objects} objet(s) OLE intégré(s) supprimé(s) (seul l'aperçu est "
                    "conservé).",
                )
            )
        destination.write_bytes(apply_edits(raw, edits))
        return issues
