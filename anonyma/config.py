"""User configuration: profile, thresholds, business lists, whitelist, limits.

Stored as JSON in ``data/config.json``. Unknown keys are ignored so that older and
newer versions can share a file.
"""

from __future__ import annotations

import csv
import dataclasses
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from anonyma import paths
from anonyma.models import EntityType, ExpansionBudget, Profile, Regime, ReplacementMode
from anonyma.textutils import normalize

ORGANIZATION_POLICIES = ("toutes", "sauf_liste_blanche", "liste_noire")
#: Model detections scored below this are discarded.
MIN_SCORE = 0.25
#: Entities scored below this are uncertain: masked all the same, shown as low
#: confidence, and not searched through the batch as a lone common word.
UNCERTAIN_SCORE = 0.6


@dataclass(slots=True)
class ArchiveLimits:
    """Anti-bomb limits for one top-level file and all its descendants."""

    max_total_bytes: int = 4 * 1024**3
    max_files: int = 100_000
    max_ratio: float = 200.0
    #: Safety net against recursive quines, not a functional limit.
    max_depth: int = 64

    def budget(self) -> ExpansionBudget:
        return ExpansionBudget(
            max_total_bytes=self.max_total_bytes,
            max_files=self.max_files,
            max_ratio=self.max_ratio,
            max_depth=self.max_depth,
        )


