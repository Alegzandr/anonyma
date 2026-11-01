"""mbox writer: anonymized messages reassembled with anonymized ``From `` lines
(valid sender address, the anonymized date of the message: see
:func:`_mailheaders.safe_from_line`)."""

from __future__ import annotations

import re
from pathlib import Path

from anonyma.extractors._mailheaders import message_moment, safe_from_line
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
        # Indexed once (20,000 mails: one scan of the segments each was quadratic), and
        # written message by message rather than held whole in memory twice.
        segments = {s.id: s for s in document.segments}
        with destination.open("wb") as out:
            for segment_id, child_id in document.properties.get("messages", []):
                output = ctx.child_outputs.get(child_id)
                if output is None or output.path is None:
                    continue  # omitted: reported by the pipeline
                message = output.path.read_bytes()
                line = plan.apply(segments[segment_id]) if segment_id is not None else ""
                separator = safe_from_line(line, message_moment(message))
                separator = separator.replace("\r", " ").replace("\n", " ")
                # mboxo quoting: no body line may look like a separator.
                message = _FROM_LINE.sub(b">From ", message)
                if not message.endswith(b"\n"):
                    message += newline
                out.write(b"From " + separator.encode("utf-8") + newline + message + newline)
        return []
