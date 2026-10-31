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
    assert "Profil recommandé" in result.output


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
    assert "Anonymisé" in result.output
    if report_name.endswith(".json"):
        data = json.loads(report.read_text(encoding="utf-8"))
        assert data["entites_par_type"] == {"PERSONNE": 3, "EMAIL": 1}
        assert data["parametres"]["Mode"] == "balises"
    if report_name.endswith(".html"):
        assert report.read_text(encoding="utf-8").startswith("<!DOCTYPE html>")


def test_run_unsupported_exits_1(tmp_path: Path, corpus: Path, business_list: Path) -> None:
    (corpus / "binaire.xyz").write_bytes(b"\x00\x01\x02")
    result = invoke(*_run_args(corpus, tmp_path / "sortie", business_list))
    assert result.exit_code == 1
    assert "Non pris en charge" in result.output


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
    assert "PERSONNE" in result.output
    assert "Mentions" in result.output


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
    assert report["parametres"]["Noms des fichiers"] == "neutres"
    assert report["parametres"]["OCR"] == "non"
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
    def boom() -> None:
        raise RuntimeError("Contrat de Quintavalle.pdf")

    monkeypatch.setattr(cli, "app", boom)
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == cli.EXIT_PARTIAL
    captured = capsys.readouterr()
    assert "RuntimeError" in captured.err
    assert "Quintavalle" not in captured.err + captured.out


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
