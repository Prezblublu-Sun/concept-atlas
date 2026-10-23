"""Stage 7 `classify` (version `classify1`): every tiered concept of an open build with `assign` done → entry +
≤ 10 deterministic example sentences (`BuildStore.examples`) + the confirmed scheme as a cached system block →
`classify` → `{domain, primary, secondary, reason, sids}` → `work/drafts/classify/<run>.yaml` whose `rows` carry the
keys of `concept_categories.yaml`. LLM stage; implemented by: increment 4 (work package S).

Gate: confirmed `concepts`, `domains`, `categories`, `subdomains` (the scheme); requires `assign` in the build.
Items are concepts (`--pilot N` = the first N by id, `--limit`, `--concept-ids`); the build's `stage_items`
(stage `classify`, key = concept id, fingerprint = stage version + prompt sha + payload sha) make reruns skip
concepts whose entry, examples, scheme and prompt are unchanged unless `--force`; skipped concepts keep their stored
row in the new draft, so a draft is always complete. A concept without example sentences is skipped (nothing to
cite). Code after the model: the primary / secondary category must belong to the chosen domain and the cited sids
to the item's examples, else the item fails (`invalid`). The accept gate: a full run needs a passing blind review
for this task and prompt sha (`atlas qa report`). Merging the draft (`atlas curate merge`) writes
`concept_categories.yaml` with `curated/overrides/categories.yaml` applied on top.
"""
from __future__ import annotations

import hashlib
import logging
from collections import Counter
from collections.abc import Mapping, Sequence

from atlas.config import Project
from atlas.llm.tasks import TaskResult
from atlas.pipeline.llm_stage import Item, LlmStage, clean_text, draft_status, fingerprint
from atlas.pipeline.runner import StageError
from atlas.store.build import BuildStore
from atlas.store.curated import curated_path, file_sha, read_curated

log = logging.getLogger(__name__)

SENT_LINE = "[{sid}] {text}"
DEFAULTS = {"examples": 10}
MAX_SIDS = 3


def cfg(project: Project, key: str, default):
    v = project.get(f"curation.classify.{key}")
    return default if v is None else v


def scheme_block(domains: Sequence[Mapping], categories: Sequence[Mapping]) -> str:
    """The scheme as one stable text block (sorted; it is the cached part of every request of a run)."""
    lines = ["Classification scheme / 分类体系:"]
    for d in sorted(domains, key=lambda d: (int(d.get("order", 0) or 0), str(d["id"]))):
        lines.append(f"Domain {d['id']}: {d.get('zh') or '-'} / {d.get('en') or '-'}")
        for c in sorted((c for c in categories if c.get("domain") == d["id"]), key=lambda c: (int(c.get("order", 0) or 0), str(c["id"]))):
            desc = clean_text(c.get("desc_zh")) or ""
            desc_en = clean_text(c.get("desc_en")) or ""
            lines.append(f"  - {c['id']}: {c.get('name_zh') or '-'} / {c.get('name_en') or '-'}"
                         + (f" — {desc}" if desc else "") + (f" / {desc_en}" if desc_en else ""))
    return "\n".join(lines) + "\n"


def scheme_sha(project: Project) -> str:
    h = hashlib.sha256()
    for name in ("domains", "categories"):
        h.update((file_sha(curated_path(project.curated_dir, name)) or "-").encode("utf-8"))
    return h.hexdigest()


