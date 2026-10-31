"""HTML documents: text, attributes, comments, scripts, title; data: URIs as children."""

from __future__ import annotations

from pathlib import Path

from anonyma.codec import TextEncoding, decode_bytes
from anonyma.extractors._html import HtmlSource, build_segments, extension_for, sniff_charset
from anonyma.models import Document, ExtractionContext, Extractor
from anonyma.registry import register_extractor

#: Segment id prefix of HTML documents.
SEGMENT_PREFIX = "h"


def decode_html(data: bytes) -> tuple[str, TextEncoding]:
    """Decode an HTML file: BOM, then ``<meta charset>``, then the codec fallbacks."""
    text, encoding = decode_bytes(data)
    declared = sniff_charset(data)
    if encoding.bom or not declared or declared in ("utf-8", "utf8"):
        return text, encoding
    # Valid non-ASCII UTF-8 wins over a contradicting declaration; pure ASCII and
    # non-UTF-8 bytes follow the declaration (also used to encode substitutes).
    if encoding.encoding == "utf-8" and not data.isascii():
        return text, encoding
    try:
        return data.decode(declared), TextEncoding(declared)
    except (UnicodeDecodeError, LookupError):
        return text, encoding


@register_extractor
class HtmlExtractor(Extractor):
    name = "html"
    extensions = {".html": "html", ".htm": "html", ".xhtml": "html"}

    def extract(self, source: Path, document: Document, ctx: ExtractionContext) -> None:
        text, encoding = decode_html(source.read_bytes())
        document.properties["encoding"] = encoding
        html_source = HtmlSource(text)
        document.segments.extend(build_segments(html_source, SEGMENT_PREFIX))
        document.properties["data_uris"] = extract_data_uris(html_source, document, ctx)


def extract_data_uris(
    html_source: HtmlSource, document: Document, ctx: ExtractionContext, prefix: str = ""
) -> dict[int, str]:
    """Extract every ``data:`` URI as a child; returns unit index -> child id."""
    children: dict[int, str] = {}
    for n, uri in enumerate(html_source.data_uris, start=1):
        ctx.check_cancelled()
        ctx.budget.consume(len(uri.data), depth=ctx.depth + 1)
        name = f"{prefix}image-incorporee-{n}{extension_for(uri.mime)}"
        child = ctx.extract_child(uri.data, name, document)
        children[uri.unit] = child.id
    return children
