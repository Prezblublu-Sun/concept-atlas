"""Integration: fixture corpus → ingest → a tiny curated set written by the test → `atlas build all` (through the
real commands) → asserts on occurrences, counts, importance, sub-domains, the freeze and the CLI verbs."""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
from pathlib import Path

import pytest

from atlas import cli
from atlas.config import init_project
from atlas.core.matcher import DEFAULT_PATTERNS
from atlas.pipeline import build as B
from atlas.pipeline import gates
from atlas.pipeline.runner import StageError, run_stage
from atlas.pipeline.stages.ingest import register_documents
from atlas.store.build import BuildStore, current_build_id, read_manifest
from atlas.store.curated import curated_path, write_yaml
from atlas.tools.fixture import make_fixture


def write_curated(project, docs: list[str]) -> None:
    cd = project.curated_dir
    write_yaml(curated_path(cd, "domains"), {"domains": [
        {"id": "DOM_ortho", "zh": "骨科", "en": "Orthopaedics", "order": 0, "scope": True},
        {"id": "DOM_fe", "zh": "有限元", "en": "Finite element", "order": 1, "scope": True}]})
    write_yaml(curated_path(cd, "concepts"), {"concepts": [
        {"id": "C_micromotion", "label": "micromotion", "zh": "微动", "type": "量与指标", "mode": "forms",
         "forms": ["micromotion", "micromotions"], "gen_forms": [], "case_forms": [], "rx": None, "probe": "", "readable": "",
         "domains": ["DOM_ortho", "DOM_fe"], "place": [], "shared_with": [], "origin": "test", "merged_into": None, "source": "test"},
        {"id": "C_stem", "label": "stem", "zh": "柄", "type": "名词术语", "mode": "forms", "forms": ["stem", "stems"], "domains": ["DOM_ortho"]},
        {"id": "C_femoralstem", "label": "femoral stem", "zh": "股骨柄", "type": "名词术语", "mode": "forms",
         "forms": ["femoral stem", "femoral stems"], "domains": ["DOM_ortho"]},
        {"id": "C_finiteelement", "label": "finite element (FE)", "zh": "有限元", "type": "方法或模型名", "mode": "forms",
         "forms": ["finite element"], "case_forms": ["FE"], "domains": ["DOM_fe"]},
        {"id": "C_primarystability", "label": "primary stability", "zh": "初始稳定性", "type": "量与指标", "mode": "forms",
         "forms": ["primary stability"], "domains": ["DOM_ortho"]},
        {"id": "C_stressstrainfield", "label": "stress/strain field", "zh": "应力场", "type": "量与指标", "mode": "literal",
         "rx": r"(?<![^ ¶])(?:stress|strain) ?field(?:s|es)?(?![^ ¶])", "probe": "field", "domains": ["DOM_fe"]},
    ]})
    write_yaml(curated_path(cd, "patterns"), dict(DEFAULT_PATTERNS))
    write_yaml(curated_path(cd, "subdomains"), {"subdomains": [
        {"id": "ORT-MM", "kind": "within", "domain": "DOM_ortho", "name_zh": "微动", "name_en": "Micromotion", "desc_zh": "", "desc_en": "",
         "order": 0, "anchors": ["C_micromotion", "C_primarystability"]},
        {"id": "X-FEMM", "kind": "cross", "domain": None, "name_zh": "有限元×微动", "name_en": "FE x micromotion", "desc_zh": "", "desc_en": "",
         "order": 1, "groups": [["C_finiteelement"], ["C_micromotion", "C_stem"]]}]})
    write_yaml(curated_path(cd, "doc_domains"), {"docs": {d: (["DOM_ortho", "DOM_fe"] if i % 2 == 0 else ["DOM_ortho"]) for i, d in enumerate(docs)}})
    write_yaml(curated_path(cd, "categories"), {"categories": [{"id": "O1", "domain": "DOM_ortho", "name_zh": "指标", "name_en": "Metrics", "desc_zh": "", "desc_en": "", "order": 0}]})
    write_yaml(curated_path(cd, "concept_categories"), {"rows": [{"concept_id": "C_micromotion", "domain": "DOM_ortho", "primary": "O1", "secondary": None, "reason": "", "source": "test"}]})
    write_yaml(curated_path(cd, "term_tree"), {"domains": [{"id": "DOM_ortho", "sections": [{"section": "title", "subs": [{"name": "指标", "order": 0, "terms": [
        {"term": "micromotion", "zh": "微动", "type": "量与指标", "concept_id": "C_micromotion", "note": "", "example": "", "example_doc": ""}]}]}]}]})
    write_yaml(curated_path(cd, "i18n"), {"strings": [{"zh": "微动", "en": "Micromotion", "grp": "framework"}]})


