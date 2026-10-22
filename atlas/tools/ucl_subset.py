"""`atlas ucl subset`: a deterministic ~120-thesis subset of the UCL thesis library as a project manifest
(increment 2 §5). Implemented by: increment 2.

Inputs: `UCL_theses_inventory.csv` (eprint_id, title, discipline, year, pages, has_text_layer, relpath) and
`UCL_theses_by_proximity.csv` (tier, rubric_total, relpath, …); PDFs under `<pdf_dir>/<relpath>`.

Rule (the manifest's `reason` column records which one admitted a thesis):
 (1) every proximity tier-1 and tier-2 thesis with a text layer and <= max_pages pages;
 (2) fill to `target` with discipline 工程 / 计算机 / 机器学习, year >= 2010, text layer, <= max_pages, highest rubric
     total first (ties by eprint id);
 (3) + 10 from 物理 / 天文 / 数学 / 统计 and 10 from 化学 / 材料科学 (year >= 2010, text layer) so the thesis doctype
     meets other layouts.
Writes `manifest.csv` (doc_id = T<eprint_id>, doc_type thesis, venue = discipline, absolute file), `project.yaml`
(doc_type thesis, pdf_dir) and `subset_report.md`.
"""
from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path

import yaml

DEFAULT_BASE = Path("H:/Weikang/01_Literature/UCL_theses")
INVENTORY = "UCL_theses_inventory.csv"
PROXIMITY = "UCL_theses_by_proximity.csv"
ENGINEERING = "工程 / 计算机 / 机器学习"
PHYSICS = "物理 / 天文 / 数学 / 统计"
CHEMISTRY = "化学 / 材料科学"


def _int(v, default=0) -> int:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default


def _yes(v) -> bool:
    return str(v or "").strip().upper() in ("Y", "YES", "1", "TRUE")


def select_subset(inventory: list[dict], proximity: list[dict], target: int = 120, max_pages: int = 450,
                  extra_each: int = 10, min_year: int = 2010) -> list[dict]:
    """Pure selection; rows get `reason` (rule 1 | rule 2 | rule 3a | rule 3b) and `tier` / `rubric`."""
    inv = {}
    for r in inventory:
        rel = (r.get("relpath") or "").strip()
        if rel:
            inv[rel] = r
    prox = {(r.get("relpath") or "").strip(): r for r in proximity}
    chosen: dict[str, dict] = {}

    def admit(rel: str, reason: str) -> None:
        r = inv[rel]
        p = prox.get(rel) or {}
        chosen[rel] = {"eprint_id": (r.get("eprint_id") or "").strip(), "title": (r.get("title") or "").strip(),
                       "discipline": (r.get("discipline") or "").strip(), "year": _int(r.get("year")) or None,
                       "pages": _int(r.get("pages")), "relpath": rel, "reason": reason,
                       "tier": _int(p.get("tier"), 0) or None, "rubric": _int(p.get("rubric_total"), 0)}

    def ok(rel: str) -> bool:
        r = inv.get(rel)
        return bool(r) and _yes(r.get("has_text_layer")) and 0 < _int(r.get("pages")) <= max_pages

    # rule 1
    for rel, p in sorted(prox.items(), key=lambda kv: (_int(kv[1].get("tier"), 9), -_int(kv[1].get("rubric_total")), kv[0])):
        if _int(p.get("tier"), 9) in (1, 2) and ok(rel):
            admit(rel, "rule 1")
    # rule 2
    pool = [rel for rel, r in inv.items() if rel not in chosen and (r.get("discipline") or "").strip() == ENGINEERING
            and _int(r.get("year")) >= min_year and ok(rel)]
    pool.sort(key=lambda rel: (-_int((prox.get(rel) or {}).get("rubric_total")), _int(inv[rel].get("eprint_id")), rel))
    for rel in pool:
        if len(chosen) >= target:
            break
        admit(rel, "rule 2")
    # rule 3
    for disc, tag in ((PHYSICS, "rule 3a"), (CHEMISTRY, "rule 3b")):
        pool = [rel for rel, r in inv.items() if rel not in chosen and (r.get("discipline") or "").strip() == disc
                and _int(r.get("year")) >= min_year and _yes(r.get("has_text_layer"))]
        pool.sort(key=lambda rel: (_int(inv[rel].get("pages")) > max_pages, -_int(inv[rel].get("year")), _int(inv[rel].get("eprint_id")), rel))
        for rel in pool[:extra_each]:
            admit(rel, tag)
    rows = list(chosen.values())
    rows.sort(key=lambda r: (["rule 1", "rule 2", "rule 3a", "rule 3b"].index(r["reason"]), r["tier"] or 9, -r["rubric"], r["eprint_id"]))
    return rows


