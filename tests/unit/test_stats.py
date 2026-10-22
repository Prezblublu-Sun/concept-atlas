"""atlas.core.stats: the v2 metric functions over plain dicts, and the `stats:` block of defaults.yaml."""
from __future__ import annotations

import math

from atlas.config import load_defaults
from atlas.core.stats import (DEFAULT_PARAMS, anchor_hits, characteristic_score, concept_metrics, is_strong, membership,
                              params_with_defaults, rank_concepts, smoothed_ratio, standalone, tier_of, uses, year_bin)


def test_defaults_yaml_stats_block_matches_the_module_defaults():
    y = load_defaults()["stats"]
    assert {k: y[k] for k in DEFAULT_PARAMS} == DEFAULT_PARAMS
    assert y["body_extra_sections"] == ["appendix"] and y["body_includes_title"] is True
    assert y["cov_hi"] == 0.10 and y["topic_hi"] == 0.10 and y["min_papers"] == 5
    assert [b[0] for b in y["year_bins"]] == ["≤2015", "2016–2020", "2021–2023", "2024–2026"]


def test_year_bin():
    assert year_bin(2010) == "≤2015" and year_bin(2015) == "≤2015" and year_bin(2016) == "2016–2020"
    assert year_bin(2023) == "2021–2023" and year_bin(2026) == "2024–2026"
    assert year_bin(None) is None and year_bin("abc") is None and year_bin(1800) is None and year_bin(2030) is None


def test_standalone_and_uses():
    c = {"body": (5, 2), "title": (1, 1), "references": (3, 0)}
    assert standalone(c, "body", False) == 3 and standalone(c, "body", True) == 5
    assert standalone(c, "title", False) == 0 and standalone(c, "title", True) == 1
    assert standalone(c, "missing", False) == 0 and standalone(None, "body", False) == 0
    assert uses(c, False) and not uses({"body": (2, 2)}, False) and uses({"body": (2, 2)}, True)


def test_tiers():
    assert tier_of(4, 1.0, 1.0) == "E"
    assert tier_of(50, 0.10, 0.10) == "A"
    assert tier_of(50, 0.10, 0.05) == "B"
    assert tier_of(50, 0.05, 0.10) == "C"
    assert tier_of(50, 0.05, 0.05) == "D"
    assert tier_of(50, 0.05, 0.05, {"cov_hi": 0.01}) == "B"


def test_params_merge():
    p = params_with_defaults({"cov_hi": 0.2, "unknown": 1, "min_papers": None})
    assert p["cov_hi"] == 0.2 and p["min_papers"] == DEFAULT_PARAMS["min_papers"] and p["unknown"] == 1


def _corpus():
    # 12 documents: D01..D12; years spread so both trend windows have >= 2 docs (trend_min_n lowered in the test)
    years = {f"D{i:02d}": y for i, y in enumerate([2010, 2012, 2015, 2017, 2019, 2020, 2021, 2022, 2023, 2024, 2025, 2026], 1)}
    per_doc = {
        "D01": {"body": (4, 1), "title": (1, 0), "introduction": (2, 0), "methods": (2, 1), "abstract": (1, 0)},
        "D02": {"body": (2, 0), "keywords": (1, 0), "results": (2, 0), "references": (1, 0)},
        "D03": {"body": (3, 3)},                                   # only nested → not a user
        "D04": {"body": (1, 0), "discussion": (1, 0)},
        "D10": {"body": (6, 0), "title": (2, 1), "conclusion": (6, 0)},   # title standalone 1
        "D11": {"body": (2, 0), "abstract": (2, 0), "methods": (2, 0)},
        "D12": {"references": (2, 0)},                             # only in the reference list
    }
    return years, per_doc


