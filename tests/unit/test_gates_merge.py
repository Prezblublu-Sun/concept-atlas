"""pipeline/gates.merge_draft: the three-way cases (add, unchanged, update of an unedited llm entry, user edit
kept + conflict, owner entry kept, retire candidates, form collisions), every draft kind, the overrides patch and
the status notices — on a bare project with hand-written drafts (no evidence store, no LLM)."""
from __future__ import annotations

import pytest

from atlas.config import init_project
from atlas.core.matcher import DEFAULT_PATTERNS
from atlas.pipeline import gates
from atlas.pipeline.llm_stage import drafts_dir, write_draft
from atlas.store.curated import curated_path, read_curated, write_yaml

LEGACY_CONCEPT_KEYS = {"id", "label", "zh", "type", "mode", "forms", "gen_forms", "case_forms", "rx", "probe", "readable",
                       "domains", "place", "shared_with", "origin", "merged_into", "source"}


def concept(cid: str, label: str, forms: list[str], **kw) -> dict:
    base = {"id": cid, "label": label, "zh": "", "type": "名词术语", "mode": "forms", "forms": forms, "gen_forms": [],
            "case_forms": [], "rx": None, "probe": "", "readable": "", "domains": [], "place": [], "shared_with": [],
            "origin": "概念（llm）", "merged_into": None, "source": "llm", "added_in": "r2026-01-01_001",
            "note": "", "merged_terms": [label], "example_sids": ["S1x"]}
    base.update(kw)
    return base


def term(t: str, **kw) -> dict:
    d = {"term": t, "zh": "译", "type": "名词术语", "gloss": "a gloss", "votes": 2, "sections": {"methods": 2},
         "example_sids": ["S1abc"], "docs": ["P001"], "n_docs": 1, "key": t.lower()}
    d.update(kw)
    return d


def draft(project, stage: str, run_id: str, body: dict, name: str | None = None) -> str:
    p = drafts_dir(project, stage) / (name or f"{run_id}.yaml")
    write_draft(p, {"stage": stage, "version": f"{stage}1", "run_id": run_id, "task": "t", "prompt_sha": "p" * 64,
                    "model": "fake", "mode": "live", "created": f"2026-01-0{run_id[-1]}T00:00:00+00:00", "items": [], **body})
    return str(p)


@pytest.fixture
def project(tmp_path):
    return init_project(tmp_path / "proj", name="merge test")


def test_terms_draft_merges_into_term_tree_three_way(project):
    d1 = draft(project, "terms", "r2026-01-01_001", {"terms": [term("micromotion"), term("Femoral stems", sections={"results": 3, "title": 1})]})
    r = gates.merge_draft(project, d1)
    f = r["files"]["term_tree"]
    assert len(f["added"]) == 2 and f["conflicts"] == [] and f["retire"] == []
    tree = read_curated(project.curated_dir, "term_tree")
    assert [d["id"] for d in tree["domains"]] == ["DOM_merge_test"]
    secs = {s["section"]: s for s in tree["domains"][0]["sections"]}
    assert set(secs) == {"methods", "results"}
    entries = [t for s in secs.values() for sub in s["subs"] for t in sub["terms"]]
    assert all(set(t) == {"term", "zh", "type", "concept_id", "note", "example", "example_doc", "source", "added_in"} for t in entries)
    assert all(t["source"] == "llm" and t["added_in"] == "r2026-01-01_001" and t["concept_id"] is None for t in entries)
    assert secs["results"]["subs"][0]["name"] == "名词术语"
    # the same draft again: nothing changes
    r = gates.merge_draft(project, d1)
    assert r["files"]["term_tree"]["unchanged"] == 2 and r["files"]["term_tree"]["added"] == []
    # a new run updates an unedited llm entry, adds one, lists the missing one as a retire candidate
    d2 = draft(project, "terms", "r2026-01-01_002", {"terms": [term("micromotion", zh="微动"), term("stress shielding")]})
    r = gates.merge_draft(project, d2)
    f = r["files"]["term_tree"]
    assert f["updated"] == ["('DOM_merge_test', 'methods', 'micromotion')"] and len(f["added"]) == 1
    assert f["retire"] == ["('DOM_merge_test', 'results', 'femoral stems')"] and f["conflicts"] == []
    tree = read_curated(project.curated_dir, "term_tree")
    entries = {t["term"]: t for s in tree["domains"][0]["sections"] for sub in s["subs"] for t in sub["terms"]}
    assert entries["micromotion"]["zh"] == "微动" and entries["micromotion"]["added_in"] == "r2026-01-01_002"
    assert "Femoral stems" in entries                     # retire candidates are reported, never removed
    # the user edits the zh of an llm entry: a later draft keeps the edit and reports a conflict
    p = curated_path(project.curated_dir, "term_tree")
    tree["domains"][0]["sections"][0]["subs"][0]["terms"][0]["zh"] = "用户的微动"
    write_yaml(p, tree)
    d3 = draft(project, "terms", "r2026-01-01_003", {"terms": [term("micromotion", zh="模型的微动")]})
    r = gates.merge_draft(project, d3)
    f = r["files"]["term_tree"]
    assert f["kept"] == ["('DOM_merge_test', 'methods', 'micromotion')"] and f["conflicts"][0]["reason"].startswith("entry edited")
    tree = read_curated(project.curated_dir, "term_tree")
    entries = {t["term"]: t for s in tree["domains"][0]["sections"] for sub in s["subs"] for t in sub["terms"]}
    assert entries["micromotion"]["zh"] == "用户的微动"
    # an owner entry is never touched
    entries["micromotion"]["source"] = "owner"
    write_yaml(p, tree)
    r = gates.merge_draft(project, d2)
    assert r["files"]["term_tree"]["conflicts"][0]["reason"].startswith("owner entry")
    # --domain places the terms elsewhere; an existing domains.yaml supplies the default
    write_yaml(curated_path(project.curated_dir, "domains"), {"domains": [{"id": "DOM_x", "zh": "x", "en": "x", "order": 0, "scope": True}]})
    r = gates.merge_draft(project, draft(project, "terms", "r2026-01-01_004", {"terms": [term("bone ingrowth")]}))
    assert r["files"]["term_tree"]["added"] == ["('DOM_x', 'methods', 'bone ingrowth')"]
    r = gates.merge_draft(project, draft(project, "terms", "r2026-01-01_005", {"terms": [term("load transfer")]}), domain="DOM_y")
    assert r["files"]["term_tree"]["added"] == ["('DOM_y', 'methods', 'load transfer')"]
    notices = gates.draft_notices(project)
    assert any("term_tree" in n and "never confirmed" in n for n in notices)
    gates.confirm(project, "term_tree")
    assert not any("term_tree" in n for n in gates.draft_notices(project))


