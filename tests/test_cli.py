from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from anonyma import cli, model_catalog
from anonyma.config import Config
from anonyma.model_catalog import ModelFile, ModelKind, ModelSpec
from anonyma.model_manager import ModelManager
from anonyma.models import Profile
from tests.canary import CANARY_EMAIL, CANARY_PERSON

runner = CliRunner()
SECRET = "phrase secrète de test"


def invoke(*args: str | Path, env: dict[str, str] | None = None, input: str | None = None) -> Any:
    # Wide help and error panels (Typer's own consoles read COLUMNS).
    env = {"COLUMNS": "250", **(env or {})}
    result = runner.invoke(cli.app, [str(a) for a in args], env=env, input=input)
    if result.exception and not isinstance(result.exception, SystemExit):
        raise result.exception
    return result


@pytest.fixture(autouse=True)
def wide_console(monkeypatch: pytest.MonkeyPatch) -> None:
    """Avoid Rich wrapping table cells at the 80-column default of non-terminals."""
    for console in (cli.out, cli.err):
        monkeypatch.setattr(console, "_width", 250)


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    folder = tmp_path / "entree"
    folder.mkdir()
    (folder / "lettre.txt").write_text(
        f"Bonjour {CANARY_PERSON}, écrivez à {CANARY_EMAIL}.\n", encoding="utf-8"
    )
    (folder / f"note {CANARY_PERSON}.md").write_text(
        f"# Note\n\n{CANARY_PERSON} a validé.\n", encoding="utf-8"
    )
    return folder


@pytest.fixture
def business_list(tmp_path: Path) -> Path:
    path = tmp_path / "liste.csv"
    path.write_text(
        f"valeur;type\n{CANARY_PERSON};PERSONNE\n{CANARY_EMAIL};EMAIL\n", encoding="utf-8"
    )
    return path


def _run_args(corpus: Path, out: Path, business_list: Path, *extra: str | Path) -> list[str | Path]:
    return [
        "run",
        corpus,
        "--sortie",
        out,
        "--profil",
        "leger",
        "--liste-metier",
        business_list,
        "-j",
        "1",
        "-q",
        *extra,
    ]


def _all_text(folder: Path) -> str:
    return "\n".join(
        p.relative_to(folder).as_posix() + "\n" + p.read_text(encoding="utf-8")
        for p in sorted(folder.rglob("*"))
        if p.is_file()
    )


# -- help / misc ------------------------------------------------------------------


def test_help_is_french() -> None:
    result = invoke("--help")
    assert result.exit_code == 0
    assert "Afficher cette aide et quitter." in result.output
    assert "Commandes" in result.output
    assert "Show this message" not in result.output
    result = invoke("run", "-h")
    assert "Utilisation" in result.output


def test_usage_errors_exit_2_in_french(tmp_path: Path) -> None:
    result = invoke("run", tmp_path)
    assert result.exit_code == 2
    assert "Option obligatoire manquante" in result.output
    result = invoke("run", tmp_path, "-o", tmp_path / "o", "--profil", "bidon")
    assert result.exit_code == 2
    assert "ne fait pas partie de" in result.output


def test_translate_error() -> None:
    assert cli.translate_error("No such option: --foo") == "Option inconnue : --foo"
    assert cli.translate_error("Missing argument 'CHEMINS...'.") == (
        "Argument obligatoire manquant : 'CHEMINS...'."
    )


def test_version() -> None:
    result = invoke("--version")
    assert result.exit_code == 0
    assert "Anonyma" in result.output


def test_formats() -> None:
    result = invoke("formats")
    assert result.exit_code == 0
    assert "txt" in result.output
    assert "Lecture" in result.output


def test_materiel() -> None:
    result = invoke("materiel")
    assert result.exit_code == 0
    assert "Mémoire vive" in result.output
    assert "Profil conseillé" in result.output
    assert "recommandé" not in result.output


# -- run ------------------------------------------------------------------------------


@pytest.mark.parametrize("report_name", ["rapport.json", "rapport.md", "rapport.html"])
def test_run_anonymizes(
    tmp_path: Path, corpus: Path, business_list: Path, report_name: str
) -> None:
    out = tmp_path / "sortie"
    report = tmp_path / report_name
    result = invoke(*_run_args(corpus, out, business_list, "--rapport", report))
    assert result.exit_code == 0, result.output
    text = _all_text(out)
    assert "[PERSONNE_001]" in text and "[EMAIL_001]" in text
    for leak in ("uintavalle", "Zéphyrin", "zephyrin"):
        assert leak not in text
        assert leak not in report.read_text(encoding="utf-8")
        assert leak not in result.output
    lines = result.output.splitlines()
    assert lines[0] == "2 fichiers anonymisés et vérifiés."
    assert "Personne" in result.output and "Occurrences" in result.output
    assert f"Rapport : {report.resolve()}" in lines
    assert f"Dossier de sortie : {out.resolve()}" in lines
    if report_name.endswith(".json"):
        data = json.loads(report.read_text(encoding="utf-8"))
        assert data["occurrences_par_type"] == {"EMAIL": 1, "PERSONNE": 3}
        assert data["entites_par_type"] == data["occurrences_par_type"]  # older name
        assert data["parametres"]["mode"] == "balises"
        assert data["parametres_libelles"]["Mode"] == "Balises"
        assert data["verdict"] == "anonymise" and data["code_sortie"] == 0
        assert {f["statut"] for f in data["fichiers"]} == {"anonymise"}  # codes, not labels
    if report_name.endswith(".html"):
        assert report.read_text(encoding="utf-8").startswith("<!DOCTYPE html>")