@dataclass(slots=True)
class Config:
    #: None = use the profile recommended by ``hardware.py``.
    profile: Profile | None = None
    mode: ReplacementMode = ReplacementMode.TAG
    regime: Regime = Regime.ANONYMIZATION

    #: Values never masked (compared after normalisation).
    whitelist: list[str] = field(default_factory=list)
    #: Values always masked: ``{"value": ..., "type": ...}`` pairs.
    always_mask: list[dict[str, str]] = field(default_factory=list)
    #: CSV files (columns ``valeur[,type]``) of values always masked.
    business_lists: list[str] = field(default_factory=list)
    #: Extra GLiNER labels -> entity type, e.g. {"nom de projet": "PROJET"}.
    custom_labels: dict[str, str] = field(default_factory=dict)
    #: Regexes for internal case numbers (type NUMERO_DOSSIER).
    case_number_patterns: list[str] = field(default_factory=list)

    gliner_model: str = ""
    llm_model: str = ""
    #: 0 = automatic.
    workers: int = 0

    ocr_enabled: bool = True
    libreoffice_path: str = ""

    #: Encrypted mapping store (pseudonymization regime).
    store_path: str = ""

    limits: ArchiveLimits = field(default_factory=ArchiveLimits)
    #: Free-form GUI preferences (theme, window geometry...).
    ui: dict[str, Any] = field(default_factory=dict)
    #: Seconds allowed for one LibreOffice conversion (legacy doc/xls/ppt).
    libreoffice_timeout: float = 180.0
    #: Ask GitHub for a newer release at GUI startup (portable build only): the one
    #: automatic network access of the application, see ``anonyma/updater.py``.
    check_updates: bool = True

    # -- detection ---------------------------------------------------------------
    #: GLiNER span threshold (lower = more recall).
    gliner_threshold: float = 0.3
    #: Intra-op threads of one GLiNER instance (0 = CPU count / worker processes).
    gliner_threads: int = 0
    #: Send every segment to the LLM, not only those with persons/organisations or
    #: indirect-identifier cues (much slower).
    llm_all_segments: bool = False

    # -- identifiers and names -----------------------------------------------------
    #: Opaque identifiers never masked (exact values or regexes prefixed ``re:``).
    opaque_id_whitelist: list[str] = field(default_factory=list)
    #: Organizations: "toutes" (mask all), "sauf_liste_blanche" (mask all except the
    #: whitelist) or "liste_noire" (mask only the blacklist).
    organization_policy: str = "sauf_liste_blanche"
    #: Organizations / domains / public URLs never masked under "sauf_liste_blanche".
    #: None = the bundled default list (``anonyma/data/organizations_whitelist.txt``).
    organization_whitelist: list[str] | None = None
    #: Organizations always masked under "liste_noire".
    organization_blacklist: list[str] = field(default_factory=list)
    #: Output names ``document_0001.pdf``... (mapping kept in the encrypted store
    #: in pseudonymization) instead of anonymized original names.
    neutral_filenames: bool = False

    # -- derived helpers ---------------------------------------------------

    def effective_profile(self) -> Profile:
        """The chosen profile, or for "auto" the hardware recommendation capped to
        the installed models (no FULL profile without a local LLM)."""
        if self.profile is not None:
            return self.profile
        from anonyma import hardware, registry

        recommended = hardware.recommend_profile(hardware.detect_hardware())
        return registry.usable_profile(self, recommended)

    def whitelist_keys(self) -> set[str]:
        return {normalize(v) for v in self.whitelist if v.strip()}

    def mask_terms(self) -> list[tuple[str, str]]:
        """All ``(value, entity_type)`` pairs from ``always_mask`` and business lists."""
        terms = [
            (t["value"], t.get("type") or EntityType.BUSINESS_TERM)
            for t in self.always_mask
            if t.get("value", "").strip()
        ]
        for name in self.business_lists:
            terms.extend(load_business_list(Path(name)))
        return terms

    def fingerprint(self) -> str:
        """Stable hash of the settings that affect detection (worker caches)."""
        payload = json.dumps(self.to_dict(), sort_keys=True, default=str)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    # -- (de)serialisation -------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Config:
        known = {f.name for f in dataclasses.fields(cls)}
        values = {k: v for k, v in data.items() if k in known}
        if values.get("profile") is not None:
            values["profile"] = Profile(values["profile"])
        if "mode" in values:
            values["mode"] = ReplacementMode(values["mode"])
        if "regime" in values:
            values["regime"] = Regime(values["regime"])
        if isinstance(values.get("limits"), dict):
            limit_fields = {f.name for f in dataclasses.fields(ArchiveLimits)}
            values["limits"] = ArchiveLimits(
                **{k: v for k, v in values["limits"].items() if k in limit_fields}
            )
        config = cls(**values)
        config.validate()
        return config

    def validate(self) -> None:
        if self.workers < 0:
            raise ValueError("Le nombre de processus ne peut pas être négatif.")
        if self.organization_policy not in ORGANIZATION_POLICIES:
            raise ValueError("Politique des organisations inconnue.")

    @classmethod
    def load(cls, path: Path | None = None) -> Config:
        path = path or paths.config_path()
        if not path.exists():
            return cls()
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def save(self, path: Path | None = None) -> None:
        path = path or paths.config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        tmp.replace(path)


def load_business_list(path: Path) -> list[tuple[str, str]]:
    """Read a CSV of values to always mask: ``valeur[,type]`` per row.

    The delimiter (``;``, tab or ``,``) is guessed; a header row starting with
    ``valeur``/``value`` is skipped.
    """
    text = path.read_text(encoding="utf-8-sig")
    # csv.Sniffer is unreliable when some rows have a single column: pick the most
    # frequent candidate delimiter of the first lines instead.
    # On ties ";" and tab win over "," (commas often appear inside names).
    head = "\n".join(text.splitlines()[:20])
    counts = {d: head.count(d) for d in (";", "\t", ",")}
    delimiter = max(counts, key=lambda d: counts[d])
    if counts[delimiter] == 0:
        delimiter = "\x1f"  # single column: never split
    rows: list[tuple[str, str]] = []
    for i, row in enumerate(csv.reader(text.splitlines(), delimiter=delimiter)):
        if not row or not row[0].strip():
            continue
        if i == 0 and row[0].strip().lower() in {"valeur", "value", "nom", "name"}:
            continue
        entity_type = row[1].strip().upper() if len(row) > 1 and row[1].strip() else ""
        rows.append((row[0].strip(), entity_type or EntityType.BUSINESS_TERM))
    return rows
