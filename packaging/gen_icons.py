"""Generate the application icons from the single source mark (``anonyma.brand``).

Writes ``packaging/icons/``:

    anonyma.svg   the mark itself (README, documentation)
    anonyma.png   512 px (Linux, previews)
    anonyma.ico   16 to 256 px (Windows executables)
    anonyma.icns  16 to 1024 px (macOS bundle)

Run after changing the mark:  uv run python packaging/gen_icons.py
The files are committed: building never needs Qt to render them.
"""

from __future__ import annotations

import io
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUT = ROOT / "packaging" / "icons"
ICO_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
ICNS_SIZES = (16, 32, 64, 128, 256, 512, 1024)


def render_png(svg: str, size: int) -> bytes:
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QRectF, Qt
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer

    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    QSvgRenderer(QByteArray(svg.encode("utf-8"))).render(painter, QRectF(0, 0, size, size))
    painter.end()
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")  # type: ignore[call-overload]  # stubs say bytes, runtime str
    return bytes(buffer.data().data())


def main() -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PIL import Image
    from PySide6.QtGui import QGuiApplication

    from anonyma.brand import APP_SVG

    _app = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "anonyma.svg").write_text(APP_SVG + "\n", encoding="utf-8")

    images = {
        size: Image.open(io.BytesIO(render_png(APP_SVG, size))).convert("RGBA")
        for size in sorted({*ICO_SIZES, *ICNS_SIZES})
    }
    images[512].save(OUT / "anonyma.png", optimize=True)
    # Each ICO entry is rendered at its own size (sharper than Pillow's downscaling).
    images[256].save(
        OUT / "anonyma.ico",
        sizes=[(s, s) for s in ICO_SIZES],
        append_images=[images[s] for s in ICO_SIZES if s != 256],
    )
    images[1024].save(
        OUT / "anonyma.icns", append_images=[images[s] for s in ICNS_SIZES if s != 1024]
    )
    for path in sorted(OUT.iterdir()):
        print(f"{path.relative_to(ROOT)}  {path.stat().st_size:>8} octets")


if __name__ == "__main__":
    main()
