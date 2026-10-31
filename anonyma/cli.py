"""Command-line interface (Typer), French only.

Exit codes: 0 everything anonymized, 1 some file has residues / failed / is
unsupported (or the operation failed), 2 usage error (bad option, wrong
passphrase, invalid configuration).
"""

from __future__ import annotations

import dataclasses
import enum
import json
import multiprocessing
import os
import re
import sys
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, NoReturn

import typer
from rich.console import Console
from rich.table import Table

from anonyma.models import DocumentStatus, Profile, Regime, ReplacementMode

if TYPE_CHECKING:
    from anonyma.config import Config
    from anonyma.pipeline import BatchAnalysis, ProgressEvent
    from anonyma.store import EncryptedMappingStore

PASSPHRASE_ENV = "ANONYMA_PASSPHRASE"
DEFAULT_STORE_NAME = "table-correspondance.anonyma"

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_USAGE = 2

#: Statuses that make ``anonyma run`` exit with code 1.
BAD_STATUSES = frozenset(
    {
        DocumentStatus.RESIDUE,
        DocumentStatus.FAILED,
        DocumentStatus.UNSUPPORTED,
        DocumentStatus.CANCELLED,
    }
)

#: Text formats handled by ``anonyma reidentifier`` (plain substitution).
REIDENTIFY_EXTENSIONS = frozenset(
    {
        ".txt",
        ".md",
        ".markdown",
        ".csv",
        ".tsv",
        ".log",
        ".json",
        ".jsonl",
        ".xml",
        ".html",
        ".htm",
        ".yaml",
        ".yml",
        ".ini",
        ".srt",
        ".vtt",
    }
)

out = Console(highlight=False)
err = Console(stderr=True, highlight=False)


# ---------------------------------------------------------------------------
# French messages for Typer internals
# ---------------------------------------------------------------------------

#: Typer (which vendors Click with hard-coded English strings) usage errors,
#: translated when displayed. Order matters: first match wins per pattern.
_ERROR_TRANSLATIONS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern), replacement)
    for pattern, replacement in (
        (r"Missing option (.+?)\.?$", r"Option obligatoire manquante : \1."),
        (r"Missing argument (.+?)\.?$", r"Argument obligatoire manquant : \1."),
        (r"Missing parameter: (\S+)", r"Paramètre obligatoire manquant : \1"),
        (r"Missing (option|argument|parameter)", r"Paramètre obligatoire manquant"),
        (r"No such option: (\S+)", r"Option inconnue : \1"),
        (r"No such command '([^']+)'\.?", r"Commande inconnue : \1."),
        (r"Missing command\.", r"Commande manquante."),
        (r"Option '([^']+)' requires an argument\.", r"L'option \1 attend une valeur."),
        (r"Got unexpected extra argument\(s\) \((.*)\)", r"Argument(s) inattendu(s) : \1"),
        (r"Invalid value for (.+?): ", r"Valeur invalide pour \1 : "),
        (r"Invalid value: ", r"Valeur invalide : "),
        (r"(\S+) is not one of (.+)\.", r"\1 ne fait pas partie de : \2."),
        (r"(\S+) is not a valid integer( range)?\.", r"\1 n'est pas un nombre entier."),
        (r"(\S+) is not a valid float( range)?\.", r"\1 n'est pas un nombre."),
        (r"(\S+) is not a valid boolean\.[^\n]*", r"\1 n'est pas une valeur oui/non."),
        (r"(\S+) is not in the range (.+)\.", r"\1 n'est pas dans l'intervalle \2."),
        (r"(\S+) is not a valid ([A-Za-z ]+)\.?$", r"\1 n'est pas une valeur valide (\2)."),
        (r"Expecting .* line (\d+) column \d+ \(char \d+\)", r"JSON mal formé (ligne \1)"),
        (r"Did you mean (.+)\?", r"Vouliez-vous dire \1 ?"),
        (r"Path '([^']+)' does not exist\.", r"Chemin introuvable : \1."),
        (r"(File|Directory|Path) '([^']+)' (is a directory|is a file)\.", r"Chemin invalide : \2."),
    )
)


def translate_error(message: str) -> str:
    for pattern, replacement in _ERROR_TRANSLATIONS:
        message = pattern.sub(replacement, message)
    return message