def _unlock(project) -> None:
    for p in project.builds_dir.glob("*/build.sqlite"):
        os.chmod(p, stat.S_IWRITE | stat.S_IREAD)


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    base = tmp_path_factory.mktemp("build 测试")
    info = make_fixture(base / "fx", 1)
    project = init_project(base / "proj", name="buildtest", pdf_dir=str(base / "fx" / "pdf"))
    shutil.copy(info["manifest"], project.manifest_path)
    assert register_documents(project).items_failed == 0
    assert run_stage(project, "ingest").items_failed == 0
    docs = [d["doc_id"] for d in info["documents"]]
    write_curated(project, docs)
    yield {"info": info, "project": project, "docs": docs}
    _unlock(project)


def test_gates_block_unconfirmed_curated_files(built):
    project = built["project"]
    assert all(r["state"] == "unconfirmed" for r in gates.status(project))
    B.new_build(project, note="unconfirmed")
    with pytest.raises(gates.GateError, match="changed after confirmation|run `atlas curate confirm concepts`"):
        run_stage(project, "match")
    gates.confirm_all(project)
    assert all(r["state"] == "confirmed" for r in gates.status(project))
    with pytest.raises(StageError, match="requires `match`"):
        run_stage(project, "assign")


def test_build_all_and_results(built):
    project, info = built["project"], built["info"]
    r = B.build_all(project, note="test", workers=2)
    bid = r["build_id"]
    assert set(r["timings"]) == {"new", "match", "assign", "finish", "promote"}
    assert current_build_id(project.builds_dir) == bid
    m = read_manifest(project.builds_dir / bid)
    assert m["status"] == "finished" and len(m["sha256"]) == 64 and m["stages"]["match"]["status"] == "done"
    assert (project.builds_dir / bid / "report.md").read_text(encoding="utf-8").startswith(f"# Build {bid}")
    st = BuildStore.open(project.builds_dir / bid, "immutable", evidence_path=project.evidence_db)
    try:
        assert st.stats()["docs"] == 9 and st.stats()["occurrences"] > 0
        # every planted micromotion sentence is one standalone body occurrence; the figure caption adds one per paper
        docs = st.concept_docs("C_micromotion", limit=50)["rows"]
        for row in docs:
            assert row["body"] == info["micromotion_counts"][row["doc_id"]] + 1, row["doc_id"]
        assert len(docs) == 9
        imp = {m["scope"]: m for m in st.importance_of("C_micromotion")}
        assert imp["all"]["papers"] == 9 and imp["all"]["coverage"] == 1.0 and imp["all"]["tier"] in ("A", "B")
        assert imp["DOM_fe"]["papers"] == 5 and imp["DOM_fe"]["lift"] is not None
        assert imp["all"]["main_section"] in ("results", "discussion", "introduction")
        # a standalone 'stem' in the title or keywords → a topic paper; 'femoral stems' nests it (v2 semantics)
        stem = {m["scope"]: m for m in st.importance_of("C_stem")}["all"]
        rx = re.compile(r"(?<!femoral )\bstems?\b", re.I)
        expected_topic = sum(1 for d in info["documents"] if rx.search(d["title"] + " " + d["keywords_line"]))
        assert 0 < expected_topic < 9
        assert stem["papers"] == 9 and stem["topic_papers"] == expected_topic and stem["tier"] == "A"
        # literal concept never nested, nested counts for the shorter concept
        fe = st.concept_sentences("C_finiteelement", standalone=False)
        assert fe["total"] >= 1
        # sub-domain assignment: every paper mentions micromotion/primary stability in title/abstract → ORT-MM
        tree = {s["id"]: s for g in st.subdomain_tree() for s in g["subs"]}
        assert tree["ORT-MM"]["papers"] == 9 and tree["ORT-MM"]["core"] >= 1 and tree["ORT-MM"]["base"] == 9
        assert tree["X-FEMM"]["base"] == 9 and 0 < tree["X-FEMM"]["papers"] <= 9
        sd = st.subdomain("ORT-MM")
        assert sd["docs"] and all(d["tier"] in ("core", "related") for d in sd["docs"])
        assert st.subdomains_of("C_micromotion")[0]["sub_id"] in ("ORT-MM", "X-FEMM")
        # sentences carry spans into the stored text
        sen = st.concept_sentences("C_micromotion", standalone=True, limit=5)["rows"]
        for row in sen:
            for a, b, form, nested in row["spans"]:
                assert row["text"][a:b].lower() == "micromotion"
        # examples are deterministic and from the top documents
        ex1 = [e["sid"] for e in st.examples("C_micromotion", 6)]
        ex2 = [e["sid"] for e in st.examples("C_micromotion", 6)]
        assert ex1 == ex2 and len(ex1) == 6
        assert not [c for c in st.checks() if c["severity"] == "error"]
    finally:
        st.close()
    # build status / list
    lst = B.list_builds(project)
    assert any(b["build_id"] == bid and b["current"] for b in lst)
    stt = B.build_status(project, bid)
    assert stt["stage_items"]["match"]["done"] == 9 and stt["counts"]["docs"] == 9
    # a frozen build cannot be reopened as open
    with pytest.raises(B.BuildError):
        run_stage(project, "match", build=bid)


