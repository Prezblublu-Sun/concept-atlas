"""atlas.store.build: build folder lifecycle, pinning from a small evidence store, scheme loading, stage outputs,
counts, queries and the freeze."""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from atlas.core.ids import extraction_id, sentence_id
from atlas.core.schemas import DocMeta, PageText, SentenceRow, Unit
from atlas.store.build import (BuildStore, current_build_id, list_build_dirs, new_build_id, read_manifest, write_manifest,
                               set_current_build)
from atlas.store.curated import write_yaml
from atlas.store.db import transaction
from atlas.store.evidence import EvidenceStore

SHA = "a" * 64
DOCS = {
    "P1": (2019, ["Finite element analysis of the stem.", "Micromotion was 20 um.", "[1] Ref entry."]),
    "P2": (2022, ["The FE model used a stem.", "Micromotion and micromotion."]),
    "P3": (2024, ["No concept here."]),
}


def _evidence(path: Path) -> None:
    ev = EvidenceStore.open(path)
    for doc_id, (year, sents) in DOCS.items():
        ev.add_document(doc_id, "paper", "manifest", path.parent / f"{doc_id}.pdf", f"{doc_id}.pdf", SHA,
                        DocMeta(title=f"Title {doc_id} stem", year=year, keywords="stem; micromotion" if doc_id == "P1" else None,
                                abstract="Micromotion abstract." if doc_id == "P2" else None))
        ext = extraction_id(doc_id, SHA, "t")
        text = "\n".join(sents)
        rows, pos = [], 0
        for i, s in enumerate(sents):
            cs = text.index(s, pos)
            section = "references" if s.startswith("[1]") else ("methods" if i == 0 else "results")
            rows.append(SentenceRow(sid=sentence_id(doc_id, s), doc_id=doc_id, text=s, ext_id=ext, seq=i, unit_id=doc_id,
                                    page_no=1, cstart=cs, cend=cs + len(s), section=section, is_body=section != "references"))
            pos = cs + len(s)
        ev.add_extraction(ext_id=ext, doc_id=doc_id, pdf_sha256=SHA, extractor_version="t", doc_type="paper",
                          pages=[PageText(1, text)], masks=[], units=[Unit(doc_id, doc_id, "document", 0, None, 1, 1)],
                          sentences=rows, text_ratio=0.9, drop_ratio=0.0)
    ev.close()


def _curated(cdir: Path) -> None:
    write_yaml(cdir / "domains.yaml", {"domains": [{"id": "DOM_a", "zh": "甲", "en": "A", "order": 0, "scope": True},
                                                   {"id": "DOM_w", "zh": "写作", "en": "W", "order": 1, "scope": False}]})
    write_yaml(cdir / "concepts.yaml", {"concepts": [
        {"id": "C_stem", "label": "stem", "zh": "柄", "type": "t", "mode": "forms", "forms": ["stem", "stems"], "domains": ["DOM_a"]},
        {"id": "C_fe", "label": "finite element (FE)", "zh": "有限元", "type": "t", "mode": "forms", "forms": ["finite element"],
         "case_forms": ["FE"], "domains": ["DOM_a"], "place": ["x"], "shared_with": ["C_stem"]},
        {"id": "C_micromotion", "label": "micromotion", "zh": "微动", "type": "t", "mode": "forms", "forms": ["micromotion"]},
        {"id": "C_lit", "label": "x/y", "zh": "", "type": "t", "mode": "literal", "rx": "um", "probe": "um", "readable": "um"},
        {"id": "C_old", "label": "old", "mode": "forms", "forms": ["old"], "merged_into": "C_stem"},
        # a frequency-only entry (previous system: 句式短语 / 期刊名, never tiered, never ranked in a sub-domain)
        {"id": "T_phrase", "label": "in this study", "zh": "本研究", "type": "p", "mode": "forms", "forms": ["in this study"],
         "domains": ["DOM_a"], "origin": "术语库条目（句式短语或期刊名）"}]})
    write_yaml(cdir / "patterns.yaml", {"version": "v2", "bad_next": {"stem": ["cell"]}})
    write_yaml(cdir / "categories.yaml", {"categories": [{"id": "A1", "domain": "DOM_a", "name_zh": "类", "name_en": "Cat", "order": 0}]})
    write_yaml(cdir / "concept_categories.yaml", {"rows": [{"concept_id": "C_stem", "domain": "DOM_a", "primary": "A1", "secondary": None, "reason": "r"}]})
    write_yaml(cdir / "subdomains.yaml", {"subdomains": [
        {"id": "A-S", "kind": "within", "domain": "DOM_a", "name_zh": "柄", "name_en": "Stems", "order": 0, "anchors": ["C_stem"]},
        {"id": "X-SM", "kind": "cross", "domain": None, "name_zh": "交", "name_en": "X", "order": 1, "groups": [["C_stem"], ["C_micromotion"]]}]})
    write_yaml(cdir / "doc_domains.yaml", {"docs": {"P1": ["DOM_a"], "P2": ["DOM_a"], "P3": []}})
    write_yaml(cdir / "term_tree.yaml", {"domains": [{"id": "DOM_a", "sections": [{"section": "title", "subs": [
        {"name": "对象", "order": 0, "terms": [{"term": "stem", "zh": "柄", "type": "t", "concept_id": "C_stem"}]}]}]}]})
    write_yaml(cdir / "i18n.yaml", {"strings": [{"zh": "柄", "en": "stem", "grp": "framework"}]})


