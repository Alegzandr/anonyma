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

from anonyma.frenchtext import plural
from anonyma.models import DocumentStatus, Profile, Regime, ReplacementMode

if TYPE_CHECKING:
    from anonyma.config import Config
    from anonyma.pipeline import BatchAnalysis, BatchResult, ProgressEvent
    from anonyma.report import BatchReport
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

#: A quoted token of a Click message: ``'--sortie'`` or, once converted, ``« --sortie »``.
_Q = r"(?:'[^'\n]*'|« [^»\n]* »)"


def _extra_arguments(match: re.Match[str]) -> str:
    extra = match.group(1)
    noun = "Argument inattendu" if len(extra.split()) < 2 else "Arguments inattendus"
    return f"{noun} : {extra}"


_Replacement = str | Callable[[re.Match[str]], str]

#: Typer (which vendors Click with hard-coded English strings) usage errors,
#: translated when displayed. Order matters: first match wins per pattern.
_ERROR_TRANSLATIONS: tuple[tuple[re.Pattern[str], _Replacement], ...] = tuple(
    (re.compile(pattern), replacement)
    for pattern, replacement in (
        (r"Missing option (.+?)\.?$", r"Option obligatoire manquante : \1."),
        (r"Missing argument (.+?)\.?$", r"Argument obligatoire manquant : \1."),
        (r"Missing parameter: (\S+)", r"Paramètre obligatoire manquant : \1"),
        (r"Missing (option|argument|parameter)", r"Paramètre obligatoire manquant"),
        (r"No such option: (\S+)", r"Option inconnue : \1"),
        (rf"No such command ({_Q})\.?", r"Commande inconnue : \1."),
        (r"Missing command\.", r"Commande manquante."),
        (rf"Option ({_Q}) requires an argument\.", r"L'option \1 attend une valeur."),
        (rf"Option ({_Q}) does not take a value\.", r"L'option \1 n'accepte pas de valeur."),
        (r"Got unexpected extra argument\(s\) \((.*)\)", _extra_arguments),
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
        # Paths are not echoed: they may carry a sensitive file name.
        (rf"Path {_Q} does not exist\.", "Chemin introuvable."),
        (rf"(File|Directory|Path) {_Q} (is a directory|is a file)\.", "Chemin invalide."),
    )
)


def translate_error(message: str) -> str:
    for pattern, replacement in _ERROR_TRANSLATIONS:
        message = pattern.sub(replacement, message)
    return message


def translate_usage_error(message: str) -> str:
    """French version of a Click usage error, its ``'x'`` quotes turned into « x »."""
    # Click's English messages hold no apostrophe besides its quotes: converting them
    # before translating never touches the French apostrophes added afterwards.
    quoted = re.sub(r"'([^'\n]*)'", r"« \1 »", message)
    return translate_error(quoted)


#: Help panels of ``run`` and ``analyser`` (in the order of the first option of each).
PANEL_OUTPUT = "Sortie"
PANEL_DETECTION = "Détection"
PANEL_REPLACEMENT = "Remplacement"
PANEL_IMAGES = "Images et PDF"
PANEL_ORGANIZATIONS = "Organisations"
PANEL_MISC = "Divers"


def _option_cells(param: Any, ctx: Any) -> tuple[str, str, str, str, str]:
    """(long names, short names, negative long names, negative short names, metavar)."""
    from typer.core import TyperArgument

    if isinstance(param, TyperArgument):
        return str(param.make_metavar(ctx=ctx)), "", "", "", ""
    longs = ", ".join(o for o in param.opts if o.startswith("--"))
    shorts = ", ".join(o for o in param.opts if not o.startswith("--"))
    negative_longs = ", ".join(o for o in param.secondary_opts if o.startswith("--"))
    negative_shorts = ", ".join(o for o in param.secondary_opts if not o.startswith("--"))
    metavar = "" if param.is_flag or param.count else str(param.make_metavar(ctx=ctx))
    return longs, shorts, negative_longs, negative_shorts, metavar


def _print_options_panel(
    *, name: str, params: Sequence[Any], ctx: Any, markup_mode: Any, console: Console
) -> None:
    """Typer's options panel, laid out so that option names are never truncated: a
    paired flag shows its negative form on a second line instead of in a column of its
    own, and all the panels of a command share the same column widths."""
    import typer.rich_utils as ru
    from rich.panel import Panel
    from rich.text import Text

    if not params:
        return
    visible = [p for p in ctx.command.get_params(ctx) if not getattr(p, "hidden", False)]
    layout = [_option_cells(p, ctx) for p in visible] or [("", "", "", "", "")]
    names_width = max(len(cells[i]) for cells in layout for i in (0, 2))
    shorts_width = max(len(cells[i]) for cells in layout for i in (1, 3))
    metavar_width = max(len(cells[4]) for cells in layout)
    with_required = any(p.required for p in visible)

    table = Table(
        show_header=False, box=None, expand=True, pad_edge=False, padding=(0, 1), highlight=False
    )
    if with_required:
        table.add_column(width=1, no_wrap=True)
    table.add_column(min_width=names_width, overflow="fold")
    if shorts_width:
        table.add_column(min_width=shorts_width, overflow="fold")
    if metavar_width:
        table.add_column(min_width=metavar_width, overflow="fold")
    table.add_column(ratio=1)
    for param in params:
        longs, shorts, negative_longs, negative_shorts, metavar = _option_cells(param, ctx)
        names = ru.highlighter(Text(longs))
        short = ru.highlighter(Text(shorts))
        if negative_longs or negative_shorts:
            names.append("\n").append_text(ru.negative_highlighter(Text(negative_longs)))
            short.append("\n").append_text(ru.negative_highlighter(Text(negative_shorts)))
        row: list[Any] = []
        if with_required:
            marker = Text(ru.REQUIRED_SHORT_STRING, style=ru.STYLE_REQUIRED_SHORT)
            row.append(marker if param.required else "")
        row.append(names)
        if shorts_width:
            row.append(short)
        if metavar_width:
            row.append(Text(metavar, style=ru.STYLE_TYPES))
        row.append(ru._get_parameter_help(param=param, ctx=ctx, markup_mode=markup_mode))
        table.add_row(*row)
    console.print(
        Panel(table, border_style=ru.STYLE_OPTIONS_PANEL_BORDER, title=name, title_align="left")
    )