def test_match_rerun_skips_unchanged_documents(built):
    project = built["project"]
    m = B.new_build(project, note="rerun")
    rep = run_stage(project, "match", build=m["build_id"], workers=1)
    assert rep.items_done == 9 and rep.items_skipped == 0
    rep2 = run_stage(project, "match", build=m["build_id"], workers=1)
    assert rep2.items_skipped == 9 and rep2.items_done == 0
    rep3 = run_stage(project, "match", build=m["build_id"], doc_ids=["P001"], workers=1)
    assert rep3.items_done == 1 and rep3.items_total == 1
    st = BuildStore.open(project.builds_dir / m["build_id"], "ro")
    try:
        assert st.concept_docs("C_micromotion")["total"] == 9        # P001's outputs were replaced, not duplicated
    finally:
        st.close()


def test_cli_build_verbs(built, capsys):
    project = built["project"]
    root = str(project.root)
    assert cli.main(["curate", "status", root]) == 0
    assert "confirmed" in capsys.readouterr().out
    assert cli.main(["build", "list", root]) == 0
    out = capsys.readouterr().out
    assert "finished" in out and "*" in out
    assert cli.main(["build", "status", root]) == 0
    assert json.loads(capsys.readouterr().out)["build_id"]
    assert cli.main(["build", "new", root, "--note", "cli"]) == 0
    assert "created" in capsys.readouterr().out
    assert cli.main(["run", "match", root, "--workers", "1"]) == 0
    assert "[match] done" in capsys.readouterr().out
    assert cli.main(["run", "assign", root]) == 0
    assert cli.main(["run", "finish", root]) == 0
    assert "sha256" in capsys.readouterr().out
    assert cli.main(["build", "promote", root]) == 0
    assert "promoted" in capsys.readouterr().out
    assert cli.main(["curate", "confirm", root, "nope"]) == 1
    assert "unknown curated file" in capsys.readouterr().err
    assert cli.main(["run", "match", root]) == 0          # the newest open build (from the rerun test): all skipped
    assert "skipped 9" in capsys.readouterr().out
    assert cli.main(["build", "status", root, "nope"]) == 1
    assert "no such build" in capsys.readouterr().err
