"""French labels for enumerations shown in the interface."""

from __future__ import annotations

from anonyma.models import (
    DocumentStatus,
    EntityType,
    Profile,
    Regime,
    ReplacementMode,
    SegmentKind,
)
from anonyma.report import STATUS_LABELS

PROFILE_LABELS: dict[Profile | None, str] = {
    None: "Automatique (selon le matériel)",
    Profile.LIGHT: "Léger — règles et listes",
    Profile.STANDARD: "Standard — + modèle GLiNER",
    Profile.FULL: "Complet — + modèle de langue",
}

PROFILE_SHORT: dict[Profile, str] = {
    Profile.LIGHT: "Léger",
    Profile.STANDARD: "Standard",
    Profile.FULL: "Complet",
}

MODE_LABELS: dict[ReplacementMode, str] = {
    ReplacementMode.TAG: "Balises — [PERSONNE_001]",
    ReplacementMode.REALISTIC: "Réaliste — substituts vraisemblables",
    ReplacementMode.BLACKOUT: "Masquage — caviardage",
}

REGIME_LABELS: dict[Regime, str] = {
    Regime.ANONYMIZATION: "Anonymisation — irréversible, aucune table conservée",
    Regime.PSEUDONYMIZATION: "Pseudonymisation — table de correspondance chiffrée",
}

KIND_LABELS: dict[SegmentKind, str] = {
    SegmentKind.BODY: "Texte",
    SegmentKind.HEADER: "En-têtes",
    SegmentKind.CELL: "Cellules",
    SegmentKind.METADATA: "Métadonnées",
    SegmentKind.OCR: "Texte reconnu (OCR)",
    SegmentKind.ATTRIBUTE: "Attributs",
    SegmentKind.COMMENT: "Commentaires et révisions",
    SegmentKind.NOTE: "Notes",
    SegmentKind.FILENAME: "Nom de fichier",
    SegmentKind.HIDDEN: "Contenu masqué",
}

#: Status -> badge level (see ``theme.BADGE_LEVELS``).
STATUS_LEVELS: dict[DocumentStatus, str] = {
    DocumentStatus.ANONYMIZED: "success",
    DocumentStatus.NEEDS_REVIEW: "warning",
    DocumentStatus.RESIDUE: "danger",
    DocumentStatus.FAILED: "danger",
    DocumentStatus.UNSUPPORTED: "neutral",
    DocumentStatus.CANCELLED: "neutral",
    DocumentStatus.PENDING: "neutral",
    DocumentStatus.EXTRACTED: "info",
}


def status_label(status: DocumentStatus) -> str:
    if status is DocumentStatus.EXTRACTED:
        return "Analysé"
    return STATUS_LABELS.get(status, str(status))


ENTITY_TYPE_LABELS: dict[str, str] = {
    EntityType.PERSON: "Personne",
    EntityType.ORGANIZATION: "Organisation",
    EntityType.LOCATION: "Lieu",
    EntityType.ADDRESS: "Adresse",
    EntityType.JOB_TITLE: "Fonction",
    EntityType.EMAIL: "Adresse électronique",
    EntityType.PHONE: "Téléphone",
    EntityType.IBAN: "IBAN",
    EntityType.BIC: "BIC",
    EntityType.CREDIT_CARD: "Carte bancaire",
    EntityType.NIR: "Numéro de sécurité sociale",
    EntityType.SIREN: "SIREN",
    EntityType.SIRET: "SIRET",
    EntityType.VAT_NUMBER: "Numéro de TVA",
    EntityType.LICENSE_PLATE: "Plaque d'immatriculation",
    EntityType.POSTAL_CODE: "Code postal",
    EntityType.IP_ADDRESS: "Adresse IP",
    EntityType.URL: "URL",
    EntityType.DATE_OF_BIRTH: "Date de naissance",
    EntityType.DATE: "Date",
    EntityType.CASE_NUMBER: "Numéro de dossier",
    EntityType.USERNAME: "Identifiant",
    EntityType.MESSAGE_ID: "Identifiant de message",
    EntityType.INDIRECT: "Identifiant indirect",
    EntityType.PROPER_NOUN: "Nom propre",
    EntityType.SUSPICIOUS_TOKEN: "Séquence suspecte",
    EntityType.BUSINESS_TERM: "Terme métier",
}


def entity_type_label(entity_type: str) -> str:
    return ENTITY_TYPE_LABELS.get(entity_type, entity_type.replace("_", " ").capitalize())


def format_bytes(size: int) -> str:
    if size <= 0:
        return "—"
    value = float(size)
    for unit in ("o", "Ko", "Mo", "Go"):
        if value < 1024 or unit == "Go":
            return f"{value:.0f} {unit}" if unit == "o" else f"{value:.1f} {unit}".replace(".", ",")
        value /= 1024
    return f"{value:.1f} To"
