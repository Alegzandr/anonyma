"""HTML writer: source copied verbatim except replaced spans and data: URIs."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from anonyma.codec import TextEncoding
from anonyma.extractors._html import HtmlSource, data_uri_value, mime_for, plan_edits
from anonyma.extractors.html import decode_html
from anonyma.models import (
    ChildOutput,
    Document,
    Issue,
    ReplacementPlan,
    WriteContext,
    Writer,
)
from anonyma.registry import register_writer


def data_uri_values(
    data_uris: Mapping[int, str], child_outputs: Mapping[str, ChildOutput]
) -> dict[int, str]:
    """New attribute values for data: URIs; an omitted child leaves an empty value."""
    values: dict[int, str] = {}
    for unit, child_id in data_uris.items():
        output = child_outputs.get(child_id)
        if output is None or output.path is None:
            values[unit] = ""
        else:
            values[unit] = data_uri_value(mime_for(output.name), output.path.read_bytes())
    return values


@register_writer
class HtmlWriter(Writer):
    name = "html"
    formats = frozenset({"html"})

    def write(
        self,
        document: Document,
        plan: ReplacementPlan,
        destination: Path,
        ctx: WriteContext,
    ) -> list[Issue]:
        data = document.source.read_bytes()
        text, detected = decode_html(data)
        encoding: TextEncoding = document.properties.get("encoding", detected)
        source = HtmlSource(text)
        edits = plan_edits(source, document.segments, plan)
        values = data_uri_values(document.properties.get("data_uris", {}), ctx.child_outputs)
        output = source.rewrite(edits, values)
        destination.write_bytes(
            encoding.bom + output.encode(encoding.encoding, errors="xmlcharrefreplace")
        )
        return []