def write_report(rows: list[dict], path: Path, missing: list[str]) -> None:
    by_rule = Counter(r["reason"] for r in rows)
    by_disc = Counter(r["discipline"] for r in rows)
    years = [r["year"] for r in rows if r["year"]]
    pages = sorted(r["pages"] for r in rows if r["pages"])
    lines = ["# UCL thesis subset", "", f"{len(rows)} theses selected ({len(missing)} listed PDFs missing on disk).", "",
             "## Per rule", ""] + [f"- {k}: {v}" for k, v in sorted(by_rule.items())]
    lines += ["", "## Disciplines", ""] + [f"- {k}: {v}" for k, v in by_disc.most_common()]
    if years:
        lines += ["", "## Years", "", f"- min {min(years)}, max {max(years)}, median {sorted(years)[len(years) // 2]}"]
    if pages:
        lines += ["", "## Pages", "", f"- min {pages[0]}, median {pages[len(pages) // 2]}, max {pages[-1]}"]
    if missing:
        lines += ["", "## Missing files", ""] + [f"- {m}" for m in missing[:50]]
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def make_subset(root: str | Path, inventory: str | Path | None = None, proximity: str | Path | None = None,
                pdf_dir: str | Path | None = None, target: int = 120) -> dict:
    root = Path(root).resolve()
    base = DEFAULT_BASE
    inv_path = Path(inventory) if inventory else base / INVENTORY
    prox_path = Path(proximity) if proximity else base / PROXIMITY
    pdf_root = Path(pdf_dir) if pdf_dir else base / "pdf"
    with open(inv_path, encoding="utf-8-sig", newline="") as f:
        inventory_rows = list(csv.DictReader(f))
    with open(prox_path, encoding="utf-8-sig", newline="") as f:
        proximity_rows = list(csv.DictReader(f))
    rows = select_subset(inventory_rows, proximity_rows, target=target)
    root.mkdir(parents=True, exist_ok=True)
    missing = []
    manifest = root / "manifest.csv"
    with open(manifest, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["doc_id", "title", "year", "authors", "doi", "venue", "doc_type", "file", "reason", "tier", "rubric", "pages"])
        for r in rows:
            p = pdf_root / r["relpath"]
            if not p.is_file():
                missing.append(r["relpath"])
            w.writerow([f"T{r['eprint_id']}", r["title"], r["year"] or "", "", "", r["discipline"], "thesis", str(p),
                        r["reason"], r["tier"] or "", r["rubric"], r["pages"]])
    pf = root / "project.yaml"
    if not pf.exists():
        with open(pf, "w", encoding="utf-8") as f:
            f.write("# concept-atlas project configuration; see atlas/defaults.yaml for every key\n")
            yaml.safe_dump({"name": root.name, "doc_type": "thesis", "paths": {"pdf_dir": str(pdf_root)}}, f,
                           allow_unicode=True, sort_keys=False)
    report = root / "subset_report.md"
    write_report(rows, report, missing)
    return {"n": len(rows), "manifest": str(manifest), "report": str(report), "missing": missing,
            "per_rule": dict(Counter(r["reason"] for r in rows))}