def test_concepts_draft_merge_patterns_deltas_and_form_collisions(project):
    d1 = draft(project, "concepts", "r2026-01-01_001", {"mode": "base", "concepts": [
        concept("C_micromotion", "micromotion", ["micromotion", "micromotions"]),
        concept("C_femoralstem", "femoral stem", ["femoral stem", "femoral stems"])], "patterns": dict(DEFAULT_PATTERNS)})
    r = gates.merge_draft(project, d1)
    assert set(r["files"]) == {"concepts", "patterns"} and len(r["files"]["concepts"]["added"]) == 2
    cs = read_curated(project.curated_dir, "concepts")["concepts"]
    assert [c["id"] for c in cs] == ["C_femoralstem", "C_micromotion"]
    assert all(set(c) == LEGACY_CONCEPT_KEYS | {"added_in"} for c in cs)
    assert read_curated(project.curated_dir, "patterns") == dict(DEFAULT_PATTERNS)
    # a draft concept whose forms collide with an existing concept is a conflict, not written
    d2 = draft(project, "concepts", "r2026-01-01_002", {"mode": "base", "concepts": [
        concept("C_stemmicro", "stem micromotion", ["stem micromotion", "micromotion"]),
        concept("C_stem", "stem", ["stem", "stems"])], "patterns": dict(DEFAULT_PATTERNS)})
    r = gates.merge_draft(project, d2)
    f = r["files"]["concepts"]
    assert f["added"] == ["C_stem"] and f["conflicts"][0]["key"] == "C_stemmicro" and "collide" in f["conflicts"][0]["reason"]
    assert set(f["retire"]) == {"C_femoralstem", "C_micromotion"}
    # delta drafts: new_form applied when free, conflicting form refused, retire only proposed
    d3 = draft(project, "concepts", "r2026-01-01_003", {"mode": "delta", "concepts": [], "patterns": dict(DEFAULT_PATTERNS), "deltas": [
        {"action": "merge_into", "term": "interface micromotion", "concept_id": "C_micromotion", "form": "interface micromotion", "source": "llm"},
        {"action": "new_form", "term": "stems", "concept_id": "C_micromotion", "form": "stems", "source": "llm"},
        {"action": "retire", "concept_id": "C_stem", "target": "C_femoralstem", "source": "llm"},
        {"action": "merge_into", "term": "x", "concept_id": "C_nope", "form": "x"}]}, name="r2026-01-01_003.delta.yaml")
    r = gates.merge_draft(project, d3)
    f = r["files"]["concepts"]
    assert f["deltas_applied"] == [{"action": "new_form", "concept_id": "C_micromotion", "form": "interface micromotion"}]
    results = [p["result"] for p in f["deltas_proposals"]]
    assert any(x.startswith("conflict") for x in results) and any(x.startswith("proposal only") for x in results) and any(x.startswith("rejected") for x in results)
    cs = {c["id"]: c for c in read_curated(project.curated_dir, "concepts")["concepts"]}
    assert "interface micromotion" in cs["C_micromotion"]["forms"] and "stems" not in cs["C_micromotion"]["forms"]
    assert cs["C_stem"]["merged_into"] is None
    # the user edits patterns.yaml: a later draft keeps it
    pats = dict(DEFAULT_PATTERNS, short_caps_max_letters=4)
    write_yaml(curated_path(project.curated_dir, "patterns"), pats)
    r = gates.merge_draft(project, d1)
    assert r["files"]["patterns"]["conflicts"] and read_curated(project.curated_dir, "patterns")["short_caps_max_letters"] == 4