@pytest.fixture
def proj(tmp_path: Path):
    ev = tmp_path / "evidence" / "evidence.sqlite"
    _evidence(ev)
    cdir = tmp_path / "curated"
    cdir.mkdir()
    _curated(cdir)
    return {"root": tmp_path, "evidence": ev, "curated": cdir, "builds": tmp_path / "builds"}


def _create(proj) -> BuildStore:
    bid = new_build_id(proj["builds"], "2026-01-02")
    st = BuildStore.create(proj["builds"], bid, "proj", proj["curated"], note="n")
    st.pin_evidence(proj["evidence"], {"P1": ["DOM_a"], "P2": ["DOM_a"], "P3": []})
    st.load_scheme()
    return st


def test_build_ids_and_manifest(proj):
    assert new_build_id(proj["builds"], "2026-01-02") == "b2026-01-02_001"
    st = _create(proj)
    assert st.build_id == "b2026-01-02_001"
    assert new_build_id(proj["builds"], "2026-01-02") == "b2026-01-02_002"
    assert new_build_id(proj["builds"], "2026-01-03") == "b2026-01-03_001"
    m = read_manifest(st.build_dir)
    assert m["status"] == "open" and m["note"] == "n" and set(m["curated"]) >= {"concepts", "patterns", "subdomains"}
    assert (st.build_dir / "curated" / "concepts.yaml").is_file()
    with pytest.raises(FileExistsError):
        BuildStore.create(proj["builds"], st.build_id, "proj", proj["curated"])
    assert [d.name for d in list_build_dirs(proj["builds"])] == ["b2026-01-02_001"]
    assert current_build_id(proj["builds"]) is None
    set_current_build(proj["builds"], st.build_id)
    assert current_build_id(proj["builds"]) == st.build_id
    st.close()


def test_new_build_id_skips_the_sequence_of_a_deleted_folder(proj):
    """Review fix: the next id is max(seq) + 1, not count + 1 — after _001 is deleted while _002 exists the id must
    not collide with _002 (the folder is write-once, so a collision stopped every build of the day)."""
    import shutil
    a = BuildStore.create(proj["builds"], new_build_id(proj["builds"], "2026-01-02"), "proj", proj["curated"])
    a.close()
    b = BuildStore.create(proj["builds"], new_build_id(proj["builds"], "2026-01-02"), "proj", proj["curated"])
    b.close()
    assert [d.name for d in list_build_dirs(proj["builds"])] == ["b2026-01-02_001", "b2026-01-02_002"]
    shutil.rmtree(proj["builds"] / "b2026-01-02_001")
    nxt = new_build_id(proj["builds"], "2026-01-02")
    assert nxt == "b2026-01-02_003" and not (proj["builds"] / nxt).exists()
    BuildStore.create(proj["builds"], nxt, "proj", proj["curated"]).close()
    (proj["builds"] / "not-a-build").mkdir()
    assert new_build_id(proj["builds"], "2026-01-02") == "b2026-01-02_004"


