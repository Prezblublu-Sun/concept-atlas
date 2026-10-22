"""Counting metrics of the previous system (v2 `frequency_importance.py`, `assign_subdomains.py`,
`subdomain_framework.py`) over plain dicts — pure, no I/O. Implemented by: increment 2 (derived layer).

Vocabulary (all counts, no judgement):
- a document *uses* a concept when it has >= 1 standalone occurrence in the body (not nested in a longer
  concept; literal concepts: any occurrence);
- coverage = papers / n_scope; topic_papers = users with a standalone hit in the title or keywords;
  topic_rate = topic_papers / papers; abstract_rate likewise for the abstract; occ = standalone occurrences;
  median_per_paper; refs_papers = documents whose reference list mentions it;
- main_section = argmax over introduction / methods / results / discussion(+conclusion) paper counts;
- lift = ((k+0.5)/(n+1)) / ((ko+0.5)/(no+1)) against the complement of the scope;
- year_cov per bin; trend = late coverage / early coverage (both smoothed), flags 'up' (>= trend_up and
  late_k >= trend_min_k) / 'down' (<= trend_down and early_k >= trend_min_k);
- tiers: A (coverage >= cov_hi and topic_rate >= topic_hi), B (coverage only), C (topic only), D, E (< min_papers).

Per-document input: {section: (n, n_nested)} where section ∈ SECTIONS ∪ {'body'}; standalone = n - n_nested
(literal: n).

Sub-domain membership (v2 `assign_subdomains.py`) works on per-unit topic counts {concept_id: (title, keywords,
abstract)}; the caller decides whether those are every occurrence (the previous system: nested hits included) or
standalone counts — the functions here only add them up.

The defaults below are repeated in `atlas/defaults.yaml` (`stats:`), which a project may override; this module is
pure (no YAML), so a test keeps the two in sync.
"""
from __future__ import annotations

import math
import statistics
from collections.abc import Iterable, Mapping, Sequence

STATS_VERSION = "s1"       # bump when any metric definition in this module changes

DEFAULT_PARAMS: dict = {
    "cov_hi": 0.10,
    "topic_hi": 0.10,
    "min_papers": 5,
    "year_bins": [["≤2015", None, 2015], ["2016–2020", 2016, 2020], ["2021–2023", 2021, 2023],
                  ["2024–2026", 2024, 2026]],
    "trend_early_bins": ["≤2015", "2016–2020"],
    "trend_late_bins": ["2024–2026"],
    "trend_min_n": 30,
    "trend_up": 2.0,
    "trend_down": 0.5,
    "trend_min_k": 10,
    "strong_abstract_hits": 2,
}
MAIN_SECTIONS = ("introduction", "methods", "results", "discussion")
TIERS = ("A", "B", "C", "D", "E")
TIER_ZH = {"A": "高覆盖·高主题率", "B": "高覆盖·低主题率", "C": "低覆盖·高主题率", "D": "低覆盖·低主题率", "E": "少于最低篇数"}
TIER_EN = {"A": "high coverage, high topic rate", "B": "high coverage, low topic rate",
           "C": "low coverage, high topic rate", "D": "low coverage, low topic rate", "E": "fewer than the minimum"}


def params_with_defaults(p: Mapping | None) -> dict:
    out = dict(DEFAULT_PARAMS)
    for k, v in (p or {}).items():
        if v is not None:
            out[k] = v
    return out


def year_bin(year: int | None, bins: Sequence[Sequence] | None = None) -> str | None:
    """Label of the year bin that contains `year` ([label, lo|None, hi|None] rows), None when no bin / no year."""
    if year is None:
        return None
    try:
        y = int(year)
    except (TypeError, ValueError):
        return None
    if not 1900 < y < 2100:
        return None
    for label, lo, hi in (bins or DEFAULT_PARAMS["year_bins"]):
        if (lo is None or y >= int(lo)) and (hi is None or y <= int(hi)):
            return str(label)
    return None


