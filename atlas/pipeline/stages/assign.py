"""Stage 6 `assign`: sub-domain membership per unit (v2 `assign_subdomains.py` rules via `atlas.core.stats`),
the ranked concepts of every sub-domain (v2 `subdomain_framework.py`) and `framework_stats` for sub-domains and
content-framework categories. No LLM. Implemented by: increment 2 (derived layer).

Membership: a unit (document, and chapters for theses) belongs to a within-sub-domain of one of its document's
domains when any anchor has a hit in its title / keywords / abstract (topic_hits, **every** occurrence counted,
nested ones included: the previous system's assignment read the first version's `term_paper` topic columns,
which had no nested distinction — `neural network` inside `convolutional neural network` is a hit of the
anchor `neural network`); `core` when an anchor is in the title or keywords or anchors are mentioned >= 2 times
in the abstract (`stats.strong_abstract_hits`), else `related`; a cross sub-domain needs every anchor group to
hit, `core` needs every group strong. The ranked concepts of a sub-domain use the standalone body users of
`counts` (as the v2 sub-domain framework).

Version history: assign1 used standalone topic hits for the anchors; assign2 counts every hit (v1/v2 semantics).
"""
from __future__ import annotations

import logging
import time
from collections import Counter

from atlas.config import Project
from atlas.core.stats import DEFAULT_PARAMS, characteristic_score, membership, params_with_defaults, rank_concepts, smoothed_ratio
from atlas.pipeline.runner import Report
from atlas.store.build import BuildStore

log = logging.getLogger(__name__)

MIN_PAPERS_IN_SUB = 3       # a concept is listed for a sub-domain when >= 3 of its documents use it (as v3)
TOP_CONCEPTS_JSON = 60
TOP_OVERLAPS = 8


def assign_units(store: BuildStore, params: dict | None) -> list[tuple[str, str, str, dict, int]]:
    th = store.topic_hits_by_unit(standalone=False)
    meta = store.doc_meta_all()
    anchors = store.anchors_all()
    subs = store.subdomains_all()
    rows: list[tuple[str, str, str, dict, int]] = []
    for u in store.units_all():
        ut = th.get(u["unit_id"])
        if not ut:
            continue
        doms = (meta.get(u["doc_id"]) or {}).get("domains") or []
        for s in subs:
            groups = anchors.get(s["id"])
            if not groups:
                continue
            if s["kind"] == "within" and s["domain"] and s["domain"] not in doms:
                continue
            r = membership(ut, groups, params)
            if r is None:
                continue
            hits, tier = r
            rows.append((u["unit_id"], s["id"], tier, hits, int(sum(hits.values()))))
    return rows


def rank_subdomain_concepts(store: BuildStore, assignments: list[tuple[str, str, str, dict, int]]) -> tuple[list[dict], list[dict]]:
    """(subdomain_concepts rows, framework_stats rows of kind 'subdomain')."""
    meta = store.doc_meta_all()
    units = {u["unit_id"]: u for u in store.units_all()}
    all_docs = set(meta)
    dom_docs: dict[str, set[str]] = {}
    for d, m in meta.items():
        for dom in m.get("domains") or []:
            dom_docs.setdefault(dom, set()).add(d)
    sub_docs: dict[str, set[str]] = {}
    sub_core: dict[str, set[str]] = {}
    for unit_id, sub_id, tier, hits, n in assignments:
        doc_id = (units.get(unit_id) or {}).get("doc_id") or unit_id.split("/", 1)[0]
        sub_docs.setdefault(sub_id, set()).add(doc_id)
        if tier == "core":
            sub_core.setdefault(sub_id, set()).add(doc_id)
    users = store.body_users()
    topic = store.topic_users()
    labels = store.concept_labels(users)
    sc_rows: list[dict] = []
    fs_rows: list[dict] = []
    for s in store.subdomains_all():
        P = sub_docs.get(s["id"], set())
        base = dom_docs.get(s["domain"], set()) if s["kind"] == "within" and s["domain"] else all_docs
        rest = base - P
        ranked_in: list[dict] = []
        if P:
            for cid, U in users.items():
                k_in = len(U & P)
                if k_in < MIN_PAPERS_IN_SUB:
                    continue
                k_out = len(U & rest)
                cov = k_in / len(P)
                cov_rest = (k_out / len(rest)) if rest else 0.0
                lift = smoothed_ratio(k_in, len(P), k_out, len(rest))
                tr = len(U & P & topic.get(cid, set())) / k_in
                ranked_in.append({"sub_id": s["id"], "concept_id": cid, "papers": k_in, "coverage": round(cov, 4),
                                  "coverage_rest": round(cov_rest, 4), "lift": round(lift, 3), "topic_rate": round(tr, 4),
                                  "score": round(characteristic_score(cov, lift), 4)})
        ranked = rank_concepts(ranked_in)
        sc_rows.extend(ranked)
        years = Counter(str(meta[d].get("year")) for d in P if meta.get(d, {}).get("year"))
        overlaps = sorted(((o, len(P & sub_docs.get(o, set()))) for o in sub_docs if o != s["id"]),
                          key=lambda kv: (-kv[1], kv[0]))
        overlaps = [[o, n] for o, n in overlaps if n > 0][:TOP_OVERLAPS]
        top = [{"id": r["concept_id"], "label": labels.get(r["concept_id"], {}).get("label", r["concept_id"]),
                "papers": r["papers"], "coverage": r["coverage"], "lift": r["lift"], "score": r["score"]}
               for r in ranked[:TOP_CONCEPTS_JSON]]
        fs_rows.append({"kind": "subdomain", "id": s["id"], "scope": "all", "papers": len(P),
                        "core": len(sub_core.get(s["id"], set())), "base": len(base),
                        "years": dict(sorted(years.items())), "overlaps": overlaps, "concepts": top})
    return sc_rows, fs_rows


