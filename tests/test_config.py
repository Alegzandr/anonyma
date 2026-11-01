from __future__ import annotations

from pathlib import Path

import pytest

from anonyma.config import Config, load_business_list
from anonyma.models import Profile, Regime, ReplacementMode


def test_roundtrip(tmp_path: Path) -> None:
    config = Config(
        profile=Profile.STANDARD,
        mode=ReplacementMode.REALISTIC,
        regime=Regime.PSEUDONYMIZATION,
        whitelist=["Paris"],
    )
    config.limits.max_files = 12
    path = tmp_path / "c.json"
    config.save(path)
    loaded = Config.load(path)
    assert loaded == config
    assert loaded.fingerprint() == config.fingerprint()


def test_unknown_keys_ignored() -> None:
    assert Config.from_dict({"mode": "masquage", "futur": 1}).mode is ReplacementMode.BLACKOUT


def test_invalid_value() -> None:
    with pytest.raises(ValueError):
        Config.from_dict({"workers": -1})


def test_settings_the_application_now_manages_are_ignored() -> None:
    """A config.json written by an older version still loads: its removed settings
    (review policy, thresholds, paranoid mode...) are dropped."""
    old = {
        "min_score": 2,
        "review_threshold": 0.9,
        "paranoid": True,
        "review_policy": "x",
        "detect_dates": False,
        "mask_opaque_ids": False,
        "tag_numbering": "aleatoire",
    }
    assert Config.from_dict(old) == Config()


def test_business_list(tmp_path: Path) -> None:
    csv_file = tmp_path / "l.csv"
    csv_file.write_text("valeur;type\nProjet Hélios;PROJET\nAcme Industrie;\n", encoding="utf-8")
    assert load_business_list(csv_file) == [
        ("Projet Hélios", "PROJET"),
        ("Acme Industrie", "TERME_METIER"),
    ]
    config = Config(business_lists=[str(csv_file)], always_mask=[{"value": "X1"}])
    assert len(config.mask_terms()) == 3


def test_business_list_semicolon_with_missing_types(tmp_path: Path) -> None:
    csv_file = tmp_path / "l.csv"
    csv_file.write_text("Acme Industrie\nProjet Hélios;PROJET\nDupont, Jean\n", encoding="utf-8")
    assert load_business_list(csv_file) == [
        ("Acme Industrie", "TERME_METIER"),
        ("Projet Hélios", "PROJET"),
        ("Dupont, Jean", "TERME_METIER"),
    ]