def standalone(counts: Mapping[str, tuple[int, int]] | None, section: str, literal: bool) -> int:
    """Standalone occurrences of one section: n - n_nested (literal concepts: n)."""
    if not counts:
        return 0
    v = counts.get(section)
    if not v:
        return 0
    n, nested = int(v[0]), int(v[1])
    return n if literal else max(0, n - nested)


def uses(counts: Mapping[str, tuple[int, int]] | None, literal: bool) -> bool:
    return standalone(counts, "body", literal) > 0


def tier_of(papers: int, coverage: float, topic_rate: float, params: Mapping | None = None) -> str:
    p = params_with_defaults(params)
    if papers < int(p["min_papers"]):
        return "E"
    hi_c = coverage >= float(p["cov_hi"])
    hi_t = topic_rate >= float(p["topic_hi"])
    return "A" if hi_c and hi_t else "B" if hi_c else "C" if hi_t else "D"


def smoothed_ratio(k: int, n: int, ko: int, no: int) -> float:
    return ((k + 0.5) / (n + 1)) / ((ko + 0.5) / (no + 1))


def concept_metrics(per_doc: Mapping[str, Mapping[str, tuple[int, int]]], scope_ids: Iterable[str],
                    doc_years: Mapping[str, int | None], literal: bool = False, params: Mapping | None = None,
                    complement_ids: Iterable[str] | None = None) -> dict:
    """The v2 importance row of one concept in one scope. `per_doc` = {doc_id: {section: (n, n_nested)}} over
    every document (any scope); `scope_ids` = the documents of the scope; `complement_ids` (optional) = the
    documents outside the scope for `lift` (None → lift None, as for the 'all' scope)."""
    p = params_with_defaults(params)
    scope = list(dict.fromkeys(scope_ids))
    sset = set(scope)
    n = len(scope)
    use = {d: per_doc[d] for d in scope if d in per_doc and uses(per_doc[d], literal)}
    any_papers = sum(1 for d in scope if d in per_doc and (per_doc[d].get("body") or (0, 0))[0] > 0)
    k = len(use)
    m: dict = {"papers": k, "any_papers": any_papers, "coverage": (k / n) if n else 0.0, "n_scope": n}
    body = [standalone(c, "body", literal) for c in use.values()]
    m["occ"] = int(sum(body))
    m["median_per_paper"] = float(statistics.median(body)) if body else 0.0
    m["topic_papers"] = sum(1 for c in use.values()
                            if standalone(c, "title", literal) > 0 or standalone(c, "keywords", literal) > 0)
    m["topic_rate"] = (m["topic_papers"] / k) if k else 0.0
    m["abstract_papers"] = sum(1 for c in use.values() if standalone(c, "abstract", literal) > 0)
    m["abstract_rate"] = (m["abstract_papers"] / k) if k else 0.0
    m["refs_papers"] = sum(1 for d in scope if d in per_doc and (per_doc[d].get("references") or (0, 0))[0] > 0)
    secp = {s: sum(1 for c in use.values() if (c.get(s) or (0, 0))[0] > 0) for s in MAIN_SECTIONS}
    # 'discussion' stands for discussion + conclusion (one part in the previous system)
    secp["discussion"] = sum(1 for c in use.values()
                             if (c.get("discussion") or (0, 0))[0] > 0 or (c.get("conclusion") or (0, 0))[0] > 0)
    m["sec_papers"] = secp
    m["main_section"] = max(MAIN_SECTIONS, key=lambda s: (secp[s], -MAIN_SECTIONS.index(s))) \
        if any(secp.values()) else ""
    # years
    bins = p["year_bins"]
    labels = [str(b[0]) for b in bins]
    yc = {b: 0 for b in labels}
    yu = {b: 0 for b in labels}
    for d in scope:
        b = year_bin(doc_years.get(d), bins)
        if b is not None:
            yc[b] += 1
            if d in use:
                yu[b] += 1
    m["year_cov"] = {b: ((yu[b] / yc[b]) if yc[b] else None) for b in labels}
    early = [b for b in p["trend_early_bins"] if b in yc]
    late = [b for b in p["trend_late_bins"] if b in yc]
    early_n, early_k = sum(yc[b] for b in early), sum(yu[b] for b in early)
    late_n, late_k = sum(yc[b] for b in late), sum(yu[b] for b in late)
    m["early_k"], m["late_k"] = early_k, late_k
    if early_n >= int(p["trend_min_n"]) and late_n >= int(p["trend_min_n"]):
        m["trend"] = smoothed_ratio(late_k, late_n, early_k, early_n)
    else:
        m["trend"] = None
    if m["trend"] is None or k < int(p["min_papers"]):
        m["trend_flag"] = ""
    elif m["trend"] >= float(p["trend_up"]) and late_k >= int(p["trend_min_k"]):
        m["trend_flag"] = "up"
    elif m["trend"] <= float(p["trend_down"]) and early_k >= int(p["trend_min_k"]):
        m["trend_flag"] = "down"
    else:
        m["trend_flag"] = ""
    # concentration against the complement
    if complement_ids is not None:
        comp = [d for d in dict.fromkeys(complement_ids) if d not in sset]
        ko = sum(1 for d in comp if d in per_doc and uses(per_doc[d], literal))
        m["lift"] = smoothed_ratio(k, n, ko, len(comp))
    else:
        m["lift"] = None
    m["tier"] = tier_of(k, m["coverage"], m["topic_rate"], p)
    return m


