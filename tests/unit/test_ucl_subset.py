"""atlas.tools.ucl_subset.select_subset / make_subset on a synthetic inventory."""
from __future__ import annotations

import csv
from pathlib import Path

from atlas.tools.ucl_subset import CHEMISTRY, ENGINEERING, PHYSICS, clean_title, make_subset, select_subset


def _inv(n_eng=200, n_phys=15, n_chem=15, n_other=50):
    rows, prox = [], []
    k = 0

    def add(disc, year, pages, text="Y", tier=None, rubric=0, title=None):
        nonlocal k
        k += 1
        rel = f"{k // 100:03d}/{k}_1_thesis.pdf"
        rows.append({"eprint_id": str(k), "title": title if title is not None else f"Study number {k} about implants",
                     "discipline": disc, "year": str(year), "pages": str(pages), "has_text_layer": text, "relpath": rel})
        if tier:
            prox.append({"tier": str(tier), "rubric_total": str(rubric), "relpath": rel})
        return rel
    tier1 = [add(ENGINEERING, 2020, 300, tier=1, rubric=20 - i) for i in range(5)]
    tier1_big = add(ENGINEERING, 2020, 900, tier=1, rubric=30)          # too many pages
    tier1_scan = add(ENGINEERING, 2020, 300, text="N", tier=1, rubric=30)   # no text layer
    tier2 = [add("临床医学 / 流行病学 / 公共卫生", 2015, 250, tier=2, rubric=10 - i, title="ProQuest Dissertations") for i in range(5)]
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
    r3a = [r for r in sel if r["reason"] == "rule 3a"]
    # physics rows: years 2008..2022, pages 200 + 40 i: recent and <= 450 pages first (2010–2014), then the newest
    # of the recent long ones; the older ones (2008, 2009) only when the recent ones run out
    assert sorted(r["year"] for r in r3a) == [2010, 2011, 2012, 2013, 2014, 2018, 2019, 2020, 2021, 2022]
    assert [r["reason"] for r in sel] == sorted((r["reason"] for r in sel), key=["rule 1", "rule 2", "rule 2b", "rule 3a", "rule 3b"].index)
    assert select_subset(rows, prox, target=120) == sel
    small = select_subset(rows, prox, target=20)
    assert len(small) == 40 and sum(1 for r in small if r["reason"] == "rule 2") == 10
    assert all(r["title"] == "" and r["title_raw"] == "ProQuest Dissertations" for r in sel if r["relpath"] in ids["tier2"])


def test_older_theses_fill_the_shortfall():
    """The real library holds only 49 recent engineering theses and 5 recent physics ones: rule 2b takes older
    engineering theses (highest rubric first) and rule 3 takes older ones of its disciplines, newest first."""
    rows, prox, ids = _inv(n_eng=80, n_phys=4, n_chem=15)        # 80 engineering rows, 60 of them >= 2010; 4 physics
    sel = select_subset(rows, prox, target=80)
    r2 = [r for r in sel if r["reason"] == "rule 2"]
    r2b = [r for r in sel if r["reason"] == "rule 2b"]
    assert len(sel) == 94 and len(r2) == 61 and len(r2b) == 9 and all(r["year"] < 2010 for r in r2b)
    assert all(r["year"] >= 2010 for r in r2) and [r["rubric"] for r in r2b] == sorted((r["rubric"] for r in r2b), reverse=True)
    r3a = [r for r in sel if r["reason"] == "rule 3a"]
    assert sorted(r["year"] for r in r3a) == [2008, 2009, 2010, 2011]         # every physics thesis, old ones included


def test_clean_title():
    assert clean_title("Microsoft Word - REVISED JB thesis 18Apr10") == ""
    assert clean_title("ProQuest Dissertations") == "" and clean_title("Thesis-final-submission_Yin") == ""
    assert clean_title("This Thesis is Dedicated With Love to My Parents") == ""
    assert clean_title("I, Nai-Hao Yin confirm that the work presented in this thesis is my own") == ""
    assert clean_title("David Colas Aberg - 13074884 - PhD Thesis") == "" and clean_title("Urinary neopterin") == ""
    assert clean_title("  Architectured Materials for  Wearable Assistive Technology ") == "Architectured Materials for Wearable Assistive Technology"
    assert clean_title("Modelling and in vitro evaluation of customised hip implants") == "Modelling and in vitro evaluation of customised hip implants"
    assert clean_title(None) == ""


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
    assert r["n"] == 40 and r["per_rule"] == {"rule 1": 10, "rule 2": 10, "rule 2b": 0, "rule 3a": 10, "rule 3b": 10}
    assert len(r["missing"]) == 39 and r["titles_blanked"] == 5
    with open(r["manifest"], encoding="utf-8", newline="") as f:
        man = list(csv.DictReader(f))
    assert list(man[0]) == ["doc_id", "title", "year", "authors", "doi", "venue", "doc_type", "file", "reason", "tier", "rubric", "pages"]
    assert man[0]["doc_id"] == f"T{ids['tier1'][0].split('/')[1].split('_')[0]}" and man[0]["doc_type"] == "thesis"
    assert man[0]["venue"] == ENGINEERING and Path(man[0]["file"]).is_absolute() and man[0]["reason"] == "rule 1"
    assert man[0]["title"].startswith("Study number") and any(m["title"] == "" for m in man if m["reason"] == "rule 1")
    assert (tmp_path / "proj" / "project.yaml").read_text(encoding="utf-8").count("doc_type: thesis") == 1
    rep = (tmp_path / "proj" / "subset_report.md").read_text(encoding="utf-8")
    assert "40 theses selected" in rep and "rule 3a: 10" in rep and "tier 1: 5" in rep and "5 inventory titles blanked" in rep
    # a second run keeps project.yaml and rewrites the manifest identically
    before = Path(r["manifest"]).read_bytes()
    (tmp_path / "proj" / "project.yaml").write_text("name: keep\ndoc_type: thesis\n", encoding="utf-8")
    make_subset(tmp_path / "proj", inventory=inv, proximity=px, pdf_dir=pdf_dir, target=20)
    assert Path(r["manifest"]).read_bytes() == before and (tmp_path / "proj" / "project.yaml").read_text(encoding="utf-8") == "name: keep\ndoc_type: thesis\n"
