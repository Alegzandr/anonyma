"""End-of-batch report. Contains anonymized names, counts and codes only: never a
sensitive value."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from anonyma.models import DocumentResult, DocumentStatus, Severity
from anonyma.pipeline import BatchResult

STATUS_LABELS = {
    DocumentStatus.ANONYMIZED: "Anonymisé",
    DocumentStatus.NEEDS_REVIEW: "À revoir",
    DocumentStatus.RESIDUE: "Résidus",
    DocumentStatus.FAILED: "Échec",
    DocumentStatus.UNSUPPORTED: "Non pris en charge",
    DocumentStatus.CANCELLED: "Annulé",
    DocumentStatus.PENDING: "En attente",
    DocumentStatus.EXTRACTED: "Extrait",
}


@dataclass(slots=True)
class BatchReport:
    batch_id: str
    started_at: str
    finished_at: str
    status_counts: dict[str, int] = field(default_factory=dict)
    entity_counts: dict[str, int] = field(default_factory=dict)
    files: list[dict[str, Any]] = field(default_factory=list)
    residues: list[dict[str, str]] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_result(cls, result: BatchResult) -> BatchReport:
        report = cls(
            result.batch_id,
            result.started_at.isoformat(timespec="seconds"),
            result.finished_at.isoformat(timespec="seconds"),
        )
        for top in result.results:
            report.status_counts[top.status] = report.status_counts.get(top.status, 0) + 1
            report.files.append(_file_entry(top))
            for r in _walk(top):
                for t, n in r.entity_counts.items():
                    report.entity_counts[t] = report.entity_counts.get(t, 0) + n
                errors = [i for i in r.issues if i.severity is Severity.ERROR]
                if errors or r.status in (DocumentStatus.FAILED, DocumentStatus.UNSUPPORTED):
                    report.failures.append(
                        {
                            "fichier": r.output_name,
                            "statut": STATUS_LABELS.get(r.status, r.status),
                            "problemes": [f"{i.code} : {i.message}" for i in r.issues],
                        }
                    )
            if top.verification:
                for res in top.verification.residues:
                    report.residues.append(
                        {
                            "fichier": top.output_name,
                            "emplacement": res.location,
                            "zone": res.segment_label,
                            "type": res.entity_type,
                            "origine": res.reason,
                            "detecteur": res.detector,
                        }
                    )
        return report

    def to_dict(self) -> dict[str, Any]:
        return {
            "lot": self.batch_id,
            "debut": self.started_at,
            "fin": self.finished_at,
            "statuts": self.status_counts,
            "entites_par_type": self.entity_counts,
            "fichiers": self.files,
            "residus": self.residues,
            "echecs": self.failures,
        }

    def save_json(self, path: Path) -> None:
        path.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")

    def to_markdown(self) -> str:
        lines = [
            f"# Rapport du lot {self.batch_id}",
            "",
            f"Début : {self.started_at} · Fin : {self.finished_at}",
            "",
            "## Fichiers",
            "",
            "| Fichier | Statut | Entités | Résidus |",
            "|---|---|---|---|",
        ]
        for f in self.files:
            lines.append(f"| {f['fichier']} | {f['statut']} | {f['entites']} | {f['residus']} |")
        lines += ["", "## Entités par type", "", "| Type | Nombre |", "|---|---|"]
        lines += [f"| {t} | {n} |" for t, n in sorted(self.entity_counts.items())]
        if self.residues:
            lines += [
                "",
                "## Résidus",
                "",
                "| Fichier | Zone | Type | Origine |",
                "|---|---|---|---|",
            ]
            lines += [
                f"| {r['emplacement']} | {r['zone']} | {r['type']} | {r['origine']} |"
                for r in self.residues
            ]
        if self.failures:
            lines += ["", "## Échecs", ""]
            for f in self.failures:
                lines.append(
                    f"- **{f['fichier']}** ({f['statut']}) : " + " ; ".join(f["problemes"])
                )
        return "\n".join(lines) + "\n"


def _walk(result: DocumentResult) -> list[DocumentResult]:
    out = [result]
    for c in result.children:
        out.extend(_walk(c))
    return out


def _file_entry(r: DocumentResult) -> dict[str, Any]:
    return {
        "fichier": r.output_name,
        "format": r.format,
        "format_sortie": r.output_format,
        "statut": STATUS_LABELS.get(r.status, r.status),
        "entites": sum(sum(x.entity_counts.values()) for x in _walk(r)),
        "residus": r.residue_count,
        "a_revoir": r.review_count,
    }
