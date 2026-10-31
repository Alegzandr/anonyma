"""Test helpers: synthetic images and a fake image plugin standing in for the
image workstream (OCR'd and anonymized images)."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from anonyma import registry
from anonyma.models import (
    Document,
    ExtractionContext,
    Extractor,
    Issue,
    ReplacementPlan,
    WriteContext,
    Writer,
)


def png_bytes(
    color: tuple[int, int, int] = (200, 30, 30), size: tuple[int, int] = (40, 40)
) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


#: What the fake image writer outputs: recognisable, valid PNG.
MARKER_PNG = png_bytes((0, 0, 255), (24, 24))


class FakeImageExtractor(Extractor):
    name = "fake_image"
    extensions = {".png": "png"}

    def extract(self, source: Path, document: Document, ctx: ExtractionContext) -> None:
        return None


class FakeImageWriter(Writer):
    name = "fake_image"
    formats = frozenset({"png"})

    def write(
        self, document: Document, plan: ReplacementPlan, destination: Path, ctx: WriteContext
    ) -> list[Issue]:
        destination.write_bytes(MARKER_PNG)
        return []


def install_fake_images(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend the image workstream is present (png only)."""
    registry.load_plugins()
    monkeypatch.setitem(registry._extensions, ".png", "png")
    monkeypatch.setitem(registry._extractors, "png", FakeImageExtractor)
    monkeypatch.setitem(registry._writers, "png", FakeImageWriter)


def remove_image_support(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend no image plugin exists (children become unsupported)."""
    registry.load_plugins()
    for fmt in ("png", "jpeg", "jpg"):
        if fmt in registry._extractors:
            monkeypatch.delitem(registry._extractors, fmt)
        if fmt in registry._writers:
            monkeypatch.delitem(registry._writers, fmt)


def zip_read(path: Path, name: str) -> bytes:
    with zipfile.ZipFile(path) as zf:
        return zf.read(name)


def zip_names(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as zf:
        return zf.namelist()
