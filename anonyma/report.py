"""End-of-batch report. Contains anonymized names, counts and codes only: never a
sensitive value."""

from __future__ import annotations

import html
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from anonyma.brand import app_svg_data_uri
from anonyma.frenchtext import NBSP, plural, typo
from anonyma.models import DocumentResult, DocumentStatus, Severity, UserError
from anonyma.pipeline import BatchResult, is_quarantined

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

INCOMPLETE_INTRO = (
    "Certaines images ou pages de ces fichiers n'ont pas pu être relues par l'OCR : leur "
    "contenu n'a pas été vérifié. Les fichiers sont « À revoir » et placés en quarantaine "
    "(dossier « …_quarantaine » à côté du dossier de sortie) : contrôlez-les visuellement "
    "avant toute diffusion."
)
REVIEW_INTRO = (
    "Entités détectées avec une faible confiance : elles sont masquées, mais peuvent être "
    "de fausses détections. Retrouvez-les dans le fichier produit grâce à leur remplacement."
)

_QUARANTINED = "en quarantaine (ne pas diffuser)"

#: Extensions accepted by :meth:`BatchReport.save`.
REPORT_EXTENSIONS = (".json", ".md", ".markdown", ".html", ".htm")


@dataclass(slots=True)
class BatchReport:
    batch_id: str
    started_at: str
    finished_at: str
    status_counts: dict[str, int] = field(default_factory=dict)
    entity_counts: dict[str, int] = field(default_factory=dict)
    files: list[dict[str, Any]] = field(default_factory=list)
    residues: list[dict[str, str]] = field(default_factory=list)
    #: Low-confidence entities awaiting review, by tag (never the original value).
    review: list[dict[str, str]] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)
    #: Files whose verification could not read every image or page (OCR): never
    #: anonymized, kept in quarantine for a visual check.
    incomplete: list[dict[str, Any]] = field(default_factory=list)
    #: Non-sensitive batch settings (profile, mode, regime...), French labels.
    settings: dict[str, str] = field(default_factory=dict)
    #: Top-level files placed in the quarantine folder (never to be shared).
    quarantined: int = 0

    @classmethod
    def from_result(cls, result: BatchResult) -> BatchReport:
        report = cls(
            result.batch_id,
            result.started_at.isoformat(timespec="seconds"),
            result.finished_at.isoformat(timespec="seconds"),
        )
        for top in result.results:
            report.status_counts[top.status] = report.status_counts.get(top.status, 0) + 1
            report.quarantined += is_quarantined(top, result.output_dir)
            report.files.append(_file_entry(top))
            report.review += [
                {"fichier": top.output_name, "type": t, "remplacement": substitute}
                for t, substitute in top.review_items
            ]
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
            if top.verification and not top.verification.complete:
                report.incomplete.append(
                    {"fichier": top.output_name, "non_relus": top.verification.unread}
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
                            "certitude": "certain" if res.certain else "possible",
                        }
                    )
        return report

    # -- verdict ----------------------------------------------------------------

    def _outcomes(self) -> dict[str, int]:
        """Top-level files by outcome; a quarantined file counts only as such."""
        counts = self.status_counts
        quarantined = self.quarantined
        review = counts.get(DocumentStatus.NEEDS_REVIEW, 0)
        residue = counts.get(DocumentStatus.RESIDUE, 0)
        # Quarantined files have the RESIDUE status or (unverified images or pages)
        # NEEDS_REVIEW: whatever is not a residue comes out of the review count.
        review = max(0, review - max(0, quarantined - residue))
        return {
            "anonymise": counts.get(DocumentStatus.ANONYMIZED, 0),
            "a_revoir": review,
            "quarantaine": max(quarantined, residue),
            "echec": counts.get(DocumentStatus.FAILED, 0)
            + counts.get(DocumentStatus.PENDING, 0)
            + counts.get(DocumentStatus.EXTRACTED, 0),
            "non_supporte": counts.get(DocumentStatus.UNSUPPORTED, 0),
            "annule": counts.get(DocumentStatus.CANCELLED, 0),
        }

    def verdict_code(self) -> str:
        """``anonymise``, ``a_revoir``, ``echec``, ``quarantaine`` or ``vide``."""
        outcomes = self._outcomes()
        if not self.files:
            return "vide"
        if outcomes["quarantaine"]:
            return "quarantaine"
        if outcomes["echec"] or outcomes["non_supporte"] or outcomes["annule"]:
            return "echec"
        if outcomes["a_revoir"]:
            return "a_revoir"
        return "anonymise"

    def verdict(self) -> tuple[str, str]:
        """One-line French verdict and its level (``ok``, ``warn`` or ``ko``):
        « 8 fichiers anonymisés et vérifiés. » or
        « 6 anonymisés · 1 à revoir · 1 en quarantaine (ne pas diffuser) »."""
        outcomes = self._outcomes()
        total = len(self.files)
        if not total:
            return "Aucun fichier traité.", "warn"
        if outcomes["anonymise"] == total:
            text = plural(total, "fichier anonymisé et vérifié", "fichiers anonymisés et vérifiés")
            return text + ".", "ok"
        parts = [
            plural(outcomes["anonymise"], "anonymisé"),
            plural(outcomes["a_revoir"], "à revoir", "à revoir"),
            plural(outcomes["quarantaine"], _QUARANTINED, _QUARANTINED),
            plural(outcomes["echec"], "en échec", "en échec"),
            plural(outcomes["non_supporte"], "non pris en charge", "non pris en charge"),
            plural(outcomes["annule"], "annulé"),
        ]
        shown = [part for part in parts if not part.startswith(f"0{NBSP}")]
        level = "warn" if self.verdict_code() == "a_revoir" else "ko"
        return " · ".join(shown), level

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "lot": self.batch_id,
            "debut": self.started_at,
            "fin": self.finished_at,
            "verdict": self.verdict_code(),
            "quarantaine": self.quarantined,
            "statuts": self.status_counts,
            "entites_par_type": self.entity_counts,
            "fichiers": self.files,
            "residus": self.residues,
            "a_revoir": self.review,
            "echecs": self.failures,
            "verifications_incompletes": self.incomplete,
        }
        if self.settings:
            data["parametres"] = self.settings
        return data

    def save_json(self, path: Path) -> None:
        path.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")

    # -- Markdown -------------------------------------------------------------

    def to_markdown(self) -> str:
        from anonyma.gui.labels import entity_type_label

        c = _md_cell
        verdict, _ = self.verdict()
        lines = [
            f"# Rapport du lot {self.batch_id}",
            "",
            f"**{c(verdict)}**",
            "",
            _period(self.started_at, self.finished_at),
            "",
        ]
        if self.settings:
            lines += [f"- {c(k)}{NBSP}: {c(v)}" for k, v in self.settings.items()] + [""]
        lines += [
            "## Fichiers",
            "",
            "| Fichier | Statut | Occurrences | Résidus |",
            "|---|---|---|---|",
        ]
        for f in self.files:
            lines.append(
                f"| {c(f['fichier'])} | {c(f['statut'])} | {f['entites']} | {f['residus']} |"
            )
        lines += [
            "",
            "## Occurrences masquées par type",
            "",
            "| Type | Code | Occurrences |",
            "|---|---|---|",
        ]
        lines += [
            f"| {c(entity_type_label(t))} | {c(t)} | {n} |"
            for t, n in sorted(self.entity_counts.items())
        ]
        if self.residues:
            lines += [
                "",
                "## Résidus",
                "",
                "| Emplacement | Zone | Type | Origine |",
                "|---|---|---|---|",
            ]
            lines += [
                f"| {c(r['emplacement'])} | {c(r['zone'])} | {c(r['type'])} | {c(r['origine'])} |"
                for r in self.residues
            ]
        if self.review:
            lines += ["", "## À revoir", "", typo(REVIEW_INTRO), ""]
            lines += ["| Fichier | Type | Remplacé par |", "|---|---|---|"]
            lines += [
                f"| {c(r['fichier'])} | {c(r['type'])} | {c(r['remplacement'])} |"
                for r in self.review
            ]
        if self.incomplete:
            lines += ["", "## Vérification incomplète", "", typo(INCOMPLETE_INTRO), ""]
            lines += [f"- **{c(f['fichier'])}**{NBSP}: {_unread(f)}" for f in self.incomplete]
        if self.failures:
            lines += ["", "## Échecs", ""]
            for f in self.failures:
                problems = " ; ".join(f["problemes"])
                detail = f"{NBSP}: {problems}" if problems else ""
                lines.append(f"- **{c(f['fichier'])}** ({f['statut']}){detail}")
        return "\n".join(lines) + "\n"

    def save_markdown(self, path: Path) -> None:
        path.write_text(self.to_markdown(), encoding="utf-8")

    # -- HTML -----------------------------------------------------------------

    def to_html(self) -> str:
        """Self-contained HTML page: inline CSS, no script, no external resource."""
        from anonyma.gui.labels import entity_type_label

        e = _esc
        verdict, level = self.verdict()
        files_count = plural(len(self.files), "fichier")
        occurrences = plural(
            sum(self.entity_counts.values()), "occurrence masquée", "occurrences masquées"
        )
        status_rows = "".join(
            f"<tr><td>{e(_status_label(s))}</td><td class=n>{n}</td></tr>"
            for s, n in sorted(self.status_counts.items())
        )
        file_rows = "".join(
            f"<tr class={_status_class(str(f['statut']))}><td>{e(f['fichier'])}</td>"
            f"<td>{e(f.get('format', ''))}</td><td class=st>{e(f['statut'])}</td>"
            f"<td class=n>{e(f['entites'])}</td><td class=n>{e(f['residus'])}</td>"
            f"<td class=n>{e(f.get('a_revoir', 0))}</td></tr>"
            for f in self.files
        )
        entity_rows = "".join(
            f"<tr><td>{e(entity_type_label(t))}</td><td><code>{e(t)}</code></td>"
            f"<td class=n>{n}</td></tr>"
            for t, n in sorted(self.entity_counts.items())
        )
        parts = [
            "<!DOCTYPE html>",
            '<html lang="fr"><head><meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width, initial-scale=1">',
            f"<title>Rapport du lot {e(self.batch_id)}</title>",
            f'<link rel="icon" href="{app_svg_data_uri()}">',
            f"<style>{_CSS}</style></head><body><main>",
            f'<p class=brand><img src="{app_svg_data_uri()}" width=28 height=28 alt="">Anonyma</p>',
            f"<h1>Rapport du lot <code>{e(self.batch_id)}</code></h1>",
            f'<p class="verdict {level}">{e(verdict)}</p>',
            f"<p class=meta>{e(_period(self.started_at, self.finished_at))} · "
            f"{e(files_count)} · {e(occurrences)}</p>",
        ]
        if self.settings:
            items = "".join(
                f"<div><dt>{e(k)}</dt><dd>{e(v)}</dd></div>" for k, v in self.settings.items()
            )
            parts.append(f"<dl class=settings>{items}</dl>")
        parts += [
            _section("Statuts", ["Statut", "#Fichiers"], status_rows),
            _section(
                "Fichiers",
                ["Fichier", "Format", "Statut", "#Occurrences", "#Résidus", "#À revoir"],
                file_rows,
            ),
            _section(
                "Occurrences masquées par type", ["Type", "Code", "#Occurrences"], entity_rows
            ),
        ]
        if self.residues:
            rows = "".join(
                f"<tr><td>{e(r['emplacement'])}</td><td>{e(r['zone'])}</td>"
                f"<td>{e(r['type'])}</td><td>{e(r['origine'])}</td>"
                f"<td>{e(r['detecteur'])}</td></tr>"
                for r in self.residues
            )
            parts.append(
                _section(
                    "Résidus",
                    ["Emplacement", "Zone", "Type", "Origine", "Détecteur"],
                    rows,
                    intro="Ces fichiers ont été placés en quarantaine (dossier "
                    f"<code>…_quarantaine</code> à côté du dossier de sortie){NBSP}: ils ne "
                    "doivent pas être diffusés.",
                )
            )
        if self.review:
            rows = "".join(
                f"<tr><td>{e(r['fichier'])}</td><td>{e(r['type'])}</td>"
                f"<td><code>{e(r['remplacement'])}</code></td></tr>"
                for r in self.review
            )
            parts.append(
                _section(
                    "À revoir",
                    ["Fichier", "Type", "Remplacé par"],
                    rows,
                    intro=e(typo(REVIEW_INTRO)),
                )
            )
        if self.incomplete:
            rows = "".join(
                f"<tr><td>{e(f['fichier'])}</td><td class=n>{e(f['non_relus'])}</td></tr>"
                for f in self.incomplete
            )
            parts.append(
                _section(
                    "Vérification incomplète",
                    ["Fichier", "#Images ou pages non relues"],
                    rows,
                    intro=e(typo(INCOMPLETE_INTRO)),
                )
            )
        if self.failures:
            items = "".join(
                f"<li><strong>{e(f['fichier'])}</strong> ({e(f['statut'])})"
                + (
                    "<ul>" + "".join(f"<li>{e(p)}</li>" for p in f["problemes"]) + "</ul>"
                    if f["problemes"]
                    else ""
                )
                + "</li>"
                for f in self.failures
            )
            parts.append(f"<section><h2>Échecs</h2><ul class=failures>{items}</ul></section>")
        parts += [
            "<footer>Rapport généré par Anonyma. Il ne contient que des noms anonymisés, "
            f"des nombres et des codes{NBSP}: aucune donnée d'origine.</footer>",
            "</main></body></html>",
        ]
        return "\n".join(parts) + "\n"

    def save_html(self, path: Path) -> None:
        path.write_text(self.to_html(), encoding="utf-8")

    # -- dispatch ---------------------------------------------------------------

    def save(self, path: Path) -> None:
        """Write the report in the format given by the extension (.json, .md, .html)."""
        path = Path(path)
        writers = {
            ".json": self.save_json,
            ".md": self.save_markdown,
            ".markdown": self.save_markdown,
            ".html": self.save_html,
            ".htm": self.save_html,
        }
        writer = writers.get(path.suffix.lower())
        if writer is None:
            raise UserError(
                "Format de rapport non reconnu : utilisez l'extension .json, .md ou .html."
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        writer(path)


_CSS = (
    "body{margin:0;background:#f6f7f9;color:#1d232b;"
    "font:15px/1.5 system-ui,-apple-system,'Segoe UI',Roboto,sans-serif}"
    "main{max-width:1100px;margin:0 auto;padding:24px 16px 48px}"
    "h1{font-size:1.5rem;margin:0 0 4px}h2{font-size:1.1rem;margin:28px 0 8px}"
    ".brand{display:flex;align-items:center;gap:10px;margin:0 0 16px;font-weight:600}"
    ".meta{color:#5b6573;margin:0 0 8px}"
    ".verdict{font-size:1.1rem;font-weight:600;margin:8px 0}"
    ".verdict.ok{color:#1f7a3a}.verdict.warn{color:#9a6700}.verdict.ko{color:#b42318}"
    ".settings{display:flex;flex-wrap:wrap;gap:4px 24px;margin:12px 0}"
    ".settings dt{font-size:.8rem;color:#5b6573}.settings dd{margin:0;font-weight:600}"
    ".scroll{overflow-x:auto}"
    "table{width:100%;border-collapse:collapse;background:#fff;border:1px solid #dde1e6}"
    "th,td{text-align:left;padding:6px 10px;border-bottom:1px solid #e8ebef;"
    "vertical-align:top;overflow-wrap:anywhere}"
    "th{background:#eef1f4;font-weight:600}.n{text-align:right;white-space:nowrap}"
    "tr.ok .st{color:#1f7a3a}tr.warn .st{color:#9a6700}tr.ko .st{color:#b42318;font-weight:600}"
    "code{font-family:ui-monospace,Consolas,monospace;font-size:.9em}"
    ".failures li{margin:4px 0}"
    "footer{margin-top:32px;color:#5b6573;font-size:.85rem}"
    "@media (prefers-color-scheme:dark){body{background:#15181c;color:#e3e6ea}"
    "table{background:#1c2026;border-color:#2c323a}th{background:#232830}"
    "th,td{border-color:#2c323a}.meta,footer,.settings dt{color:#9aa4b1}"
    "tr.ok .st{color:#5cc27a}tr.warn .st{color:#e0b341}tr.ko .st{color:#f97066}"
    ".verdict.ok{color:#5cc27a}.verdict.warn{color:#e0b341}.verdict.ko{color:#f97066}}"
)


def _human_date(iso: str) -> tuple[str, str]:
    """(« 01/01/2026 », « 10:00:00 ») from an ISO timestamp, ("", "") if unparsable."""
    try:
        moment = datetime.fromisoformat(iso)
    except ValueError:
        return "", ""
    return moment.strftime("%d/%m/%Y"), moment.strftime("%H:%M:%S")


def _period(started_at: str, finished_at: str) -> str:
    """« Le 01/01/2026, de 10:00:00 à 10:01:00 » (both dates when they differ)."""
    start_day, start_time = _human_date(started_at)
    end_day, end_time = _human_date(finished_at)
    if not (start_day and end_day):
        return typo(f"Début : {started_at} · Fin : {finished_at}")
    if start_day == end_day:
        return f"Le {start_day}, de {start_time} à {end_time}"
    return f"Du {start_day} à {start_time} au {end_day} à {end_time}"


def _unread(entry: dict[str, Any]) -> str:
    count = int(entry["non_relus"])
    return plural(count, "image ou page non relue", "images ou pages non relues")


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def _md_cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _section(title: str, headers: list[str], rows: str, intro: str = "") -> str:
    """Table section; a header starting with ``#`` is a right-aligned number column."""
    head = "".join(
        f"<th class=n>{_esc(h[1:])}</th>" if h.startswith("#") else f"<th>{_esc(h)}</th>"
        for h in headers
    )
    if not rows:
        rows = f'<tr><td colspan="{len(headers)}">Aucun élément.</td></tr>'
    intro_html = f"<p>{intro}</p>" if intro else ""
    return (
        f"<section><h2>{_esc(title)}</h2>{intro_html}<div class=scroll><table>"
        f"<thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table></div></section>"
    )


def _status_label(status: str) -> str:
    try:
        return STATUS_LABELS.get(DocumentStatus(status), status)
    except ValueError:
        return status


def _status_class(label: str) -> str:
    if label == STATUS_LABELS[DocumentStatus.ANONYMIZED]:
        return "ok"
    if label == STATUS_LABELS[DocumentStatus.NEEDS_REVIEW]:
        return "warn"
    return "ko"


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