def test_concept_metrics_all_scope():
    years, per_doc = _corpus()
    ids = sorted(years)
    p = dict(DEFAULT_PARAMS, trend_min_n=2)
    m = concept_metrics(per_doc, ids, years, literal=False, params=p)
    assert m["papers"] == 5 and m["any_papers"] == 6 and m["n_scope"] == 12
    assert math.isclose(m["coverage"], 5 / 12)
    assert m["occ"] == 3 + 2 + 1 + 6 + 2 and m["median_per_paper"] == 2.0
    assert m["topic_papers"] == 3 and math.isclose(m["topic_rate"], 3 / 5)     # D01 title, D02 keywords, D10 title
    assert m["abstract_papers"] == 2 and math.isclose(m["abstract_rate"], 2 / 5)
    assert m["refs_papers"] == 2                                            # D02 and D12
    assert m["sec_papers"] == {"introduction": 1, "methods": 2, "results": 1, "discussion": 2}  # discussion+conclusion
    assert m["main_section"] == "methods"                                   # tie → earlier section wins
    assert m["year_cov"]["≤2015"] == 2 / 3 and m["year_cov"]["2024–2026"] == 2 / 3
    assert m["lift"] is None
    assert m["tier"] == "A"
    assert m["early_k"] == 3 and m["late_k"] == 2 and m["trend"] is not None


def test_concept_metrics_domain_scope_lift_and_trend_flags():
    years, per_doc = _corpus()
    ids = sorted(years)
    scope = ["D01", "D02", "D03", "D10", "D11"]
    p = dict(DEFAULT_PARAMS, trend_min_n=1, trend_min_k=1)
    m = concept_metrics(per_doc, scope, years, literal=False, params=p, complement_ids=ids)
    assert m["papers"] == 4 and m["n_scope"] == 5
    ko = 1   # D04 outside the scope uses it
    assert math.isclose(m["lift"], smoothed_ratio(4, 5, ko, 7))
    assert m["trend_flag"] in ("", "up", "down")
    # a literal concept: nested occurrences count
    lit = concept_metrics({"D03": {"body": (3, 3)}}, ids, years, literal=True, params=p)
    assert lit["papers"] == 1 and lit["occ"] == 3 and lit["tier"] == "E"
    # no counts at all
    empty = concept_metrics({}, ids, years, params=p)
    assert empty["papers"] == 0 and empty["tier"] == "E" and empty["main_section"] == "" and empty["trend_flag"] == ""
    assert empty["trend"] is not None          # the smoothed ratio exists whenever both windows are large enough


def test_trend_requires_enough_documents():
    years, per_doc = _corpus()
    m = concept_metrics(per_doc, sorted(years), years, params=DEFAULT_PARAMS)       # trend_min_n 30 > 12 docs
    assert m["trend"] is None and m["trend_flag"] == ""


def test_subdomain_membership_rules():
    ut = {"C_a": (1, 0, 0), "C_b": (0, 0, 1), "C_c": (0, 0, 2)}
    assert anchor_hits(ut, ["C_a", "C_b", "C_x"]) == {"C_a": 1, "C_b": 1}
    assert is_strong(ut, ["C_a"]) and not is_strong(ut, ["C_b"]) and is_strong(ut, ["C_c"]) and is_strong(ut, ["C_b", "C_c"]) is True
    assert membership(ut, [["C_x"]]) is None
    assert membership(ut, [["C_b"]]) == ({"C_b": 1}, "related")
    assert membership(ut, [["C_a"], ["C_b"]]) == ({"C_a": 1, "C_b": 1}, "related")      # second group weak
    assert membership(ut, [["C_a"], ["C_c"]]) == ({"C_a": 1, "C_c": 2}, "core")
    assert membership(ut, [["C_a"], ["C_x"]]) is None                                     # every group must hit


def test_scores_and_ranking():
    assert characteristic_score(0.5, 1.0) == 0.5 and characteristic_score(0.5, 0.2) == 0.5
    assert math.isclose(characteristic_score(0.5, 3.0), 0.5 * 2.0)
    rows = rank_concepts([{"concept_id": "b", "score": 1.0, "papers": 3}, {"concept_id": "a", "score": 1.0, "papers": 3},
                          {"concept_id": "c", "score": 2.0, "papers": 1}])
    assert [(r["concept_id"], r["rank"]) for r in rows] == [("c", 1), ("a", 2), ("b", 3)]
