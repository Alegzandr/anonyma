"""End-of-batch report. Contains anonymized names, counts and codes only: never a
sensitive value.

The verdict, the exit code of ``run`` and every count come from the same outcomes of
the top-level files (:meth:`BatchReport.file_outcomes`): they never disagree. The
JSON vocabulary (:meth:`BatchReport.to_dict`, :meth:`BatchReport.counts`) is the one
documented in REFERENCE.md (« Automatisation »).
"""

from __future__ import annotations

import html
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from anonyma.brand import app_svg_data_uri
from anonyma.frenchtext import NBSP, plural, typo
from anonyma.models import DocumentResult, DocumentStatus, Regime, Severity, UserError
from anonyma.pipeline import RESIDUE_ISSUE, BatchResult, is_quarantined

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
RESIDUE_INTRO = (
    "Résidu certain : le fichier est placé en quarantaine (dossier « …_quarantaine » à "
    "côté du dossier de sortie) et ne doit pas être diffusé. Résidu possible : signal "
    "statistique que la seconde passe n'a pas pu masquer, le fichier est « À revoir » : "
    "vérifiez la zone indiquée avant diffusion."
)

_QUARANTINED = "en quarantaine (ne pas diffuser)"

#: Version of the JSON vocabulary of ``run --json`` and of JSON reports.
SCHEMA_VERSION = 3

#: Outcome of a top-level file outside quarantine, by status. A RESIDUE file that is
#: not in quarantine only holds possible residues: it is to be reviewed.
_OUTCOME_BY_STATUS = {
    DocumentStatus.ANONYMIZED: "anonymise",
    DocumentStatus.NEEDS_REVIEW: "a_revoir",
    DocumentStatus.RESIDUE: "a_revoir",
    DocumentStatus.UNSUPPORTED: "non_supporte",
    DocumentStatus.CANCELLED: "annule",
}
#: Outcomes of top-level files, in display order.
OUTCOMES = ("anonymise", "a_revoir", "quarantaine", "echec", "non_supporte", "annule")
#: Exit code of ``run`` for each verdict.
EXIT_CODE_BY_VERDICT = {"anonymise": 0, "a_revoir": 0, "vide": 0, "quarantaine": 1, "echec": 1}
#: Statuses of an item that produced no output (unless an output path is known).
_NOT_WRITTEN = frozenset(
    {DocumentStatus.FAILED, DocumentStatus.UNSUPPORTED, DocumentStatus.CANCELLED}
)
#: Separator in the path of an embedded item (« archive.zip › membre.pdf »).
PATH_SEPARATOR = " › "
#: Issue code of an embedded item left out of its container.
OMITTED_CODE = "child.omitted"

#: Residue origins (``Residue.reason``): JSON code and French label.
_ORIGINS = {
    "original_value": ("valeur_origine", "valeur d'origine"),
    "detector": ("detecteur", "détecteur"),
}
#: French labels of the verification sources that are not detectors of a profile.
_VERIFICATION_SOURCES = {
    "table": "Table du lot",
    "table_variante": "Table du lot (variante)",
    "table_ocr": "Table du lot (lecture OCR)",
    "nom_origine": "Nom d'origine du fichier",
    "octets_bruts": "Octets bruts du fichier",
}
_TYPE_LABELS = {
    "NOM_FICHIER": "Nom de fichier",
    "FICHIER_INCORPORE": "Fichier incorporé non vérifié",
}

#: Extensions accepted by :meth:`BatchReport.save`.
REPORT_EXTENSIONS = (".json", ".md", ".markdown", ".html", ".htm")


def status_label(status: DocumentStatus | str, regime: Regime = Regime.ANONYMIZATION) -> str:
    """French label of a status; « Pseudonymisé » for a pseudonymized output."""
    try:
        status = DocumentStatus(status)
    except ValueError:
        return str(status)
    if status is DocumentStatus.ANONYMIZED and regime is Regime.PSEUDONYMIZATION:
        return "Pseudonymisé"
    return STATUS_LABELS.get(status, str(status))


def detector_label(name: str) -> str:
    """French label of a detector or verification source (« Règles », « Table du lot »)."""
    from anonyma.gui.labels import detector_label as gui_label

    return _VERIFICATION_SOURCES.get(name) or gui_label(name)


