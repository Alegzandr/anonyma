"""Comparison tables of two measurements (Markdown, French).

``uv run python -m tests.benchmark.compare baseline final`` prints the tables used in
``RESULTS.md``: recall per type, per format and per type x format, precision per
format, file statuses, for every profile present in both result files.
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from typing import Any

from tests.benchmark.common import DIRECT_TYPES, INDIRECT_TYPES, RESULTS

SHORT = {
    "PERSONNE": "PERS",
    "EMAIL": "MAIL",
    "TELEPHONE": "TEL",
    "IBAN": "IBAN",
    "CARTE": "CB",
    "NIR": "NIR",
    "ADRESSE": "ADR",
    "PLAQUE": "PLAQ",
    "IP": "IP",
    "DATE_NAISSANCE": "DDN",
    "IDENTIFIANT": "LOGIN",
}


def pct(ok: int, total: int) -> str:
    return "—" if total == 0 else f"{100 * ok / total:.1f} %"


def load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((RESULTS / f"{name}.json").read_text(encoding="utf-8"))
    return data


def matrix(res: dict[str, Any]) -> dict[str, dict[str, list[int]]]:
    out: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    for doc in res["docs"]:
        for e in doc["entries"]:
            cell = out[doc["format"]][e["type"]]
            cell[0] += 0 if e["leaked"] else 1
            cell[1] += 1
    return out


def summary(profile: str, before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    b, a = before["aggregate"], after["aggregate"]
    rows = [
        ("Rappel identifiants directs", b["direct"], a["direct"]),
        ("Rappel autres types", b["indirect"], a["indirect"]),
        ("Précision (segments remplacés)", b["precision"], a["precision"]),
        ("Précision hors dates non liées à une naissance", b["precision_without_dates"],
         a["precision_without_dates"]),
        ("Phrases témoins intactes", b["keep"], a["keep"]),
    ]  # fmt: skip
    lines = [f"### Profil {profile}", "", "| Mesure | Référence | Final |", "|---|---|---|"]
    for label, x, y in rows:
        lines.append(f"| {label} | {pct(*x)} ({x[0]}/{x[1]}) | **{pct(*y)}** ({y[0]}/{y[1]}) |")
    seconds = f"{before['seconds']:.0f} s | {after['seconds']:.0f} s"
    lines.append(f"| Durée du lot (24 documents) | {seconds} |")
    return [*lines, ""]


def by_type(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    lines = ["| Type | Valeurs | Référence | Final |", "|---|---|---|---|"]
    for kind in (*DIRECT_TYPES, *INDIRECT_TYPES):
        x = before["aggregate"]["by_type"].get(kind)
        y = after["aggregate"]["by_type"].get(kind)
        if y is None:
            continue
        tag = "" if kind in DIRECT_TYPES else " *"
        lines.append(f"| {kind}{tag} | {y[1]} | {pct(*x)} | {pct(*y)} |")
    return [*lines, ""]


def by_format(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    lines = [
        "| Format | Rappel direct réf. | Rappel direct final | Précision réf. | Précision finale |"
        " Statuts finaux |",
        "|---|---|---|---|---|---|",
    ]

    def direct(res: dict[str, Any]) -> dict[str, list[int]]:
        out: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for doc in res["docs"]:
            for e in doc["entries"]:
                if e["direct"]:
                    out[doc["format"]][0] += 0 if e["leaked"] else 1
                    out[doc["format"]][1] += 1
        return out

    db, da = direct(before), direct(after)
    pb = before["aggregate"]["precision_by_format"]
    pa = after["aggregate"]["precision_by_format"]
    for fmt in sorted(da):
        statuses: Counter[str] = Counter()
        for doc in after["docs"]:
            if doc["format"] == fmt:
                statuses.update(after["statuses"].get(doc["slug"], ["(aucun)"]))
        status = ", ".join(f"{k} ×{v}" for k, v in sorted(statuses.items()))
        lines.append(
            f"| {fmt} | {pct(*db[fmt])} | {pct(*da[fmt])} | {pct(*pb.get(fmt, [0, 0]))} | "
            f"{pct(*pa.get(fmt, [0, 0]))} | {status} |"
        )
    return [*lines, ""]


def type_format(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    mb, ma = matrix(before), matrix(after)
    head = "| Format | " + " | ".join(SHORT[t] for t in DIRECT_TYPES) + " |"
    lines = [head, "|---|" + "---|" * len(DIRECT_TYPES)]
    for fmt in sorted(ma):
        cells = []
        for kind in DIRECT_TYPES:
            y = ma[fmt].get(kind)
            if y is None:
                cells.append("")
                continue
            x = mb[fmt].get(kind, [0, 0])
            cell = f"{y[0]}/{y[1]}"
            if x[0] != y[0]:
                cell += f" ({x[0]})"
            cells.append(cell)
        lines.append(f"| {fmt} | " + " | ".join(cells) + " |")
    return [*lines, ""]


def statuses(res: dict[str, Any]) -> str:
    counts: Counter[str] = Counter()
    for values in res["statuses"].values():
        counts.update(values)
    return ", ".join(f"{k} ×{v}" for k, v in sorted(counts.items()))


def main(argv: list[str]) -> None:
    before, after = load(argv[0]), load(argv[1])
    out: list[str] = []
    for profile in after["profiles"]:
        if profile not in before["profiles"]:
            continue
        b, a = before["profiles"][profile], after["profiles"][profile]
        out += summary(profile, b, a)
        out += ["Statuts des fichiers : référence " + statuses(b) + " ; final " + statuses(a), ""]
        out += [f"#### Rappel par type ({profile})", "", *by_type(b, a)]
        out += [f"#### Par format ({profile})", "", *by_format(b, a)]
        out += [
            f"#### Identifiants directs par type × format ({profile}, final)",
            "",
            "Valeurs protégées / valeurs ; entre parenthèses, la référence quand elle diffère.",
            "",
            *type_format(b, a),
        ]
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    print("\n".join(out))


if __name__ == "__main__":
    main(sys.argv[1:] or ["baseline", "final"])
