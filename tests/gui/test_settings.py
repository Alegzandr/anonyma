from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtWidgets import QTableWidgetItem

from anonyma import libreoffice, paths
from anonyma.config import Config
from anonyma.gui import theme
from anonyma.gui.main_window import MainWindow
from anonyma.gui.pages.settings import libreoffice_status, validate_patterns
from anonyma.models import Profile, Regime, ReplacementMode


def _select(combo, value) -> None:  # type: ignore[no-untyped-def]
    combo.setCurrentIndex(combo.findData(value))


def test_settings_round_trip(window: MainWindow, tmp_path: Path) -> None:
    page = window.settings
    assert not page.dirty
    _select(page.profile, Profile.STANDARD.value)
    page.min_score.setValue(0.3)
    page.review_threshold.setValue(0.75)
    page.paranoid.setChecked(True)
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
    page.workers.setValue(2)
    page.ocr.setChecked(False)
    page.use_libreoffice.setChecked(True)
    page.libreoffice_path.setText(r"C:\LibreOffice\program\soffice.exe")
    assert page.dirty
    assert page.save()
    assert not page.dirty

    saved = Config.load(window.context.config_path)
    assert saved.profile is Profile.STANDARD
    assert saved.min_score == 0.3 and saved.review_threshold == 0.75
    assert saved.paranoid
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
    assert saved.workers == 2 and not saved.ocr_enabled
    assert saved.libreoffice_path.endswith("soffice.exe")

    # Reloading the page from the saved configuration shows the same values.
    page.load()
    assert page.profile.currentData() == Profile.STANDARD.value
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
    page.min_score.setValue(0.9)
    page.review_threshold.setValue(0.5)
    assert not page.save()
    assert "seuil" in page.banner.text()
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


def test_detection_and_policy_options_round_trip(window: MainWindow) -> None:
    from anonyma.policy import default_whitelist

    page = window.settings
    # Defaults: bundled organization whitelist shown, stored as None (follows updates).
    assert page.organization_whitelist.toPlainText().splitlines() == list(default_whitelist())
    assert page.organization_policy.currentData() == "sauf_liste_blanche"
    assert page.mask_opaque_ids.isChecked() and page.detect_dates.isChecked()
    assert page.tag_numbering.currentData() == "sequentiel"

    page.detect_dates.setChecked(False)
    page.mask_opaque_ids.setChecked(True)
    page.opaque_id_whitelist.setPlainText("ABCD-1234-EFGH-5678\n" + r"re:FACT-\d{6}" + "\n")
    _select(page.tag_numbering, "aleatoire")
    page.neutral_filenames.setChecked(True)
    _select(page.organization_policy, "liste_noire")
    assert not page.organization_whitelist.isEnabled()
    assert page.organization_blacklist.isEnabled()
    page.organization_blacklist.setPlainText("Signaturio\nsignaturio.example\n")
    page.ocr_full_pages.setChecked(False)
    page.decode_barcodes.setChecked(False)
    page.remove_invisible_text.setChecked(False)
    assert page.dirty
    assert page.save()

    saved = Config.load(window.context.config_path)
    assert not saved.detect_dates and saved.mask_opaque_ids
    assert saved.opaque_id_whitelist == ["ABCD-1234-EFGH-5678", r"re:FACT-\d{6}"]
    assert saved.tag_numbering == "aleatoire"
    assert saved.neutral_filenames
    assert saved.organization_policy == "liste_noire"
    assert saved.organization_whitelist is None
    assert saved.organization_blacklist == ["Signaturio", "signaturio.example"]
    assert not saved.ocr_full_pages and not saved.decode_barcodes
    assert not saved.remove_invisible_text

    page.load()
    assert page.tag_numbering.currentData() == "aleatoire"
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


def test_libreoffice_status_says_what_is_used(
    window: MainWindow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = window.settings
    exe = tmp_path / "LibreOffice" / "program" / libreoffice._executable_names()[0]
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    monkeypatch.setattr(libreoffice, "bundled_locations", lambda: [])
    monkeypatch.setattr(libreoffice, "standard_locations", lambda: [])
    monkeypatch.setattr(libreoffice.shutil, "which", lambda name: None)
    page.use_libreoffice.setChecked(True)
    # Edited, so that the status is recomputed with the patched lookups (an empty field
    # left empty emits no change, and CI machines have a LibreOffice installed).
    page.libreoffice_path.setText("x")
    page.libreoffice_path.setText("")
    status = page.libreoffice_status.text()
    assert status.startswith("Aucun LibreOffice trouvé")
    assert "les .xls sont restitués en .xlsx" in status
    page.libreoffice_path.setText(str(exe.parent.parent))
    assert page.libreoffice_status.text().startswith("LibreOffice utilisé")
    assert str(exe) in page.libreoffice_status.text()
    page.use_libreoffice.setChecked(False)
    assert page.libreoffice_status.text().startswith("LibreOffice désactivé")
    assert libreoffice_status(True, str(tmp_path / "absent")).startswith(
        "Aucun LibreOffice à ce chemin ni ailleurs"
    )
