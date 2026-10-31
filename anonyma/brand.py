"""The Anonyma mark, shared by the GUI, the HTML report and the packaging icons.

Pure data (no Qt import): the CLI and the report module can use it too.
"""

from __future__ import annotations

import base64

#: Application mark: a capital A whose crossbar is a redaction bar, on an accent tile.
#: The thin tile-coloured cut around the bar keeps it a separate object at 16 px.
APP_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
    '<rect x="2" y="2" width="60" height="60" rx="14" fill="#2b5a92"/>'
    '<path d="M27.5 12.5H36.5L51 51H43L32 21.7L21 51H13Z" fill="#ffffff"/>'
    '<rect x="8.25" y="31.25" width="47.5" height="10.5" fill="#2b5a92"/>'
    '<rect x="10" y="33" width="44" height="7" rx="1.2" fill="#0f1520"/>'
    "</svg>"
)


def app_svg_data_uri() -> str:
    """The mark as a ``data:`` URI (favicon and header of the HTML report).

    Base64 keeps the SVG namespace URL out of the report text, which stays free of
    any "http" string (it never references anything outside itself).
    """
    return "data:image/svg+xml;base64," + base64.b64encode(APP_SVG.encode("utf-8")).decode()
