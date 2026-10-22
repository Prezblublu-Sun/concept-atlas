"""atlas.tools.ucl_subset.select_subset on a synthetic inventory."""
from __future__ import annotations

import csv
from pathlib import Path

from atlas.tools.ucl_subset import CHEMISTRY, ENGINEERING, PHYSICS, make_subset, select_subset


def _inv(n_eng=200, n_phys=15, n_chem=15, n_other=50):
    rows, prox = [], []
    k = 0

    def add(disc, year, pages, text="Y", tier=None, rubric=0):
        nonlocal k
        k += 1
        rel = f"{k // 100:03d}/{k}_1_thesis.pdf"
        rows.append({"eprint_id": str(k), "title": f"Thesis {k}", "discipline": disc, "year": str(year), "pages": str(pages),
                     "has_text_layer": text, "relpath": rel})
        if tier:
            prox.append({"tier": str(tier), "rubric_total": str(rubric), "relpath": rel})
        return rel
    tier1 = [add(ENGINEERING, 2020, 300, tier=1, rubric=20 - i) for i in range(5)]
    tier1_big = add(ENGINEERING, 2020, 900, tier=1, rubric=30)          # too many pages
    tier1_scan = add(ENGINEERING, 2020, 300, text="N", tier=1, rubric=30)   # no text layer
    tier2 = [add("临床医学 / 流行病学 / 公共卫生", 2015, 250, tier=2, rubric=10 - i) for i in range(5)]
    tier3 = [add(ENGINEERING, 2021, 250, tier=3, rubric=40)]           # tier 3 only via rule 2 (high rubric)
    for i in range(n_eng):
        add(ENGINEERING, 2005 + i % 20, 100 + i, rubric=i % 7)
    for i in range(n_phys):
        add(PHYSICS, 2008 + i, 200 + 40 * i)
    for i in range(n_chem):
        add(CHEMISTRY, 2008 + i, 150)
    for i in range(n_other):
        add("其他", 2020, 100)
    return rows, prox, {"tier1": tier1, "tier1_big": tier1_big, "tier1_scan": tier1_scan, "tier2": tier2, "tier3": tier3}


def test_rules_and_determinism():
    rows, prox, ids = _inv()
    sel = select_subset(rows, prox, target=120)
    by_rel = {r["relpath"]: r for r in sel}
    assert len(sel) == 140                                           # 120 + 10 + 10
    for rel in ids["tier1"] + ids["tier2"]:
        assert by_rel[rel]["reason"] == "rule 1"
    assert ids["tier1_big"] not in by_rel and ids["tier1_scan"] not in by_rel
    assert by_rel[ids["tier3"][0]]["reason"] == "rule 2"              # highest rubric among the engineering pool
    r2 = [r for r in sel if r["reason"] == "rule 2"]
    assert len(r2) == 120 - 10 and all(r["discipline"] == ENGINEERING and r["year"] >= 2010 and r["pages"] <= 450 for r in r2)
    assert [r["rubric"] for r in r2] == sorted((r["rubric"] for r in r2), reverse=True)
    assert sum(1 for r in sel if r["reason"] == "rule 3a") == 10 and all(r["discipline"] == PHYSICS for r in sel if r["reason"] == "rule 3a")
    assert sum(1 for r in sel if r["reason"] == "rule 3b") == 10 and all(r["year"] >= 2010 for r in sel if r["reason"] == "rule 3b")
    assert select_subset(rows, prox, target=120) == sel
    small = select_subset(rows, prox, target=20)
    assert len(small) == 40 and sum(1 for r in small if r["reason"] == "rule 2") == 10


def test_make_subset_writes_manifest_project_and_report(tmp_path: Path):
    rows, prox, ids = _inv(n_eng=30, n_phys=12, n_chem=12)
    inv = tmp_path / "inv.csv"
    with open(inv, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    px = tmp_path / "prox.csv"
    with open(px, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["tier", "rubric_total", "relpath"])
        w.writeheader()
        w.writerows(prox)
    pdf_dir = tmp_path / "pdf"
    first = ids["tier1"][0]
    (pdf_dir / first).parent.mkdir(parents=True)
    (pdf_dir / first).write_bytes(b"%PDF-1.4\n")
    r = make_subset(tmp_path / "proj", inventory=inv, proximity=px, pdf_dir=pdf_dir, target=20)
    assert r["n"] == 40 and r["per_rule"]["rule 1"] == 10 and len(r["missing"]) == 39
    with open(r["manifest"], encoding="utf-8", newline="") as f:
        man = list(csv.DictReader(f))
    assert man[0]["doc_id"].startswith("T") and man[0]["doc_type"] == "thesis" and man[0]["venue"] == ENGINEERING
    assert Path(man[0]["file"]).is_absolute() and man[0]["reason"] == "rule 1"
    assert (tmp_path / "proj" / "project.yaml").read_text(encoding="utf-8").count("doc_type: thesis") == 1
    rep = (tmp_path / "proj" / "subset_report.md").read_text(encoding="utf-8")
    assert "40 theses selected" in rep and "rule 3a: 10" in rep