def test_upgrade_brings_a_schema_1_build_to_the_current_schema_in_place(proj):
    """`atlas build upgrade`: a finished build of schema 1 (occurrences without the denormalised columns, index
    occ_concept) gets the columns filled from build_sentences, the covering index, user_version 2 and a fresh
    freeze — the queries then answer exactly as on a freshly built file."""
    from atlas.store.db import BUILD_SCHEMA_VERSION
    st = _create(proj)
    st.close()
    st = BuildStore.open(st.build_dir, "rw", evidence_path=proj["evidence"])
    s = {doc: {r["seq"]: r["sid"] for r in st.sentences_of_doc(doc)} for doc in DOCS}
    st.add_occurrences([(s["P1"][0], "C_fe", "finite element", 0, 14, None), (s["P1"][0], "C_stem", "stem", 31, 35, None),
                        (s["P2"][1], "C_micromotion", "micromotion", 0, 11, None), (s["P2"][1], "C_micromotion", "micromotion", 16, 27, "C_fe")])
    st.add_topic_hits([("P1", "C_stem", "title", 1, 0)])
    st.rebuild_counts()
    expected = st.concept_sentences("C_micromotion")
    expected_pc = st.page_concepts("P2", 1, ["C_micromotion"])
    # back to the shape of schema 1 (what b2026-10-22_002 holds)
    with transaction(st.conn) as c:
        c.execute("DROP INDEX occ_concept_doc")
        for col in ("doc_id", "seq", "page_no", "section", "is_body", "tags"):
            c.execute(f"ALTER TABLE occurrences DROP COLUMN {col}")
        c.execute("CREATE INDEX occ_concept ON occurrences(concept_id, sid)")
    st.conn.execute("PRAGMA user_version = 1")
    sha1 = st.freeze()
    write_manifest(st.build_dir, dict(read_manifest(st.build_dir), status="finished", sha256=sha1))
    assert {r[1] for r in BuildStore.open(st.build_dir, "immutable").conn.execute("PRAGMA table_info(occurrences)")} == {
        "sid", "concept_id", "form", "cstart", "cend", "nested_in"}
    ro = BuildStore.open(st.build_dir, "immutable")
    assert ro.schema_version == 1 and not ro.schema_current
    ro.close()
    r = BuildStore.upgrade(st.build_dir)
    assert r["from"] == 1 and r["to"] == BUILD_SCHEMA_VERSION and len(r["sha256"]) == 64 and r["sha256"] != sha1
    m = read_manifest(st.build_dir)
    assert m["sha256"] == r["sha256"] and m["schema_version"] == BUILD_SCHEMA_VERSION and m["status"] == "finished"
    assert not (os.stat(st.db_path).st_mode & stat.S_IWRITE) and not (st.build_dir / "build.sqlite-wal").exists()
    up = BuildStore.open(st.build_dir, "immutable", evidence_path=proj["evidence"])
    try:
        assert up.schema_current and up.schema_version == BUILD_SCHEMA_VERSION
        assert {r[1] for r in up.conn.execute("PRAGMA index_list(occurrences)")} >= {"occ_concept_doc"}
        assert up.concept_sentences("C_micromotion") == expected and up.page_concepts("P2", 1, ["C_micromotion"]) == expected_pc
        assert up.sentence_count("C_fe") == 1 and up.examples("C_micromotion", 6)[0]["doc_id"] == "P2"
        assert up.conn.execute("SELECT COUNT(*) FROM occurrences WHERE doc_id = ''").fetchone()[0] == 0
    finally:
        up.close()
    # idempotent: a current build is left alone (sha unchanged)
    again = BuildStore.upgrade(st.build_dir)
    assert again["unchanged"] is True and again["sha256"] == r["sha256"]
    # an open build is not touched
    o = _create(proj)
    o.close()
    with pytest.raises(ValueError, match="not finished"):
        BuildStore.upgrade(o.build_dir)
    os.chmod(st.db_path, stat.S_IWRITE | stat.S_IREAD)


