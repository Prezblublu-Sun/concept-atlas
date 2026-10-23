"""verify/qa: the review sheet round trip (sample → fill → record → report) and the accept gate thresholds, on a
bare project with hand-written drafts."""
from __future__ import annotations

import json

import pytest
from openpyxl import load_workbook

from atlas.config import Project, deep_merge, init_project
from atlas.pipeline.llm_stage import drafts_dir, write_draft
from atlas.verify import qa


def terms_draft(project, run_id: str, n: int = 12, sha: str = "a" * 64):
    terms = [{"term": f"term {i:02d}", "zh": f"术语{i}", "type": "名词术语" if i % 2 else "量与指标", "gloss": "g", "votes": i,
              "sections": {"methods": 1}, "example_sids": [f"S1{i:04x}"], "docs": ["P001"], "n_docs": 1, "key": f"term {i:02d}"}
             for i in range(n)]
    p = drafts_dir(project, "terms") / f"{run_id}.yaml"
    write_draft(p, {"stage": "terms", "version": "terms1", "run_id": run_id, "task": "terms_extract", "prompt_sha": sha,
                    "model": "fake", "mode": "live", "created": "2026-01-01T00:00:00+00:00", "items": [], "terms": terms})
    return p


def fill(xlsx: str, values: list) -> None:
    wb = load_workbook(xlsx)
    ws = wb[qa.SHEET]
    for i, v in enumerate(values, start=2):
        ws.cell(row=i, column=5, value=v)
    wb.save(xlsx)


@pytest.fixture
def project(tmp_path):
    return init_project(tmp_path / "proj", name="qa test")


def test_sample_record_report_round_trip(project):
    terms_draft(project, "r2026-01-01_001")
    with pytest.raises(ValueError):
        qa.sample(project, "concepts")
    r = qa.sample(project, "terms", n=5, seed=3)
    assert r["n"] == 5 and r["total"] == 12 and r["run"] == "r2026-01-01_001" and r["task"] == "terms_extract"
    r2 = qa.sample(project, "terms", n=5, seed=3)
    rows = [json.loads(l) for l in open(r["jsonl"], encoding="utf-8") if l.strip()]
    rows2 = [json.loads(l) for l in open(r2["jsonl"], encoding="utf-8") if l.strip()]
    assert [x["key"] for x in rows] == [x["key"] for x in rows2]                 # seeded
    assert {tuple(x[k] for k in ("task", "prompt_sha", "scheme_sha", "run", "item")) for x in rows} and len({x["item"] for x in rows}) == 5
    assert all(x["stratum"] in ("名词术语", "量与指标") for x in rows)
    wb = load_workbook(r["xlsx"])
    ws = wb[qa.SHEET]
    assert [c.value for c in ws[1]] == list(qa.COLUMNS) and ws.max_row == 6 and all(ws.cell(row=i, column=5).value is None for i in range(2, 7))
    assert dict(wb[qa.META_SHEET].iter_rows(values_only=True))["prompt_sha"] == "a" * 64
    # two judgements missing, one unparsable, two correct, one wrong
    fill(r["xlsx"], ["y", "0", "maybe", 1, None])
    rec = qa.record(project, r["xlsx"])
    assert rec["n"] == 5 and rec["judged"] == 3 and rec["correct"] == 2 and rec["unparsed"][0]["value"] == "maybe"
    rep = qa.report(project)
    p = rep["stages"]["terms"]["prompts"]["a" * 64]
    assert p["judged"] == 3 and p["accuracy"] == pytest.approx(0.6667, abs=1e-3) and p["passed"] is False
    assert set(p["strata"]) <= {"名词术语", "量与指标"} and p["runs"] == ["r2026-01-01_001"]
    st = qa.accept_status(project, "terms_extract", "a" * 64)
    assert not st["passed"] and "min_judged" in st["reason"]
    assert "not passed" in qa.format_report(rep)
    # all correct but fewer than min_judged: still refused; a lower min_judged passes
    fill(r["xlsx"], ["对", "是", "✓", "true", "ok"])
    qa.record(project, r["xlsx"])
    assert qa.accept_status(project, "terms_extract", "a" * 64)["reason"].endswith("min_judged 10")
    low = Project(root=project.root, config=deep_merge(project.config, {"qa": {"min_judged": 5}}))
    assert qa.accept_status(low, "terms_extract", "a" * 64)["passed"]
    assert not qa.accept_status(low, "terms_extract", "b" * 64)["passed"]        # another prompt sha: no review
    # accuracy below the threshold with enough judgements
    fill(r["xlsx"], ["y", "y", "y", "n", "n"])
    qa.record(project, r["xlsx"])
    st = qa.accept_status(low, "terms_extract", "a" * 64)
    assert not st["passed"] and "threshold" in st["reason"] and st["accuracy"] == 0.6
    assert qa.accept_status(Project(root=project.root, config=deep_merge(low.config, {"qa": {"accept_threshold": 0.5}})),
                            "terms_extract", "a" * 64)["passed"]


def test_parse_judgement_and_missing_inputs(project):
    assert qa.parse_judgement("Yes") is True and qa.parse_judgement(" 0 ") is False and qa.parse_judgement("") is None
    assert qa.parse_judgement(True) is True and qa.parse_judgement(2) is None and qa.parse_judgement("错") is False
    with pytest.raises(FileNotFoundError):
        qa.sample(project, "terms")
    terms_draft(project, "r2026-01-01_001")
    with pytest.raises(FileNotFoundError):
        qa.sample(project, "terms", run_id="r2026-01-01_009")
    with pytest.raises(FileNotFoundError):
        qa.record(project, project.root / "nope.xlsx")
    assert qa.report(project) == {"threshold": 0.85, "min_judged": 10, "stages": {}}
