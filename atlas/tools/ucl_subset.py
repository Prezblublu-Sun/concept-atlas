"""`atlas ucl subset`: a deterministic ~120-thesis subset of the UCL thesis library as a project manifest
(increment 2 §5). Implemented by: increment 2 (agent thesis).

Inputs: `UCL_theses_inventory.csv` (eprint_id, title, discipline, year, pages, has_text_layer, title_source, relpath;
1,030 rows) and `UCL_theses_by_proximity.csv` (tier 1–3, rubric_total, relpath, …; 1,401 rows, not all of them in
the inventory); PDFs under `<pdf_dir>/<relpath>`.

Rule (deterministic; the manifest's `reason` column records which step admitted a thesis):
 (1) `rule 1`  every proximity tier-1 and tier-2 thesis with a text layer and <= max_pages pages (ordered by tier,
               rubric total desc, relpath);
 (2) `rule 2`  fill up to `target` with discipline 工程 / 计算机 / 机器学习, year >= min_year, text layer, <= max_pages,
               highest rubric total first (ties: eprint id, then relpath); the library holds only 49 such theses, so
               when they run out the fill continues with the older engineering theses of the same shape, highest
               rubric first (`rule 2b`);
 (3) `rule 3a` + `extra_each` from 物理 / 天文 / 数学 / 统计 and `rule 3b` + `extra_each` from 化学 / 材料科学 (text layer;
               year >= min_year first, newest first, then shortest within <= max_pages, then eprint id; older ones
               only when the recent ones run out) so the thesis doctype meets other layouts.
A thesis admitted by an earlier rule is never re-admitted. Rows are sorted by rule, tier, rubric desc, eprint id.

Manifest row: doc_id = T<eprint_id>, title = the inventory title unless it is a file name, 'ProQuest Dissertations',
a dedication or a declaration sentence (then empty, so `atlas docs add` takes the PDF's own metadata / first-page
guess), year, venue = discipline, doc_type thesis, file = absolute path, plus reason, tier, rubric, pages.
Writes `manifest.csv`, `project.yaml` (doc_type thesis, pdf_dir = the UCL pdf folder; never overwritten) and
`subset_report.md` (counts per rule and tier, disciplines, years, pages, blanked titles, missing files).
"""
from __future__ import annotations

import csv
import re
from collections import Counter
from pathlib import Path

import yaml

DEFAULT_BASE = Path("H:/Weikang/01_Literature/UCL_theses")
INVENTORY = "UCL_theses_inventory.csv"
PROXIMITY = "UCL_theses_by_proximity.csv"
ENGINEERING = "工程 / 计算机 / 机器学习"
PHYSICS = "物理 / 天文 / 数学 / 统计"
CHEMISTRY = "化学 / 材料科学"
RULES = ("rule 1", "rule 2", "rule 2b", "rule 3a", "rule 3b")
MANIFEST_COLUMNS = ("doc_id", "title", "year", "authors", "doi", "venue", "doc_type", "file", "reason", "tier",
                    "rubric", "pages")
#: inventory titles that are not titles: file names, ProQuest cover sheets, dedications, declaration sentences
JUNK_TITLE_RE = re.compile(r"proquest|microsoft word|\.docx?\b|\.pdf\b|^thesis\b|[-_]thesis|thesis[-_ ]?(?:final|"
                           r"submission|ecopy)|\bphd thesis\b|dedicat|confirm that|^final\b|^\d+$|\b\d{5,}\b", re.I)
TITLE_MIN_WORDS = 3


def _int(v, default=0) -> int:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default


def _yes(v) -> bool:
    return str(v or "").strip().upper() in ("Y", "YES", "1", "TRUE")


def clean_title(title: str | None) -> str:
    """The inventory title, or '' when it is a file name / cover-sheet text / too short to be a title."""
    t = " ".join((title or "").split())
    if len(t.split()) < TITLE_MIN_WORDS or JUNK_TITLE_RE.search(t):
        return ""
    return t