def test_pin_and_scheme(proj):
    st = _create(proj)
    assert st.pin_counts() == {"docs": 3, "sentences": 6, "units": 3}
    assert st.doc_meta("P1")["domains"] == ["DOM_a"] and st.doc_meta("P3")["domains"] == []
    assert st.doc_meta("P2")["abstract"] == "Micromotion abstract." and st.doc_meta("P1")["keywords"] == "stem; micromotion"
    rows = st.sentences_of_doc("P1")
    assert [r["section"] for r in rows] == ["methods", "results", "references"]
    assert rows[0]["text"] == "Finite element analysis of the stem."      # pinning attached the evidence
    st.close()
    st = BuildStore.open(st.build_dir, "rw")
    assert st.sentences_of_doc("P1")[0]["text"] is None                     # opened without the evidence
    st.close()
    st = BuildStore.open(st.build_dir, "rw", evidence_path=proj["evidence"])
    assert st.sentences_of_doc("P1")[0]["text"]
    sc = st.scheme_counts()
    assert sc["concepts"] == 6 and sc["concept_forms"] == 8 and sc["subdomains"] == 2 and sc["anchors"] == 3   # 7 forms + 1 literal
    assert st.concept_scope()["T_phrase"] == {"domains": ["DOM_a"], "tiered": False} and st.concept_scope()["C_fe"]["tiered"]
    assert "C_old" not in st.concept_scope() and st.concept("T_phrase")["tiered"] is False and st.concept("C_fe")["tiered"] is True
    concepts, patterns = st.matcher_data()
    by = {c["id"]: c for c in concepts}
    assert by["C_fe"]["case_forms"] == ["FE"] and by["C_lit"]["rx"] == "um" and patterns["bad_next"] == {"stem": ["cell"]}
    assert st.literal_ids() == {"C_lit"}
    assert st.scope_ids() == ["all", "DOM_a"]
    assert st.anchors_all() == {"A-S": [["C_stem"]], "X-SM": [["C_stem"], ["C_micromotion"]]}
    c = st.concept("C_fe")
    assert c["forms"] == ["finite element"] and c["case_forms"] == ["FE"] and c["shared_with"] == ["C_stem"] and c["place"] == ["x"]
    assert st.concept("nope") is None
    assert st.i18n_strings() == {"柄": "stem"}
    st.close()


