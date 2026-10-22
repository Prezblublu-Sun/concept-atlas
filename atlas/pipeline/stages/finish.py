"""Stage 12 `finish`: build-level checks → `report.md` → freeze (wal_checkpoint(TRUNCATE) → VACUUM →
journal_mode=DELETE → read-only file attribute → sha256 into manifest.json, status `finished`).
Implemented by: increment 2 (derived layer).

Checks (check_results): every occurrences.sid exists in build_sentences; every anchor resolves to a concept;
counts consistent with occurrences on 20 sampled concepts.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from atlas.config import Project
from atlas.core.stats import TIERS
from atlas.pipeline.runner import Report
from atlas.store.build import REPORT, BuildStore

log = logging.getLogger(__name__)


def run_checks(store: BuildStore, project: Project | None = None) -> list[tuple[str, str, str, str, str | None]]:
    rows: list[tuple[str, str, str, str, str | None]] = []
    extra = tuple((project.get("stats.body_extra_sections", ["appendix"]) if project else ["appendix"]) or ())
    with_title = bool(project.get("stats.body_includes_title", True)) if project else True
    n = store.orphan_occurrence_sids()
    rows.append(("build", store.build_id, "occurrences_sids_exist", "error" if n else "info",
                 f"{n} occurrences point at unknown sentences" if n else "ok"))
    bad = store.unresolved_anchors()
    rows.append(("build", store.build_id, "anchors_resolve", "error" if bad else "info",
                 ("unresolved anchors: " + ", ".join(bad[:20])) if bad else "ok"))
    lits = store.literal_ids()
    mismatches = 0
    sampled = store.sampled_concepts(20)
    for cid in sampled:
        a = store.recount_body(cid, extra, with_title)
        b = store.body_counts(cid)
        if a != b:
            mismatches += 1
            rows.append(("concept", cid, "counts_match_occurrences", "error",
                         f"counts differ for {len(set(a) ^ set(b)) or 'some'} documents"))
    rows.append(("build", store.build_id, "counts_sampled", "error" if mismatches else "info",
                 f"{len(sampled)} concepts sampled, {mismatches} mismatches" + (" (literal concepts: %d)" % len(lits & set(sampled)))))
    return rows


def write_report(store: BuildStore, checks: list[tuple[str, str, str, str, str | None]], report: Report) -> Path:
    m = store.manifest()
    st = store.stats()
    lines = [f"# Build {store.build_id}", "", f"project: {m.get('project')}  ", f"created: {m.get('created')}  ",
             f"note: {m.get('note') or '-'}", "",
             f"documents: {st['docs']}  sentences: {st['sentences']}  concepts: {st['concepts']}  "
             f"occurrences: {store.total_occurrences()}", "", "## Stages", ""]
    for stage, d in (m.get("stages") or {}).items():
        secs = d.get("seconds")
        lines.append(f"- **{stage}**: {d.get('status', 'done')} ({secs} s)" if isinstance(secs, (int, float))
                     else f"- **{stage}**: {d.get('status', 'done')}")
    summ = store.stage_summary()
    if summ.get("match"):
        s = summ["match"]
        lines.append(f"  - match items: done {s.get('done', 0)}, failed {s.get('failed', 0)}")
    lines += ["", "## Tier histogram (per scope)", "", "| scope | " + " | ".join(TIERS) + " |",
              "|---|" + "---|" * len(TIERS)]
    hist = store.tier_histogram()
    for scope in store.scope_ids():
        h = hist.get(scope, {})
        lines.append(f"| {scope} | " + " | ".join(str(h.get(t, 0)) for t in TIERS) + " |")
    lines += ["", "## Top concepts (scope all, by documents)", "", "| concept | label | papers | coverage | topic rate | tier |",
              "|---|---|---|---|---|---|"]
    for r in store.top_concepts("all", 25):
        lines.append(f"| {r['id']} | {r['label']} | {r['papers']} | {r['coverage']:.3f} | {r['topic_rate']:.3f} | {r['tier']} |")
    lines += ["", "## Sub-domains", "", "| id | name | documents | core | base |", "|---|---|---|---|---|"]
    for g in store.subdomain_tree():
        for s in g["subs"]:
            lines.append(f"| {s['id']} | {s['zh'] or ''} | {s['papers']} | {s['core']} | {s['base']} |")
    lines += ["", "## Checks", ""]
    for kind, tid, check, sev, detail in checks:
        lines.append(f"- [{sev}] {kind} {tid}: {check} — {detail}")
    lines += ["", "## Warnings", ""]
    warns = report.warnings
    for stage, d in (m.get("stages") or {}).items():
        if isinstance(d, dict) and d.get("empty_subdomains"):
            warns = warns + [f"{stage}: empty sub-domains {d['empty_subdomains']}"]
    lines += [f"- {w}" for w in warns] or ["(none)"]
    lines.append("")
    p = store.build_dir / REPORT
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


class FinishStage:
    name = "finish"
    version = "finish1"
    llm = False
    requires: tuple[str, ...] = ("match", "assign")
    gate: tuple[str, ...] = ()

    def run(self, project: Project, build: BuildStore, **opts) -> Report:
        report = Report(stage=self.name)
        t0 = time.perf_counter()
        checks = run_checks(build, project)
        build.add_checks(checks)
        errors = [c for c in checks if c[3] == "error"]
        report.items_total = len(checks)
        report.items_done = len(checks) - len(errors)
        report.items_failed = len(errors)
        for c in errors:
            report.warnings.append(f"check failed: {c[2]} ({c[1]}): {c[4]}")
        build.meta_set("n_occurrences", str(build.total_occurrences()))
        build.meta_set("n_docs", str(build.pin_counts()["docs"]))
        build.meta_set("n_sentences", str(build.pin_counts()["sentences"]))
        build.meta_set("n_concepts", str(len(build.concept_ids())))
        if errors:
            build.set_item(self.name, "*", "failed", self.version, {"errors": [c[2] for c in errors]})
            log.error("finish: %d checks failed; build %s stays open", len(errors), build.build_id)
            return report
        build.set_item(self.name, "*", "done", self.version, {"checks": len(checks)})
        m = build.manifest()
        stages = m.get("stages") or {}
        stages[self.name] = {"status": "done", "version": self.version, "seconds": round(time.perf_counter() - t0, 2),
                             "checks": len(checks)}
        build.update_manifest(stages=stages)
        report_path = write_report(build, checks, report)
        sha = build.freeze()
        build.update_manifest(status="finished", sha256=sha, finished=time.strftime("%Y-%m-%dT%H:%M:%S"),
                              report=report_path.name)
        report.details.update({"sha256": sha, "report": str(report_path), "seconds": round(time.perf_counter() - t0, 2)})
        log.info("build %s finished (sha %s)", build.build_id, sha[:12])
        return report
