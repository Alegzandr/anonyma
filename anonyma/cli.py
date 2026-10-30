"""Command-line interface (Typer).

Phase 0: minimal ``run`` command. The infrastructure workstream extends it.
"""

from __future__ import annotations

import multiprocessing
from pathlib import Path
from typing import Annotated

import typer

app = typer.Typer(help="Anonyma : anonymisation locale de documents.", no_args_is_help=True)


@app.command("run")
def run(
    inputs: Annotated[list[Path], typer.Argument(help="Fichiers ou dossiers à anonymiser.")],
    output: Annotated[Path, typer.Option("--sortie", "-o", help="Dossier de sortie.")],
) -> None:
    """Anonymise des fichiers et dossiers."""
    from anonyma import netguard, paths
    from anonyma.config import Config
    from anonyma.pipeline import Pipeline
    from anonyma.report import BatchReport

    paths.configure_environment()
    netguard.install()
    paths.clean_tmp()
    result = Pipeline(Config.load()).run(inputs, output)
    report = BatchReport.from_result(result)
    typer.echo(report.to_markdown())


def main() -> None:
    multiprocessing.freeze_support()
    app()


if __name__ == "__main__":
    main()