def test_outputs_counts_and_queries(proj):
    st = _create(proj)
    st.close()
    st = BuildStore.open(st.build_dir, "rw", evidence_path=proj["evidence"])
    s = {doc: {r["seq"]: r["sid"] for r in st.sentences_of_doc(doc)} for doc in DOCS}
    st.add_occurrences([
        (s["P1"][0], "C_fe", "finite element", 0, 14, None), (s["P1"][0], "C_stem", "stem", 31, 35, None),
        (s["P1"][1], "C_micromotion", "micromotion", 0, 11, None), (s["P1"][1], "C_lit", "um", 19, 21, None),
        (s["P2"][0], "C_fe", "FE", 4, 6, None), (s["P2"][0], "C_stem", "stem", 18, 22, None),
        (s["P2"][1], "C_micromotion", "micromotion", 0, 11, None), (s["P2"][1], "C_micromotion", "micromotion", 16, 27, "C_fe"),
    ])
    st.add_topic_hits([("P1", "C_stem", "title", 1, 0), ("P1", "C_stem", "keywords", 1, 0), ("P1", "C_micromotion", "keywords", 1, 0),
                       ("P2", "C_stem", "title", 1, 0), ("P2", "C_micromotion", "abstract", 1, 0), ("P3", "C_stem", "title", 1, 0)])
    assert st.rebuild_counts() > 0
    body = st.body_counts("C_micromotion")
    assert body == {"P1": (1, 0), "P2": (2, 1)}
    assert st.recount_body("C_micromotion") == body
    assert {d for d in st.body_users()["C_micromotion"]} == {"P1", "P2"}
    assert st.topic_users()["C_stem"] == {"P1", "P2", "P3"}
    # review fix: a nested-only title hit ('neural network' inside 'convolutional neural network') is no standalone
    # topic user (importance) but counts for the sub-domain ranking's topic_rate (v2 read any hit)
    st.add_topic_hits([("P3", "C_micromotion", "title", 1, 1)])
    st.rebuild_counts()
    assert st.topic_users()["C_micromotion"] == {"P1"}                       # P1: a standalone keywords hit
    assert st.topic_users(standalone=False)["C_micromotion"] == {"P1", "P3"}
    th = st.topic_hits_by_unit()
    assert th["P1"]["C_stem"] == (1, 1, 0) and th["P2"]["C_micromotion"] == (0, 0, 1)
    st.add_topic_hits([("P3", "C_micromotion", "abstract", 2, 2), ("P3", "C_lit", "abstract", 1, 1)])
    assert st.topic_hits_by_unit()["P3"] == {"C_stem": (1, 0, 0), "C_micromotion": (1, 0, 2), "C_lit": (0, 0, 1)}   # every hit
    assert st.topic_hits_by_unit(standalone=True)["P3"] == {"C_stem": (1, 0, 0), "C_lit": (0, 0, 1)}            # literal: n
    st.add_topic_hits([("P3", "C_micromotion", "abstract", 2, 2), ("P3", "C_lit", "abstract", 1, 1)])     # idempotent
    st.rebuild_counts()
    per = dict(st.iter_counts())
    assert per["C_micromotion"]["P2"]["body"] == (2, 1) and per["C_lit"]["P1"]["body"] == (1, 0)
    assert per["C_stem"]["P3"] == {"title": (1, 0), "body": (1, 0)}   # a title hit counts as body use (v2 full text)
    assert st.rebuild_counts(body_includes_title=False) > 0
    assert dict(st.iter_counts())["C_stem"]["P3"] == {"title": (1, 0)}
    assert st.rebuild_counts() > 0
    # stage items
    st.set_items("match", [("P1", "done", "fp", {"n": 1}), ("P2", "failed", "fp", {"error": "x"})])
    st.set_item("match", "*", "done", "fp", {"k": 1})
    assert st.stage_status("match") == "done" and st.stage_status("assign") is None
    assert st.stage_summary()["match"]["done"] == 1 and st.stage_summary()["match"]["failed"] == 1
    # importance + framework rows round trip
    st.write_importance([{"concept_id": "C_stem", "scope": "all", "papers": 2, "any_papers": 2, "coverage": 2 / 3, "topic_papers": 2,
                          "topic_rate": 1.0, "abstract_rate": 0.0, "occ": 2, "median_per_paper": 1.0, "refs_papers": 0,
                          "main_section": "methods", "lift": None, "year_cov": {"≤2015": None}, "trend": None, "trend_flag": "", "tier": "E"}])
    imp = st.importance_of("C_stem")
    assert imp[0]["year_cov"] == {"≤2015": None} and imp[0]["tier"] == "E"
    # a frequency-only entry carries tier NULL and stays out of the tier histogram
    st.write_importance(st.importance_of("C_stem") + [dict(imp[0], concept_id="T_phrase", tier=None, year_cov={})])
    assert st.importance_of("T_phrase")[0]["tier"] is None and st.tier_histogram() == {"all": {"E": 1}}
    st.write_unit_subdomains([("P1", "A-S", "core", {"C_stem": 2}, 2), ("P2", "X-SM", "related", {"C_stem": 1, "C_micromotion": 1}, 2)])
    st.write_subdomain_concepts([{"sub_id": "A-S", "concept_id": "C_fe", "rank": 1, "papers": 1, "coverage": 1.0, "coverage_rest": 0.5,
                                  "lift": 1.2, "topic_rate": 0.0, "score": 0.9}])
    st.write_framework_stats([{"kind": "subdomain", "id": "A-S", "scope": "all", "papers": 1, "core": 1, "base": 2, "years": {"2019": 1},
                               "overlaps": [["X-SM", 1]], "concepts": [{"id": "C_fe"}]}])
    # queries
    lst = st.list_concepts(sort="papers")
    assert lst["total"] == 4 and lst["rows"][0]["id"] == "C_stem" and "C_old" not in [r["id"] for r in lst["rows"]]
    assert lst["kind"] == "concept" and "T_phrase" not in [r["id"] for r in lst["rows"]]        # default: tiered concepts
    assert [r["id"] for r in st.list_concepts(kind="other")["rows"]] == ["T_phrase"] and st.list_concepts(kind="all")["total"] == 5
    assert st.list_concepts(kind="other")["rows"][0]["tier"] is None
    assert st.list_concepts(q="微")["rows"][0]["id"] == "C_micromotion"
    assert st.list_concepts(domain="DOM_a")["total"] == 2
    assert st.list_concepts(tier="E")["total"] == 1 and st.list_concepts(sub="A-S")["total"] == 1 and st.list_concepts(cat="A1")["total"] == 1
    assert [r["id"] for r in st.list_concepts(sort="label", dir="desc")["rows"]][0] == "C_lit"       # 'x/y' sorts last
    assert [r["id"] for r in st.list_concepts(sort="label")["rows"]][0] == "C_fe"
    assert st.list_concepts(sort="label")["dir"] == "asc"
    secs = {r["section"]: r for r in st.sections_of("C_micromotion")}
    assert secs["body"]["papers"] == 2 and secs["body"]["occ"] == 2 and secs["results"]["papers"] == 2
    du = st.domains_use("C_micromotion")
    assert du[0]["domain"] == "DOM_a" and du[0]["papers"] == 2 and du[0]["total"] == 2 and du[-1]["domain"] is None and du[-1]["total"] == 1
    assert [(r["year"], r["n"]) for r in st.years_of("C_micromotion")] == [(2019, 1), (2022, 1)]
    assert st.year_totals() == {"2019": 1, "2022": 1, "2024": 1}
    assert st.categories_of("C_stem")[0]["primary_zh"] == "类"
    assert st.subdomains_of("C_fe")[0]["sub_id"] == "A-S" and st.subdomains_of("C_fe")[0]["sub_papers"] == 1
    assert [a["sub_id"] for a in st.anchor_of("C_stem")] == ["A-S", "X-SM"]
    assert st.term_tree_places("C_stem")[0]["sub_name"] == "对象"
    ex = st.examples("C_micromotion", 6)
    # both documents have one standalone body occurrence → tie broken by doc_id
    assert [e["doc_id"] for e in ex] == ["P1", "P2"] and ex[0]["text"].startswith("Micromotion") and ex[0]["spans"][0][:2] == [0, 11]
    sen = st.concept_sentences("C_micromotion", standalone=True, sort="year")
    assert sen["total"] == 2 and sen["rows"][0]["doc_id"] == "P2" and len(sen["rows"][0]["spans"]) == 2
    # the "all sentences" button count = the view's total (sentences, nested included), not importance.occ
    assert st.sentence_count("C_micromotion") == st.concept_sentences("C_micromotion")["total"] == 2
    assert st.sentence_count("C_micromotion", standalone=True) == 2 and st.sentence_count("C_fe") == 2
    assert st.concept_sentences("C_micromotion", doc="P1")["total"] == 1
    assert st.concept_sentences("C_micromotion", section="methods")["total"] == 0
    assert st.concept_sentences("C_micromotion", sub="X-SM")["rows"][0]["doc_id"] == "P2"
    assert st.concept_sentences("C_micromotion", domain="DOM_a", limit=1, offset=1)["rows"][0]["doc_id"] == "P2"
    docs = st.concept_docs("C_micromotion")
    assert docs["total"] == 2 and docs["rows"][0]["doc_id"] == "P2" and docs["rows"][0]["body"] == 1 and docs["rows"][0]["body_nested"] == 1
    assert st.concept_docs("C_stem", use="any")["total"] == 3 and st.concept_docs("C_stem", scope="DOM_a")["total"] == 2
    tree = st.term_tree()
    assert tree[0]["id"] == "DOM_a" and tree[0]["sections"][0]["subs"][0]["terms"][0]["concept_id"] == "C_stem" and tree[0]["n"] == 1
    assert "example" not in tree[0]["sections"][0]["subs"][0]["terms"][0]          # the tree view never shows examples
    assert "example" in st.term_tree(full=True)[0]["sections"][0]["subs"][0]["terms"][0]
    cats = st.categories_tree()
    assert cats[0]["categories"][0]["concepts"][0]["id"] == "C_stem"
    sdt = st.subdomain_tree()
    assert sdt[0]["id"] == "DOM_a" and sdt[0]["subs"][0]["papers"] == 1 and sdt[-1]["id"] == "cross"
    sd = st.subdomain("A-S")
    assert sd["anchors"] == [[{"id": "C_stem", "label": "stem", "zh": "柄", "type": "t"}]] and sd["overlaps"][0]["id"] == "X-SM"
    assert sd["docs"][0]["doc_id"] == "P1" and sd["docs"][0]["hits"] == [{"id": "C_stem", "label": "stem", "n": 2}]
    assert st.subdomain_docs("X-SM", tier="related")["total"] == 1 and st.subdomain_docs("X-SM", tier="core")["total"] == 0
    ext = st.doc_extension("P2")
    assert ext["subdomains"][0]["sub_id"] == "X-SM" and ext["units"][0]["kind"] == "document"
    assert {c["id"] for c in ext["top_concepts"]} == {"C_micromotion", "C_fe", "C_stem"}
    assert ext["top_concepts"][0]["id"] == "C_stem" and ext["top_concepts"][0]["n"] == 2     # body + title hit
    pc = st.page_concepts("P2", 1, ["C_micromotion", "C_fe"])
    assert pc[s["P2"][1]][1]["nested_in"] == "C_fe" and len(pc[s["P2"][0]]) == 1
    assert st.subdomain_label("X-SM")["kind"] == "cross" and st.subdomain_label("nope") is None
    # rerun support: deleting a document's outputs
    st.delete_doc_outputs(["P2"])
    assert st.concept_sentences("C_micromotion")["total"] == 1 and "P2" not in st.topic_hits_by_unit()
    st.close()


def test_freeze_makes_the_file_read_only_and_immutable(proj):
    st = _create(proj)
    st.close()
    st = BuildStore.open(st.build_dir, "rw", evidence_path=proj["evidence"])
    st.meta_set("n_occurrences", "0")
    sha = st.freeze()
    assert len(sha) == 64 and not (st.db_path.parent / "build.sqlite-wal").exists()
    assert not os.access(st.db_path, os.W_OK) or not (os.stat(st.db_path).st_mode & stat.S_IWRITE)
    ro = BuildStore.open(st.build_dir, "immutable", evidence_path=proj["evidence"])
    assert ro.stats()["docs"] == 3 and ro.stats()["sentences"] == 6
    assert ro.sentences_of_doc("P1")[0]["text"]
    ro.close()
    os.chmod(st.db_path, stat.S_IWRITE | stat.S_IREAD)        # let tmp_path clean up on Windows
