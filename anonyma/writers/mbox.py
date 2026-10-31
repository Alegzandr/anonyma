"""mbox writer: anonymized messages reassembled with anonymized ``From `` lines."""

from __future__ import annotations

import re
from pathlib import Path

from anonyma.models import Document, Issue, ReplacementPlan, WriteContext, Writer
from anonyma.registry import register_writer

_FROM_LINE = re.compile(rb"^From ", re.M)


@register_writer
class MboxWriter(Writer):
    name = "mbox"
    formats = frozenset({"mbox"})

    def write(
        self,
        document: Document,
        plan: ReplacementPlan,
        destination: Path,
        ctx: WriteContext,
    ) -> list[Issue]:
        newline: bytes = str(document.properties.get("newline", "\n")).encode()
        out = bytearray()
        for segment_id, child_id in document.properties.get("messages", []):
            output = ctx.child_outputs.get(child_id)
            if output is None or output.path is None:
                continue  # omitted: reported by the pipeline
            separator = "MAILER-DAEMON Thu Jan  1 00:00:00 1970"
            if segment_id is not None:
                separator = plan.apply(document.segment(segment_id))
            separator = separator.replace("\r", " ").replace("\n", " ")
            message = output.path.read_bytes()
            # mboxo quoting: no body line may look like a separator.
            message = _FROM_LINE.sub(b">From ", message)
            if not message.endswith(b"\n"):
                message += newline
            out += b"From " + separator.encode("utf-8") + newline + message + newline
        destination.write_bytes(bytes(out))
        return []
