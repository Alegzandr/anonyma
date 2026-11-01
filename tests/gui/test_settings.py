from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QTableWidgetItem

from anonyma import paths
from anonyma.config import Config
from anonyma.gui import theme
from anonyma.gui.context import AppContext
from anonyma.gui.main_window import MainWindow
from anonyma.gui.pages.settings import validate_patterns
from anonyma.models import Profile, Regime, ReplacementMode


def _select(combo, value) -> None:  # type: ignore[no-untyped-def]
    combo.setCurrentIndex(combo.findData(value))


def test_settings_round_trip(window: MainWindow, tmp_path: Path) -> None:
    page = window.settings
    assert not page.dirty
    _select(page.mode, ReplacementMode.BLACKOUT.value)
    _select(page.regime, Regime.PSEUDONYMIZATION.value)
    page.store_path.setText(str(tmp_path / "table.db"))
    page.whitelist.setPlainText("Paris\n\n  Lyon  \n")
    page._add_term_row("Projet Hélios", "projet")
    csv = tmp_path / "clients.csv"
    csv.write_text("valeur;type\nDupont SA;ORGANISATION\nMartin;PERSONNE\n", encoding="utf-8")
    assert page.import_business_list(csv)
    page._add_row(page.custom_labels, "nom de projet", "PROJET")
    page.case_patterns.setPlainText(r"DOS-\d{4}")
    page.check_updates.setChecked(False)
    assert page.dirty
    assert page.save()
    assert not page.dirty

    saved = Config.load(window.context.config_path)
    assert saved.mode is ReplacementMode.BLACKOUT
    assert saved.regime is Regime.PSEUDONYMIZATION
    assert saved.store_path == str(tmp_path / "table.db")
    assert saved.whitelist == ["Paris", "Lyon"]
    assert saved.always_mask == [{"value": "Projet Hélios", "type": "PROJET"}]
    assert len(saved.business_lists) == 1
    copied = Path(saved.business_lists[0])
    assert copied.parent == paths.data_dir() / "listes" and copied.exists()
    assert ("Dupont SA", "ORGANISATION") in saved.mask_terms()
    assert saved.custom_labels == {"nom de projet": "PROJET"}
    assert saved.case_number_patterns == [r"DOS-\d{4}"]
    assert not saved.check_updates

    # Reloading the page from the saved configuration shows the same values.
    page.load()
    assert page.mode.currentData() == ReplacementMode.BLACKOUT.value
    assert page.business_lists.count() == 1
    assert "2 termes" in page.business_lists.item(0).text()

    # Removing the list deletes the portable copy on save.
    page.business_lists.setCurrentRow(0)
    page.business_lists.item(0).setSelected(True)
    page._remove_list()
    assert page.save()
    assert not copied.exists()


def test_invalid_settings_are_refused(window: MainWindow) -> None:
    page = window.settings
    before = (
        window.context.config_path.read_text("utf-8") if window.context.config_path.exists() else ""
    )
    page.case_patterns.setPlainText("DOS-(\\d{4}")
    assert "invalide" in page.pattern_status.text()
    assert not page.save()
    assert "Numéros de dossier" in page.banner.text()
    page.case_patterns.setPlainText("")
    page.load()
    page._add_row(page.custom_labels, "étiquette", "type invalide !")
    page.custom_labels.setItem(0, 1, QTableWidgetItem("!!"))
    assert not page.save()
    after = (
        window.context.config_path.read_text("utf-8") if window.context.config_path.exists() else ""
    )
    assert before == after


def test_theme_choice_applies_and_persists(window: MainWindow) -> None:
    page = window.settings
    _select(page.theme, "sombre")
    assert theme.is_dark()
    assert Config.load(window.context.config_path).ui["theme"] == "sombre"
    _select(page.theme, "clair")
    assert not theme.is_dark()


def test_validate_patterns() -> None:
    errors = validate_patterns([r"\d+", "[a-z"])
    assert [line for line, _ in errors] == [2]
    assert errors[0][1].startswith("ligne 2 : expression invalide")


def test_policy_options_round_trip(window: MainWindow) -> None:
    from anonyma.policy import default_whitelist

    page = window.settings
    # Defaults: bundled organization whitelist shown, stored as None (follows updates).
    assert page.organization_whitelist.toPlainText().splitlines() == list(default_whitelist())
    assert page.organization_policy.currentData() == "sauf_liste_blanche"

    page.opaque_id_whitelist.setPlainText("ABCD-1234-EFGH-5678\n" + r"re:FACT-\d{6}" + "\n")
    page.neutral_filenames.setChecked(True)
    _select(page.organization_policy, "liste_noire")
    assert not page.organization_whitelist.isEnabled()
    assert page.organization_blacklist.isEnabled()
    page.organization_blacklist.setPlainText("Signaturio\nsignaturio.example\n")
    assert page.dirty
    assert page.save()

    saved = Config.load(window.context.config_path)
    assert saved.opaque_id_whitelist == ["ABCD-1234-EFGH-5678", r"re:FACT-\d{6}"]
    assert saved.neutral_filenames
    assert saved.organization_policy == "liste_noire"
    assert saved.organization_whitelist is None
    assert saved.organization_blacklist == ["Signaturio", "signaturio.example"]

    page.load()
    assert page.neutral_filenames.isChecked()
    assert page.organization_policy.currentData() == "liste_noire"
    assert "signaturio.example" in page.organization_blacklist.toPlainText()

    # An edited whitelist is stored as such; « Rétablir » brings the bundled list back.
    _select(page.organization_policy, "sauf_liste_blanche")
    assert page.organization_whitelist.isEnabled()
    page.organization_whitelist.setPlainText("Éditeur Exemple\nexemple-editeur.test\n")
    assert page.save()
    assert Config.load(window.context.config_path).organization_whitelist == [
        "Éditeur Exemple",
        "exemple-editeur.test",
    ]
    page._reset_organization_whitelist()
    assert page.save()
    assert Config.load(window.context.config_path).organization_whitelist is None


def test_invalid_opaque_id_pattern_is_refused(window: MainWindow) -> None:
    page = window.settings
    page.opaque_id_whitelist.setPlainText(r"re:FACT-(\d{6}")
    assert not page.save()
    assert "Identifiants techniques" in page.banner.text()


def test_gui_manages_detection_choices(tmp_path: Path) -> None:
    """Profile, workers, OCR and models saved by older versions never linger unseen."""
    path = tmp_path / "config.json"
    Config(
        profile=Profile.LIGHT, workers=3, ocr_enabled=False, gliner_model="x", llm_model="y"
    ).save(path)
    config = AppContext(config_path=path).config
    assert config.profile is None and config.workers == 0 and config.ocr_enabled
    assert config.gliner_model == "" and config.llm_model == ""


def test_settings_page_shows_only_user_choices(window: MainWindow) -> None:
    page = window.settings
    tabs = [page.tabs.tabText(i) for i in range(page.tabs.count())]
    assert tabs == ["Remplacement", "Listes", "Organisations", "Avancé", "Application"]
    for gone in ("profile", "min_score", "paranoid", "workers", "ocr", "libreoffice_path"):
        assert not hasattr(page, gone)
