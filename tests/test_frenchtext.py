from __future__ import annotations

from anonyma.frenchtext import NBSP, NNBSP, plural, typo


def test_plural_agrees_with_french_rules() -> None:
    assert plural(0, "fichier") == f"0{NBSP}fichier"
    assert plural(1, "entité") == f"1{NBSP}entité"
    assert plural(2, "fichier") == f"2{NBSP}fichiers"
    assert plural(3, "travail", "travaux") == f"3{NBSP}travaux"


def test_typo_makes_french_spaces_unbreakable() -> None:
    assert typo("Statut : prêt ; continuer ? « Revue »") == (
        f"Statut{NBSP}: prêt{NNBSP}; continuer{NNBSP}? «{NBSP}Revue{NBSP}»"
    )
    assert typo("Lecteur C:/dossier") == "Lecteur C:/dossier"