def _frenchify_typer() -> None:
    """Translate the framework texts (help panels, usage line, errors) to French and lay
    the options out so that they fit a terminal."""
    import typer.rich_utils as ru
    from typer._click import decorators, formatting
    from typer._click.exceptions import ClickException
    from typer.core import TyperArgument

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

    original_metavar = TyperArgument.make_metavar

    def make_metavar(self: TyperArgument, ctx: Any, *, usage: bool = False) -> str:
        # Typer writes a required argument « {CHEMINS...} » in the usage line.
        metavar = original_metavar(self, ctx, usage=usage)
        if metavar.startswith("{") and metavar.endswith("}"):
            return metavar[1:-1]
        return metavar

    TyperArgument.make_metavar = make_metavar  # type: ignore[method-assign]

    original_error = ru.rich_format_error

    def rich_format_error(self: ClickException) -> None:
        format_message = self.format_message
        self.format_message = lambda: translate_usage_error(format_message())  # type: ignore[method-assign]
        original_error(self)

    ru.rich_format_error = rich_format_error

    original_format_help = ru.rich_format_help

    def rich_format_help(*, obj: Any, ctx: Any, markup_mode: Any) -> None:
        # In a command whose options are grouped, --help joins the miscellaneous ones.
        params = obj.get_params(ctx)
        if any(getattr(p, "rich_help_panel", None) for p in params):
            for param in params:
                if param.name == "help" and not getattr(param, "rich_help_panel", None):
                    param.rich_help_panel = PANEL_MISC
        original_format_help(obj=obj, ctx=ctx, markup_mode=markup_mode)

    ru.rich_format_help = rich_format_help
    ru._print_options_panel = _print_options_panel


_frenchify_typer()

CONTEXT = {"help_option_names": ["-h", "--help"]}

SUBCOMMAND = "COMMANDE [ARGUMENTS]..."

app = typer.Typer(
    help="Anonyma : anonymisation locale et en masse de documents.",
    no_args_is_help=True,
    add_completion=False,
    context_settings=CONTEXT,
    rich_markup_mode="rich",
    subcommand_metavar=SUBCOMMAND,
)
models_app = typer.Typer(
    help="Gérer les modèles d'IA (téléchargement, import hors ligne).",
    no_args_is_help=True,
    context_settings=CONTEXT,
    subcommand_metavar=SUBCOMMAND,
)
config_app = typer.Typer(
    help="Afficher ou modifier la configuration.",
    no_args_is_help=True,
    context_settings=CONTEXT,
    subcommand_metavar=SUBCOMMAND,
)
app.add_typer(models_app, name="modeles")
app.add_typer(config_app, name="config")


# ---------------------------------------------------------------------------
# Bootstrap and helpers
# ---------------------------------------------------------------------------

_bootstrapped = False


#: ``ANONYMA_PASSPHRASE`` taken out of the environment by :func:`take_env_passphrase`.
_env_passphrase: str | None = None


def take_env_passphrase() -> None:
    """Move ``ANONYMA_PASSPHRASE`` out of ``os.environ`` before anything else runs, so
    that worker processes and LibreOffice never inherit it: the mapping table is only
    opened by this process, the workers never need its key."""
    global _env_passphrase
    _env_passphrase = os.environ.pop(PASSPHRASE_ENV, None) or None


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
    take_env_passphrase()
    bootstrap()


def fail(message: str, code: int = EXIT_USAGE) -> NoReturn:
    err.print(f"[bold red]Erreur :[/] {_markup_escape(message)}", soft_wrap=True)
    raise typer.Exit(code)


def _markup_escape(text: str) -> str:
    from rich.markup import escape

    return escape(text)


class ProfileChoice(enum.StrEnum):
    AUTO = "auto"
    LIGHT = "leger"
    STANDARD = "standard"
    FULL = "complet"


class OrganizationChoice(enum.StrEnum):
    ALL = "toutes"
    EXCEPT_WHITELIST = "sauf_liste_blanche"
    BLACKLIST = "liste_noire"


class NumberingChoice(enum.StrEnum):
    SEQUENTIAL = "sequentiel"
    RANDOM = "aleatoire"


@dataclasses.dataclass(slots=True)
class ProtectionOptions:
    """Options of the global fix (OCR, codes, invisible text, identifiers,
    organizations, names); None = keep the configuration value."""

    ocr: bool | None = None
    ocr_pages: bool | None = None
    barcodes: bool | None = None
    remove_invisible: bool | None = None
    opaque_ids: bool | None = None
    opaque_whitelist: Sequence[str] = ()
    organizations: OrganizationChoice | None = None
    organization_whitelist: Path | None = None
    organization_blacklist: Path | None = None
    neutral_names: bool | None = None
    numbering: NumberingChoice | None = None