def test_run_unsupported_exits_1(tmp_path: Path, corpus: Path, business_list: Path) -> None:
    (corpus / "binaire.xyz").write_bytes(b"\x00\x01\x02")
    result = invoke(*_run_args(corpus, tmp_path / "sortie", business_list))
    assert result.exit_code == 1
    assert result.output.splitlines()[0] == "2 anonymisés · 1 non pris en charge"


def test_run_usage_errors(tmp_path: Path, corpus: Path, business_list: Path) -> None:
    assert invoke("run", tmp_path / "absent", "-o", tmp_path / "o").exit_code == 2
    result = invoke(*_run_args(corpus, tmp_path / "o", business_list, "--rapport", "r.pdf"))
    assert result.exit_code == 2
    assert ".json, .md ou .html" in result.output
    result = invoke(*_run_args(corpus, corpus / "sortie", business_list))
    assert result.exit_code == 2
    assert "distinct" in result.output
    result = invoke("run", corpus, "-o", tmp_path / "o", "--liste-metier", tmp_path / "absent.csv")
    assert result.exit_code == 2
    result = invoke("run", corpus, "-o", tmp_path / "o", "--config", tmp_path / "absent.json")
    assert result.exit_code == 2
    assert "introuvable" in result.output


def test_run_whitelist_and_config_file(tmp_path: Path, corpus: Path, business_list: Path) -> None:
    whitelist = tmp_path / "blanche.txt"
    whitelist.write_text(f"# commentaire\n{CANARY_EMAIL}\n", encoding="utf-8")
    config_file = tmp_path / "conf.json"
    Config(profile=Profile.LIGHT, workers=1, mode=cli.ReplacementMode.TAG).save(config_file)
    out = tmp_path / "sortie"
    result = invoke(
        "run",
        corpus / "lettre.txt",
        "-o",
        out,
        "--config",
        config_file,
        "--liste-metier",
        business_list,
        "--liste-blanche",
        whitelist,
        "-q",
    )
    assert result.exit_code == 0, result.output
    text = _all_text(out)
    assert CANARY_EMAIL in text  # whitelisted
    assert "[PERSONNE_001]" in text


# -- analyser ------------------------------------------------------------------------


