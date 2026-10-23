"""Integration: fixture corpus → ingest → a tiny curated set written by the test (incl. one literal regex concept
and one cross sub-domain) → `atlas build all` (through the real commands) → asserts on occurrences, counts,
importance, sub-domains, the freeze, the gates, the CLI verbs, and that no stage touches `curated/`."""
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
from atlas.pipeline.stages.match import topic_texts
from atlas.store.build import BuildStore, current_build_id, read_manifest
from atlas.store.curated import curated_path, curated_shas, write_yaml
from atlas.tools.fixture import make_fixture

ATLAS = Path(__file__).resolve().parents[1] / "atlas"
# modules that run inside a build: they must never write a curated file
STAGE_MODULES = [ATLAS / "pipeline" / "runner.py", ATLAS / "pipeline" / "build.py", ATLAS / "store" / "build.py",
                 *sorted((ATLAS / "pipeline" / "stages").glob("*.py"))]


def write_curated(project, docs: list[str]) -> None:
    cd = project.curated_dir
    write_yaml(curated_path(cd, "domains"), {"domains": [
        {"id": "DOM_ortho", "zh": "骨科", "en": "Orthopaedics", "order": 0, "scope": True},
        {"id": "DOM_fe", "zh": "有限元", "en": "Finite element", "order": 1, "scope": True}]})
    write_yaml(curated_path(cd, "concepts"), {"concepts": [
        {"id": "C_micromotion", "label": "micromotion", "zh": "微动", "type": "量与指标", "mode": "forms",
         "forms": ["micromotion", "micromotions"], "gen_forms": [], "case_forms": [], "rx": None, "probe": "", "readable": "",
         "domains": ["DOM_ortho", "DOM_fe"], "place": [], "shared_with": [], "origin": "概念（test）", "merged_into": None, "source": "test"},
        {"id": "C_stem", "label": "stem", "zh": "柄", "type": "名词术语", "mode": "forms", "forms": ["stem", "stems"], "domains": ["DOM_ortho"]},
        {"id": "C_femoralstem", "label": "femoral stem", "zh": "股骨柄", "type": "名词术语", "mode": "forms",
         "forms": ["femoral stem", "femoral stems"], "domains": ["DOM_ortho"]},
        {"id": "C_finiteelement", "label": "finite element (FE)", "zh": "有限元", "type": "方法或模型名", "mode": "forms",
         "forms": ["finite element"], "case_forms": ["FE"], "domains": ["DOM_fe"]},
        {"id": "C_primarystability", "label": "primary stability", "zh": "初始稳定性", "type": "量与指标", "mode": "forms",
         "forms": ["primary stability"], "domains": ["DOM_ortho"]},
        # literal regex concept (the fixture writes 'stress shielding' / 'proximal strain distribution' in titles,
        # keywords and sentences); tests/test_http.py reuses this set and depends on these six ids
        {"id": "C_stressstrainfield", "label": "stress/strain field", "zh": "应力场", "type": "量与指标", "mode": "literal",
         "rx": r"(?<![^ ¶])(?:stress|strain) ?(?:field|shielding|distribution)(?:s|es)?(?![^ ¶])", "probe": "str",
         "domains": ["DOM_fe"]},
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
    with pytest.raises(B.BuildError, match="no open build"):
        run_stage(project, "finish")                       # no build yet (finish has no gate)
    B.new_build(project, note="unconfirmed")
    with pytest.raises(gates.GateError) as e:
        run_stage(project, "match")
    assert str(e.value) == "`concepts` changed after confirmation — run `atlas curate confirm concepts`"
    gates.confirm(project, "concepts")
    with pytest.raises(gates.GateError) as e:
        run_stage(project, "match")
    assert str(e.value) == "`patterns` changed after confirmation — run `atlas curate confirm patterns`"
    gates.confirm_all(project)
    assert all(r["state"] == "confirmed" for r in gates.status(project))
    with pytest.raises(StageError, match="requires `match`"):
        run_stage(project, "assign")
    # editing a confirmed file re-arms the gate with the same message
    p = curated_path(project.curated_dir, "subdomains")
    p.write_text(p.read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8")
    assert [r["state"] for r in gates.status(project) if r["name"] == "subdomains"] == ["changed"]
    run_stage(project, "match", workers=1)
    with pytest.raises(gates.GateError) as e:
        run_stage(project, "assign")
    assert str(e.value) == "`subdomains` changed after confirmation — run `atlas curate confirm subdomains`"
    gates.confirm(project, "subdomains")


def test_stages_never_write_curated(built):
    """Static: no build-time module imports a curated writer or opens a curated path for writing."""
    for py in STAGE_MODULES:
        src = py.read_text(encoding="utf-8")
        assert "write_yaml" not in src and "write_confirmations" not in src and "confirm(" not in src, py
        for m in re.finditer(r"open\(([^)]*)\)", src):
            assert not ("curated" in m.group(1) and re.search(r"['\"]w", m.group(1))), f"{py}: {m.group(0)}"


def test_build_all_and_results(built):
    project, info = built["project"], built["info"]
    before = curated_shas(project.curated_dir)
    r = B.build_all(project, note="test", workers=2)
    assert curated_shas(project.curated_dir) == before                     # runtime: stages never touch curated/
    bid = r["build_id"]
    assert set(r["timings"]) == {"new", "match", "assign", "finish", "promote"}
    assert current_build_id(project.builds_dir) == bid
    m = read_manifest(project.builds_dir / bid)
    assert m["status"] == "finished" and len(m["sha256"]) == 64 and m["stages"]["match"]["status"] == "done"
    assert {"build_id", "created", "status", "project", "evidence_sha_at_pin", "curated", "docs", "sentences", "stages"} <= set(m)
    assert len(m["evidence_sha_at_pin"]) == 64 and m["curated"] == before and m["docs"] == 9
    assert m["stages"]["match"]["version"] == "match5" and m["stages"]["assign"]["version"] == "assign3"
    assert m["stages"]["match"]["fingerprint"].startswith("m3:")
    report = (project.builds_dir / bid / "report.md").read_text(encoding="utf-8")
    assert report.startswith(f"# Build {bid}") and "## Tier histogram" in report and "## Checks" in report
    assert "| all |" in report and "C_micromotion" in report and "ORT-MM" in report
    # frozen: no WAL left, read-only, journal mode DELETE
    assert not (project.builds_dir / bid / "build.sqlite-wal").exists()
    assert not (os.stat(project.builds_dir / bid / "build.sqlite").st_mode & stat.S_IWRITE)
    assert (project.builds_dir / bid / "build.sqlite").read_bytes()[18:20] == b"\x01\x01"      # legacy (rollback) journal
    snap = project.builds_dir / bid / "curated"
    assert curated_shas(snap) == before and (snap / "confirmations.json").is_file()
    st = BuildStore.open(project.builds_dir / bid, "immutable", evidence_path=project.evidence_db)
    try:
        assert st.stats()["docs"] == 9 and st.stats()["occurrences"] > 0
        # the literal regex concept: matched on lit_norm, offsets map back to the stored text, never nested
        lit = st.concept_sentences("C_stressstrainfield", limit=200)
        assert lit["total"] >= 1
        for row in lit["rows"]:
            for a, b, form, nested in row["spans"]:
                assert re.fullmatch(r"(stress|strain)[- ]?(field|shielding|distribution)s?", row["text"][a:b], re.I), row["text"][a:b]
                assert nested is None and form == re.sub(r"[- ]", " ", row["text"][a:b].lower())
        lit_imp = {m["scope"]: m for m in st.importance_of("C_stressstrainfield")}["all"]
        assert lit_imp["papers"] >= 1 and lit_imp["papers"] == st.concept_docs("C_stressstrainfield")["total"]
        assert lit_imp["topic_papers"] >= 1          # 'stress shielding' is a title / keywords phrase of the fixture
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
    # a frozen build cannot be reopened as open; promote refuses an unfinished build
    with pytest.raises(B.BuildError):
        run_stage(project, "match", build=bid)
    m2 = B.new_build(project, note="open")
    with pytest.raises(B.BuildError, match="not finished"):
        B.promote(project, m2["build_id"])
    assert current_build_id(project.builds_dir) == bid
    assert B.relink(project, bid, m2["build_id"]) == {}


def test_topic_texts_prefer_meta_fields_over_the_same_sentences():
    unit = {"unit_id": "P1", "kind": "document", "heading": None}
    sents = [("s1", "Keywords: stem, micromotion", "keywords", "P1", 1), ("s2", "Micromotion was measured.", "abstract", "P1", 1),
             ("s3", "Abstract", "abstract", "P1", 1), ("s4", "Body text.", "methods", "P1", 1)]
    t = topic_texts(unit, sents, {"title": "A  title", "keywords": None, "abstract": None})
    assert t == {"title": ["A title"], "keywords": ["stem, micromotion"], "abstract": ["Micromotion was measured."]}
    t = topic_texts(unit, sents, {"title": "A title", "keywords": "stem; micromotion", "abstract": "Micromotion was measured."})
    assert t["keywords"] == ["stem; micromotion"] and t["abstract"] == ["Micromotion was measured."]     # not twice
    ch = {"unit_id": "P1/ch", "kind": "chapter", "heading": "Chapter 2 Methods"}
    csents = [("c1", " ".join(f"w{i}" for i in range(200)), "methods", "P1/ch", 1), ("c2", "ref", "references", "P1/ch", 0),
              ("c3", " ".join(f"v{i}" for i in range(200)), "methods", "P1/ch", 1), ("c4", "late", "methods", "P1/ch", 1)]
    t = topic_texts(ch, csents, {})
    assert t["title"] == ["Chapter 2 Methods"] and t["keywords"] == []
    assert sum(len(x.split()) for x in t["abstract"]) == 300 and t["abstract"][-1].split()[-1] == "v99"


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
    assert cli.main(["build", "all", root, "--note", "cli all", "--workers", "1"]) == 0
    out = capsys.readouterr().out
    assert "finished and promoted" in out and "[match] done 9/9" in out
    assert read_manifest(project.builds_dir / current_build_id(project.builds_dir))["note"] == "cli all"
    assert cli.main(["curate", "confirm", root, "all"]) == 0
    assert capsys.readouterr().out.startswith("confirmed: ")