class ClassifyStage(LlmStage):
    name = "classify"
    version = "classify1"
    task = "classify"
    prompt_file = "classify"
    requires: tuple[str, ...] = ("assign",)
    gate: tuple[str, ...] = ("concepts", "domains", "categories", "subdomains")
    needs_build = True
    accept_gate = True
    max_tokens = 400
    caps = {"sids": MAX_SIDS}

    def prepare(self, project: Project, build: BuildStore | None = None, *, pilot: int | None = None,
                seed: int | None = None, force: bool = False, concept_ids: Sequence[str] | None = None, **_) -> dict:
        if build is None:
            raise StageError("`classify` runs on an open build; run `atlas build new` first")
        domains = (read_curated(project.curated_dir, "domains") or {}).get("domains") or []
        categories = (read_curated(project.curated_dir, "categories") or {}).get("categories") or []
        if not domains or not categories:
            raise StageError("`classify` needs domains.yaml and categories.yaml with entries (run `scheme`, merge, confirm)")
        dom_ids = [str(d["id"]) for d in domains]
        cats_by_dom: dict[str, list[str]] = {}
        for c in categories:
            cats_by_dom.setdefault(str(c.get("domain")), []).append(str(c["id"]))
        block = scheme_block(domains, categories)
        n_examples = int(cfg(project, "examples", DEFAULTS["examples"]))
        prompt_sha = self.prompt_sha
        scope = build.concept_scope()
        ids = sorted(cid for cid, cs in scope.items() if cs["tiered"])
        if concept_ids:
            wanted = {str(x).strip() for x in concept_ids}
            ids = [c for c in ids if c in wanted]
        if pilot is not None:
            ids = ids[:int(pilot)]
        done = {it["item_key"]: it for it in build.stage_items(self.name) if it["status"] == "done"}
        rows: list[dict] = []
        reused: list[dict] = []
        skipped_no_examples: list[str] = []
        for cid in ids:
            c = build.concept(cid)
            if c is None:
                continue
            ex = [e for e in build.examples(cid, n_examples) if e.get("text")]
            if not ex:
                skipped_no_examples.append(cid)
                continue
            ex.sort(key=lambda e: str(e["sid"]))
            payload = {"concept_id": cid, "examples": "\n".join(SENT_LINE.format(sid=e["sid"], text=clean_text(e["text"])) for e in ex),
                       "forms": "; ".join(list(c.get("forms") or []) + list(c.get("case_forms") or [])) or "-",
                       "label": clean_text(c.get("label")) or cid, "n_examples": len(ex), "type": c.get("type") or "-",
                       "zh": clean_text(c.get("zh")) or "-"}
            item = Item(key=cid, payload=payload, meta={"concept_id": cid, "sids": [str(e["sid"]) for e in ex]},
                        context=(block,))
            fp = fingerprint(self.version, prompt_sha, item.content_sha)
            prev = done.get(cid)
            if prev and prev.get("fingerprint") == fp and not force and not concept_ids and isinstance((prev.get("detail") or {}).get("row"), dict):
                reused.append(dict(prev["detail"]["row"], status="skipped"))
                continue
            rows.append(item)
        summary = {"concepts": len(ids), "items": len(rows), "skipped": len(reused), "no_examples": len(skipped_no_examples),
                   "domains": len(dom_ids), "categories": len(categories), "examples_per_concept": n_examples}
        warnings = [f"{len(skipped_no_examples)} concepts without example sentences skipped"] if skipped_no_examples else []
        return {"project": project, "build": build, "items_list": rows, "reused": reused, "dom_ids": dom_ids,
                "cat_ids": sorted({str(c["id"]) for c in categories}), "cats_by_dom": cats_by_dom, "block": block,
                "sample_summary": summary, "n_skipped": len(reused), "warnings": warnings, "pilot": pilot,
                "no_examples": skipped_no_examples, "scheme_sha": scheme_sha(project)}

    def items(self, ctx: dict) -> list[Item]:
        return list(ctx["items_list"])

    def schema(self, ctx: dict) -> dict:
        return {"type": "object", "required": ["domain", "primary", "reason", "sids"],
                "properties": {"domain": {"type": "string", "enum": list(ctx["dom_ids"])},
                               "primary": {"type": "string", "enum": list(ctx["cat_ids"])},
                               "secondary": {"type": ["string", "null"], "enum": list(ctx["cat_ids"]) + [None]},
                               "reason": {"type": "string", "maxLength": 400},
                               "sids": {"type": "array", "minItems": 1, "maxItems": MAX_SIDS, "items": {"type": "string", "maxLength": 40}}}}

    def finish(self, ctx: dict, items: Sequence[Item], results: Mapping[str, TaskResult], run: dict) -> dict:
        build: BuildStore = ctx["build"]
        rows: list[dict] = list(ctx["reused"])
        stage_rows = []
        stats: Counter = Counter()
        for it in items:
            r = results.get(it.key)
            if r is None:
                continue
            fp = run["fingerprints"][it.key]
            if not r.ok:
                stage_rows.append((it.key, "failed", fp, {"errors": [r.error] if r.error else [], "status": draft_status(r)}))
                continue
            d = r.data or {}
            dom, prim = str(d.get("domain")), str(d.get("primary"))
            sec = d.get("secondary")
            errors = []
            if prim not in ctx["cats_by_dom"].get(dom, []):
                errors.append(f"primary {prim} is not a category of {dom}")
            if sec is not None and str(sec) not in ctx["cats_by_dom"].get(dom, []):
                errors.append(f"secondary {sec} is not a category of {dom}")
            sids = [s for s in (d.get("sids") or []) if s in set(it.meta["sids"])]
            if not sids:
                errors.append("no cited sid is an example sentence of the input")
            if errors:                       # the stage's own check failed: the item is `invalid` in the draft
                r.status, r.error = "failed:invalid", "; ".join(errors)
                stats["invalid"] += 1
                stage_rows.append((it.key, "failed", fp, {"errors": errors, "status": "invalid"}))
                continue
            row = {"concept_id": it.key, "domain": dom, "primary": prim, "secondary": str(sec) if sec else None,
                   "reason": clean_text(d.get("reason")), "evidence_sids": sids, "source": "llm", "added_in": run["run_id"]}
            rows.append(dict(row, status="ok"))
            stage_rows.append((it.key, "done", fp, {"row": row}))
        if stage_rows:
            build.set_items(self.name, stage_rows)
        n_ok = sum(1 for r in rows if r.get("status") == "ok")
        detail = {"concepts": ctx["sample_summary"]["concepts"], "classified": n_ok, "reused": len(ctx["reused"]),
                  "failed": sum(1 for s in stage_rows if s[1] == "failed"), "no_examples": len(ctx["no_examples"]),
                  "run_id": run["run_id"], "prompt_sha": run["prompt_sha"], "scheme_sha": ctx["scheme_sha"]}
        build.set_item(self.name, "*", "done", fingerprint(self.version, run["prompt_sha"], ctx["scheme_sha"]), detail)
        m = build.manifest()
        stages = m.get("stages") or {}
        stages[self.name] = {"status": "done", "version": self.version, **detail}
        build.update_manifest(stages=stages)
        rows.sort(key=lambda r: r["concept_id"])
        by_dom = Counter(r["domain"] for r in rows)
        return {"build_id": build.build_id, "scheme_sha": ctx["scheme_sha"], "inputs": dict(ctx["sample_summary"]),
                "stats": {"n_rows": len(rows), "classified": n_ok, "reused": len(ctx["reused"]), "invalid": int(stats["invalid"]),
                          "no_examples": list(ctx["no_examples"]), "by_domain": dict(by_dom.most_common())},
                "rows": rows}

    def report_lines(self, ctx: dict, draft: Mapping) -> list[str]:
        st = draft.get("stats") or {}
        lines = ["## Classification", "", f"build {draft.get('build_id')}, scheme sha {str(draft.get('scheme_sha'))[:12]}; rows "
                 f"{st.get('n_rows')} (new {st.get('classified')}, reused {st.get('reused')}, invalid {st.get('invalid')}, "
                 f"without examples {len(st.get('no_examples') or [])})",
                 "- by domain: " + ", ".join(f"{k} {v}" for k, v in (st.get("by_domain") or {}).items()), "",
                 "| concept | domain | primary | secondary | reason |", "|---|---|---|---|---|"]
        for r in (draft.get("rows") or [])[:80]:
            lines.append(f"| {r['concept_id']} | {r['domain']} | {r['primary']} | {r.get('secondary') or '-'} | {r.get('reason', '')[:80]} |")
        return lines
