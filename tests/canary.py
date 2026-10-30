"""Synthetic canary values shared by contract tests (never real personal data)."""

CANARY_PERSON = "Zéphyrin Quintavalle"
CANARY_EMAIL = "zephyrin.quintavalle@exemple.test"
CANARY_TERMS = [
    {"value": CANARY_PERSON, "type": "PERSONNE"},
    {"value": CANARY_EMAIL, "type": "EMAIL"},
]