def test_analyser_json(tmp_path: Path, corpus: Path, business_list: Path) -> None:
    result = invoke(
        "analyser", corpus, "--profil", "leger", "--liste-metier", business_list, "--json"
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["fichiers"] == 2
    assert data["mentions_par_type"] == {"EMAIL": 1, "PERSONNE": 3}
    assert data["entites_distinctes_par_type"] == {"EMAIL": 1, "PERSONNE": 1}
    assert "uintavalle" not in result.output
    assert not any((tmp_path / "home" / "data" / "tmp").iterdir())


def test_missing_models_are_announced(
    monkeypatch: pytest.MonkeyPatch, corpus: Path, business_list: Path
) -> None:
    from anonyma import registry

    gliner = registry.detector_classes()["gliner"]
    monkeypatch.setattr(gliner, "is_available", classmethod(lambda cls, config: False))
    result = invoke("analyser", corpus, "-p", "standard", "--liste-metier", business_list, "-q")
    assert result.exit_code == 0
    assert "non disponible : GLiNER" in " ".join(result.output.split())
    settings = cli._settings(Config(profile=Profile.STANDARD))
    assert settings["Profil"].endswith("non disponible : GLiNER")


def test_analyser_table(corpus: Path, business_list: Path) -> None:
    result = invoke("analyser", corpus, "-p", "leger", "--liste-metier", business_list, "-q")
    assert result.exit_code == 0
    assert "PERSONNE" in result.output and "Personne" in result.output
    assert "Entités" in result.output and "Occurrences" in result.output
    # No raw internal status (« Statuts : Extrait : 2 »).
    assert "Extrait" not in result.output and "Statuts" not in result.output
    assert "profil Léger" in result.output


# -- pseudonymization / reidentifier --------------------------------------------------


def test_pseudonymization_roundtrip(tmp_path: Path, corpus: Path, business_list: Path) -> None:
    table = tmp_path / "table.anonyma"
    env = {cli.PASSPHRASE_ENV: SECRET}
    out = tmp_path / "sortie"
    result = invoke(
        *_run_args(corpus, out, business_list, "--regime", "pseudonymisation", "--table", table),
        env=env,
    )
    assert result.exit_code == 0, result.output
    assert table.is_file()
    assert CANARY_PERSON.encode() not in table.read_bytes()

    # Pseudonymization: new values get random ids (never the size of the table).
    written = next(out.rglob("lettre.txt")).read_text(encoding="utf-8")
    tag = re.search(r"\[PERSONNE_[A-Z2-9]{4}\]", written)
    assert tag is not None
    result = invoke("reidentifier", "--balise", tag.group(), "--table", table, env=env)
    assert result.exit_code == 0
    assert CANARY_PERSON in result.output
    result = invoke("reidentifier", "-b", "[PERSONNE_404]", "--table", table, env=env)
    assert result.exit_code == 1

    restored = tmp_path / "restaure"
    result = invoke("reidentifier", out, "--sortie", restored, "--table", table, env=env)
    assert result.exit_code == 0, result.output
    letter = next(restored.rglob("lettre.txt")).read_text(encoding="utf-8")
    assert letter == f"Bonjour {CANARY_PERSON}, écrivez à {CANARY_EMAIL}.\n"
    assert any(CANARY_PERSON in p.name for p in restored.rglob("*.md"))


def test_pseudonymization_prompts_passphrase(
    tmp_path: Path, corpus: Path, business_list: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(cli.PASSPHRASE_ENV, raising=False)
    monkeypatch.setattr(cli, "_interactive", lambda: True)  # a user at a console
    table = tmp_path / "table.anonyma"
    args = _run_args(corpus, tmp_path / "s1", business_list, "-r", "pseudonymisation")
    result = invoke(*args, "--table", table, input=f"{SECRET}\n{SECRET}\n")
    assert result.exit_code == 0, result.output
    assert SECRET not in result.output
    result = invoke("reidentifier", "-b", "[EMAIL_001]", "--table", table, input="mauvaise\n")
    assert result.exit_code == 2
    assert "incorrecte" in result.output


def test_reidentifier_errors(tmp_path: Path) -> None:
    env = {cli.PASSPHRASE_ENV: SECRET}
    assert invoke("reidentifier", env=env).exit_code == 2
    result = invoke("reidentifier", "-b", "[X]", "--table", tmp_path / "absente", env=env)
    assert result.exit_code == 2
    assert "introuvable" in result.output


def test_reidentify_text_word_boundaries() -> None:
    mapping = {"[PERSONNE_001]": "Alpha", "Jean Martin": "Bravo"}
    text, count = cli.reidentify_text("[PERSONNE_001] et Jean Martin, pas Jean Martinez.", mapping)
    assert text == "Alpha et Bravo, pas Jean Martinez."
    assert count == 2


# -- config --------------------------------------------------------------------------


def test_config_set_and_show(tmp_path: Path) -> None:
    conf = tmp_path / "c.json"
    assert invoke("config", "definir", "profile", "complet", "--config", conf).exit_code == 0
    assert invoke("config", "definir", "workers", "3", "--config", conf).exit_code == 0
    assert invoke("config", "definir", "paranoid", "oui", "--config", conf).exit_code == 0
    assert invoke("config", "definir", "whitelist", "a, b", "--config", conf).exit_code == 0
    assert invoke("config", "definir", "limits.max_files", "10", "--config", conf).exit_code == 0
    assert invoke("config", "definir", "min_score", "0,4", "--config", conf).exit_code == 0
    config = Config.load(conf)
    assert config.profile is Profile.FULL
    assert config.workers == 3
    assert config.paranoid is True
    assert config.whitelist == ["a", "b"]
    assert config.limits.max_files == 10
    assert config.min_score == 0.4
    assert invoke("config", "definir", "profile", "auto", "--config", conf).exit_code == 0
    assert Config.load(conf).profile is None
    result = invoke("config", "afficher", "--config", conf)
    assert result.exit_code == 0
    assert json.loads(result.stdout)["workers"] == 3


def test_config_set_errors(tmp_path: Path) -> None:
    conf = tmp_path / "c.json"
    result = invoke("config", "definir", "inexistant", "1", "--config", conf)
    assert result.exit_code == 2
    assert "Clé inconnue" in result.output
    assert invoke("config", "definir", "workers", "-1", "--config", conf).exit_code == 2
    assert invoke("config", "definir", "workers", "abc", "--config", conf).exit_code == 2
    assert invoke("config", "definir", "mode", "inconnu", "--config", conf).exit_code == 2
    assert invoke("config", "definir", "limits", "1", "--config", conf).exit_code == 2


def test_config_default_location(portable_home: Path) -> None:
    assert invoke("config", "definir", "mode", "realiste").exit_code == 0
    assert (portable_home / "data" / "config.json").is_file()


# -- modeles ---------------------------------------------------------------------------

PAYLOAD = b"poids du modele factice" * 100


@pytest.fixture
def fake_model(monkeypatch: pytest.MonkeyPatch) -> ModelSpec:
    spec = ModelSpec(
        id="modele-test",
        kind=ModelKind.GLINER,
        repo_id="exemple/modele-test",
        revision="f" * 40,
        files=(ModelFile("model.onnx", hashlib.sha256(PAYLOAD).hexdigest(), len(PAYLOAD)),),
        license="apache-2.0",
        title="Modèle de test",
    )
    monkeypatch.setattr(model_catalog, "CATALOG", [spec])
    return spec


def test_models_list_import_remove(tmp_path: Path, fake_model: ModelSpec) -> None:
    result = invoke("modeles", "liste")
    assert result.exit_code == 0
    assert "modele-test" in result.output
    assert "non installé" in result.output
    src = tmp_path / "usb"
    src.mkdir()
    (src / "model.onnx").write_bytes(PAYLOAD)
    result = invoke("modeles", "importer", "modele-test", src)
    assert result.exit_code == 0, result.output
    assert "installé, vérifié" in invoke("modeles", "liste").output
    assert invoke("modeles", "supprimer", "modele-test", "--oui").exit_code == 0
    assert "non installé" in invoke("modeles", "liste").output
    assert invoke("modeles", "importer", "inconnu", src).exit_code == 2
    (src / "model.onnx").write_bytes(b"autre")
    assert invoke("modeles", "importer", "modele-test", src).exit_code == 1


def test_models_download(
    tmp_path: Path, fake_model: ModelSpec, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_model_manager import FakeApi, FakeTransport

    transport = FakeTransport(data={"model.onnx": PAYLOAD})
    monkeypatch.setattr(
        cli,
        "_manager",
        lambda: ModelManager(tmp_path / "m", api=FakeApi(), transport=transport),
    )
    monkeypatch.setattr("tests.test_model_manager.REVISION", fake_model.revision)
    result = invoke("modeles", "telecharger", "modele-test", "--oui")
    assert result.exit_code == 0, result.output
    assert (tmp_path / "m" / "modele-test" / "model.onnx").read_bytes() == PAYLOAD

    monkeypatch.setattr(
        cli,
        "_manager",
        lambda: ModelManager(
            tmp_path / "m2", api=FakeApi(license="cc-by-nc-4.0", tags=[]), transport=transport
        ),
    )
    result = invoke("modeles", "telecharger", "modele-test", "--oui")
    assert result.exit_code == 1
    assert "non commercial" in result.output
    result = invoke("modeles", "telecharger", "modele-test", input="n\n")
    assert result.exit_code == 1


# -- protection options (global fix) --------------------------------------------------


def test_run_protection_options(tmp_path: Path, corpus: Path, business_list: Path) -> None:
    orgs = tmp_path / "organisations.txt"
    orgs.write_text("Éditions Exemplaires\nexemple.test\n", encoding="utf-8")
    out = tmp_path / "sortie"
    result = invoke(
        *_run_args(
            corpus,
            out,
            business_list,
            "--noms-neutres",
            "--sans-ocr",
            "--sans-codes-barres",
            "--conserver-identifiants",
            "--identifiant-autorise",
            "re:^REF-",
            "--organisations",
            "sauf_liste_blanche",
            "--organisations-blanches",
            orgs,
            "--numerotation",
            "aleatoire",
            "--rapport",
            tmp_path / "rapport.json",
        )
    )
    assert result.exit_code == 0, result.output
    assert sorted(p.relative_to(out).as_posix() for p in out.rglob("*.*")) == [
        "dossier_0001/document_0001.txt",
        "dossier_0001/document_0002.md",
    ]
    report = json.loads((tmp_path / "rapport.json").read_text(encoding="utf-8"))
    assert report["parametres"]["noms_neutres"] is True
    assert report["parametres"]["ocr"] is False
    assert report["parametres_libelles"]["Noms des fichiers"] == "neutres"
    assert "uintavalle" not in (tmp_path / "rapport.json").read_text(encoding="utf-8")


def test_apply_protection_options() -> None:
    config = Config()
    cli._apply_protection(
        config,
        cli.ProtectionOptions(
            ocr=False,
            ocr_pages=False,
            barcodes=False,
            remove_invisible=False,
            opaque_ids=False,
            opaque_whitelist=["re:^X"],
            organizations=cli.OrganizationChoice.BLACKLIST,
            neutral_names=True,
            numbering=cli.NumberingChoice.RANDOM,
        ),
    )
    assert not (config.ocr_enabled or config.ocr_full_pages or config.decode_barcodes)
    assert not (config.remove_invisible_text or config.mask_opaque_ids)
    assert config.opaque_id_whitelist == ["re:^X"]
    assert config.organization_policy == "liste_noire"
    assert config.neutral_filenames and config.tag_numbering == "aleatoire"


def test_missing_input_path_is_not_echoed(tmp_path: Path) -> None:
    result = invoke("run", tmp_path / f"CV {CANARY_PERSON}.pdf", "-o", tmp_path / "o")
    assert result.exit_code == 2
    assert "introuvable" in result.output and "uintavalle" not in result.output


def test_config_set_organization_whitelist(tmp_path: Path) -> None:
    conf = tmp_path / "c.json"
    key = "organization_whitelist"
    assert (
        invoke("config", "definir", key, "Exemple, exemple.test", "--config", conf).exit_code == 0
    )
    assert Config.load(conf).organization_whitelist == ["Exemple", "exemple.test"]
    assert invoke("config", "definir", key, "defaut", "--config", conf).exit_code == 0
    assert Config.load(conf).organization_whitelist is None


def test_reidentifier_neutral_names(tmp_path: Path, corpus: Path, business_list: Path) -> None:
    table = tmp_path / "table.anonyma"
    env = {cli.PASSPHRASE_ENV: SECRET}
    args = _run_args(corpus, tmp_path / "sortie", business_list, "--noms-neutres")
    result = invoke(*args, "--regime", "pseudonymisation", "--table", table, env=env)
    assert result.exit_code == 0, result.output
    assert CANARY_PERSON.encode() not in table.read_bytes()
    result = invoke("reidentifier", "--noms", "--table", table, env=env)
    assert result.exit_code == 0, result.output
    assert "document_0002.md" in result.output
    assert f"note {CANARY_PERSON}.md" in result.output


def test_unexpected_error_shows_its_type_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(**kwargs: Any) -> None:
        raise RuntimeError("Contrat de Quintavalle.pdf")

    monkeypatch.setattr(cli, "app", boom)
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == cli.EXIT_PARTIAL
    captured = capsys.readouterr()
    assert "RuntimeError" in captured.err
    assert "Quintavalle" not in captured.err + captured.out


# -- help layout, errors, output encoding (agents and scripts) --------------------------


def _option_names(command: str) -> list[str]:
    import typer.main

    group = typer.main.get_command(cli.app)
    click_command = group.commands[command]  # type: ignore[attr-defined]
    names = [name for p in click_command.params for name in (*p.opts, *p.secondary_opts)]
    return [name for name in names if name.startswith("-")]


@pytest.mark.parametrize("width", [80, 100, 120])
@pytest.mark.parametrize("command", ["run", "analyser"])
def test_help_never_truncates_option_names(command: str, width: int) -> None:
    result = invoke(command, "--help", env={"COLUMNS": str(width)})
    assert result.exit_code == 0
    lines = result.output.splitlines()
    assert all(len(line) <= width for line in lines)
    assert "…" not in result.output
    words = set(result.output.replace("│", " ").split())
    for name in _option_names(command):
        assert name in words, name
    assert "x>=0" not in result.output
    assert "<auto|" not in result.output  # short metavars, choices in the help text


def test_run_help_groups_and_lists_choices() -> None:
    output = invoke("run", "--help").output
    for panel in ("Sortie", "Détection", "Remplacement", "Images et PDF", "Organisations"):
        assert f"─ {panel} ─" in output
    assert "─ Divers ─" in output and "─ Options ─" not in output
    for choice in ("leger", "standard", "complet", "balises", "realiste", "masquage"):
        assert choice in output
    assert "Utilisation : anonyma run [OPTIONS] CHEMINS..." in output  # no « {CHEMINS...} »


def test_commands_help_uses_infinitives() -> None:
    output = invoke("--help").output
    for verb in ("Anonymiser", "Analyser", "Afficher", "Lister", "retrouver"):
        assert verb in output
    assert "COMMANDE [ARGUMENTS]..." in output
    assert "recommandé" not in output
    models = invoke("modeles", "--help").output
    for verb in ("Lister", "Télécharger", "Installer", "Supprimer"):
        assert verb in models


def test_usage_errors_use_french_quotes() -> None:
    result = invoke("run", "x", "-o", "y", "--mode", "bidon")
    assert result.exit_code == 2
    assert "« --mode » / « -m »" in result.output and "« bidon »" in result.output
    result = invoke("run", "x")
    assert "Option obligatoire manquante : « --sortie » / « -o »." in result.output
    result = invoke("run", "x", "-o", "y", "-j", "-2")
    assert result.exit_code == 2
    assert "indiquez 0 (automatique) ou un nombre positif" in result.output
    assert cli.translate_usage_error("Option '-o' requires an argument.") == (
        "L'option « -o » attend une valeur."
    )
    assert cli.translate_usage_error("Got unexpected extra argument(s) (a b)") == (
        "Arguments inattendus : a b"
    )


def test_force_utf8(monkeypatch: pytest.MonkeyPatch) -> None:
    import io

    buffer = io.BytesIO()
    stdout = io.TextIOWrapper(buffer, encoding="cp1252")
    monkeypatch.setattr("sys.stdout", stdout)
    cli.force_utf8()
    assert stdout.encoding == "utf-8"
    stdout.write("Écriture → vérifiée")
    stdout.flush()
    assert buffer.getvalue().decode("utf-8") == "Écriture → vérifiée"


def test_anonymiser_is_an_alias_of_run(tmp_path: Path, corpus: Path, business_list: Path) -> None:
    args = _run_args(corpus, tmp_path / "sortie", business_list)
    result = invoke("anonymiser", *args[1:])
    assert result.exit_code == 0, result.output
    assert "[PERSONNE_001]" in _all_text(tmp_path / "sortie")


# -- run: output folder, quarantine, --json -------------------------------------------


def test_invalid_output_fails_before_the_analysis(
    tmp_path: Path, corpus: Path, business_list: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from anonyma.pipeline import Pipeline

    def no_analysis(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the analysis must not start")

    monkeypatch.setattr(Pipeline, "analyze", no_analysis)
    result = invoke(*_run_args(corpus, corpus / "sortie", business_list))
    assert result.exit_code == 2
    assert "distinct" in result.output
    existing = tmp_path / "fichier.txt"
    existing.write_text("x", encoding="utf-8")
    result = invoke(*_run_args(corpus, existing, business_list))
    assert result.exit_code == 2
    assert "fichier existant" in result.output


def _quarantined_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the pipeline report one clean file and one file in quarantine."""
    from datetime import datetime

    from anonyma.models import DocumentResult, VerificationResult
    from anonyma.pipeline import BatchResult, Pipeline, quarantine_dir

    def fake_run(self: Pipeline, inputs: Any, output_dir: Path, **kwargs: Any) -> BatchResult:
        output_dir = Path(output_dir).resolve()
        quarantine = quarantine_dir(output_dir)
        return BatchResult(
            "lot-test",
            output_dir,
            [
                DocumentResult(
                    "d1",
                    "entree/lettre.txt",
                    "txt",
                    "txt",
                    cli.DocumentStatus.ANONYMIZED,
                    output_path=output_dir / "entree" / "lettre.txt",
                    entity_counts={"PERSONNE": 2},
                ),
                DocumentResult(
                    "d2",
                    "entree/[PERSONNE_001].md",
                    "md",
                    "md",
                    cli.DocumentStatus.RESIDUE,
                    output_path=quarantine / "entree" / "[PERSONNE_001].md",
                    entity_counts={"PERSONNE": 1},
                    residue_count=1,
                    verification=VerificationResult(verified=True),
                ),
            ],
            started_at=datetime(2026, 1, 1, 10, 0, 0),
        )

    monkeypatch.setattr(Pipeline, "run", fake_run)


def test_run_prints_the_quarantine_path(
    tmp_path: Path, corpus: Path, business_list: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from anonyma.pipeline import quarantine_dir

    _quarantined_run(monkeypatch)
    out = tmp_path / "sortie"
    result = invoke(*_run_args(corpus, out, business_list))
    assert result.exit_code == 1
    lines = result.output.splitlines()
    assert lines[0] == "1 anonymisé · 1 en quarantaine (ne pas diffuser)"
    assert f"Quarantaine (ne pas diffuser) : {quarantine_dir(out.resolve())}" in lines


def test_run_json_summary(
    tmp_path: Path, corpus: Path, business_list: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "sortie"
    report = tmp_path / "rapport.md"
    result = invoke(*_run_args(corpus, out, business_list, "--json", "--rapport", report))
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)  # stdout holds the JSON only
    assert data["verdict"] == "anonymise" and data["code_sortie"] == 0
    assert data["fichiers"] == 2 and data["statuts"] == {"anonymise": 2}
    assert data["occurrences_par_type"] == {"EMAIL": 1, "PERSONNE": 3}
    assert data["dossier_sortie"] == str(out.resolve())
    assert data["dossier_quarantaine"] is None
    assert data["rapport"] == str(report.resolve())
    assert data["parametres"]["profil"] == "leger" and data["parametres"]["mode"] == "balises"
    assert data["parametres"]["regime"] == "anonymisation"
    assert {d["statut"] for d in data["documents"]} == {"anonymise"}
    assert "uintavalle" not in result.stdout
    assert "fichiers anonymisés et vérifiés" in result.output  # human summary on stderr

    _quarantined_run(monkeypatch)
    result = invoke(*_run_args(corpus, tmp_path / "s2", business_list, "--json"))
    assert result.exit_code == 1
    data = json.loads(result.stdout)
    assert data["verdict"] == "quarantaine" and data["code_sortie"] == 1
    assert data["quarantaine"] == 1
    assert data["dossier_quarantaine"] == str((tmp_path / "s2_quarantaine").resolve())
    assert [d["quarantaine"] for d in data["documents"]] == [False, True]


def test_run_json_usage_error_leaves_stdout_empty(tmp_path: Path) -> None:
    result = invoke("run", tmp_path / "absent", "-o", tmp_path / "o", "--json")
    assert result.exit_code == 2
    assert result.stdout == ""


def _env_spy(seen: list[str | None], method: Any) -> Any:
    def spy(self: Any, *args: Any, **kwargs: Any) -> Any:
        # Worker processes and LibreOffice inherit this environment.
        seen.append(os.environ.get(cli.PASSPHRASE_ENV))
        return method(self, *args, **kwargs)

    return spy


def test_passphrase_never_reaches_child_processes(
    tmp_path: Path, corpus: Path, business_list: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from anonyma.pipeline import Pipeline

    seen: list[str | None] = []
    monkeypatch.setattr(Pipeline, "run", _env_spy(seen, Pipeline.run))
    table = tmp_path / "table.anonyma"
    args = _run_args(corpus, tmp_path / "sortie", business_list, "-r", "pseudonymisation")
    result = invoke(*args, "--table", table, env={cli.PASSPHRASE_ENV: SECRET})
    assert result.exit_code == 0, result.output
    assert table.is_file()
    # Analysis only, no table opened: the variable is gone as well.
    monkeypatch.setattr(Pipeline, "analyze", _env_spy(seen, Pipeline.analyze))
    result = invoke("analyser", corpus, "-p", "leger", "-q", env={cli.PASSPHRASE_ENV: SECRET})
    assert result.exit_code == 0, result.output
    assert seen == [None, None]


def test_possible_residue_is_to_review_not_quarantine(
    tmp_path: Path, corpus: Path, business_list: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import datetime

    from anonyma.models import DocumentResult, Residue, VerificationResult
    from anonyma.pipeline import BatchResult, Pipeline

    def fake_run(self: Pipeline, inputs: Any, output_dir: Path, **kwargs: Any) -> BatchResult:
        output_dir = Path(output_dir).resolve()
        residue = Residue(
            "entree/scan.pdf", "page 1", "PERSONNE", "detector", "gliner", "x", certain=False
        )
        return BatchResult(
            "lot-test",
            output_dir,
            [
                DocumentResult(
                    "d1",
                    "entree/scan.pdf",
                    "pdf",
                    "pdf",
                    cli.DocumentStatus.NEEDS_REVIEW,
                    output_path=output_dir / "entree" / "scan.pdf",
                    verification=VerificationResult(verified=True, residues=[residue]),
                )
            ],
            started_at=datetime(2026, 1, 1, 10, 0, 0),
        )

    monkeypatch.setattr(Pipeline, "run", fake_run)
    result = invoke(*_run_args(corpus, tmp_path / "sortie", business_list, "--json"))
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["verdict"] == "a_revoir" and data["quarantaine"] == 0
    assert data["dossier_quarantaine"] is None
    assert "1 résidu possible à vérifier avant diffusion" in result.output
    assert "Quarantaine" not in result.output


# -- verdict, exit code and JSON vocabulary (client test, round 2) -------------------


def _zip_input(tmp_path: Path, name: str, data: bytes) -> Path:
    folder = tmp_path / "zips"
    folder.mkdir(exist_ok=True)
    (folder / name).write_bytes(data)
    return folder / name


def test_omitted_member_makes_the_archive_to_review(tmp_path: Path, business_list: Path) -> None:
    from tests.test_archive import NOTE, _encrypt_first_entry, _zip

    data = _encrypt_first_entry(_zip({"secret.txt": b"chiffre", "clair.txt": NOTE}))
    archive = _zip_input(tmp_path, "c.zip", data)
    report = tmp_path / "r.html"
    result = invoke(
        *_run_args(archive, tmp_path / "s", business_list, "--json", "--rapport", report)
    )
    data_json = json.loads(result.stdout)
    # Verdict and exit code agree: a member left out is « À revoir », not « Anonymisé ».
    assert data_json["verdict"] == "a_revoir" and data_json["code_sortie"] == 0
    assert result.exit_code == 0
    assert data_json["resultats"]["a_revoir"] == 1 and data_json["elements_omis"] == 1
    assert data_json["nombre_fichiers"] == 1 and data_json["nombre_elements_inclus"] == 2
    document = data_json["documents"][0]
    assert document["resultat"] == "a_revoir" and document["elements_omis"] == 1
    assert any(p["code"] == "container.incomplete" for p in document["problemes"])
    # The failed member is located in its container.
    assert "c.zip › secret.txt" in result.output
    assert "c.zip › secret.txt" in report.read_text(encoding="utf-8")


def test_archive_with_nothing_kept_fails(tmp_path: Path, business_list: Path) -> None:
    from tests.test_archive import _encrypt_first_entry, _zip

    archive = _zip_input(tmp_path, "e.zip", _encrypt_first_entry(_zip({"s.txt": b"x"})))
    result = invoke(*_run_args(archive, tmp_path / "s", business_list, "--json"))
    data = json.loads(result.stdout)
    assert data["verdict"] == "echec" and data["code_sortie"] == 1 == result.exit_code
    assert result.output.splitlines()[0].replace(" ", " ") == "1 en échec"
    assert not any((tmp_path / "s").rglob("*.zip"))  # no empty shell produced


def test_json_counts_have_one_meaning_each(
    tmp_path: Path, corpus: Path, business_list: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (corpus / "binaire.xyz").write_bytes(b"\x00\x01")
    result = invoke(*_run_args(corpus, tmp_path / "s", business_list, "--json"))
    data = json.loads(result.stdout)
    assert result.exit_code == data["code_sortie"] == 1
    assert data["nombre_fichiers"] == data["fichiers"] == 3
    assert sum(data["resultats"].values()) == data["nombre_fichiers"]
    assert data["resultats"]["non_supporte"] == 1
    unsupported = next(d for d in data["documents"] if d["statut"] == "non_supporte")
    assert unsupported["occurrences"] == 0 and unsupported["chemin"] is None
    assert unsupported["statut_libelle"] == "Non pris en charge"
    assert data["occurrences"] == sum(data["occurrences_par_type"].values()) == 4
    assert data["quarantaine"] == 0 and data["residus_certains"] == 0

    _quarantined_run(monkeypatch)
    data = json.loads(invoke(*_run_args(corpus, tmp_path / "s2", business_list, "--json")).stdout)
    assert data["quarantaine"] == data["resultats"]["quarantaine"] == 1
    assert data["fichiers_a_revoir"] == 0 and data["entites_a_revoir"] == 0


def test_unsupported_file_shows_no_occurrence_count(
    tmp_path: Path, corpus: Path, business_list: Path
) -> None:
    (corpus / f"{CANARY_PERSON}.xyz").write_bytes(b"\x00\x01")
    report = tmp_path / "r.md"
    invoke(*_run_args(corpus, tmp_path / "s", business_list, "--rapport", report))
    line = next(x for x in report.read_text(encoding="utf-8").splitlines() if ".xyz" in x)
    assert "| Non pris en charge | — |" in line


def test_pseudonymized_outputs_are_called_so(
    tmp_path: Path, corpus: Path, business_list: Path
) -> None:
    table = tmp_path / "t.anonyma"
    report = tmp_path / "r.html"
    args = _run_args(corpus, tmp_path / "s", business_list, "-r", "pseudonymisation")
    result = invoke(*args, "--table", table, "--rapport", report, env={cli.PASSPHRASE_ENV: SECRET})
    assert result.exit_code == 0, result.output
    assert "2 fichiers pseudonymisés et vérifiés." in result.output.splitlines()
    html = report.read_text(encoding="utf-8")
    assert "Pseudonymisé" in html and "anonymisés et vérifiés" not in html


def test_rerun_into_a_used_folder_warns_and_replace_removes_only_our_files(
    tmp_path: Path, corpus: Path, business_list: Path
) -> None:
    out = tmp_path / "s"
    assert invoke(*_run_args(corpus, out, business_list)).exit_code == 0
    first = sorted(p.relative_to(out) for p in out.rglob("*") if p.is_file())
    (out / "notes-perso.txt").write_text("à garder", encoding="utf-8")

    result = invoke(*_run_args(corpus, out, business_list, "--json"))
    assert "le dossier de sortie n'est pas vide" in result.output
    assert json.loads(result.stdout)["dossier_sortie_non_vide"] is True
    assert any(" (2)" in p.name for p in out.rglob("*"))

    result = invoke(*_run_args(corpus, out, business_list, "--json", "--remplacer"))
    data = json.loads(result.stdout)
    assert data["sorties_precedentes_supprimees"] == 4
    assert sorted(p.relative_to(out) for p in out.rglob("*") if p.is_file()) == sorted(
        [*first, Path("notes-perso.txt")]
    )
    assert (out / "notes-perso.txt").read_text(encoding="utf-8") == "à garder"


def test_messages_hold_no_internal_code_or_english(tmp_path: Path, business_list: Path) -> None:
    folder = tmp_path / "e"
    folder.mkdir()
    (folder / "vide.docx").write_bytes(b"")
    (folder / "abime.docx").write_bytes(b"PK\x03\x04 pas un document")
    (folder / "inconnu.xyz").write_bytes(b"\x00\x01")
    report = tmp_path / "r.json"
    result = invoke(*_run_args(folder, tmp_path / "s", business_list, "--rapport", report))
    text = result.output + report.read_text(encoding="utf-8").split('"echecs"')[1]
    assert "vide.docx : Échec de la lecture du fichier : fichier vide." in result.output
    assert "fichier endommagé ou tronqué" in result.output
    for internal in ("BadZipFile", "format.unknown", "extract.error :", "Error"):
        assert internal not in result.output, internal
    assert "format.unknown :" not in text


def test_invalid_configuration_is_explained_in_french(tmp_path: Path, corpus: Path) -> None:
    config_file = tmp_path / "c.json"
    config_file.write_text('{"min_score": "0,5"}', encoding="utf-8")
    result = invoke("analyser", corpus, "--config", config_file)
    assert result.exit_code == 2
    assert "la valeur de « min_score » doit être un nombre" in " ".join(result.output.split())
    assert "not supported" not in result.output
    config_file.write_text('{"mode": "bidon"}', encoding="utf-8")
    result = invoke("analyser", corpus, "--config", config_file)
    assert result.exit_code == 2
    output = " ".join(result.output.split())
    assert "mode de remplacement (balises, realiste, masquage)" in output
    assert "ReplacementMode" not in output
    result = invoke("config", "definir", "regime", "bidon", "--config", tmp_path / "n.json")
    assert "Regime" not in result.output and "régime" in result.output


def test_missing_language_engine_is_named_precisely(
    monkeypatch: pytest.MonkeyPatch, corpus: Path, business_list: Path
) -> None:
    from anonyma import hardware, registry

    llm = registry.detector_classes()["llm"]
    monkeypatch.setattr(llm, "is_available", classmethod(lambda cls, config: False))
    real = cli.engine_available
    monkeypatch.setattr(cli, "engine_available", lambda name: name != "llm" and real(name))
    result = invoke("analyser", corpus, "-p", "complet", "--liste-metier", business_list, "-q")
    output = " ".join(result.output.split())
    assert "moteur du modèle de langue non disponible dans cette version" in output
    assert "modèle de langue (modèle non installé)" not in output

    monkeypatch.setattr(hardware, "recommend_profile", lambda info: Profile.FULL)
    output = " ".join(invoke("materiel").output.split())
    assert "Profil conseillé Complet" not in output
    assert "moteur du modèle de langue non disponible dans cette version" in output


def test_usage_shows_the_documented_program_name(monkeypatch: pytest.MonkeyPatch) -> None:
    assert cli.program_name() == "anonyma"
    monkeypatch.setattr("sys.frozen", True, raising=False)
    assert cli.program_name() == "anonyma-cli"
    help_text = " ".join(invoke("run", "--help").output.split())
    assert "interrompu" in help_text


def test_passphrase_without_console_fails_at_once(
    tmp_path: Path, corpus: Path, business_list: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(cli.PASSPHRASE_ENV, raising=False)
    monkeypatch.setattr(cli, "_interactive", lambda: False)
    args = _run_args(corpus, tmp_path / "s", business_list, "-r", "pseudonymisation")
    result = invoke(*args, "--table", tmp_path / "t.anonyma", "--json")
    assert result.exit_code == 2 and result.stdout == ""
    assert cli.PASSPHRASE_ENV in result.output
    assert not (tmp_path / "s").exists()


def test_reidentifier_restores_the_exact_text_and_names_ignored_files(
    tmp_path: Path, business_list: Path
) -> None:
    folder = tmp_path / "e"
    folder.mkdir()
    texts = {
        "a.txt": "Signé Zéphyrin Quintavalle.\n",
        "b.txt": "Signé ZÉPHYRIN QUINTAVALLE.\n",  # another form of the same person
    }
    for name, text in texts.items():
        (folder / name).write_text(text, encoding="utf-8")
    table = tmp_path / "t.anonyma"
    env = {cli.PASSPHRASE_ENV: SECRET}
    out = tmp_path / "s"
    args = _run_args(folder, out, business_list, "-r", "pseudonymisation", "-m", "realiste")
    assert invoke(*args, "--table", table, env=env).exit_code == 0
    (out / "e" / "image.png").write_bytes(b"PNG image")
    result = invoke("reidentifier", out, "-o", tmp_path / "r", "--table", table, env=env)
    assert result.exit_code == 1
    for name, text in texts.items():  # as written, not the canonical form
        assert (tmp_path / "r" / "s" / "e" / name).read_text(encoding="utf-8") == text
    output = " ".join(result.output.split())
    assert "« s/e/image.png » ignoré" in output
    assert "2 fichiers ré-identifiés" in output and "1 fichier ignoré" in output


@pytest.mark.parametrize("width", [80, 200])
def test_models_list_never_cuts_identifiers(monkeypatch: pytest.MonkeyPatch, width: int) -> None:
    monkeypatch.setattr(cli.out, "_width", width)
    output = invoke("modeles", "liste").output
    for spec in model_catalog.CATALOG:
        assert spec.id in output.split(), spec.id
        assert spec.revision in output  # needed for an offline import
    assert "…" not in output