# ---- sub-domain membership (v2 assign_subdomains.py) -------------------------------------------------------

def anchor_hits(unit_topic: Mapping[str, Sequence[int]], anchors: Iterable[str]) -> dict[str, int]:
    """{anchor concept: title + keywords + abstract hits} for the anchors present in the unit's topic fields.
    `unit_topic` = {concept_id: (title, keywords, abstract)} standalone counts."""
    out: dict[str, int] = {}
    for a in anchors:
        v = unit_topic.get(a)
        if v:
            n = int(v[0]) + int(v[1]) + int(v[2])
            if n > 0:
                out[a] = n
    return out


def is_strong(unit_topic: Mapping[str, Sequence[int]], anchors: Iterable[str], params: Mapping | None = None) -> bool:
    """Core membership: an anchor in the title or keywords, or anchors mentioned >= strong_abstract_hits times in
    the abstract."""
    p = params_with_defaults(params)
    tk = ab = 0
    for a in anchors:
        v = unit_topic.get(a)
        if v:
            tk += int(v[0]) + int(v[1])
            ab += int(v[2])
    return tk > 0 or ab >= int(p["strong_abstract_hits"])


def membership(unit_topic: Mapping[str, Sequence[int]], groups: Sequence[Sequence[str]],
               params: Mapping | None = None) -> tuple[dict[str, int], str] | None:
    """(hits, tier) when the unit belongs to a sub-domain described by anchor `groups` (a within-sub-domain has
    one group; a cross sub-domain needs every group to hit), else None. tier = 'core' when every group is strong,
    else 'related'."""
    merged: dict[str, int] = {}
    strong = True
    for g in groups:
        h = anchor_hits(unit_topic, g)
        if not h:
            return None
        merged.update(h)
        if not is_strong(unit_topic, g, params):
            strong = False
    return merged, ("core" if strong else "related")


def characteristic_score(coverage: float, lift: float) -> float:
    """v2 sub-domain framework: coverage inside the sub-domain weighted by how much more often the concept is used
    there than in the comparison documents."""
    return float(coverage) * math.log2(max(float(lift), 1.0) + 1.0)


def rank_concepts(rows: list[dict], key: str = "score") -> list[dict]:
    """Stable rank (1-based) by `key` desc, then papers desc, then id; adds 'rank' to each row (copies)."""
    out = [dict(r) for r in rows]
    out.sort(key=lambda r: (-float(r.get(key) or 0.0), -int(r.get("papers") or 0), str(r.get("concept_id") or r.get("id"))))
    for i, r in enumerate(out, start=1):
        r["rank"] = i
    return out