def select_subset(inventory: list[dict], proximity: list[dict], target: int = 120, max_pages: int = 450,
                  extra_each: int = 10, min_year: int = 2010) -> list[dict]:
    """Pure selection; rows get `reason` (rule 1 | rule 2 | rule 3a | rule 3b), `tier` / `rubric` and the cleaned
    `title` (`title_raw` keeps the inventory text)."""
    inv: dict[str, dict] = {}
    for r in inventory:
        rel = (r.get("relpath") or "").strip()
        if rel:
            inv[rel] = r
    prox = {(r.get("relpath") or "").strip(): r for r in proximity}
    chosen: dict[str, dict] = {}

    def admit(rel: str, reason: str) -> None:
        r = inv[rel]
        p = prox.get(rel) or {}
        chosen[rel] = {"eprint_id": (r.get("eprint_id") or "").strip(), "title": clean_title(r.get("title")),
                       "title_raw": " ".join((r.get("title") or "").split()),
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
    # rule 2 (recent engineering theses first, then older ones as rule 2b)
    pool = [rel for rel, r in inv.items() if rel not in chosen and (r.get("discipline") or "").strip() == ENGINEERING
            and ok(rel)]
    pool.sort(key=lambda rel: (_int(inv[rel].get("year")) < min_year, -_int((prox.get(rel) or {}).get("rubric_total")),
                               _int(inv[rel].get("eprint_id")), rel))
    for rel in pool:
        if len(chosen) >= target:
            break
        admit(rel, "rule 2" if _int(inv[rel].get("year")) >= min_year else "rule 2b")
    # rule 3
    for disc, tag in ((PHYSICS, "rule 3a"), (CHEMISTRY, "rule 3b")):
        pool = [rel for rel, r in inv.items() if rel not in chosen and (r.get("discipline") or "").strip() == disc
                and _yes(r.get("has_text_layer"))]
        pool.sort(key=lambda rel: (_int(inv[rel].get("year")) < min_year, _int(inv[rel].get("pages")) > max_pages,
                                   -_int(inv[rel].get("year")), _int(inv[rel].get("pages")), _int(inv[rel].get("eprint_id")), rel))
        for rel in pool[:extra_each]:
            admit(rel, tag)
    rows = list(chosen.values())
    rows.sort(key=lambda r: (RULES.index(r["reason"]), r["tier"] or 9, -r["rubric"], _int(r["eprint_id"]), r["relpath"]))
    return rows


def _median(values: list[int]) -> int:
    s = sorted(values)
    return s[len(s) // 2]


def write_report(rows: list[dict], path: Path, missing: list[str], target: int, max_pages: int, min_year: int) -> None:
    by_rule = Counter(r["reason"] for r in rows)
    by_tier = Counter(f"tier {r['tier']}" if r["tier"] else "no proximity row" for r in rows)
    by_disc = Counter(r["discipline"] for r in rows)
    years = [r["year"] for r in rows if r["year"]]
    pages = [r["pages"] for r in rows if r["pages"]]
    blanked = [r for r in rows if not r["title"]]
    lines = ["# UCL thesis subset", "",
             f"{len(rows)} theses selected ({len(missing)} listed PDFs missing on disk); target {target}, "
             f"max {max_pages} pages, year >= {min_year} for rules 2 and 3.", "",
             "Rule: (1) every proximity tier-1/tier-2 thesis with a text layer and <= max pages; (2) fill to the target "
             f"with 工程 / 计算机 / 机器学习 of year >= {min_year}, highest rubric total first, then (2b) older engineering "
             "theses the same way when the recent ones run out; (3a) 10 from 物理 / 天文 / 数学 / 统计 and (3b) 10 from "
             "化学 / 材料科学 (recent first, newest first) for other layouts. The manifest's `reason` column names the "
             "admitting rule.", "",
             "## Per rule", ""] + [f"- {k}: {by_rule.get(k, 0)}" for k in RULES]
    lines += ["", "## Per proximity tier", ""] + [f"- {k}: {v}" for k, v in sorted(by_tier.items())]
    lines += ["", "## Disciplines", ""] + [f"- {k}: {v}" for k, v in by_disc.most_common()]
    if years:
        decades = Counter(f"{y // 10 * 10}s" for y in years)
        lines += ["", "## Years", "", f"- min {min(years)}, median {_median(years)}, max {max(years)}"]
        lines += [f"- {k}: {v}" for k, v in sorted(decades.items())]
    if pages:
        lines += ["", "## Pages", "", f"- min {min(pages)}, median {_median(pages)}, max {max(pages)}, total {sum(pages)}"]
    lines += ["", "## Titles", "",
              f"- {len(blanked)} inventory titles blanked (file names / cover sheets; `atlas docs add` takes the PDF's own)"]
    lines += [f"  - T{r['eprint_id']}: {r['title_raw'][:80]}" for r in blanked[:30]]
    if missing:
        lines += ["", "## Missing files", ""] + [f"- {m}" for m in missing[:50]]
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def make_subset(root: str | Path, inventory: str | Path | None = None, proximity: str | Path | None = None,
                pdf_dir: str | Path | None = None, target: int = 120, max_pages: int = 450,
                min_year: int = 2010) -> dict:
    """Write manifest.csv, project.yaml (unless present) and subset_report.md under `root`; returns
    {n, manifest, report, missing, per_rule, titles_blanked}."""
    root = Path(root).resolve()
    base = DEFAULT_BASE
    inv_path = Path(inventory) if inventory else base / INVENTORY
    prox_path = Path(proximity) if proximity else base / PROXIMITY
    pdf_root = Path(pdf_dir) if pdf_dir else base / "pdf"
    with open(inv_path, encoding="utf-8-sig", newline="") as f:
        inventory_rows = list(csv.DictReader(f))
    with open(prox_path, encoding="utf-8-sig", newline="") as f:
        proximity_rows = list(csv.DictReader(f))
    rows = select_subset(inventory_rows, proximity_rows, target=target, max_pages=max_pages, min_year=min_year)
    root.mkdir(parents=True, exist_ok=True)
    missing: list[str] = []
    manifest = root / "manifest.csv"
    with open(manifest, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(MANIFEST_COLUMNS)
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
    write_report(rows, report, missing, target, max_pages, min_year)
    return {"n": len(rows), "manifest": str(manifest), "report": str(report), "missing": missing,
            "per_rule": {k: sum(1 for r in rows if r["reason"] == k) for k in RULES},
            "titles_blanked": sum(1 for r in rows if not r["title"])}