def load_config(config_file: Path | None) -> Config:
    from anonyma.config import Config

    if config_file is not None and not config_file.is_file():
        fail(f"Fichier de configuration introuvable : {config_file}")
    try:
        return Config.load(config_file)
    except (ValueError, TypeError, OSError) as exc:
        fail(f"Configuration invalide : {translate_error(str(exc))}")


def _read_whitelist(path: Path, what: str = "Liste blanche") -> list[str]:
    if not path.is_file():
        fail(f"{what} introuvable : {path}")
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        fail(f"{what} illisible : enregistrez-la en UTF-8, une valeur par ligne.")
    return [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")]


def _apply_protection(config: Config, options: ProtectionOptions) -> None:
    flags = {
        "ocr_enabled": options.ocr,
        "ocr_full_pages": options.ocr_pages,
        "decode_barcodes": options.barcodes,
        "remove_invisible_text": options.remove_invisible,
        "mask_opaque_ids": options.opaque_ids,
        "neutral_filenames": options.neutral_names,
    }
    for name, value in flags.items():
        if value is not None:
            setattr(config, name, value)
    if options.opaque_whitelist:
        config.opaque_id_whitelist = [*config.opaque_id_whitelist, *options.opaque_whitelist]
    if options.organizations is not None:
        config.organization_policy = options.organizations.value
    if options.organization_whitelist is not None:
        config.organization_whitelist = _read_whitelist(
            options.organization_whitelist, "Liste blanche des organisations"
        )
    if options.organization_blacklist is not None:
        config.organization_blacklist = [
            *config.organization_blacklist,
            *_read_whitelist(options.organization_blacklist, "Liste noire des organisations"),
        ]
    if options.numbering is not None:
        config.tag_numbering = options.numbering.value


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
    protection: ProtectionOptions | None = None,
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
    if protection is not None:
        _apply_protection(config, protection)
    try:
        config.validate()
    except ValueError as exc:
        fail(str(exc))
    return config


def _check_inputs(inputs: Sequence[Path]) -> list[Path]:
    if not inputs:
        fail("Indiquez au moins un fichier ou un dossier.")
    for index, path in enumerate(inputs, start=1):
        if not path.exists():
            # The path itself is not echoed: input names may be sensitive.
            fail(f"Fichier ou dossier introuvable (chemin n°{index}).")
    return list(inputs)


def store_path(config: Config, table: Path | None) -> Path:
    from anonyma import paths

    if table is not None:
        return table
    if config.store_path:
        return Path(config.store_path)
    return paths.data_dir() / DEFAULT_STORE_NAME


def read_passphrase(*, confirm: bool) -> str:
    global _env_passphrase
    env = _env_passphrase or os.environ.pop(PASSPHRASE_ENV, None)
    _env_passphrase = None
    if env:
        return env
    try:
        # On stderr: stdout stays free for --json.
        value: str = typer.prompt(
            "Phrase secrète de la table de correspondance",
            hide_input=True,
            confirmation_prompt="Confirmez la phrase secrète" if confirm else False,
            prompt_suffix=" : ",
            err=True,
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
        _print_path(err, "Création d'une nouvelle table de correspondance chiffrée", path)
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


#: French names of detectors that may be missing (model not installed...).
_DETECTOR_LABELS = {"gliner": "GLiNER", "llm": "modèle de langage local"}


def _missing_detectors(config: Config) -> list[str]:
    """French names of the detectors the profile enables but cannot run."""
    from anonyma import registry

    names = registry.missing_detectors(config, config.effective_profile())
    return [_DETECTOR_LABELS.get(name, name) for name in names]


def _short_label(label: str) -> str:
    """« Léger — règles et listes » -> « Léger » (GUI labels carry a gloss)."""
    return label.split(" — ", 1)[0]


def _profile_name(profile: Profile) -> str:
    """Short French name of a profile, as in the GUI (« Léger »)."""
    from anonyma.gui.labels import PROFILE_LABELS

    return _short_label(PROFILE_LABELS.get(profile, profile.value))


def _profile_warning(config: Config) -> str | None:
    """Say when the detection actually run is below the chosen or advised profile."""
    from anonyma import hardware

    effective = config.effective_profile()
    if config.profile is None:
        advised = hardware.recommend_profile(hardware.detect_hardware())
        if advised is effective:
            return None
        return (
            f"Profil appliqué : {_profile_name(effective)}. Le profil "
            f"{_profile_name(advised)} conseillé pour cette machine demande des modèles "
            "non installés (voir « anonyma modeles liste »)."
        )
    missing = _missing_detectors(config)
    if not missing:
        return None
    without = "ce détecteur" if len(missing) < 2 else "ces détecteurs"
    return (
        f"Profil {_profile_name(effective)} choisi, mais non disponible : "
        f"{', '.join(missing)}. La détection se fait sans {without} "
        "(voir « anonyma modeles liste »)."
    )


def _warn_profile(config: Config) -> None:
    warning = _profile_warning(config)
    if warning:
        err.print(f"[bold yellow]Attention :[/] {_markup_escape(warning)}", soft_wrap=True)


def _settings(config: Config) -> dict[str, str]:
    """Non-sensitive batch settings for the report, with the GUI's French labels."""
    from anonyma.gui.labels import MODE_LABELS, REGIME_LABELS

    label = _profile_name(config.effective_profile())
    missing = _missing_detectors(config)
    if missing:
        label += f" — non disponible : {', '.join(missing)}"
    return {
        "Profil": label,
        "Mode": _short_label(MODE_LABELS.get(config.mode, config.mode.value)),
        "Régime": _short_label(REGIME_LABELS.get(config.regime, config.regime.value)),
        "Mode paranoïaque": "oui" if config.paranoid else "non",
        "OCR": "oui" if config.ocr_enabled else "non",
        "Noms des fichiers": "neutres" if config.neutral_filenames else "anonymisés",
        "Organisations": config.organization_policy.replace("_", " "),
    }


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def _check_workers(value: int | None) -> int | None:
    if value is not None and value < 0:
        raise typer.BadParameter("indiquez 0 (automatique) ou un nombre positif.")
    return value


InputsArg = Annotated[
    list[Path],
    typer.Argument(
        help="Fichiers ou dossiers à traiter.", metavar="CHEMINS...", show_default=False
    ),
]
OutputOpt = Annotated[
    Path,
    typer.Option(
        "--sortie",
        "-o",
        help="Dossier de sortie, distinct des originaux. Les fichiers non vérifiés vont dans "
        "le dossier voisin <sortie>_quarantaine.",
        metavar="DOSSIER",
        show_default=False,
        rich_help_panel=PANEL_OUTPUT,
    ),
]
ReportOpt = Annotated[
    Path | None,
    typer.Option(
        "--rapport",
        help="Enregistrer le rapport : .json, .md ou .html.",
        metavar="FICHIER",
        show_default=False,
        rich_help_panel=PANEL_OUTPUT,
    ),
]
RunJsonOpt = Annotated[
    bool,
    typer.Option(
        "--json",
        help="Écrire sur la sortie standard un bilan JSON (statuts, nombres, dossiers, "
        "rapport) ; progression et messages passent sur la sortie d'erreur.",
        rich_help_panel=PANEL_OUTPUT,
    ),
]
AnalysisJsonOpt = Annotated[
    bool,
    typer.Option(
        "--json", help="Résultat au format JSON (nombres seulement).", rich_help_panel=PANEL_OUTPUT
    ),
]
QuietOpt = Annotated[
    bool,
    typer.Option(
        "--silencieux",
        "-q",
        help="Ne pas afficher la progression.",
        rich_help_panel=PANEL_OUTPUT,
    ),
]
NeutralNamesOpt = Annotated[
    bool | None,
    typer.Option(
        "--noms-neutres/--noms-anonymises",
        help="Nommer les fichiers produits document_0001, piece_jointe_0001... au lieu "
        "d'anonymiser les noms d'origine.",
        show_default=False,
        rich_help_panel=PANEL_OUTPUT,
    ),
]
ProfileOpt = Annotated[
    ProfileChoice | None,
    typer.Option(
        "--profil",
        "-p",
        help="Profil de détection : auto (selon le matériel), leger, standard ou complet.",
        metavar="PROFIL",
        case_sensitive=False,
        show_default=False,
        rich_help_panel=PANEL_DETECTION,
    ),
]
ParanoidOpt = Annotated[
    bool | None,
    typer.Option(
        "--paranoiaque/--non-paranoiaque",
        help="Masquer aussi tout nom propre et toute séquence suspecte.",
        show_default=False,
        rich_help_panel=PANEL_DETECTION,
    ),
]
WhitelistOpt = Annotated[
    Path | None,
    typer.Option(
        "--liste-blanche",
        help="Fichier texte de valeurs à ne jamais masquer (une par ligne).",
        metavar="FICHIER",
        show_default=False,
        rich_help_panel=PANEL_DETECTION,
    ),
]
BusinessOpt = Annotated[
    list[Path] | None,
    typer.Option(
        "--liste-metier",
        help="CSV de valeurs à toujours masquer (valeur[,type]) ; répétable.",
        metavar="CSV",
        show_default=False,
        rich_help_panel=PANEL_DETECTION,
    ),
]
OpaqueOpt = Annotated[
    bool | None,
    typer.Option(
        "--masquer-identifiants/--conserver-identifiants",
        help="Masquer les identifiants techniques (empreintes, UUID, références).",
        show_default=False,
        rich_help_panel=PANEL_DETECTION,
    ),
]
OpaqueAllowOpt = Annotated[
    list[str] | None,
    typer.Option(
        "--identifiant-autorise",
        help="Identifiant technique à ne jamais masquer : valeur exacte, ou expression "
        "régulière préfixée par re: (ex. re:^REF-) ; répétable.",
        metavar="VALEUR",
        show_default=False,
        rich_help_panel=PANEL_DETECTION,
    ),
]
ModeOpt = Annotated[
    ReplacementMode | None,
    typer.Option(
        "--mode",
        "-m",
        help="Remplacement : balises ([PERSONNE_001]), realiste (substituts vraisemblables) "
        "ou masquage (caviardage).",
        metavar="MODE",
        show_default=False,
        rich_help_panel=PANEL_REPLACEMENT,
    ),
]
RegimeOpt = Annotated[
    Regime | None,
    typer.Option(
        "--regime",
        "-r",
        help="anonymisation (irréversible, par défaut) ou pseudonymisation (table de "
        "correspondance chiffrée).",
        metavar="REGIME",
        show_default=False,
        rich_help_panel=PANEL_REPLACEMENT,
    ),
]
TableOpt = Annotated[
    Path | None,
    typer.Option(
        "--table",
        help="Table de correspondance chiffrée (pseudonymisation).",
        metavar="FICHIER",
        show_default=False,
        rich_help_panel=PANEL_REPLACEMENT,
    ),
]
NumberingOpt = Annotated[
    NumberingChoice | None,
    typer.Option(
        "--numerotation",
        help="Numérotation des balises : sequentiel ([PERSONNE_001]) ou aleatoire.",
        metavar="TYPE",
        case_sensitive=False,
        show_default=False,
        rich_help_panel=PANEL_REPLACEMENT,
    ),
]
OcrOpt = Annotated[
    bool | None,
    typer.Option(
        "--ocr/--sans-ocr",
        help="Lire le texte des images et des pages numérisées (sans OCR, aucun fichier "
        "contenant des images n'est marqué anonymisé).",
        show_default=False,
        rich_help_panel=PANEL_IMAGES,
    ),
]
OcrPagesOpt = Annotated[
    bool | None,
    typer.Option(
        "--ocr-pages/--sans-ocr-pages",
        help="PDF : lire aussi chaque page entière par OCR (tampons, texte vectorisé).",
        show_default=False,
        rich_help_panel=PANEL_IMAGES,
    ),
]
BarcodesOpt = Annotated[
    bool | None,
    typer.Option(
        "--codes-barres/--sans-codes-barres",
        help="Décoder les QR codes et codes-barres (toujours masqués).",
        show_default=False,
        rich_help_panel=PANEL_IMAGES,
    ),
]
InvisibleOpt = Annotated[
    bool | None,
    typer.Option(
        "--supprimer-texte-invisible/--conserver-texte-invisible",
        help="PDF : supprimer le texte invisible au lieu de seulement l'anonymiser.",
        show_default=False,
        rich_help_panel=PANEL_IMAGES,
    ),
]
OrganizationsOpt = Annotated[
    OrganizationChoice | None,
    typer.Option(
        "--organisations",
        help="Organisations masquées : toutes, sauf_liste_blanche ou liste_noire "
        "(seulement celles de la liste noire).",
        metavar="POLITIQUE",
        case_sensitive=False,
        show_default=False,
        rich_help_panel=PANEL_ORGANIZATIONS,
    ),
]
OrganizationWhitelistOpt = Annotated[
    Path | None,
    typer.Option(
        "--organisations-blanches",
        help="Fichier texte des organisations, domaines et URL publiques à conserver (une "
        "par ligne) ; remplace la liste fournie.",
        metavar="FICHIER",
        show_default=False,
        rich_help_panel=PANEL_ORGANIZATIONS,
    ),
]
OrganizationBlacklistOpt = Annotated[
    Path | None,
    typer.Option(
        "--organisations-noires",
        help="Fichier texte des organisations à toujours masquer (une par ligne).",
        metavar="FICHIER",
        show_default=False,
        rich_help_panel=PANEL_ORGANIZATIONS,
    ),
]
WorkersOpt = Annotated[
    int | None,
    typer.Option(
        "--processus",
        "-j",
        metavar="N",
        help="Nombre de processus (0 = automatique).",
        show_default=False,
        callback=_check_workers,
        rich_help_panel=PANEL_MISC,
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
RunConfigOpt = Annotated[
    Path | None,
    typer.Option(
        "--config",
        help="Fichier de configuration JSON à utiliser.",
        metavar="FICHIER",
        show_default=False,
        rich_help_panel=PANEL_MISC,
    ),
]
#: ``--table`` outside ``run`` (no panels there).
PlainTableOpt = Annotated[
    Path | None,
    typer.Option(
        "--table",
        help="Table de correspondance chiffrée (pseudonymisation).",
        metavar="FICHIER",
        show_default=False,
    ),
]


RUN_HELP = (
    "Anonymiser des fichiers et dossiers vers un dossier de sortie (alias : anonymiser).\n\n"
    "Codes de sortie : 0 tout est anonymisé et vérifié ; 1 au moins un fichier en "
    "quarantaine, en échec ou non pris en charge ; 2 erreur d'utilisation. Avec --json, la "
    "sortie standard ne contient que le bilan JSON."
)


@app.command("run", help=RUN_HELP)
def run(
    inputs: InputsArg,
    output: OutputOpt,
    report_path: ReportOpt = None,
    as_json: RunJsonOpt = False,
    quiet: QuietOpt = False,
    neutral_names: NeutralNamesOpt = None,
    profile: ProfileOpt = None,
    paranoid: ParanoidOpt = None,
    whitelist: WhitelistOpt = None,
    business_lists: BusinessOpt = None,
    opaque_ids: OpaqueOpt = None,
    opaque_whitelist: OpaqueAllowOpt = None,
    mode: ModeOpt = None,
    regime: RegimeOpt = None,
    table: TableOpt = None,
    numbering: NumberingOpt = None,
    ocr: OcrOpt = None,
    ocr_pages: OcrPagesOpt = None,
    barcodes: BarcodesOpt = None,
    remove_invisible: InvisibleOpt = None,
    organizations: OrganizationsOpt = None,
    organization_whitelist: OrganizationWhitelistOpt = None,
    organization_blacklist: OrganizationBlacklistOpt = None,
    workers: WorkersOpt = None,
    config_file: RunConfigOpt = None,
) -> None:
    """Anonymize files and folders into an output folder (help text: ``RUN_HELP``)."""
    from anonyma.pipeline import OutputDirError, Pipeline
    from anonyma.report import REPORT_EXTENSIONS, BatchReport

    _check_inputs(inputs)
    _check_output_dir(inputs, output)
    if report_path is not None and report_path.suffix.lower() not in REPORT_EXTENSIONS:
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
        protection=ProtectionOptions(
            ocr=ocr,
            ocr_pages=ocr_pages,
            barcodes=barcodes,
            remove_invisible=remove_invisible,
            opaque_ids=opaque_ids,
            opaque_whitelist=opaque_whitelist or (),
            organizations=organizations,
            organization_whitelist=organization_whitelist,
            organization_blacklist=organization_blacklist,
            neutral_names=neutral_names,
            numbering=numbering,
        ),
    )
    _warn_profile(config)
    store = None
    if config.regime is Regime.PSEUDONYMIZATION:
        store = open_store(store_path(config, table), create=True)
    try:
        pipeline = Pipeline(config, store=store)
        with progress_display(not quiet) as progress:
            result = pipeline.run(inputs, output, progress=progress)
    except OutputDirError as exc:
        fail(str(exc))
    except KeyboardInterrupt:
        fail("Traitement interrompu.", EXIT_PARTIAL)
    finally:
        if store is not None:
            store.close()

    report = BatchReport.from_result(result)
    report.settings = _settings(pipeline.config)
    if report_path is not None:
        try:
            report.save(report_path)
        except OSError:
            fail("Impossible d'enregistrer le rapport.", EXIT_PARTIAL)
    summary = run_summary(report, result, pipeline.config, report_path)
    if as_json:
        _print_run_summary(err, report, summary)
        typer.echo(json.dumps(summary, indent=2, ensure_ascii=False))
    else:
        _print_run_summary(out, report, summary)
    raise typer.Exit(summary["code_sortie"])


app.command("anonymiser", hidden=True, help="Alias de « run ».")(run)


def _check_output_dir(inputs: Sequence[Path], output: Path) -> None:
    """Refuse an unusable ``--sortie`` before the (long) analysis starts."""
    from anonyma.pipeline import OutputDirError, validate_output_dir

    if output.exists() and not output.is_dir():
        fail("Le dossier de sortie indiqué est un fichier existant.")
    try:
        validate_output_dir(inputs, output)
    except OutputDirError as exc:
        fail(str(exc))


def run_summary(
    report: BatchReport, result: BatchResult, config: Config, report_path: Path | None
) -> dict[str, Any]:
    """Machine-readable outcome of ``run``: codes, counts and paths only (never an
    original value or file name; file names are the anonymized ones)."""
    from anonyma.pipeline import is_quarantined, quarantine_dir

    statuses = {r.status for r in result.all_results()}
    quarantined = report.quarantined
    # Quarantined outputs (residues, incomplete verification) are never a success.
    code = EXIT_PARTIAL if statuses & BAD_STATUSES or quarantined else EXIT_OK
    documents = [
        {
            "fichier": r.output_name,
            "statut": r.status.value,
            "quarantaine": is_quarantined(r, result.output_dir),
            "occurrences": entry["entites"],
            "residus": r.residue_count,
            "a_revoir": r.review_count,
        }
        for r, entry in zip(result.results, report.files, strict=True)
    ]
    return {
        "verdict": report.verdict_code(),
        "code_sortie": code,
        "lot": report.batch_id,
        "fichiers": len(result.results),
        "elements": sum(1 for _ in result.all_results()),
        "statuts": {str(s): n for s, n in sorted(report.status_counts.items())},
        "quarantaine": quarantined,
        "a_revoir": len(report.review),
        "residus": len(report.residues),
        "echecs": len(report.failures),
        "occurrences_par_type": dict(sorted(report.entity_counts.items())),
        "dossier_sortie": str(result.output_dir),
        "dossier_quarantaine": str(quarantine_dir(result.output_dir)) if quarantined else None,
        "rapport": str(report_path.resolve()) if report_path is not None else None,
        "parametres": {
            "profil": config.effective_profile().value,
            "mode": config.mode.value,
            "regime": config.regime.value,
        },
        "documents": documents,
    }


_VERDICT_STYLES = {"ok": "bold green", "warn": "bold yellow", "ko": "bold red"}


def _print_run_summary(console: Console, report: BatchReport, summary: dict[str, Any]) -> None:
    text, level = report.verdict()
    console.print(text, style=_VERDICT_STYLES[level], markup=False, soft_wrap=True)
    if report.entity_counts:
        console.print(_occurrences_table(report.entity_counts, title="Données masquées"))
    for failure in report.failures:
        problems = " ; ".join(failure["problemes"]) or failure["statut"]
        console.print(
            f"[red]•[/] {_markup_escape(failure['fichier'])} : {_markup_escape(problems)}",
            soft_wrap=True,
        )
    _print_path(console, "Dossier de sortie", summary["dossier_sortie"])
    if summary["dossier_quarantaine"]:
        _print_path(
            console, "Quarantaine (ne pas diffuser)", summary["dossier_quarantaine"], "bold red"
        )
    if summary["rapport"]:
        _print_path(console, "Rapport", summary["rapport"])


def _print_path(console: Console, label: str, path: str | Path, style: str = "") -> None:
    """``label : path`` on one line, never wrapped, so the path can be copied."""
    from rich.text import Text

    line = Text(f"{label} : ", style=style)
    line.append(str(path))
    console.print(line, soft_wrap=True)


def _occurrences_table(
    occurrences: dict[str, int], distinct: dict[str, int] | None = None, title: str | None = None
) -> Table:
    """Type (French label), code, [distinct entities,] occurrences."""
    from anonyma.gui.labels import entity_type_label

    table = Table(title=title, show_edge=False)
    table.add_column("Type")
    table.add_column("Code")
    if distinct is not None:
        table.add_column("Entités", justify="right")
    table.add_column("Occurrences", justify="right")
    for entity_type, n in sorted(occurrences.items()):
        row = [entity_type_label(entity_type), entity_type]
        if distinct is not None:
            row.append(str(distinct.get(entity_type, 0)))
        table.add_row(*(_markup_escape(cell) for cell in row), str(n))
    return table


# ---------------------------------------------------------------------------
# analyser
# ---------------------------------------------------------------------------


@app.command("analyser")
def analyse(
    inputs: InputsArg,
    as_json: AnalysisJsonOpt = False,
    quiet: QuietOpt = False,
    profile: ProfileOpt = None,
    paranoid: ParanoidOpt = None,
    whitelist: WhitelistOpt = None,
    business_lists: BusinessOpt = None,
    opaque_ids: OpaqueOpt = None,
    opaque_whitelist: OpaqueAllowOpt = None,
    ocr: OcrOpt = None,
    ocr_pages: OcrPagesOpt = None,
    barcodes: BarcodesOpt = None,
    organizations: OrganizationsOpt = None,
    organization_whitelist: OrganizationWhitelistOpt = None,
    organization_blacklist: OrganizationBlacklistOpt = None,
    workers: WorkersOpt = None,
    config_file: RunConfigOpt = None,
) -> None:
    """Analyser à blanc : compter les entités détectées, sans rien écrire."""
    from anonyma.pipeline import Pipeline

    _check_inputs(inputs)
    config = apply_options(
        load_config(config_file),
        profile=profile,
        paranoid=paranoid,
        whitelist=whitelist,
        business_lists=business_lists or (),
        workers=workers,
        protection=ProtectionOptions(
            ocr=ocr,
            ocr_pages=ocr_pages,
            barcodes=barcodes,
            opaque_ids=opaque_ids,
            opaque_whitelist=opaque_whitelist or (),
            organizations=organizations,
            organization_whitelist=organization_whitelist,
            organization_blacklist=organization_blacklist,
        ),
    )
    _warn_profile(config)
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
    occurrences = dict(sorted(mentions.items()))
    return {
        "fichiers": len(analysis.documents),
        "elements": sum(1 for _ in analysis.all_documents()),
        "statuts": statuses,
        "formats": formats,
        # "mentions_par_type" is kept for existing scripts; "occurrences_par_type" is
        # the same count under the name used everywhere else.
        "mentions_par_type": occurrences,
        "occurrences_par_type": dict(occurrences),
        "entites_distinctes_par_type": dict(sorted(distinct.items())),
        "a_revoir": len(analysis.review_clusters()),
    }


#: Status of an element the analysis could not read -> (singular, plural) wording.
_NOT_ANALYSED = {
    DocumentStatus.FAILED: ("en échec", "en échec"),
    DocumentStatus.UNSUPPORTED: ("non pris en charge", "non pris en charge"),
    DocumentStatus.CANCELLED: ("annulé", "annulés"),
    DocumentStatus.PENDING: ("en attente", "en attente"),
}


def _print_analysis(summary: dict[str, Any]) -> None:
    files = plural(summary["fichiers"], "fichier analysé", "fichiers analysés")
    line = f"{files} à blanc, profil {_profile_name(Profile(summary['profil']))}"
    if summary["elements"] != summary["fichiers"]:
        elements = plural(summary["elements"], "élément")
        line += f" ({elements}, pièces jointes et contenus d'archives compris)"
    out.print(line + ". Rien n'a été écrit.", markup=False, soft_wrap=True)
    if summary["occurrences_par_type"]:
        out.print(
            _occurrences_table(
                summary["occurrences_par_type"],
                summary["entites_distinctes_par_type"],
                title="Entités détectées",
            )
        )
    else:
        out.print("Aucune entité détectée.")
    problems = [
        plural(n, *_NOT_ANALYSED.get(DocumentStatus(s), ("en échec", "en échec")))
        for s, n in summary["statuts"].items()
        if s != DocumentStatus.EXTRACTED
    ]
    if problems:
        out.print("Éléments non analysés : " + " · ".join(problems), style="yellow")
    if summary["a_revoir"]:
        n = summary["a_revoir"]
        verb = "serait" if n < 2 else "seraient"
        out.print(f"{plural(n, 'entité')} de faible confiance {verb} à revoir.")


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
    table: PlainTableOpt = None,
    config_file: ConfigOpt = None,
    names: Annotated[
        bool,
        typer.Option(
            "--noms",
            help="Afficher la correspondance des noms de fichiers neutres "
            "(document_0001...) avec les noms d'origine.",
        ),
    ] = False,
) -> None:
    """Pseudonymisation : retrouver les valeurs d'origine (fichiers texte ou balises)."""
    from anonyma.pipeline import collect_inputs

    if not inputs and not tags and not names:
        fail("Indiquez des fichiers à ré-identifier, une option --balise ou --noms.")
    if inputs and output is None:
        fail("L'option --sortie est obligatoire pour ré-identifier des fichiers.")
    if inputs:
        _check_inputs(inputs)
    config = load_config(config_file)
    store = open_store(store_path(config, table), create=False)
    code = EXIT_OK
    try:
        if names:
            _print_names(store.filenames())
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
            if _reidentify_files(items, output, mapping) != EXIT_OK:
                code = EXIT_PARTIAL
    finally:
        store.close()
    raise typer.Exit(code)


def _print_names(entries: Sequence[Any]) -> None:
    if not entries:
        out.print("Aucun nom de fichier neutre dans cette table.")
        return
    listing = Table(show_edge=False)
    for column in ("Lot", "Nom produit", "Nom d'origine"):
        listing.add_column(column)
    for entry in entries:
        listing.add_row(entry.batch_id, entry.output_name, _markup_escape(entry.original))
    out.print(listing)


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
        try:
            data = path.read_bytes()
        except OSError:
            out.print(f"[yellow]Fichier n°{index} ignoré : lecture impossible.[/]")
            code = EXIT_PARTIAL
            continue
        decoded = _decode(data)
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
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(restored.encode(encoding))
        except OSError:
            out.print(f"[yellow]Fichier n°{index} non écrit : écriture impossible.[/]")
            code = EXIT_PARTIAL
            continue
        total += count
    files = plural(len(items), "fichier traité", "fichiers traités")
    substitutes = plural(total, "substitut remplacé", "substituts remplacés")
    out.print(f"{files}, {substitutes}.", soft_wrap=True)
    _print_path(out, "Dossier de sortie", output)
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
    """Lister les modèles du catalogue et leur état."""
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
        title = spec.title + (" [cyan](conseillé)[/]" if spec.id == recommended else "")
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
    """Télécharger un modèle depuis Hugging Face (reprise automatique)."""
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
    _print_path(out, "Modèle installé et vérifié", path, "green")


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
    """Installer un modèle hors ligne à partir de fichiers copiés (clé USB...)."""
    from anonyma.model_manager import ModelError

    _spec_or_fail(model_id)
    try:
        path = _manager().import_files(model_id, files)
    except ModelError as exc:
        fail(str(exc), EXIT_PARTIAL)
    _print_path(out, "Modèle importé et vérifié", path, "green")


@models_app.command("supprimer")
def models_remove(
    model_id: Annotated[
        str, typer.Argument(help="Identifiant du modèle.", metavar="MODELE", show_default=False)
    ],
    yes: Annotated[bool, typer.Option("--oui", "-y", help="Ne pas demander confirmation.")] = False,
) -> None:
    """Supprimer un modèle installé (et ses téléchargements partiels)."""
    _spec_or_fail(model_id)
    if not yes and not typer.confirm(f"Supprimer le modèle « {model_id} » ?", default=False):
        raise typer.Exit(EXIT_OK)
    from anonyma.model_manager import ModelError

    try:
        _manager().remove(model_id)
    except ModelError as exc:
        fail(str(exc), EXIT_PARTIAL)
    out.print(f"Modèle « {model_id} » supprimé.")


# ---------------------------------------------------------------------------
# materiel / formats
# ---------------------------------------------------------------------------


@app.command("materiel")
def hardware_info(config_file: ConfigOpt = None) -> None:
    """Afficher le matériel détecté et le profil conseillé."""
    from anonyma import hardware
    from anonyma.gui.labels import PROFILE_LABELS

    info = hardware.detect_hardware()
    table = Table(show_header=False, show_edge=False)
    table.add_column(style="bold")
    table.add_column()
    # Same words as the GUI: « conseillé », profile labels of the Settings page.
    rows = {label: value for label, value in hardware.summary(info)}
    rows["Profil recommandé"] = PROFILE_LABELS[hardware.recommend_profile(info)]
    renamed = {
        "Profil recommandé": "Profil conseillé",
        "Modèle de langage recommandé": "Modèle de langue conseillé",
    }
    for label, value in rows.items():
        table.add_row(renamed.get(label, label), _markup_escape(value))
    config = load_config(config_file)
    if config.profile is not None:
        table.add_row("Profil imposé", PROFILE_LABELS[config.profile])
    out.print(table)
    for note in info.notes:
        out.print(f"• {note}")


@app.command("formats")
def formats() -> None:
    """Lister les formats de fichiers pris en charge."""
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
    """Afficher la configuration (JSON)."""
    from anonyma import paths

    config = load_config(config_file)
    typer.echo(json.dumps(config.to_dict(), indent=2, ensure_ascii=False, default=str))
    _print_path(err, "Fichier", config_file or paths.config_path())


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
    """Modifier une valeur de la configuration et l'enregistrer."""
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
    if name == "organization_whitelist" and current is None:
        current = []  # None = the bundled list
    if name == "organization_whitelist" and value.strip().lower() in {"", "defaut", "défaut"}:
        container[name] = None  # back to the bundled list
    elif name == "profile" or current is not None:
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


def force_utf8() -> None:
    """Write UTF-8 on stdout and stderr whatever the console code page, so that output
    redirected to a file or a pipe is never mojibake (Windows defaults to cp1252)."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):  # pragma: no cover - stream already in use
                reconfigure(errors="replace")


def _stdout_closed() -> bool:
    """Whether stdout is a pipe whose reader has gone (Windows reports EINVAL, not EPIPE)."""
    try:
        sys.stdout.flush()
    except OSError:
        return True
    return False


def _silence_stdout() -> None:
    """Point stdout at the null device so that the interpreter's final flush is quiet."""
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
    except (OSError, ValueError, AttributeError):  # pragma: no cover - no real stdout
        pass


def main() -> None:
    multiprocessing.freeze_support()
    force_utf8()
    try:
        app()
    except Exception as exc:
        if isinstance(exc, OSError) and _stdout_closed():
            # ``anonyma ... | head``: the reader left, nothing more to say.
            _silence_stdout()
            raise SystemExit(EXIT_PARTIAL) from None
        # Type only: messages and tracebacks may quote file names or values.
        from anonyma.logs import get_logger

        get_logger("cli").error("unhandled exception: %s", type(exc).__name__)
        err.print(
            f"[bold red]Erreur :[/] erreur inattendue ({type(exc).__name__}). Le journal "
            "(data/logs) indique le type d'erreur, sans aucune valeur sensible."
        )
        raise SystemExit(EXIT_PARTIAL) from None


if __name__ == "__main__":
    main()