def _frenchify_typer() -> None:
    """Translate the framework texts (help panels, usage line, errors) to French."""
    import typer.rich_utils as ru
    from typer._click import decorators, formatting
    from typer._click.exceptions import ClickException

    ru.ARGUMENTS_PANEL_TITLE = "Arguments"
    ru.OPTIONS_PANEL_TITLE = "Options"
    ru.COMMANDS_PANEL_TITLE = "Commandes"
    ru.ERRORS_PANEL_TITLE = "Erreur"
    ru.ABORTED_TEXT = "Abandon."
    ru.RICH_HELP = "Essayez [blue]« {command_path} {help_option} »[/] pour obtenir de l'aide."
    ru.DEFAULT_STRING = "[défaut : {}]"
    ru.REQUIRED_LONG_STRING = "[obligatoire]"
    ru.ENVVAR_STRING = "[variable d'environnement : {}]"
    ru.DEPRECATED_STRING = "(obsolète) "

    if getattr(decorators.help_option, "_anonyma", False):
        return
    original_help = decorators.help_option

    def help_option(param_decls: list[str]) -> Callable[[Any], Any]:
        decorate = original_help(param_decls)

        def apply(command: Any) -> Any:
            command = decorate(command)
            params = getattr(command, "params", None) or getattr(command, "__click_params__", [])
            if params:
                params[-1].help = "Afficher cette aide et quitter."
            return command

        return apply

    help_option._anonyma = True  # type: ignore[attr-defined]
    decorators.help_option = help_option

    original_usage = formatting.HelpFormatter.write_usage

    def write_usage(self: Any, prog: str, args: str = "", prefix: str | None = None) -> None:
        original_usage(self, prog, args, "Utilisation : " if prefix is None else prefix)

    formatting.HelpFormatter.write_usage = write_usage  # type: ignore[method-assign]

    original_error = ru.rich_format_error

    def rich_format_error(self: ClickException) -> None:
        format_message = self.format_message
        self.format_message = lambda: translate_error(format_message())  # type: ignore[method-assign]
        original_error(self)

    ru.rich_format_error = rich_format_error


_frenchify_typer()

CONTEXT = {"help_option_names": ["-h", "--help"]}

app = typer.Typer(
    help="Anonyma : anonymisation locale et en masse de documents.",
    no_args_is_help=True,
    add_completion=False,
    context_settings=CONTEXT,
    rich_markup_mode="rich",
)
models_app = typer.Typer(
    help="Gérer les modèles d'IA (téléchargement, import hors ligne).",
    no_args_is_help=True,
    context_settings=CONTEXT,
)
config_app = typer.Typer(
    help="Afficher ou modifier la configuration.",
    no_args_is_help=True,
    context_settings=CONTEXT,
)
app.add_typer(models_app, name="modeles")
app.add_typer(config_app, name="config")


# ---------------------------------------------------------------------------
# Bootstrap and helpers
# ---------------------------------------------------------------------------

_bootstrapped = False


def bootstrap() -> None:
    """Portable environment, network guard, temp cleanup and logging (idempotent)."""
    global _bootstrapped
    from anonyma import logs, netguard, paths

    paths.configure_environment()
    netguard.install()
    if not _bootstrapped:
        paths.clean_tmp()
        _bootstrapped = True
    logs.setup_logging()


def _version(value: bool) -> None:
    if value:
        from anonyma import __version__

        out.print(f"Anonyma {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version,
            is_eager=True,
            help="Afficher la version et quitter.",
        ),
    ] = False,
) -> None:
    bootstrap()


def fail(message: str, code: int = EXIT_USAGE) -> NoReturn:
    err.print(f"[bold red]Erreur :[/] {_markup_escape(message)}")
    raise typer.Exit(code)


def _markup_escape(text: str) -> str:
    from rich.markup import escape

    return escape(text)


class ProfileChoice(enum.StrEnum):
    AUTO = "auto"
    LIGHT = "leger"
    STANDARD = "standard"
    FULL = "complet"


def load_config(config_file: Path | None) -> Config:
    from anonyma.config import Config

    if config_file is not None and not config_file.is_file():
        fail(f"Fichier de configuration introuvable : {config_file}")
    try:
        return Config.load(config_file)
    except (ValueError, TypeError, OSError) as exc:
        fail(f"Configuration invalide : {translate_error(str(exc))}")