def test_scheme_and_classify_drafts_with_overrides(project):
    write_yaml(curated_path(project.curated_dir, "concepts"), {"concepts": [concept("C_a", "a", ["a"]), concept("C_b", "b", ["b"])]})
    src = {"source": "llm", "added_in": "r2026-01-01_001"}
    d = draft(project, "scheme", "r2026-01-01_001", {
        "domains": [{"id": "DOM_o", "zh": "骨科", "en": "Ortho", "order": 0, "scope": True, **src}],
        "categories": [{"id": "O1", "domain": "DOM_o", "name_zh": "一", "name_en": "One", "desc_zh": "", "desc_en": "", "order": 0, **src}],
        "subdomains": [{"id": "O-A", "kind": "within", "domain": "DOM_o", "name_zh": "甲", "name_en": "A", "desc_zh": "", "desc_en": "",
                        "order": 0, "anchors": ["C_a"], "notes": "", "evidence_docs": ["P1"], **src},
                       {"id": "X-AB", "kind": "cross", "domain": None, "name_zh": "乙", "name_en": "B", "desc_zh": "", "desc_en": "",
                        "order": 1, "groups": [["C_a"], ["C_zzz"]], "notes": "", "evidence_docs": [], **src}]})
    r = gates.merge_draft(project, d)
    assert set(r["files"]) == {"domains", "categories", "subdomains"}
    subs = read_curated(project.curated_dir, "subdomains")["subdomains"]
    assert all("evidence_docs" not in s for s in subs) and subs[1]["groups"] == [["C_a"], ["C_zzz"]]
    assert any("C_zzz" in n for n in r["notes"])
    assert read_curated(project.curated_dir, "categories")["categories"][0]["id"] == "O1"
    # classify rows; overrides patch on top; a failed row is skipped
    d = draft(project, "classify", "r2026-01-01_001", {"rows": [
        {"concept_id": "C_a", "domain": "DOM_o", "primary": "O1", "secondary": None, "reason": "r", "evidence_sids": ["S1x"], "status": "ok", **src},
        {"concept_id": "C_b", "domain": "DOM_o", "primary": "O1", "secondary": None, "reason": "r", "evidence_sids": [], "status": "invalid", **src}]})
    r = gates.merge_draft(project, d)
    rows = read_curated(project.curated_dir, "concept_categories")["rows"]
    assert [x["concept_id"] for x in rows] == ["C_a"] and set(rows[0]) == {"concept_id", "domain", "primary", "secondary", "reason", "source", "added_in"}
    ov = project.curated_dir / gates.OVERRIDES_DIR / gates.CATEGORY_OVERRIDES
    ov.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(ov, {"C_a": {"primary": "O2", "reason": "owner says"}, "C_c": {"domain": "DOM_o", "primary": "O1"}})
    r = gates.merge_draft(project, d)
    assert r["files"]["concept_categories"]["overrides_applied"] == ["C_a", "C_c"]
    rows = {x["concept_id"]: x for x in read_curated(project.curated_dir, "concept_categories")["rows"]}
    assert rows["C_a"]["primary"] == "O2" and rows["C_a"]["source"] == "owner" and rows["C_c"]["source"] == "owner"
    assert gates.format_merge_report(r).startswith("merged classify draft")
    with pytest.raises(FileNotFoundError):
        gates.merge_draft(project, project.root / "nope.yaml")
    bad = draft(project, "other", "r2026-01-01_009", {})
    with pytest.raises(gates.MergeError):
        gates.merge_draft(project, bad)
