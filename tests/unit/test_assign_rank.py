"""Review fixes of the derived layer (increment 2): the sub-domain concept ranking restricts its candidates to the
tiered concepts of the sub-domain's domain (v2 `subdomain_framework.py`) and counts topic_rate over every
title/keywords hit; `compute_importance` tiers only the 概念 items (phrases / journal names get tier NULL)."""
from __future__ import annotations

import pytest

from atlas.core.stats import is_tiered
from atlas.pipeline.stages.assign import AssignStage, rank_subdomain_concepts
from atlas.pipeline.stages.match import compute_importance
from atlas.store.build import BuildStore
from tests.unit.test_store_build import DOCS, _create, proj  # noqa: F401 - fixture re-export


def test_is_tiered_follows_the_v2_origin_rule():
    assert is_tiered(None) and is_tiered("") and is_tiered("概念（已合并）") and is_tiered("概念")
    assert not is_tiered("术语库条目（句式短语或期刊名）") and not is_tiered("通用部分用语") and not is_tiered("歧义缩写")


@pytest.fixture
def store(proj) -> BuildStore:  # noqa: F811 - pytest fixture
    st = _create(proj)
    st.close()
    st = BuildStore.open(st.build_dir, "rw", evidence_path=proj["evidence"])
    s = {doc: {r["seq"]: r["sid"] for r in st.sentences_of_doc(doc)} for doc in DOCS}
    # every document uses stem, micromotion and the phrase in its body; C_fe (domain DOM_a) too
    rows = []
    for doc in ("P1", "P2", "P3"):
        rows += [(s[doc][0], "C_stem", "stem", 0, 4, None), (s[doc][0], "C_micromotion", "micromotion", 5, 16, None),
                 (s[doc][0], "T_phrase", "in this study", 17, 30, None), (s[doc][0], "C_fe", "finite element", 31, 45, None)]
    st.add_occurrences(rows)
    # title hits: C_fe standalone in P1, nested-only in P2 and P3 (as 'neural network' inside 'convolutional neural network')
    st.add_topic_hits([("P1", "C_fe", "title", 1, 0), ("P2", "C_fe", "title", 1, 1), ("P3", "C_fe", "title", 1, 1),
                       ("P1", "C_stem", "title", 1, 0)])
    st.rebuild_counts()
    yield st
    st.close()


def test_sub_domain_ranking_filters_candidates_and_counts_nested_topic_hits(store, monkeypatch):
    from atlas.pipeline.stages import assign as mod
    monkeypatch.setattr(mod, "MIN_PAPERS_IN_SUB", 1)
    # C_micromotion has no domains: a foreign concept for the within-sub-domain A-S (domain DOM_a) …
    assignments = [("P1", "A-S", "core", {"C_stem": 1}, 1), ("P2", "A-S", "related", {"C_stem": 1}, 1),
                   ("P1", "X-SM", "core", {"C_stem": 1, "C_micromotion": 1}, 2)]
    sc, fs = rank_subdomain_concepts(store, assignments)
    by_sub = {}
    for r in sc:
        by_sub.setdefault(r["sub_id"], {})[r["concept_id"]] = r
    assert set(by_sub["A-S"]) == {"C_stem", "C_fe"}            # not C_micromotion (foreign domain), not T_phrase (untiered)
    assert "T_phrase" not in by_sub.get("X-SM", {}) and "C_micromotion" not in by_sub.get("X-SM", {})
    assert set(by_sub["X-SM"]) == {"C_stem", "C_fe"}            # cross: any counting-scope domain (DOM_a); DOM_w is no scope
    # topic_rate of the ranking counts every title/keywords hit: C_fe hits P1 (standalone) and P2 (nested) → 2 of 2
    assert by_sub["A-S"]["C_fe"]["papers"] == 2 and by_sub["A-S"]["C_fe"]["topic_rate"] == 1.0
    assert by_sub["A-S"]["C_stem"]["topic_rate"] == 0.5
    tops = {f["id"]: [c["id"] for c in f["concepts"]] for f in fs}
    assert "T_phrase" not in tops["A-S"] and "C_micromotion" not in tops["A-S"]
    assert AssignStage.version == "assign3"


def test_importance_tiers_only_the_concept_items(store):
    rows = compute_importance(store)
    by = {(r["concept_id"], r["scope"]): r for r in rows}
    assert by[("T_phrase", "all")]["papers"] == 3 and by[("T_phrase", "all")]["tier"] is None      # frequencies, no tier
    assert by[("C_stem", "all")]["tier"] in ("A", "B", "C", "D", "E") and by[("C_fe", "DOM_a")]["tier"] is not None
    assert by[("C_fe", "all")]["topic_papers"] == 1                     # importance keeps the standalone rule
    store.write_importance(rows)
    assert "T_phrase" not in {r["id"] for r in store.top_concepts("all", 100) if r["tier"] is not None}
    hist = store.tier_histogram()["all"]
    assert sum(hist.values()) == 4 and None not in hist                 # C_stem, C_fe, C_micromotion, C_lit
    assert store.list_concepts(kind="other")["rows"][0]["papers"] == 3  # the frequency columns stay available
