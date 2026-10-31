"""7z writer: rebuilt from anonymized children with neutral timestamps."""

from __future__ import annotations

import tempfile
from pathlib import Path

import py7zr

from anonyma.extractors._archive import (
    UniqueNames,
    anonymized_directories,
    copy_file,
    neutral_timestamp,
)
from anonyma.models import Document, Issue, ReplacementPlan, WriteContext, Writer
from anonyma.registry import register_writer


@register_writer
class SevenZipWriter(Writer):
    name = "7z"
    formats = frozenset({"7z"})

    def write(
        self,
        document: Document,
        plan: ReplacementPlan,
        destination: Path,
        ctx: WriteContext,
    ) -> list[Issue]:
        unique = UniqueNames()
        staging = ctx.temp_path("-7z")
        staging.mkdir(parents=True)
        with py7zr.SevenZipFile(destination, "w") as archive:
            for i, child in enumerate(document.children):
                output = ctx.child_outputs.get(child.id)
                if output is None or output.path is None:
                    continue
                # Copy so that the timestamp change never touches a shared file.
                staged = staging / f"{i}"
                copy_file(output.path, staged)
                neutral_timestamp(staged)
                archive.write(staged, unique(output.name))
            for directory in anonymized_directories(document, plan):
                empty = Path(tempfile.mkdtemp(dir=staging))
                neutral_timestamp(empty)
                archive.write(empty, unique(directory))
        return []