def category_stats(store: BuildStore) -> list[dict]:
    meta = store.doc_meta_all()
    dom_docs: dict[str, set[str]] = {}
    for d, m in meta.items():
        for dom in m.get("domains") or []:
            dom_docs.setdefault(dom, set()).add(d)
    users = store.body_users()
    by_cat: dict[str, list[str]] = {}
    for r in store.concept_categories_all():
        by_cat.setdefault(r["primary_cat"], []).append(r["concept_id"])
        if r.get("secondary_cat"):
            by_cat.setdefault(r["secondary_cat"], []).append(r["concept_id"])
    imp = {r["id"]: r for r in store.top_concepts("all", 10 ** 6)}
    out = []
    for cat in store.categories_all():
        cids = sorted(set(by_cat.get(cat["id"], [])), key=lambda c: -int((imp.get(c) or {}).get("papers") or 0))
        base = dom_docs.get(cat["domain"], set())
        used = set()
        for c in cids:
            used |= users.get(c, set()) & base
        out.append({"kind": "category", "id": cat["id"], "scope": "all", "papers": len(used), "core": 0,
                    "base": len(base), "years": {}, "overlaps": [],
                    "concepts": [{"id": c, "label": (imp.get(c) or {}).get("label", c),
                                  "papers": int((imp.get(c) or {}).get("papers") or 0),
                                  "tier": (imp.get(c) or {}).get("tier")} for c in cids[:TOP_CONCEPTS_JSON]]})
    return out


class AssignStage:
    name = "assign"
    version = "assign2"
    llm = False
    requires: tuple[str, ...] = ("match",)
    gate: tuple[str, ...] = ("subdomains", "domains", "doc_domains")

    def run(self, project: Project, build: BuildStore, **opts) -> Report:
        report = Report(stage=self.name)
        t0 = time.perf_counter()
        params = params_with_defaults(project.get("stats", DEFAULT_PARAMS))
        subs = build.subdomains_all()
        report.items_total = len(subs)
        rows = assign_units(build, params)
        n_units = build.write_unit_subdomains(rows)
        t_assign = time.perf_counter() - t0
        sc_rows, fs_rows = rank_subdomain_concepts(build, rows)
        fs_rows += category_stats(build)
        n_sc = build.write_subdomain_concepts(sc_rows)
        n_fs = build.write_framework_stats(fs_rows)
        report.items_done = len(subs)
        empty = [r["id"] for r in fs_rows if r["kind"] == "subdomain" and r["papers"] == 0]
        for sid in empty:
            report.warnings.append(f"sub-domain {sid}: no document assigned")
        detail = {"unit_subdomains": n_units, "subdomain_concepts": n_sc, "framework_stats": n_fs,
                  "empty_subdomains": empty,
                  "seconds": {"assign": round(t_assign, 2), "rank": round(time.perf_counter() - t0 - t_assign, 2)}}
        build.set_item(self.name, "*", "done", self.version, detail)
        m = build.manifest()
        stages = m.get("stages") or {}
        stages[self.name] = {"status": "done", "version": self.version, "seconds": round(time.perf_counter() - t0, 2),
                             **detail}
        build.update_manifest(stages=stages)
        report.details.update(detail)
        log.info(report.summary())
        return report