def _read_whitelist(path: Path) -> list[str]:
    if not path.is_file():
        fail(f"Liste blanche introuvable : {path}")
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        fail("Liste blanche illisible : enregistrez-la en UTF-8, une valeur par ligne.")
    return [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")]


def apply_options(
    config: Config,
    *,
    profile: ProfileChoice | None = None,
    mode: ReplacementMode | None = None,
    regime: Regime | None = None,
    paranoid: bool | None = None,
    whitelist: Path | None = None,
    business_lists: Sequence[Path] = (),
    workers: int | None = None,
) -> Config:
    from anonyma.config import load_business_list

    if profile is not None:
        config.profile = None if profile is ProfileChoice.AUTO else Profile(profile.value)
    if mode is not None:
        config.mode = mode
    if regime is not None:
        config.regime = regime
    if paranoid is not None:
        config.paranoid = paranoid
    if whitelist is not None:
        config.whitelist = [*config.whitelist, *_read_whitelist(whitelist)]
    for csv_path in business_lists:
        if not csv_path.is_file():
            fail(f"Liste métier introuvable : {csv_path}")
        try:
            load_business_list(csv_path)
        except (OSError, UnicodeDecodeError, ValueError):
            fail("Liste métier illisible : CSV UTF-8 attendu (colonnes valeur[,type]).")
        config.business_lists = [*config.business_lists, str(csv_path.resolve())]
    if workers is not None:
        config.workers = workers
    try:
        config.validate()
    except ValueError as exc:
        fail(str(exc))
    return config


def _check_inputs(inputs: Sequence[Path]) -> list[Path]:
    if not inputs:
        fail("Indiquez au moins un fichier ou un dossier.")
    for path in inputs:
        if not path.exists():
            fail(f"Fichier ou dossier introuvable : {path}")
    return list(inputs)


def store_path(config: Config, table: Path | None) -> Path:
    from anonyma import paths

    if table is not None:
        return table
    if config.store_path:
        return Path(config.store_path)
    return paths.data_dir() / DEFAULT_STORE_NAME


def read_passphrase(*, confirm: bool) -> str:
    env = os.environ.get(PASSPHRASE_ENV)
    if env:
        return env
    try:
        value: str = typer.prompt(
            "Phrase secrète de la table de correspondance",
            hide_input=True,
            confirmation_prompt="Confirmez la phrase secrète" if confirm else False,
        )
    except typer.Abort:
        fail("Phrase secrète non saisie.")
    if not value:
        fail("La phrase secrète ne peut pas être vide.")
    return value


def open_store(path: Path, *, create: bool) -> EncryptedMappingStore:
    from anonyma.store import EncryptedMappingStore, StoreError

    new = not path.is_file()
    if new and not create:
        fail(f"Table de correspondance introuvable : {path}")
    if new:
        err.print(f"Création d'une nouvelle table de correspondance chiffrée : {path}")
    passphrase = read_passphrase(confirm=new)
    try:
        return EncryptedMappingStore.open(path, passphrase, create=create)
    except StoreError as exc:
        fail(str(exc))


@contextmanager
def progress_display(enabled: bool) -> Iterator[Callable[[ProgressEvent], None] | None]:
    """A Rich progress bar on stderr fed by pipeline ``ProgressEvent``s."""
    if not enabled:
        yield None
        return
    from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn

    labels = {"analyse": "Analyse", "ecriture": "Écriture et vérification"}
    with Progress(
        TextColumn("{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=err,
        transient=False,
    ) as bar:
        tasks: dict[str, Any] = {}

        def update(event: ProgressEvent) -> None:
            if event.stage not in tasks:
                tasks[event.stage] = bar.add_task(
                    labels.get(event.stage, event.stage), total=event.total
                )
            bar.update(tasks[event.stage], completed=event.done, total=event.total)

        yield update


def _settings(config: Config) -> dict[str, str]:
    from anonyma.hardware import PROFILE_LABELS

    profile = config.effective_profile()
    return {
        "Profil": PROFILE_LABELS.get(profile, profile.value),
        "Mode": config.mode.value,
        "Régime": config.regime.value,
        "Mode paranoïaque": "oui" if config.paranoid else "non",
    }


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------

InputsArg = Annotated[
    list[Path],
    typer.Argument(
        help="Fichiers ou dossiers à traiter.", metavar="CHEMINS...", show_default=False
    ),
]
ProfileOpt = Annotated[
    ProfileChoice | None,
    typer.Option(
        "--profil",
        "-p",
        help="Profil de détection (auto = selon le matériel).",
        case_sensitive=False,
        show_default=False,
    ),
]
ParanoidOpt = Annotated[
    bool | None,
    typer.Option(
        "--paranoiaque/--non-paranoiaque",
        help="Masquer aussi tout nom propre et toute séquence suspecte.",
        show_default=False,
    ),
]
WhitelistOpt = Annotated[
    Path | None,
    typer.Option(
        "--liste-blanche",
        help="Fichier texte de valeurs à ne jamais masquer (une par ligne).",
        metavar="FICHIER",
        show_default=False,
    ),
]
BusinessOpt = Annotated[
    list[Path] | None,
    typer.Option(
        "--liste-metier",
        help="CSV de valeurs à toujours masquer (valeur[,type]) ; option répétable.",
        metavar="CSV",
        show_default=False,
    ),
]
WorkersOpt = Annotated[
    int | None,
    typer.Option(
        "--processus",
        "-j",
        min=0,
        metavar="N",
        help="Nombre de processus (0 = automatique).",
        show_default=False,
    ),
]
ConfigOpt = Annotated[
    Path | None,
    typer.Option(
        "--config",
        help="Fichier de configuration JSON à utiliser.",
        metavar="FICHIER",
        show_default=False,
    ),
]
TableOpt = Annotated[
    Path | None,
    typer.Option(
        "--table",
        help="Table de correspondance chiffrée (pseudonymisation).",
        metavar="FICHIER",
        show_default=False,
    ),
]
QuietOpt = Annotated[
    bool, typer.Option("--silencieux", "-q", help="Ne pas afficher la progression.")
]


@app.command("run")
def run(
    inputs: InputsArg,
    output: Annotated[
        Path,
        typer.Option(
            "--sortie", "-o", help="Dossier de sortie.", metavar="DOSSIER", show_default=False
        ),
    ],
    profile: ProfileOpt = None,
    mode: Annotated[
        ReplacementMode | None,
        typer.Option("--mode", "-m", help="Mode de remplacement.", show_default=False),
    ] = None,
    regime: Annotated[
        Regime | None,
        typer.Option(
            "--regime",
            "-r",
            help="Anonymisation (irréversible) ou pseudonymisation (table chiffrée).",
            show_default=False,
        ),
    ] = None,
    paranoid: ParanoidOpt = None,
    whitelist: WhitelistOpt = None,
    business_lists: BusinessOpt = None,
    workers: WorkersOpt = None,
    config_file: ConfigOpt = None,
    report_path: Annotated[
        Path | None,
        typer.Option(
            "--rapport",
            help="Enregistrer le rapport (.json, .md ou .html).",
            metavar="FICHIER",
            show_default=False,
        ),
    ] = None,
    table: TableOpt = None,
    quiet: QuietOpt = False,
) -> None:
    """Anonymise des fichiers et dossiers vers un dossier de sortie."""
    from anonyma.pipeline import QUARANTINE_DIR, Pipeline
    from anonyma.report import REPORT_EXTENSIONS, BatchReport

    _check_inputs(inputs)
    if report_path is not None and report_path.suffix.lower() not in (
        *REPORT_EXTENSIONS,
        ".htm",
        ".markdown",
    ):
        fail("Le rapport doit avoir l'extension .json, .md ou .html.")
    config = apply_options(
        load_config(config_file),
        profile=profile,
        mode=mode,
        regime=regime,
        paranoid=paranoid,
        whitelist=whitelist,
        business_lists=business_lists or (),
        workers=workers,
    )
    store = None
    if config.regime is Regime.PSEUDONYMIZATION:
        store = open_store(store_path(config, table), create=True)
    try:
        pipeline = Pipeline(config, store=store)
        with progress_display(not quiet) as progress:
            result = pipeline.run(inputs, output, progress=progress)
    except ValueError as exc:  # e.g. output folder inside the inputs
        fail(str(exc))
    except KeyboardInterrupt:
        fail("Traitement interrompu.", EXIT_PARTIAL)
    finally:
        if store is not None:
            store.close()

    report = BatchReport.from_result(result)
    report.settings = _settings(pipeline.config)
    _print_run_summary(report, result.output_dir, QUARANTINE_DIR)
    if report_path is not None:
        report.save(report_path)
        out.print(f"Rapport enregistré : {report_path}")
    statuses = {r.status for r in result.all_results()}
    raise typer.Exit(EXIT_PARTIAL if statuses & BAD_STATUSES else EXIT_OK)


def _print_run_summary(report: Any, output_dir: Path, quarantine: str) -> None:
    from anonyma.report import STATUS_LABELS

    table = Table(title="Résultat du lot", show_edge=False)
    table.add_column("Statut")
    table.add_column("Fichiers", justify="right")
    for status, n in sorted(report.status_counts.items()):
        table.add_row(STATUS_LABELS.get(DocumentStatus(status), str(status)), str(n))
    out.print(table)
    if report.entity_counts:
        entities = Table(title="Entités masquées", show_edge=False)
        entities.add_column("Type")
        entities.add_column("Nombre", justify="right")
        for entity_type, n in sorted(report.entity_counts.items()):
            entities.add_row(entity_type, str(n))
        out.print(entities)
    for failure in report.failures:
        problems = " ; ".join(failure["problemes"]) or failure["statut"]
        out.print(f"[red]•[/] {_markup_escape(failure['fichier'])} : {_markup_escape(problems)}")
    out.print(f"Dossier de sortie : {output_dir}")
    if report.residues or (output_dir / quarantine).exists():
        out.print(
            f"[yellow]Des fichiers ont été placés en quarantaine dans « {quarantine} » : "
            "ne les diffusez pas.[/]"
        )


# ---------------------------------------------------------------------------
# analyser
# ---------------------------------------------------------------------------


@app.command("analyser")
def analyse(
    inputs: InputsArg,
    profile: ProfileOpt = None,
    paranoid: ParanoidOpt = None,
    whitelist: WhitelistOpt = None,
    business_lists: BusinessOpt = None,
    workers: WorkersOpt = None,
    config_file: ConfigOpt = None,
    as_json: Annotated[bool, typer.Option("--json", help="Résultat au format JSON.")] = False,
    quiet: QuietOpt = False,
) -> None:
    """Analyse à blanc : compte les entités détectées, sans rien écrire."""
    from anonyma.pipeline import Pipeline

    _check_inputs(inputs)
    config = apply_options(
        load_config(config_file),
        profile=profile,
        paranoid=paranoid,
        whitelist=whitelist,
        business_lists=business_lists or (),
        workers=workers,
    )
    pipeline = Pipeline(config)
    try:
        with progress_display(not quiet and not as_json) as progress:
            analysis = pipeline.analyze(inputs, progress=progress)
    except KeyboardInterrupt:
        fail("Analyse interrompue.", EXIT_PARTIAL)
    try:
        summary = analysis_summary(analysis)
    finally:
        pipeline.close(analysis)
    summary["profil"] = pipeline.config.effective_profile().value
    if as_json:
        typer.echo(json.dumps(summary, indent=2, ensure_ascii=False))
    else:
        _print_analysis(summary)
    bad = {"echec", "non_supporte", "annule"}
    raise typer.Exit(EXIT_PARTIAL if bad & set(summary["statuts"]) else EXIT_OK)


def analysis_summary(analysis: BatchAnalysis) -> dict[str, Any]:
    """Counts only (no value, no file name)."""
    statuses: dict[str, int] = {}
    formats: dict[str, int] = {}
    for doc in analysis.all_documents():
        statuses[doc.status.value] = statuses.get(doc.status.value, 0) + 1
        formats[doc.format] = formats.get(doc.format, 0) + 1
    mentions: dict[str, int] = {}
    for entity in analysis.entities:
        mentions[entity.entity_type] = mentions.get(entity.entity_type, 0) + 1
    distinct: dict[str, int] = {}
    for cluster in analysis.clusters.values():
        distinct[cluster.entity_type] = distinct.get(cluster.entity_type, 0) + 1
    return {
        "fichiers": len(analysis.documents),
        "elements": sum(1 for _ in analysis.all_documents()),
        "statuts": statuses,
        "formats": formats,
        "mentions_par_type": dict(sorted(mentions.items())),
        "entites_distinctes_par_type": dict(sorted(distinct.items())),
        "a_revoir": len(analysis.review_clusters()),
    }


def _print_analysis(summary: dict[str, Any]) -> None:
    from anonyma.report import STATUS_LABELS

    out.print(
        f"{summary['fichiers']} fichier(s), {summary['elements']} élément(s) analysé(s) "
        f"(profil {summary['profil']})."
    )
    table = Table(title="Entités détectées", show_edge=False)
    table.add_column("Type")
    table.add_column("Mentions", justify="right")
    table.add_column("Distinctes", justify="right")
    for entity_type, n in summary["mentions_par_type"].items():
        table.add_row(
            entity_type, str(n), str(summary["entites_distinctes_par_type"].get(entity_type, 0))
        )
    if summary["mentions_par_type"]:
        out.print(table)
    else:
        out.print("Aucune entité détectée.")
    statuses = ", ".join(
        f"{STATUS_LABELS.get(DocumentStatus(s), s)} : {n}" for s, n in summary["statuts"].items()
    )
    out.print(f"Statuts : {statuses}")
    if summary["a_revoir"]:
        out.print(f"{summary['a_revoir']} entité(s) de faible confiance seraient à revoir.")


# ---------------------------------------------------------------------------
# reidentifier
# ---------------------------------------------------------------------------


@app.command("reidentifier")
def reidentify(
    inputs: Annotated[
        list[Path] | None,
        typer.Argument(
            help="Fichiers texte pseudonymisés à ré-identifier.",
            metavar="CHEMINS...",
            show_default=False,
        ),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option(
            "--sortie", "-o", help="Dossier de sortie.", metavar="DOSSIER", show_default=False
        ),
    ] = None,
    tags: Annotated[
        list[str] | None,
        typer.Option(
            "--balise", "-b", help="Substitut à rechercher, ex. [PERSONNE_001] ; répétable."
        ),
    ] = None,
    table: TableOpt = None,
    config_file: ConfigOpt = None,
) -> None:
    """Pseudonymisation : retrouve les valeurs d'origine (fichiers texte ou balises)."""
    from anonyma.pipeline import collect_inputs

    if not inputs and not tags:
        fail("Indiquez des fichiers à ré-identifier ou une option --balise.")
    if inputs and output is None:
        fail("L'option --sortie est obligatoire pour ré-identifier des fichiers.")
    if inputs:
        _check_inputs(inputs)
    config = load_config(config_file)
    store = open_store(store_path(config, table), create=False)
    code = EXIT_OK
    try:
        for tag in tags or []:
            original = store.reverse(tag)
            if original is None:
                out.print(f"{_markup_escape(tag)} : [yellow]inconnu dans cette table[/]")
                code = EXIT_PARTIAL
            else:
                out.print(f"{_markup_escape(tag)} → {_markup_escape(original)}")
        if inputs and output is not None:
            mapping = {e.substitute: e.original for e in store.items()}
            items = collect_inputs(inputs)
            _check_reidentify_output(inputs, output)
            if _reidentify_files(items, output, mapping) is not EXIT_OK:
                code = EXIT_PARTIAL
    finally:
        store.close()
    raise typer.Exit(code)


def _check_reidentify_output(inputs: Sequence[Path], output: Path) -> None:
    target = output.resolve()
    for raw in inputs:
        src = raw.resolve()
        folder = src if src.is_dir() else src.parent
        if target == folder or folder in target.parents or (src.is_dir() and src in target.parents):
            fail("Le dossier de sortie doit être distinct des fichiers et dossiers d'origine.")


def substitute_pattern(substitutes: Sequence[str]) -> re.Pattern[str] | None:
    """Regex matching any substitute, longest first, not inside a word."""
    ordered = sorted({s for s in substitutes if s}, key=len, reverse=True)
    if not ordered:
        return None
    alternation = "|".join(re.escape(s) for s in ordered)
    return re.compile(rf"(?<!\w)(?:{alternation})(?!\w)")


def reidentify_text(text: str, mapping: dict[str, str]) -> tuple[str, int]:
    pattern = substitute_pattern(list(mapping))
    if pattern is None:
        return text, 0
    count = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return mapping[match.group(0)]

    return pattern.sub(replace, text), count


def _decode(data: bytes) -> tuple[str, str] | None:
    """(text, encoding to write it back with) or None when not decodable."""
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        encodings: tuple[str, ...] = ("utf-16",)
    elif data[:3] == b"\xef\xbb\xbf":
        encodings = ("utf-8-sig",)
    else:
        encodings = ("utf-8", "cp1252")
    for encoding in encodings:
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return None


def _reidentify_files(items: list[tuple[Path, str]], output: Path, mapping: dict[str, str]) -> int:
    from anonyma.textutils import safe_filename

    code = EXIT_OK
    total = 0
    for index, (path, name) in enumerate(items, start=1):
        if path.suffix.lower() not in REIDENTIFY_EXTENSIONS:
            out.print(f"[yellow]Fichier n°{index} ignoré : format non pris en charge.[/]")
            code = EXIT_PARTIAL
            continue
        decoded = _decode(path.read_bytes())
        if decoded is None:
            out.print(f"[yellow]Fichier n°{index} ignoré : encodage non reconnu.[/]")
            code = EXIT_PARTIAL
            continue
        text, encoding = decoded
        restored, count = reidentify_text(text, mapping)
        parts = [
            safe_filename(reidentify_text(part, mapping)[0])
            for part in name.split("/")
            if part not in ("", ".", "..")
        ]
        destination = output.joinpath(*parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(restored.encode(encoding))
        total += count
    out.print(f"{len(items)} fichier(s) traité(s), {total} substitut(s) remplacé(s).")
    out.print(f"Dossier de sortie : {output}")
    return code


# ---------------------------------------------------------------------------
# modeles
# ---------------------------------------------------------------------------


def _manager() -> Any:
    from anonyma.model_manager import ModelManager

    return ModelManager()


def _status_label(status: Any, partial: int) -> str:
    if status.installed:
        return "[green]installé, vérifié[/]" if status.verified else "[red]installé, altéré[/]"
    if partial:
        from anonyma.hardware import format_bytes

        return f"[yellow]partiel ({format_bytes(partial)})[/]"
    return "non installé"


@models_app.command("liste")
def models_list() -> None:
    """Liste les modèles du catalogue et leur état."""
    from anonyma import hardware

    manager = _manager()
    statuses = manager.statuses()
    if not statuses:
        out.print("Le catalogue de modèles est vide.")
        raise typer.Exit()
    recommended = hardware.recommend_llm(hardware.detect_hardware())
    table = Table(show_edge=False)
    for column in ("Identifiant", "Type", "Titre", "Licence", "Taille", "État"):
        table.add_column(column)
    for status in statuses:
        spec = status.spec
        size = sum(f.size for f in spec.files)
        title = spec.title + (" [cyan](recommandé)[/]" if spec.id == recommended else "")
        table.add_row(
            spec.id,
            spec.kind.value,
            title,
            spec.license,
            hardware.format_bytes(size) if size else "?",
            _status_label(status, manager.partial_bytes(spec.id)),
        )
    out.print(table)


def _spec_or_fail(model_id: str) -> Any:
    from anonyma.model_catalog import get_spec

    spec = get_spec(model_id)
    if spec is None:
        fail(f"Modèle inconnu : {model_id} (voir « anonyma modeles liste »).")
    return spec


@models_app.command("telecharger")
def models_download(
    model_id: Annotated[
        str, typer.Argument(help="Identifiant du modèle.", metavar="MODELE", show_default=False)
    ],
    yes: Annotated[bool, typer.Option("--oui", "-y", help="Ne pas demander confirmation.")] = False,
) -> None:
    """Télécharge un modèle depuis Hugging Face (reprise automatique)."""
    from rich.progress import (
        BarColumn,
        DownloadColumn,
        Progress,
        TextColumn,
        TimeRemainingColumn,
        TransferSpeedColumn,
    )

    from anonyma.hardware import format_bytes
    from anonyma.model_manager import DownloadCancelledError, ModelError

    spec = _spec_or_fail(model_id)
    size = sum(f.size for f in spec.files)
    out.print(
        f"{spec.title} — dépôt {spec.repo_id}, licence {spec.license}, "
        f"{format_bytes(size) if size else 'taille inconnue'}."
    )
    if not yes and not typer.confirm(
        "Autoriser l'accès réseau vers Hugging Face pour ce téléchargement ?", default=True
    ):
        fail("Téléchargement annulé.", EXIT_PARTIAL)
    manager = _manager()
    try:
        with Progress(
            TextColumn("Téléchargement"),
            BarColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
            console=err,
        ) as bar:
            task = bar.add_task("", total=size or None)

            def progress(done: int, total: int) -> None:
                bar.update(task, completed=done, total=total or None)

            path = manager.download(model_id, progress=progress)
    except KeyboardInterrupt:
        fail("Téléchargement interrompu ; relancez la commande pour le reprendre.", EXIT_PARTIAL)
    except DownloadCancelledError:
        fail("Téléchargement annulé ; relancez la commande pour le reprendre.", EXIT_PARTIAL)
    except ModelError as exc:
        fail(str(exc), EXIT_PARTIAL)
    out.print(f"[green]Modèle installé et vérifié :[/] {path}")


@models_app.command("importer")
def models_import(
    model_id: Annotated[
        str, typer.Argument(help="Identifiant du modèle.", metavar="MODELE", show_default=False)
    ],
    files: Annotated[
        list[Path],
        typer.Argument(
            help="Fichiers ou dossier contenant le modèle.",
            metavar="CHEMINS...",
            show_default=False,
        ),
    ],
) -> None:
    """Installe un modèle hors ligne à partir de fichiers copiés (clé USB...)."""
    from anonyma.model_manager import ModelError

    _spec_or_fail(model_id)
    try:
        path = _manager().import_files(model_id, files)
    except ModelError as exc:
        fail(str(exc), EXIT_PARTIAL)
    out.print(f"[green]Modèle importé et vérifié :[/] {path}")


@models_app.command("supprimer")
def models_remove(
    model_id: Annotated[
        str, typer.Argument(help="Identifiant du modèle.", metavar="MODELE", show_default=False)
    ],
    yes: Annotated[bool, typer.Option("--oui", "-y", help="Ne pas demander confirmation.")] = False,
) -> None:
    """Supprime un modèle installé (et ses téléchargements partiels)."""
    _spec_or_fail(model_id)
    if not yes and not typer.confirm(f"Supprimer le modèle « {model_id} » ?", default=False):
        raise typer.Exit(EXIT_OK)
    _manager().remove(model_id)
    out.print(f"Modèle « {model_id} » supprimé.")


# ---------------------------------------------------------------------------
# materiel / formats
# ---------------------------------------------------------------------------


@app.command("materiel")
def hardware_info(config_file: ConfigOpt = None) -> None:
    """Affiche le matériel détecté et le profil recommandé."""
    from anonyma import hardware

    info = hardware.detect_hardware()
    table = Table(show_header=False, show_edge=False)
    table.add_column(style="bold")
    table.add_column()
    for label, value in hardware.summary(info):
        table.add_row(label, value)
    config = load_config(config_file)
    if config.profile is not None:
        table.add_row("Profil imposé", hardware.PROFILE_LABELS[config.profile])
    out.print(table)
    for note in info.notes:
        out.print(f"• {note}")


@app.command("formats")
def formats() -> None:
    """Liste les formats de fichiers pris en charge."""
    from anonyma import registry

    supported = registry.supported_formats()
    extensions: dict[str, list[str]] = {}
    for ext, fmt in sorted(registry.supported_extensions().items()):
        extensions.setdefault(fmt, []).append(ext)
    table = Table(show_edge=False)
    for column in ("Format", "Extensions", "Lecture", "Écriture"):
        table.add_column(column)
    for fmt, (can_read, can_write) in supported.items():
        table.add_row(
            fmt,
            " ".join(extensions.get(fmt, [])),
            "oui" if can_read else "[red]non[/]",
            "oui" if can_write else "[red]non[/]",
        )
    out.print(table)
    if registry.unavailable:
        out.print("Modules indisponibles :")
        for module, reason in sorted(registry.unavailable.items()):
            out.print(f"• {module.rsplit('.', 1)[-1]} : {reason}")


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


@config_app.command("afficher")
def config_show(config_file: ConfigOpt = None) -> None:
    """Affiche la configuration (JSON)."""
    from anonyma import paths

    config = load_config(config_file)
    typer.echo(json.dumps(config.to_dict(), indent=2, ensure_ascii=False, default=str))
    err.print(f"Fichier : {config_file or paths.config_path()}")


_TRUE = {"1", "oui", "o", "vrai", "true", "yes", "on"}
_FALSE = {"0", "non", "n", "faux", "false", "no", "off"}


def parse_setting(current: Any, raw: str, key: str) -> Any:
    """Convert ``raw`` to the type of the current value of ``key``."""
    if key == "profile":
        return None if raw.lower() in {"auto", "", "aucun"} else Profile(raw.lower()).value
    if isinstance(current, enum.Enum):
        return type(current)(raw.lower()).value
    if isinstance(current, bool):
        lowered = raw.strip().lower()
        if lowered in _TRUE:
            return True
        if lowered in _FALSE:
            return False
        raise ValueError("valeur booléenne attendue (oui/non)")
    if isinstance(current, int):
        return int(raw)
    if isinstance(current, float):
        return float(raw.replace(",", "."))
    if isinstance(current, list):
        if raw.strip().startswith("["):
            value = json.loads(raw)
            if not isinstance(value, list):
                raise ValueError("liste JSON attendue")
            return value
        return [part.strip() for part in raw.split(",") if part.strip()]
    if isinstance(current, dict):
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("objet JSON attendu")
        return value
    return raw


@config_app.command("definir")
def config_set(
    key: Annotated[
        str,
        typer.Argument(
            help="Clé, ex. profile, mode, workers, limits.max_files.",
            metavar="CLE",
            show_default=False,
        ),
    ],
    value: Annotated[
        str, typer.Argument(help="Nouvelle valeur.", metavar="VALEUR", show_default=False)
    ],
    config_file: ConfigOpt = None,
) -> None:
    """Modifie une valeur de la configuration et l'enregistre."""
    from anonyma.config import ArchiveLimits, Config

    config = load_config(config_file) if config_file is None or config_file.exists() else Config()
    data = config.to_dict()
    top, _, sub = key.partition(".")
    fields = {f.name for f in dataclasses.fields(Config)}
    limit_fields = {f.name for f in dataclasses.fields(ArchiveLimits)}
    if top not in fields or (sub and (top != "limits" or sub not in limit_fields)):
        known = ", ".join(
            sorted(fields - {"limits"}) + [f"limits.{f}" for f in sorted(limit_fields)]
        )
        fail(f"Clé inconnue : {key}. Clés possibles : {known}.")
    if top == "limits" and not sub:
        fail("Précisez le champ, ex. limits.max_files.")
    container = data["limits"] if sub else data
    name = sub or top
    current = getattr(config.limits, name) if sub else getattr(config, name)
    if name == "profile" or current is not None:
        try:
            container[name] = parse_setting(current, value, name)
        except (ValueError, json.JSONDecodeError) as exc:
            fail(f"Valeur invalide pour {key} : {translate_error(str(exc))}")
    else:
        container[name] = value
    try:
        updated = Config.from_dict(json.loads(json.dumps(data, default=str)))
    except (ValueError, TypeError) as exc:
        fail(f"Valeur invalide pour {key} : {translate_error(str(exc))}")
    updated.save(config_file)
    out.print(f"{key} = {json.dumps(container[name], ensure_ascii=False, default=str)}")


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def main() -> None:
    multiprocessing.freeze_support()
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="replace")
    app()


if __name__ == "__main__":
    main()