def type_label(entity_type: str) -> str:
    """French label of an entity type (« Personne »)."""
    from anonyma.gui.labels import entity_type_label

    return _TYPE_LABELS.get(entity_type) or entity_type_label(entity_type)


def origin_label(reason: str) -> str:
    """French label of a residue origin (« valeur d'origine », « détecteur »)."""
    return _ORIGINS.get(reason, (reason, reason))[1]


def written(result: DocumentResult) -> bool:
    """Whether an output exists for ``result`` (so that its occurrences were masked)."""
    return result.output_path is not None or result.status not in _NOT_WRITTEN


def omitted_count(result: DocumentResult) -> int:
    """Embedded items of ``result``'s tree left out of their container."""
    return sum(1 for r in _walk(result) for i in r.issues if i.code == OMITTED_CODE)


def masked_counts(result: DocumentResult) -> dict[str, int]:
    """Occurrences masked in ``result``'s output, by type (none without an output)."""
    counts: dict[str, int] = {}

    def visit(r: DocumentResult) -> None:
        if not written(r):
            return
        for t, n in r.entity_counts.items():
            counts[t] = counts.get(t, 0) + n
        for child in r.children:
            visit(child)

    visit(result)
    return counts


@dataclass(slots=True)
class BatchReport:
    batch_id: str
    started_at: str
    finished_at: str
    #: Top-level files by status.
    status_counts: dict[str, int] = field(default_factory=dict)
    #: Occurrences masked, by type (an item without an output counts none).
    entity_counts: dict[str, int] = field(default_factory=dict)
    #: One entry per top-level file. ``statut`` is the French label (« Anonymisé »)
    #: and ``code`` the status code; the JSON export gives codes (:meth:`to_dict`).
    files: list[dict[str, Any]] = field(default_factory=list)
    residues: list[dict[str, str]] = field(default_factory=list)
    #: Occurrences masked by the corrective pass, after a first verification.
    corrected: int = 0
    #: Items (any depth) that failed or hold an error: their path (« archive.zip ›
    #: membre.pdf »), French label, status code, messages and issue codes.
    failures: list[dict[str, Any]] = field(default_factory=list)
    #: Files whose verification could not read every image or page (OCR): never
    #: anonymized, kept in quarantine for a visual check.
    incomplete: list[dict[str, Any]] = field(default_factory=list)
    #: Non-sensitive batch settings (profile, mode, regime...), French labels.
    settings: dict[str, str] = field(default_factory=dict)
    #: The same settings as machine values (``{"profil": "leger", ...}``).
    parameters: dict[str, Any] = field(default_factory=dict)
    regime: Regime = Regime.ANONYMIZATION
    #: For each entry of ``files`` (same order): its status, and whether its output was
    #: placed in the quarantine folder.
    file_statuses: list[DocumentStatus] = field(default_factory=list)
    file_placed: list[bool] = field(default_factory=list)
    #: Every item (top-level files, attachments, archive members) by status.
    item_status_counts: dict[str, int] = field(default_factory=dict)
    #: Embedded items left out of their container (unreadable, encrypted...).
    omitted: int = 0

    @classmethod
    def from_result(cls, result: BatchResult) -> BatchReport:
        report = cls(
            result.batch_id,
            result.started_at.isoformat(timespec="seconds"),
            result.finished_at.isoformat(timespec="seconds"),
            regime=getattr(result, "regime", Regime.ANONYMIZATION),
        )
        for top in result.results:
            report.status_counts[top.status] = report.status_counts.get(top.status, 0) + 1
            placed = is_quarantined(top, result.output_dir)
            report.files.append(_file_entry(top))
            report.file_statuses.append(top.status)
            report.file_placed.append(placed)
            report.omitted += omitted_count(top)
            report.corrected += top.corrected_count
            for t, n in masked_counts(top).items():
                report.entity_counts[t] = report.entity_counts.get(t, 0) + n
            for r, path in walk_paths(top):
                status = r.status.value
                report.item_status_counts[status] = report.item_status_counts.get(status, 0) + 1
                # A residue is reported (and counted) as such, not as a failure: the
                # file was anonymized and verified, then quarantined.
                errors = [
                    i for i in r.issues if i.severity is Severity.ERROR and i.code != RESIDUE_ISSUE
                ]
                if errors or r.status in (DocumentStatus.FAILED, DocumentStatus.UNSUPPORTED):
                    report.failures.append(
                        {
                            "fichier": path,
                            "statut": STATUS_LABELS.get(r.status, r.status),
                            "code": r.status.value,
                            "problemes": [i.message for i in r.issues],
                            "codes": [i.code for i in r.issues],
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

    def _in_quarantine(self) -> list[bool]:
        """Per entry of ``files``: placed in quarantine, or holding a *certain* residue.
        A possible (statistical) residue only sends its file to review."""
        certain = {r["fichier"] for r in self.residues if r.get("certitude") == "certain"}
        placed = self.file_placed or [False] * len(self.files)
        return [p or f["fichier"] in certain for f, p in zip(self.files, placed, strict=True)]

    @property
    def quarantined(self) -> int:
        """Number of top-level files in quarantine (never to be shared)."""
        return sum(self._in_quarantine())

    def file_outcomes(self) -> list[str]:
        """Outcome of each entry of ``files`` (one of :data:`OUTCOMES`)."""
        statuses = self.file_statuses or [DocumentStatus.FAILED] * len(self.files)
        return [
            "quarantaine" if quarantined else _OUTCOME_BY_STATUS.get(status, "echec")
            for status, quarantined in zip(statuses, self._in_quarantine(), strict=True)
        ]

    def _outcomes(self) -> dict[str, int]:
        """Top-level files by outcome; a quarantined file counts only as such."""
        outcomes = dict.fromkeys(OUTCOMES, 0)
        for outcome in self.file_outcomes():
            outcomes[outcome] += 1
        return outcomes

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

    def exit_code(self) -> int:
        """Exit code of ``run`` (0 or 1), derived from the verdict."""
        return EXIT_CODE_BY_VERDICT[self.verdict_code()]

    @property
    def _done_word(self) -> str:
        return "pseudonymisé" if self.regime is Regime.PSEUDONYMIZATION else "anonymisé"

    def verdict(self) -> tuple[str, str]:
        """One-line French verdict and its level (``ok``, ``warn`` or ``ko``):
        « 8 fichiers anonymisés et vérifiés. » or
        « 6 anonymisés · 1 à revoir · 1 en quarantaine (ne pas diffuser) »
        (« pseudonymisés » in pseudonymization)."""
        outcomes = self._outcomes()
        total = len(self.files)
        if not total:
            return "Aucun fichier traité.", "warn"
        done = self._done_word
        if outcomes["anonymise"] == total:
            text = plural(total, f"fichier {done} et vérifié", f"fichiers {done}s et vérifiés")
            return text + ".", "ok"
        parts = [
            plural(outcomes["anonymise"], done),
            plural(outcomes["a_revoir"], "à revoir", "à revoir"),
            plural(outcomes["quarantaine"], _QUARANTINED, _QUARANTINED),
            plural(outcomes["echec"], "en échec", "en échec"),
            plural(outcomes["non_supporte"], "non pris en charge", "non pris en charge"),
            plural(outcomes["annule"], "annulé"),
        ]
        shown = [part for part in parts if not part.startswith(f"0{NBSP}")]
        level = "warn" if self.verdict_code() == "a_revoir" else "ko"
        return " · ".join(shown), level

    # -- JSON -------------------------------------------------------------------

    def counts(self) -> dict[str, Any]:
        """Batch counts, shared by the JSON report and ``run --json``."""
        outcomes = self._outcomes()
        items = sum(self.item_status_counts.values())
        certain = sum(1 for r in self.residues if r.get("certitude") == "certain")
        occurrences = dict(sorted(self.entity_counts.items()))
        return {
            "nombre_fichiers": len(self.files),
            "nombre_elements": items,
            "nombre_elements_inclus": max(0, items - len(self.files)),
            "elements_omis": self.omitted,
            "statuts": {str(s): n for s, n in sorted(self.status_counts.items())},
            "resultats": outcomes,
            "statuts_elements": dict(sorted(self.item_status_counts.items())),
            "quarantaine": self.quarantined,
            "fichiers_a_revoir": outcomes["a_revoir"],
            "fichiers_en_echec": outcomes["echec"],
            "masques_apres_verification": self.corrected,
            "residus_certains": certain,
            "residus_possibles": len(self.residues) - certain,
            "occurrences": sum(occurrences.values()),
            "occurrences_par_type": occurrences,
        }

    def file_records(self) -> list[dict[str, Any]]:
        """JSON entries of ``files``: codes, French labels apart."""
        records = []
        for entry, outcome, quarantined in zip(
            self.files, self.file_outcomes(), self._in_quarantine(), strict=True
        ):
            code = str(entry.get("code", ""))
            records.append(
                {
                    "fichier": entry["fichier"],
                    "format": entry.get("format", ""),
                    "format_sortie": entry.get("format_sortie", ""),
                    "statut": code,
                    "statut_libelle": status_label(code, self.regime),
                    "resultat": outcome,
                    "quarantaine": quarantined,
                    "occurrences": entry["entites"],
                    # Obsolete name of « occurrences ».
                    "entites": entry["entites"],
                    "residus": entry["residus"],
                    "residus_possibles": entry.get("residus_possibles", 0),
                    "masques_apres_verification": entry.get("masques_apres_verification", 0),
                    "elements_omis": entry.get("elements_omis", 0),
                }
            )
        return records

    def residue_records(self) -> list[dict[str, str]]:
        """JSON entries of ``residues``: codes, French labels apart."""
        return [
            {
                **r,
                "origine": _ORIGINS.get(r["origine"], (r["origine"], r["origine"]))[0],
                "origine_libelle": origin_label(r["origine"]),
                "type_libelle": type_label(r["type"]),
                "detecteur_libelle": detector_label(r["detecteur"]),
            }
            for r in self.residues
        ]

    def failure_records(self) -> list[dict[str, Any]]:
        """JSON entries of ``failures``: status code, French label apart."""
        return [
            {
                "fichier": f["fichier"],
                "statut": str(f.get("code", "")),
                "statut_libelle": status_label(str(f.get("code", "")), self.regime),
                "problemes": list(f["problemes"]),
                "codes": list(f.get("codes", [])),
            }
            for f in self.failures
        ]

    def to_dict(self) -> dict[str, Any]:
        """The JSON report: every count of :meth:`counts`, then the details. Codes
        are machine values; French labels sit in ``*_libelle`` keys."""
        text, _ = self.verdict()
        counts = self.counts()
        data: dict[str, Any] = {
            "schema": SCHEMA_VERSION,
            "lot": self.batch_id,
            "debut": self.started_at,
            "fin": self.finished_at,
            "regime": self.regime.value,
            "verdict": self.verdict_code(),
            "verdict_libelle": text,
            "code_sortie": self.exit_code(),
            **counts,
            # Obsolete name of « occurrences_par_type » (these are occurrences).
            "entites_par_type": counts["occurrences_par_type"],
            "fichiers": self.file_records(),
            "residus": self.residue_records(),
            "echecs": self.failure_records(),
            "verifications_incompletes": self.incomplete,
        }
        if self.parameters or self.settings:
            data["parametres"] = self.parameters
            data["parametres_libelles"] = self.settings
        return data

    def save_json(self, path: Path) -> None:
        path.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")

    # -- Markdown -------------------------------------------------------------

    def _file_rows(self) -> Iterator[tuple[dict[str, Any], str, str]]:
        """(entry, French status label, occurrences cell) of each file."""
        for entry in self.files:
            label = status_label(str(entry.get("code", "")), self.regime)
            if not entry.get("code"):
                label = str(entry["statut"])
            occurrences = str(entry["entites"]) if entry.get("ecrit", True) else "—"
            yield entry, label, occurrences

    def to_markdown(self) -> str:
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
        for f, label, occurrences in self._file_rows():
            lines.append(f"| {c(f['fichier'])} | {c(label)} | {occurrences} | {f['residus']} |")
        lines += [
            "",
            "## Occurrences masquées par type",
            "",
            "| Type | Code | Occurrences |",
            "|---|---|---|",
        ]
        lines += [
            f"| {c(type_label(t))} | {c(t)} | {n} |" for t, n in sorted(self.entity_counts.items())
        ]
        if self.residues:
            lines += [
                "",
                "## Résidus",
                "",
                typo(RESIDUE_INTRO),
                "",
                "| Fichier | Emplacement | Zone | Type | Origine | Détecteur | Certitude |",
                "|---|---|---|---|---|---|---|",
            ]
            lines += [
                f"| {c(r['fichier'])} | {c(r['emplacement'])} | {c(r['zone'])} "
                f"| {c(type_label(r['type']))} | {c(origin_label(r['origine']))} "
                f"| {c(detector_label(r['detecteur']))} | {c(r['certitude'])} |"
                for r in self.residues
            ]
        if self.incomplete:
            lines += ["", "## Vérification incomplète", "", typo(INCOMPLETE_INTRO), ""]
            lines += [f"- **{c(f['fichier'])}**{NBSP}: {_unread(f)}" for f in self.incomplete]
        if self.failures:
            lines += ["", "## Échecs", ""]
            for f in self.failures:
                problems = " ; ".join(f["problemes"])
                detail = f"{NBSP}: {problems}" if problems else ""
                label = status_label(str(f.get("code", "")), self.regime)
                lines.append(f"- **{c(f['fichier'])}** ({label}){detail}")
        return "\n".join(lines) + "\n"

    def save_markdown(self, path: Path) -> None:
        path.write_text(self.to_markdown(), encoding="utf-8")

    # -- HTML -----------------------------------------------------------------

    def to_html(self) -> str:
        """Self-contained HTML page: inline CSS, no script, no external resource."""
        e = _esc
        verdict, level = self.verdict()
        files_count = plural(len(self.files), "fichier")
        occurrences = plural(
            sum(self.entity_counts.values()), "occurrence masquée", "occurrences masquées"
        )
        status_rows = "".join(
            f"<tr><td>{e(status_label(s, self.regime))}</td><td class=n>{n}</td></tr>"
            for s, n in sorted(self.status_counts.items())
        )
        file_rows = "".join(
            f"<tr class={_status_class(str(f.get('code', '')))}><td>{e(f['fichier'])}</td>"
            f"<td>{e(f.get('format', ''))}</td><td class=st>{e(label)}</td>"
            f"<td class=n>{e(occurrences_cell)}</td><td class=n>{e(f['residus'])}</td>"
            f"<td class=n>{e(f.get('residus_possibles', 0))}</td></tr>"
            for f, label, occurrences_cell in self._file_rows()
        )
        entity_rows = "".join(
            f"<tr><td>{e(type_label(t))}</td><td><code>{e(t)}</code></td><td class=n>{n}</td></tr>"
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
                ["Fichier", "Format", "Statut", "#Occurrences", "#Résidus", "#Possibles"],
                file_rows,
            ),
            _section(
                "Occurrences masquées par type", ["Type", "Code", "#Occurrences"], entity_rows
            ),
        ]
        if self.residues:
            rows = "".join(
                f"<tr><td>{e(r['fichier'])}</td><td>{e(r['emplacement'])}</td>"
                f"<td>{e(r['zone'])}</td><td>{e(type_label(r['type']))}</td>"
                f"<td>{e(origin_label(r['origine']))}</td>"
                f"<td>{e(detector_label(r['detecteur']))}</td><td>{e(r['certitude'])}</td></tr>"
                for r in self.residues
            )
            parts.append(
                _section(
                    "Résidus",
                    ["Fichier", "Emplacement", "Zone", "Type", "Origine", "Détecteur", "Certitude"],
                    rows,
                    intro=e(typo(RESIDUE_INTRO)),
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
                f"<li><strong>{e(f['fichier'])}</strong> "
                f"({e(status_label(str(f.get('code', '')), self.regime))})"
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


def _status_class(code: str) -> str:
    if code == DocumentStatus.ANONYMIZED:
        return "ok"
    if code == DocumentStatus.NEEDS_REVIEW:
        return "warn"
    return "ko"


def _walk(result: DocumentResult) -> list[DocumentResult]:
    out = [result]
    for c in result.children:
        out.extend(_walk(c))
    return out


def walk_paths(result: DocumentResult, prefix: str = "") -> Iterator[tuple[DocumentResult, str]]:
    """Every item of ``result``'s tree with its path (« archive.zip › membre.pdf »)."""
    path = f"{prefix}{PATH_SEPARATOR}{result.output_name}" if prefix else result.output_name
    yield result, path
    for child in result.children:
        yield from walk_paths(child, path)


def _file_entry(r: DocumentResult) -> dict[str, Any]:
    return {
        "fichier": r.output_name,
        "format": r.format,
        "format_sortie": r.output_format,
        "statut": STATUS_LABELS.get(r.status, r.status),
        "code": r.status.value,
        "ecrit": written(r),
        "entites": sum(masked_counts(r).values()),
        "residus": r.residue_count,
        "residus_possibles": r.possible_residue_count,
        "masques_apres_verification": r.corrected_count,
        "elements_omis": omitted_count(r),
    }
