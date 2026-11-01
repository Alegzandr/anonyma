"""Command-line interface (Typer), French only.

Exit codes: 0 every file anonymized (or to review), 1 some file in quarantine,
failed or unsupported, or the operation was interrupted or failed, 2 usage error
(bad option, wrong passphrase, invalid configuration, no console for a prompt).
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

from anonyma.frenchtext import item_progress, plural
from anonyma.models import DocumentStatus, Profile, Regime, ReplacementMode, Severity

if TYPE_CHECKING:
    from anonyma.config import Config
    from anonyma.pipeline import (
        BatchAnalysis,
        BatchResult,
        LlmEstimate,
        Pipeline,
        ProgressEvent,
    )
    from anonyma.report import BatchReport
    from anonyma.store import EncryptedMappingStore

PASSPHRASE_ENV = "ANONYMA_PASSPHRASE"
DEFAULT_STORE_NAME = "table-correspondance.anonyma"

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_USAGE = 2

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


def program_name() -> str:
    """The command as users type it: ``anonyma-cli`` in the portable build (see
    ``packaging/``), ``anonyma`` from sources (``uv run anonyma``)."""
    return "anonyma-cli" if getattr(sys, "frozen", False) else "anonyma"


# ---------------------------------------------------------------------------
# French messages for Typer internals
# ---------------------------------------------------------------------------

#: A quoted token of a Click message: ``'--sortie'`` or, once converted, ``« --sortie »``.
_Q = r"(?:'[^'\n]*'|« [^»\n]* »)"


def _extra_arguments(match: re.Match[str]) -> str:
    extra = match.group(1)
    noun = "Argument inattendu" if len(extra.split()) < 2 else "Arguments inattendus"
    return f"{noun} : {extra}"


#: Enumerations of the configuration: French name of the setting.
_ENUM_SETTINGS = {
    "ReplacementMode": ("mode de remplacement", ReplacementMode),
    "Regime": ("régime", Regime),
    "Profile": ("profil", Profile),
}


def _invalid_choice(match: re.Match[str]) -> str:
    """« 'bidon' is not a valid ReplacementMode » in French, with the valid values."""
    value, name = match.group(1), match.group(2)
    known = _ENUM_SETTINGS.get(name)
    if known is None:
        return f"{value} n'est pas une valeur valide."
    setting, enum_type = known
    choices = ", ".join(str(member.value) for member in enum_type)
    return f"{value} n'est pas une valeur valide pour le {setting} ({choices})."


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
        (r"(\S+) is not a valid ([A-Za-z]+)\.?$", _invalid_choice),
        (r"(\S+) is not a valid ([A-Za-z ]+)\.?$", r"\1 n'est pas une valeur valide."),
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
    name=program_name(),
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


@dataclasses.dataclass(slots=True)
class ProtectionOptions:
    """Options of the global fix (OCR, codes, invisible text, identifiers,
    organizations, names); None = keep the configuration value."""

    ocr: bool | None = None
    opaque_whitelist: Sequence[str] = ()
    organizations: OrganizationChoice | None = None
    organization_whitelist: Path | None = None
    organization_blacklist: Path | None = None
    neutral_names: bool | None = None


def load_config(config_file: Path | None) -> Config:
    from anonyma.config import Config

    if config_file is not None and not config_file.is_file():
        fail(f"Fichier de configuration introuvable : {config_file}")
    try:
        return Config.load(config_file)
    except TypeError:
        # A Python message (« '<=' not supported between... ») tells users nothing.
        fail(f"Configuration invalide : {_type_problem(config_file)}")
    except UnicodeDecodeError:
        fail("Configuration invalide : enregistrez le fichier en UTF-8.")
    except (ValueError, OSError) as exc:
        fail(f"Configuration invalide : {translate_error(str(exc))}")


#: French name of the JSON type of a setting.
_JSON_KINDS = ((bool, "oui/non (true ou false)"), ((int, float), "un nombre"), (str, "un texte"))


def _type_problem(config_file: Path | None) -> str:
    """Which setting of a configuration file has a value of the wrong type."""
    from anonyma import paths
    from anonyma.config import Config

    generic = "une valeur n'a pas le type attendu (nombre, texte, oui/non ou liste)."
    try:
        raw = json.loads((config_file or paths.config_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return generic
    defaults = Config().to_dict()
    if not isinstance(raw, dict):
        return "le fichier doit contenir un objet JSON."
    for scope, values, expected in (
        ("", raw, defaults),
        ("limits.", raw.get("limits") or {}, defaults["limits"]),
    ):
        if not isinstance(values, dict):
            return f"« {scope.rstrip('.') or 'limits'} » doit être un objet JSON."
        for key, value in values.items():
            default = expected.get(key)
            if key not in expected or default is None or value is None:
                continue
            kind = _expected_kind(default)
            if kind and not _same_kind(value, default):
                return f"la valeur de « {scope}{key} » doit être {kind}."
    return generic


def _expected_kind(default: Any) -> str:
    if isinstance(default, list):
        return "une liste"
    if isinstance(default, dict):
        return "un objet JSON"
    for types, kind in _JSON_KINDS:
        if isinstance(default, types):
            return kind
    return ""


def _same_kind(value: Any, default: Any) -> bool:
    if isinstance(default, bool) or isinstance(value, bool):
        return isinstance(value, bool) and isinstance(default, bool)
    if isinstance(default, (int, float)):
        return isinstance(value, (int, float))
    return isinstance(value, type(default))


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


def apply_options(
    config: Config,
    *,
    profile: ProfileChoice | None = None,
    mode: ReplacementMode | None = None,
    regime: Regime | None = None,
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
    if not _interactive():
        # A prompt nobody can answer would hang a script or an agent.
        fail(
            "Phrase secrète requise, mais aucune console interactive pour la saisir : "
            f"définissez la variable d'environnement {PASSPHRASE_ENV}."
        )
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


def confirm_or_fail(question: str, *, default: bool) -> bool:
    """Ask a yes/no question; without a console to answer it, fail at once (code 2)
    instead of reading an empty answer, so that a script never mistakes a skipped
    operation for a done one. Ctrl+C or end of input counts as « non »."""
    if not _interactive():
        fail(
            "Confirmation requise, mais aucune console interactive pour la saisir : "
            "ajoutez l'option --oui."
        )
    try:
        return bool(typer.confirm(question, default=default, err=True))
    except typer.Abort:
        return False


def _interactive() -> bool:
    """Whether a user can answer a prompt (standard input is a terminal)."""
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except (ValueError, OSError):
        return False


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


#: French names of the pipeline stages (progress bar and heartbeat).
STAGE_LABELS = {
    "analyse": "Analyse",
    "langue": "Modèle de langue",
    "ecriture": "Écriture et vérification",
}
_STAGE_DONE = {
    "analyse": "Analyse terminée",
    "langue": "Modèle de langue terminé",
    "ecriture": "Écriture et vérification terminées",
}
#: Seconds between two heartbeat lines when stderr is not a terminal.
HEARTBEAT_SECONDS = 10.0


def _elapsed(seconds: float) -> str:
    """« 45 s », « 3 min 05 s », « 1 h 02 min »."""
    total = int(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours} h {minutes:02d} min"
    if minutes:
        return f"{minutes} min {secs:02d} s"
    return f"{secs} s"


class Heartbeat:
    """Plain progress lines for a stderr that is no terminal (a log, an agent): one
    line at each stage change, at the end of a stage and at most every
    ``interval`` seconds in between (also while one long file shows no progress).
    Counts only: never a value or a file name."""

    def __init__(
        self,
        write: Callable[[str], None],
        interval: float = HEARTBEAT_SECONDS,
        clock: Callable[[], float] | None = None,
    ) -> None:
        import threading
        import time

        self._write = write
        self._interval = interval
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._started = self._clock()
        self._last = self._started
        self._state: tuple[str, int, int] | None = None
        #: Progress inside the current file (``ProgressEvent.item_*``), if any.
        self._item = ""

    def update(self, event: ProgressEvent) -> None:
        with self._lock:
            previous = self._state
            self._state = (event.stage, event.done, event.total)
            if event.note:
                self._write(f"{event.note} ({_elapsed(self._clock() - self._started)})")
            if event.item_total:
                self._item = item_progress(event.item_done, event.item_total, event.item_step)
            elif previous != self._state:
                self._item = ""  # a file finished: its inner progress is over
            now = self._clock()
            changed = previous is None or previous[0] != event.stage
            finished = 0 < event.total <= event.done and previous != self._state
            if changed or finished or now - self._last >= self._interval:
                self._emit(now)

    def tick(self) -> None:
        """Called periodically: repeat the current state once ``interval`` elapsed."""
        with self._lock:
            now = self._clock()
            if self._state is not None and now - self._last >= self._interval:
                self._emit(now, waiting=True)

    def _emit(self, now: float, *, waiting: bool = False) -> None:
        assert self._state is not None
        stage, done, total = self._state
        label = STAGE_LABELS.get(stage, stage)
        elapsed = _elapsed(now - self._started)
        if waiting and total and done >= total:
            # Between two stages (batch-wide resolution, substitutes, report).
            done_label = _STAGE_DONE.get(stage, label)
            self._write(f"{done_label}, étape suivante en préparation… ({elapsed})")
        else:
            files = "fichier" if total < 2 else "fichiers"
            ending = ", terminé" if total and done >= total else "…"
            inner = f" · {self._item}" if self._item and done < total else ""
            self._write(f"{label} : {done}/{total} {files}{ending}{inner} ({elapsed})")
        self._last = now


@contextmanager
def _heartbeat() -> Iterator[Callable[[ProgressEvent], None]]:
    import threading

    def write(line: str) -> None:
        try:
            sys.stderr.write(line + "\n")
            sys.stderr.flush()
        except (OSError, ValueError):
            pass  # stderr closed: progress is best effort

    beat = Heartbeat(write)
    stop = threading.Event()

    def loop() -> None:
        while not stop.wait(1.0):
            beat.tick()

    thread = threading.Thread(target=loop, name="anonyma-heartbeat", daemon=True)
    thread.start()
    try:
        yield beat.update
    finally:
        stop.set()
        thread.join(timeout=2.0)


@contextmanager
def progress_display(enabled: bool) -> Iterator[Callable[[ProgressEvent], None] | None]:
    """Progress on stderr fed by pipeline ``ProgressEvent``s: a Rich bar on a
    terminal, :class:`Heartbeat` lines otherwise (Rich would only print the final
    state of a redirected bar)."""
    if not enabled:
        yield None
        return
    if not err.is_terminal:
        with _heartbeat() as update:
            yield update
        return
    from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn

    labels = STAGE_LABELS
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
            label = labels.get(event.stage, event.stage)
            if event.stage not in tasks:
                tasks[event.stage] = bar.add_task(label, total=event.total)
            if event.item_total and event.done < event.total:
                inner = item_progress(event.item_done, event.item_total, event.item_step)
                label = f"{label} · {inner}"
            bar.update(
                tasks[event.stage], completed=event.done, total=event.total, description=label
            )

        yield update


#: French names of detectors that may be missing (model not installed...).
_DETECTOR_LABELS = {"gliner": "GLiNER", "llm": "modèle de langue"}
#: Detectors whose engine (a library) may be left out of a build, and how to get it.
_ENGINE_MISSING = {
    "gliner": "moteur GLiNER (onnxruntime) absent",
    "llm": "moteur du modèle de langue (llama-cpp-python) absent",
}
_ENGINE_SOURCE_HINT = {"llm": "uv sync --extra llm"}


def _engine_missing(name: str) -> str:
    """Why a detector cannot run here and what would fix it: the portable build does
    not ship llama-cpp-python (it is an optional extra of the sources)."""
    reason = _ENGINE_MISSING[name]
    if getattr(sys, "frozen", False):
        return f"{reason} de la version portable, qui ne permet pas ce détecteur"
    hint = _ENGINE_SOURCE_HINT.get(name)
    return f"{reason} : l'installer avec « {hint} »" if hint else reason


#: Detector each profile adds, so that it is worth choosing.
_PROFILE_DETECTOR = {Profile.STANDARD: "gliner", Profile.FULL: "llm"}


def engine_available(detector: str) -> bool:
    """Whether this build ships the library a model-based detector runs on (a model
    may be installed while its engine is not: llama-cpp-python left out...)."""
    import importlib.util

    modules = {"gliner": ("onnxruntime", "tokenizers"), "llm": ("llama_cpp",)}
    return all(importlib.util.find_spec(m) is not None for m in modules.get(detector, ()))


def runnable_profile(profile: Profile) -> Profile:
    """``profile``, or the most thorough lower one whose engines this build ships."""
    for candidate in (Profile.FULL, Profile.STANDARD):
        if candidate.rank <= profile.rank and engine_available(_PROFILE_DETECTOR[candidate]):
            return candidate
    return Profile.LIGHT


def _missing_detectors(config: Config) -> list[str]:
    """French names of the detectors the profile enables but cannot run."""
    return [_DETECTOR_LABELS.get(name, name) for name in _missing_detector_names(config)]


def _missing_detector_names(config: Config) -> list[str]:
    from anonyma import registry

    return registry.missing_detectors(config, config.effective_profile())


def _missing_reason(name: str) -> str:
    """« GLiNER (modèle non installé) », « modèle de langue (moteur ... version) »."""
    label = _DETECTOR_LABELS.get(name, name)
    return f"{label} ({_unavailable_reason(name)})"


def _unavailable_reason(name: str) -> str:
    """« modèle non installé », or the missing engine and how to get it."""
    if not engine_available(name) and name in _ENGINE_MISSING:
        return _engine_missing(name)
    return "modèle non installé"


def _short_label(label: str) -> str:
    """« Léger — règles et listes » -> « Léger » (GUI labels carry a gloss)."""
    return label.split(" — ", 1)[0]


def _profile_name(profile: Profile) -> str:
    """Short French name of a profile, as in the GUI (« Léger »)."""
    from anonyma.gui.labels import PROFILE_LABELS

    return _short_label(PROFILE_LABELS.get(profile, profile.value))


def _advised_profile() -> Profile:
    """The profile advised for this machine, never one whose engine this build lacks."""
    from anonyma import hardware

    return runnable_profile(hardware.recommend_profile(hardware.detect_hardware()))


def profile_degraded(config: Config) -> bool:
    """Whether the batch runs a lighter detection than the chosen profile (or, in
    "auto", the advised one) because a model or engine is missing: the condition of
    :func:`_profile_warning`, as a machine value (``profil_degrade``)."""
    if config.profile is None:
        return _advised_profile().rank > config.effective_profile().rank
    return bool(_missing_detector_names(config))


def _profile_warning(config: Config) -> str | None:
    """Say when the detection actually run is below the chosen or advised profile."""
    effective = config.effective_profile()
    if config.profile is None:
        advised = _advised_profile()
        if advised.rank <= effective.rank:
            return None
        return (
            f"Profil appliqué : {_profile_name(effective)}. Le profil "
            f"{_profile_name(advised)} conseillé pour cette machine demande des modèles "
            f"non installés (voir « {program_name()} modeles liste »)."
        )
    names = _missing_detector_names(config)
    if not names:
        return None
    without = "ce détecteur" if len(names) < 2 else "ces détecteurs"
    hint = (
        f" (voir « {program_name()} modeles liste »)"
        if any(engine_available(name) for name in names)
        else ""
    )
    return (
        f"Profil {_profile_name(effective)} choisi, mais non disponible : "
        f"{', '.join(_missing_reason(n) for n in names)}. La détection se fait sans "
        f"{without}{hint}."
    )


def _warn_profile(config: Config) -> None:
    warning = _profile_warning(config)
    if warning:
        err.print(f"[bold yellow]Attention :[/] {_markup_escape(warning)}", soft_wrap=True)


def _estimate_llm(pipeline: Pipeline, inputs: Sequence[Path]) -> LlmEstimate | None:
    """FULL only: estimate the language model's work before starting and warn (stderr)
    when it should take more than ``LLM_WARNING_MINUTES``. Counts only."""
    estimate = pipeline.estimate_llm(inputs)
    if estimate is not None and estimate.long:
        err.print(
            f"[bold yellow]Attention :[/] {_markup_escape(estimate.warning('--profil standard'))}",
            soft_wrap=True,
        )
    return estimate


def _add_estimates(
    target: dict[str, Any], estimate: LlmEstimate | None, pipeline: Pipeline
) -> None:
    """``estimation_minutes`` (before the run, from a sample; ``_max``: its upper
    bound) and ``estimation_minutes_modele`` (exact workload once the other
    detectors were done), FULL only."""
    if estimate is not None:
        target["estimation_minutes"] = round(estimate.minutes)
        target["estimation_minutes_max"] = round(estimate.max_minutes)
    if pipeline.llm_workload is not None:
        target["estimation_minutes_modele"] = round(pipeline.llm_workload.minutes)


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
        "OCR": "oui" if config.ocr_enabled else "non",
        "Noms des fichiers": "neutres" if config.neutral_filenames else _done_word(config),
        "Organisations": config.organization_policy.replace("_", " "),
    }


def _done_word(config: Config) -> str:
    """« anonymisés » or, in pseudonymization, « pseudonymisés »."""
    return "pseudonymisés" if config.regime is Regime.PSEUDONYMIZATION else "anonymisés"


def _parameters(config: Config) -> dict[str, Any]:
    """The batch settings of :func:`_settings` as machine values (JSON): the chosen
    profile, the one the installed models and engines actually serve, the detectors
    that ran and those the profile enables but could not run (with the reason)."""
    from anonyma import registry

    chosen = config.effective_profile()
    missing = registry.missing_detectors(config, chosen)
    return {
        "profil": chosen.value,
        "profil_effectif": registry.usable_profile(config, chosen).value,
        "detecteurs": registry.active_detectors(config, chosen),
        "mode": config.mode.value,
        "regime": config.regime.value,
        "ocr": config.ocr_enabled,
        "noms_neutres": config.neutral_filenames,
        "organisations": config.organization_policy,
        "detecteurs_indisponibles": missing,
        "detecteurs_indisponibles_raisons": {name: _unavailable_reason(name) for name in missing},
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
ReplaceOpt = Annotated[
    bool,
    typer.Option(
        "--remplacer",
        help="Supprimer d'abord les fichiers qu'une exécution précédente d'Anonyma a écrits "
        "dans le dossier de sortie et sa quarantaine (s'ils n'ont pas été modifiés depuis) ; "
        "les autres fichiers ne sont jamais touchés.",
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
    "Codes de sortie : 0 tout est anonymisé et vérifié, ou à revoir ; 1 au moins un "
    "fichier en quarantaine, en échec ou non pris en charge, ou traitement interrompu ; "
    "2 erreur d'utilisation. Avec --json, la sortie standard ne contient que le bilan JSON.\n\n"
    "Arborescence produite : un dossier donné garde son nom sous le dossier de sortie "
    "(« run C:/export -o C:/sortie » écrit C:/export/eml/x.eml dans "
    "C:/sortie/export/eml/x.eml), un fichier donné seul est écrit directement dedans "
    "(C:/sortie/lettre.docx) ; un fichier en quarantaine suit la même arborescence sous "
    "C:/sortie_quarantaine. Les noms de fichiers et de "
    "dossiers sont eux-mêmes anonymisés (--noms-neutres : noms neutres). Le bilan --json "
    "donne pour chaque fichier « chemin » et « chemin_relatif »."
)


@app.command("run", help=RUN_HELP)
def run(
    inputs: InputsArg,
    output: OutputOpt,
    report_path: ReportOpt = None,
    as_json: RunJsonOpt = False,
    quiet: QuietOpt = False,
    replace_previous: ReplaceOpt = False,
    neutral_names: NeutralNamesOpt = None,
    profile: ProfileOpt = None,
    whitelist: WhitelistOpt = None,
    business_lists: BusinessOpt = None,
    opaque_whitelist: OpaqueAllowOpt = None,
    mode: ModeOpt = None,
    regime: RegimeOpt = None,
    table: TableOpt = None,
    ocr: OcrOpt = None,
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
        whitelist=whitelist,
        business_lists=business_lists or (),
        workers=workers,
        protection=ProtectionOptions(
            ocr=ocr,
            opaque_whitelist=opaque_whitelist or (),
            organizations=organizations,
            organization_whitelist=organization_whitelist,
            organization_blacklist=organization_blacklist,
            neutral_names=neutral_names,
        ),
    )
    _warn_profile(config)
    store = None
    if config.regime is Regime.PSEUDONYMIZATION:
        store = open_store(store_path(config, table), create=True)
    previous = _prepare_output(output, replace_previous)
    try:
        pipeline = Pipeline(config, store=store)
        estimate = _estimate_llm(pipeline, inputs)
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
    report.parameters = _parameters(pipeline.config)
    report.profile_degraded = profile_degraded(config)  # before "auto" is resolved
    if report_path is not None:
        try:
            report.save(report_path)
        except OSError:
            fail("Impossible d'enregistrer le rapport.", EXIT_PARTIAL)
    summary = run_summary(report, result, pipeline.config, report_path, previous=previous)
    _add_estimates(summary["parametres"], estimate, pipeline)
    if as_json:
        _print_run_summary(err, report, summary)
        typer.echo(json.dumps(summary, indent=2, ensure_ascii=False))
    else:
        _print_run_summary(out, report, summary)
    raise typer.Exit(summary["code_sortie"])


app.command("anonymiser", hidden=True, help="Alias de « run ».")(run)


@dataclasses.dataclass(frozen=True, slots=True)
class OutputState:
    """The output folder before the run: whether it already held something (after
    ``--remplacer``) and how many files of earlier runs were deleted."""

    not_empty: bool = False
    removed: int = 0


def _prepare_output(output: Path, replace_previous: bool) -> OutputState:
    """Apply ``--remplacer`` and warn (stderr) when the output folder is not empty."""
    from anonyma import outputs

    removed = 0
    if replace_previous:
        cleanup = outputs.remove_previous(output)
        removed = cleanup.removed
        if cleanup.removed or cleanup.kept:
            done = "supprimé" if cleanup.removed < 2 else "supprimés"
            message = f"{plural(cleanup.removed, 'fichier')} d'une exécution précédente {done}."
            if cleanup.kept:
                kept = "conservé" if cleanup.kept < 2 else "conservés"
                message += f" {plural(cleanup.kept, 'fichier modifié', 'fichiers modifiés')}"
                message += f" depuis, {kept}."
            err.print(message, markup=False, soft_wrap=True)
    not_empty = outputs.holds_files(output)
    if not_empty:
        advice = (
            "les fichiers qui ne viennent pas d'Anonyma sont conservés"
            if replace_previous
            else "utilisez --remplacer pour supprimer d'abord ce qu'une exécution précédente "
            "y a écrit"
        )
        err.print(
            "[bold yellow]Attention :[/] le dossier de sortie n'est pas vide. Rien n'est "
            "écrasé : un fichier produit sous un nom déjà pris reçoit un suffixe numéroté "
            f"(« (2) ») ; {advice}.",
            soft_wrap=True,
        )
    return OutputState(not_empty, removed)


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
    report: BatchReport,
    result: BatchResult,
    config: Config,
    report_path: Path | None,
    *,
    previous: OutputState | None = None,
) -> dict[str, Any]:
    """Machine-readable outcome of ``run`` (``--json``): codes, counts and paths only,
    never an original value or file name (file names are the anonymized ones).

    The vocabulary is documented in REFERENCE.md (« Automatisation ») and shared with
    the JSON report (:meth:`BatchReport.counts`); ``code_sortie`` is derived from
    ``verdict`` (:meth:`BatchReport.exit_code`)."""
    from anonyma.pipeline import is_quarantined, quarantine_dir
    from anonyma.report import SCHEMA_VERSION, walk_paths

    previous = previous or OutputState()
    quarantine = quarantine_dir(result.output_dir)
    counts = report.counts()
    text, _ = report.verdict()
    documents = []
    for r, record in zip(result.results, report.file_records(), strict=True):
        problems = [
            {"element": path, "code": i.code, "gravite": i.severity.value, "message": i.message}
            for item, path in walk_paths(r)
            for i in item.issues
            if i.severity is not Severity.INFO
        ]
        documents.append(
            {
                **record,
                "quarantaine": is_quarantined(r, result.output_dir),
                "chemin": str(r.output_path) if r.output_path is not None else None,
                "chemin_relatif": _relative_output(r.output_path, result.output_dir, quarantine),
                "problemes": problems,
            }
        )
    in_quarantine = counts["quarantaine"] or quarantine.exists()
    return {
        "schema": SCHEMA_VERSION,
        "verdict": report.verdict_code(),
        "verdict_libelle": text,
        "code_sortie": report.exit_code(),
        "profil_degrade": report.profile_degraded,
        "lot": report.batch_id,
        "regime": report.regime.value,
        # Same counts as nombre_fichiers / nombre_elements (names kept for scripts).
        "fichiers": counts["nombre_fichiers"],
        "elements": counts["nombre_elements"],
        **counts,
        # Older names: residus = certain + possible residues, echecs = items (any
        # depth) listed among the failures.
        "residus": counts["residus_certains"] + counts["residus_possibles"],
        "echecs": len(report.failures),
        "dossier_sortie": str(result.output_dir),
        "dossier_sortie_non_vide": previous.not_empty,
        "sorties_precedentes_supprimees": previous.removed,
        # Also given when the folder exists (left by an earlier batch, for instance).
        "dossier_quarantaine": str(quarantine) if in_quarantine else None,
        "rapport": str(report_path.resolve()) if report_path is not None else None,
        "parametres": _parameters(config),
        "documents": documents,
    }


def _relative_output(path: Path | None, output: Path, quarantine: Path) -> str | None:
    """``export/eml/x.eml``: where a file was written, relative to the output folder
    (or to the quarantine folder for a quarantined one), with ``/`` separators."""
    if path is None:
        return None
    for base in (quarantine, output):
        try:
            return path.resolve().relative_to(base.resolve()).as_posix()
        except ValueError:
            continue
    return None


_VERDICT_STYLES = {"ok": "bold green", "warn": "bold yellow", "ko": "bold red"}


def _print_run_summary(console: Console, report: BatchReport, summary: dict[str, Any]) -> None:
    from anonyma.pipeline import RESIDUE_ISSUE

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
    for document in summary["documents"]:  # quarantined: residues, not failures
        for problem in document["problemes"]:
            if problem["code"] == RESIDUE_ISSUE:
                console.print(
                    f"[red]•[/] {_markup_escape(document['fichier'])} : "
                    f"{_markup_escape(problem['message'])}",
                    soft_wrap=True,
                )
    possible = sum(1 for r in report.residues if r.get("certitude") != "certain")
    if possible:
        found = plural(possible, "résidu possible", "résidus possibles")
        console.print(f"{found} à vérifier avant diffusion (voir le rapport).", style="yellow")
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
    whitelist: WhitelistOpt = None,
    business_lists: BusinessOpt = None,
    opaque_whitelist: OpaqueAllowOpt = None,
    ocr: OcrOpt = None,
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
        whitelist=whitelist,
        business_lists=business_lists or (),
        workers=workers,
        protection=ProtectionOptions(
            ocr=ocr,
            opaque_whitelist=opaque_whitelist or (),
            organizations=organizations,
            organization_whitelist=organization_whitelist,
            organization_blacklist=organization_blacklist,
        ),
    )
    _warn_profile(config)
    pipeline = Pipeline(config)
    estimate = _estimate_llm(pipeline, inputs)
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
    summary["profil_degrade"] = profile_degraded(config)
    _add_estimates(summary, estimate, pipeline)
    if as_json:
        typer.echo(json.dumps(summary, indent=2, ensure_ascii=False))
    else:
        _print_analysis(summary)
    bad = {"echec", "non_supporte", "annule"}
    raise typer.Exit(EXIT_PARTIAL if bad & set(summary["statuts"]) else EXIT_OK)


#: Statuses of an item the analysis could not read.
_UNREAD = frozenset({DocumentStatus.FAILED, DocumentStatus.UNSUPPORTED, DocumentStatus.CANCELLED})


def analysis_summary(analysis: BatchAnalysis) -> dict[str, Any]:
    """Counts, and the items that could not be read under their anonymized names
    (never a value or an original name)."""
    from anonyma.pipeline import display_names
    from anonyma.report import status_label

    unread = [d for d in analysis.all_documents() if d.status in _UNREAD]
    names = display_names(analysis) if unread else {}
    problems = [
        {
            "fichier": names.get(doc.id, "_"),
            "statut": doc.status.value,
            "statut_libelle": status_label(doc.status),
            "problemes": [i.message for i in doc.issues if i.severity is not Severity.INFO],
            "codes": [i.code for i in doc.issues if i.severity is not Severity.INFO],
        }
        for doc in unread
    ]
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
        # Items that could not be read (failed, unsupported, cancelled), any depth.
        "non_analyses": problems,
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
        for item in summary["non_analyses"]:
            reasons = " ; ".join(item["problemes"]) or item["statut_libelle"]
            out.print(
                f"[yellow]•[/] {_markup_escape(item['fichier'])} : {_markup_escape(reasons)}",
                soft_wrap=True,
            )


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
            from anonyma.store import reidentification_map

            mapping = reidentification_map(store.items())
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
        listing.add_column(column, overflow="fold")  # names are never cut
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
    done = ignored = 0

    def skip(name: str, reason: str) -> None:
        # ``name``: a pseudonymized output name (what the user gave), safe to show.
        nonlocal code, ignored
        out.print(f"[yellow]« {_markup_escape(name)} » ignoré : {reason}.[/]", soft_wrap=True)
        code = EXIT_PARTIAL
        ignored += 1

    for path, name in items:
        if path.suffix.lower() not in REIDENTIFY_EXTENSIONS:
            skip(name, "format non pris en charge (fichiers texte seulement)")
            continue
        try:
            data = path.read_bytes()
        except OSError:
            skip(name, "lecture impossible")
            continue
        decoded = _decode(data)
        if decoded is None:
            skip(name, "encodage non reconnu")
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
            skip(name, "écriture impossible")
            continue
        total += count
        done += 1
    files = plural(done, "fichier ré-identifié", "fichiers ré-identifiés")
    substitutes = plural(total, "substitut remplacé", "substituts remplacés")
    line = f"{files}, {substitutes}"
    if ignored:
        line += f" ; {plural(ignored, 'fichier ignoré', 'fichiers ignorés')}"
    out.print(f"{line}.", soft_wrap=True)
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


ModelsJsonOpt = Annotated[
    bool,
    typer.Option(
        "--json",
        help="Écrire la liste en JSON sur la sortie standard (identifiant, type, état, "
        "taille, licence, révision, dossier, moteur disponible).",
    ),
]


def models_json(manager: Any, statuses: Sequence[Any], recommended: str | None) -> list[Any]:
    """Machine-readable ``modeles liste``: one object per catalogue model."""
    from anonyma.model_catalog import ModelKind

    engines = {ModelKind.LLM: "llm", ModelKind.GLINER: "gliner"}
    rows = []
    for status in statuses:
        spec = status.spec
        engine = engines.get(spec.kind)
        usable = engine is None or engine_available(engine)
        rows.append(
            {
                "id": spec.id,
                "type": spec.kind.value,
                "titre": spec.title,
                "installe": status.installed,
                "verifie": status.installed and status.verified,
                "partiel_octets": 0 if status.installed else manager.partial_bytes(spec.id),
                "taille_octets": sum(f.size for f in spec.files),
                "licence": spec.license,
                "revision": spec.revision,
                "depot": spec.repo_id,
                "chemin": str(status.path) if status.installed and status.path else None,
                "conseille": spec.id == recommended,
                "moteur_disponible": usable,
                "moteur_absent": None if usable or engine is None else _engine_missing(engine),
            }
        )
    return rows


@models_app.command("liste")
def models_list(as_json: ModelsJsonOpt = False) -> None:
    """Lister les modèles du catalogue et leur état."""
    from anonyma import hardware

    manager = _manager()
    statuses = manager.statuses()
    recommended = hardware.recommend_llm(hardware.detect_hardware())
    if not engine_available("llm"):
        recommended = None
    if as_json:
        typer.echo(
            json.dumps(models_json(manager, statuses, recommended), indent=2, ensure_ascii=False)
        )
        raise typer.Exit()
    if not statuses:
        out.print("Le catalogue de modèles est vide.")
        raise typer.Exit()
    from anonyma.model_catalog import ModelKind

    engines = {ModelKind.LLM: "llm", ModelKind.GLINER: "gliner"}
    # Identifiers and revisions are typed or copied: never cut. On a narrow terminal
    # the licence and the revision (to download by hand for an offline import) are
    # listed under the table instead of in columns.
    wide = out.width >= WIDE_MODEL_LIST
    table = Table(show_edge=False)
    longest = max(len(s.spec.id) for s in statuses)
    table.add_column("Identifiant", no_wrap=True, min_width=longest)
    table.add_column("Type", no_wrap=True)
    table.add_column("Titre", min_width=14)
    if wide:
        table.add_column("Licence", no_wrap=True)
    table.add_column("Taille", no_wrap=True)
    table.add_column("État", min_width=12)
    if wide:
        table.add_column("Révision", no_wrap=True)
    for status in statuses:
        spec = status.spec
        size = sum(f.size for f in spec.files)
        title = _markup_escape(spec.title)
        if spec.id == recommended:
            title += " [cyan](conseillé)[/]"
        state = _status_label(status, manager.partial_bytes(spec.id))
        engine = engines.get(spec.kind)
        if status.installed and engine and not engine_available(engine):
            state += f" [yellow]({_markup_escape(_engine_missing(engine))})[/]"
        size_text = hardware.format_bytes(size) if size else "?"
        if wide:
            table.add_row(
                spec.id, spec.kind.value, title, spec.license, size_text, state, spec.revision
            )
        else:
            table.add_row(spec.id, spec.kind.value, title, size_text, state)
    out.print(table)
    if not wide:
        out.print("Licence et révision (import hors ligne) :")
        for status in statuses:
            spec = status.spec
            out.print(f"• {spec.id} : {spec.license}, révision {spec.revision}", soft_wrap=True)


#: Terminal width from which ``modeles liste`` shows licences and revisions as columns.
WIDE_MODEL_LIST = 150


def _spec_or_fail(model_id: str) -> Any:
    from anonyma.model_catalog import get_spec

    spec = get_spec(model_id)
    if spec is None:
        fail(f"Modèle inconnu : {model_id} (voir « {program_name()} modeles liste »).")
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
    if not yes and not confirm_or_fail(
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
    if not yes and not confirm_or_fail(f"Supprimer le modèle « {model_id} » ?", default=False):
        fail("Suppression annulée.", EXIT_PARTIAL)
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
    recommended = hardware.recommend_profile(info)
    advised = runnable_profile(recommended)  # never a profile this build cannot run
    rows["Profil recommandé"] = PROFILE_LABELS[advised]
    notes = list(info.notes)
    if not engine_available("llm"):
        rows["Modèle de langage recommandé"] = f"aucun ({_engine_missing('llm')})"
    if advised is not recommended:
        detector = _PROFILE_DETECTOR[recommended]
        notes.append(
            f"Le profil {PROFILE_LABELS[recommended]} conviendrait à cette machine, mais "
            f"{_engine_missing(detector)}."
        )
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
    for note in notes:
        out.print(f"• {_markup_escape(note)}")


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
        # Usage lines and hints show the documented name, whatever argv[0] is
        # (« anonyma-cli.exe », a full path, « python -m anonyma »...).
        app(prog_name=program_name())
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
