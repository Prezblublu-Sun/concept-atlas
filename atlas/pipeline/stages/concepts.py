"""Stage 3 `concepts` (version `concepts1`): candidate terms → rule-based groups → `concepts_merge` per group batch
→ `work/drafts/concepts/<run>.yaml` (concept entries with the exact keys of `curated/concepts.yaml` plus a
`patterns` draft) or, when confirmed concepts exist, `<run>.delta.yaml` (new concepts + delta proposals
add | merge_into | new_form | retire). LLM stage; implemented by: increment 4 (work package S).

Candidates come from the merged terms draft — the `term_tree.yaml` entries without a `concept_id` (what
`atlas curate merge <terms draft>` wrote; their example sids are recovered from the draft named by `added_in`) —
and/or from a terms draft given with `--draft`. Without candidates and without confirmed concepts the stage stops.

Rules first: terms are joined into groups when they share a variant key (`core/matcher.norm_key`, `form_keys`
for hyphen readings, a singular of the last token) or share an evidence sentence and a token. A candidate that is
already a form of an existing concept is covered by rule and never sent. Groups are packed into items of ≤ 12 groups
/ ≤ 40 terms (the payload lists each term with zh, type, votes and ≤ 2 evidence sentences); in delta mode the item
also lists the existing concepts that share tokens with its terms (≤ 15).

Code after the model: a concept must name candidate terms of its item (`merged_terms`, one concept per term) and cite
sids of the item; ids are `C_<slug>` (the model's id is kept when well-formed and free); the label and the merged
terms are forms; all-caps forms become case forms; duplicate forms across new concepts stay with the first concept;
a new concept whose forms collide with an existing concept becomes a `merge_into` delta instead. Types map to the
v3 `origin` (概念 / 句式短语 / 通用句式 / 期刊名) so the tier rule stays the same.
"""
from __future__ import annotations

import hashlib
import logging
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path

from atlas.config import Project
from atlas.core.matcher import DEFAULT_PATTERNS, form_keys, norm_key
from atlas.core.sample import TERM_TYPES
from atlas.llm.tasks import TaskResult
from atlas.pipeline.llm_stage import Item, LlmStage, clean_text, drafts_dir, read_draft
from atlas.pipeline.runner import StageError
from atlas.store.curated import read_curated
from atlas.store.evidence import EvidenceStore

log = logging.getLogger(__name__)

SENT_LINE = "  [{sid}] {text}"
NO_EXISTING = "No existing concepts (base mode): every concept is new; \"deltas\" stays empty."
EXISTING_HEAD = "Existing confirmed concepts that may overlap (delta mode), one per line as id: label (zh) [type] forms | case_forms:"
ORIGIN_BY_TYPE = {"句式短语": "句式短语", "通用句式": "通用句式", "期刊名": "期刊名"}
ORIGIN_DEFAULT = "概念（llm）"
ID_RE = re.compile(r"^C_[a-z0-9]+$")
CASE_FORM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-]{0,23}$")
DEFAULTS = {"groups_per_item": 12, "terms_per_item": 40, "evidence_per_term": 2, "existing_per_item": 15}
ACTIONS = ("add", "merge_into", "new_form", "retire")
SCHEMA: dict = {
    "type": "object", "required": ["concepts"],
    "properties": {
        "concepts": {"type": "array", "maxItems": 40, "items": {
            "type": "object", "required": ["label", "type", "merged_terms"],
            "properties": {"id": {"type": "string", "maxLength": 48}, "label": {"type": "string", "maxLength": 100},
                           "zh": {"type": "string", "maxLength": 60}, "type": {"type": "string", "enum": list(TERM_TYPES)},
                           "forms": {"type": "array", "maxItems": 16, "items": {"type": "string", "maxLength": 80}},
                           "case_forms": {"type": "array", "maxItems": 8, "items": {"type": "string", "maxLength": 24}},
                           "merged_terms": {"type": "array", "minItems": 1, "maxItems": 20, "items": {"type": "string", "maxLength": 100}},
                           "sids": {"type": "array", "maxItems": 3, "items": {"type": "string", "maxLength": 40}},
                           "note": {"type": "string", "maxLength": 240}}}},
        "deltas": {"type": "array", "maxItems": 40, "items": {
            "type": "object", "required": ["action"],
            "properties": {"action": {"type": "string", "enum": list(ACTIONS)}, "term": {"type": "string", "maxLength": 100},
                           "concept_id": {"type": "string", "maxLength": 48}, "target": {"type": "string", "maxLength": 48},
                           "form": {"type": "string", "maxLength": 80}, "note": {"type": "string", "maxLength": 240}}}},
    },
}


def cfg(project: Project, key: str, default):
    v = project.get(f"curation.concepts.{key}")
    return default if v is None else v


# ---- keys and ids --------------------------------------------------------------------------------------------------

def singular_key(key: str) -> str:
    toks = key.split()
    if not toks:
        return key
    last = toks[-1]
    if len(last) > 4 and last.endswith("ies"):
        last = last[:-3] + "y"
    elif len(last) > 4 and last.endswith("es") and last[-3] in "sxzh":
        last = last[:-2]
    elif len(last) > 3 and last.endswith("s") and not last.endswith("ss"):
        last = last[:-1]
    return " ".join(toks[:-1] + [last])


def variant_keys(term: str) -> set[str]:
    """The keys under which a candidate term is the same lexical item: its norm key, the hyphen-joined key, and the
    singular of each."""
    keys = set(form_keys(term))
    k = norm_key(term)
    if k:
        keys.add(k)
    return {x for x in (set(keys) | {singular_key(x) for x in keys}) if x}


def concept_keys(c: Mapping) -> set[str]:
    out: set[str] = set()
    for f in list(c.get("forms") or []) + list(c.get("gen_forms") or []):
        out.update(form_keys(str(f)))
    for f in c.get("case_forms") or []:
        k = norm_key(str(f))
        if k:
            out.add(k)
    lab = norm_key(str(c.get("label") or ""))
    if lab:
        out.add(lab)
    return out


def concept_id_for(label: str, taken: set[str]) -> str:
    base = "C_" + re.sub(r"[^a-z0-9]", "", norm_key(label))[:40]
    if base == "C_":
        base = "C_concept"
    cand, k = base, 1
    while cand in taken:
        k += 1
        cand = f"{base}{k}"
    return cand


def is_case_form(f: str) -> bool:
    return bool(CASE_FORM_RE.match(f)) and any(ch.isupper() for ch in f) and f.upper() == f and len(f) <= 10


class _UF:
    def __init__(self, n: int):
        self.p = list(range(n))

    def find(self, i: int) -> int:
        while self.p[i] != i:
            self.p[i] = self.p[self.p[i]]
            i = self.p[i]
        return i

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[max(ra, rb)] = min(ra, rb)


def group_candidates(cands: Sequence[Mapping]) -> list[list[int]]:
    """Indices of the candidates grouped by shared variant key or (shared sid and shared token); groups and their
    members sorted by term."""
    uf = _UF(len(cands))
    by_key: dict[str, list[int]] = {}
    toks: list[set[str]] = []
    for i, c in enumerate(cands):
        for k in variant_keys(str(c["term"])):
            by_key.setdefault(k, []).append(i)
        toks.append(set(norm_key(str(c["term"])).split()))
    for ids in by_key.values():
        for j in ids[1:]:
            uf.union(ids[0], j)
    by_sid: dict[str, list[int]] = {}
    for i, c in enumerate(cands):
        for s in c.get("sids") or []:
            by_sid.setdefault(str(s), []).append(i)
    for ids in by_sid.values():
        for a in range(len(ids)):
            for b in range(a + 1, len(ids)):
                if toks[ids[a]] & toks[ids[b]]:
                    uf.union(ids[a], ids[b])
    groups: dict[int, list[int]] = {}
    for i in range(len(cands)):
        groups.setdefault(uf.find(i), []).append(i)
    out = [sorted(g, key=lambda i: str(cands[i]["term"])) for g in groups.values()]
    out.sort(key=lambda g: str(cands[g[0]]["term"]))
    return out


# ---- candidates --------------------------------------------------------------------------------------------------

def _draft_terms(draft: Mapping, origin: str) -> list[dict]:
    out = []
    for t in draft.get("terms") or []:
        term = clean_text(t.get("term"))
        if term:
            out.append({"term": term, "zh": clean_text(t.get("zh")), "type": str(t.get("type") or "名词术语"),
                        "gloss": clean_text(t.get("gloss")), "votes": int(t.get("votes") or 1),
                        "sids": list(t.get("example_sids") or []), "docs": list(t.get("docs") or []), "from": origin})
    return out


def load_candidates(project: Project, draft: str | Path | None = None) -> tuple[list[dict], list[str]]:
    """(candidates deduplicated by norm key, sources). From the given terms draft and from the term-tree entries
    without a concept id (sids recovered from the terms draft named by `added_in`)."""
    cands: list[dict] = []
    sources: list[str] = []
    if draft:
        d = read_draft(draft)
        if d.get("stage") != "terms":
            raise StageError(f"not a terms draft: {draft}")
        cands += _draft_terms(d, f"draft:{d.get('run_id')}")
        sources.append(str(draft))
    tree = read_curated(project.curated_dir, "term_tree") or {}
    cache: dict[str, dict] = {}
    n_tree = 0
    for dom in tree.get("domains") or []:
        for sec in dom.get("sections") or []:
            for sub in sec.get("subs") or []:
                for t in sub.get("terms") or []:
                    if t.get("concept_id"):
                        continue
                    term = clean_text(t.get("term"))
                    if not term:
                        continue
                    sids: list[str] = []
                    run = t.get("added_in")
                    if run:
                        if run not in cache:
                            p = drafts_dir(project, "terms") / f"{run}.yaml"
                            try:
                                cache[run] = {norm_key(x["term"]): x for x in (read_draft(p).get("terms") or [])} if p.is_file() else {}
                            except Exception:
                                cache[run] = {}
                        src = cache[run].get(norm_key(term))
                        if src:
                            sids = list(src.get("example_sids") or [])
                    cands.append({"term": term, "zh": clean_text(t.get("zh")), "type": str(t.get("type") or "名词术语"),
                                  "gloss": clean_text(t.get("note")), "votes": 1, "sids": sids,
                                  "example": clean_text(t.get("example")), "example_doc": t.get("example_doc") or "",
                                  "docs": [], "from": "term_tree"})
                    n_tree += 1
    if n_tree:
        sources.append(f"term_tree ({n_tree} entries without concept_id)")
    merged: dict[str, dict] = {}
    for c in cands:
        k = norm_key(c["term"]) or c["term"].lower()
        if k in merged:
            m = merged[k]
            m["votes"] += c["votes"]
            m["sids"] = list(dict.fromkeys(m["sids"] + c["sids"]))
            m["docs"] = sorted(set(m["docs"]) | set(c["docs"]))
            m["zh"] = m["zh"] or c["zh"]
            m["gloss"] = m["gloss"] or c["gloss"]
        else:
            merged[k] = dict(c, key=k)
    return sorted(merged.values(), key=lambda c: c["term"]), sources


# ---- the stage ---------------------------------------------------------------------------------------------------

class ConceptsStage(LlmStage):
    name = "concepts"
    version = "concepts1"
    task = "concepts_merge"
    prompt_file = "concepts_merge"
    requires: tuple[str, ...] = ()
    gate: tuple[str, ...] = ()
    max_tokens = 3000
    caps = {"concepts": 40, "deltas": 40, "concepts[].forms": 16, "concepts[].case_forms": 8, "concepts[].sids": 3,
            "concepts[].merged_terms": 20}

    def prepare(self, project: Project, build=None, *, pilot: int | None = None, seed: int | None = None,
                draft: str | Path | None = None, **_) -> dict:
        cands, sources = load_candidates(project, draft)
        existing_data = read_curated(project.curated_dir, "concepts") or {}
        existing = [c for c in (existing_data.get("concepts") or []) if not c.get("merged_into")]
        if not cands and not existing:
            raise StageError("`concepts` needs a merged terms draft (`atlas curate merge <terms draft>`) or `--draft "
                             "<terms draft>`, or confirmed concepts in curated/concepts.yaml")
        ex_keys: dict[str, str] = {}
        tok_index: dict[str, set[str]] = {}
        for c in existing:
            for k in concept_keys(c):
                ex_keys.setdefault(k, str(c["id"]))
                for tok in k.split():
                    tok_index.setdefault(tok, set()).add(str(c["id"]))
        covered: list[dict] = []
        open_cands: list[dict] = []
        for c in cands:
            hit = next((ex_keys[k] for k in sorted(variant_keys(c["term"])) if k in ex_keys), None)
            if hit:
                covered.append({"term": c["term"], "concept_id": hit})
            else:
                open_cands.append(c)
        if pilot is not None:
            open_cands = open_cands[:int(pilot)]
        groups = group_candidates(open_cands)
        sids = sorted({s for c in open_cands for s in (c.get("sids") or [])})
        texts: dict[str, str] = {}
        if sids and project.evidence_db.is_file():
            store = EvidenceStore.open(project.evidence_db, "ro")
            try:
                texts = store.texts_of(sids)
            finally:
                store.close()
        domains_data = read_curated(project.curated_dir, "domains") or {}
        domains = [str(d["id"]) for d in (domains_data.get("domains") or []) if d.get("scope", True)]
        patterns = read_curated(project.curated_dir, "patterns") or dict(DEFAULT_PATTERNS)
        summary = {"candidates": len(cands), "already_covered": len(covered), "open": len(open_cands), "groups": len(groups),
                   "existing_concepts": len(existing), "mode": "delta" if existing else "base", "sources": sources}
        return {"project": project, "cands": open_cands, "groups": groups, "texts": texts, "existing": existing,
                "existing_by_id": {str(c["id"]): c for c in existing}, "ex_keys": ex_keys, "tok_index": tok_index,
                "covered": covered, "domains": domains, "patterns": patterns, "delta": bool(existing),
                "sample_summary": summary, "pilot": pilot,
                "groups_per_item": int(cfg(project, "groups_per_item", DEFAULTS["groups_per_item"])),
                "terms_per_item": int(cfg(project, "terms_per_item", DEFAULTS["terms_per_item"])),
                "evidence_per_term": int(cfg(project, "evidence_per_term", DEFAULTS["evidence_per_term"])),
                "existing_per_item": int(cfg(project, "existing_per_item", DEFAULTS["existing_per_item"]))}

    def _render_group(self, ctx: dict, j: int, idx: Sequence[int]) -> tuple[str, list[str], list[str]]:
        lines = [f"## G{j}"]
        terms, sids_all = [], []
        for i in idx:
            c = ctx["cands"][i]
            terms.append(c["term"])
            lines.append(f"- term: {c['term']} | zh: {c['zh'] or '-'} | type: {c['type']} | votes: {c['votes']} | docs: {len(c.get('docs') or [])}")
            if c.get("gloss"):
                lines.append(f"  gloss: {c['gloss']}")
            ev = [(s, ctx["texts"].get(s)) for s in sorted(c.get("sids") or []) if ctx["texts"].get(s)]
            ev = ev[:ctx["evidence_per_term"]]
            if not ev and c.get("example"):
                ev = [("-", c["example"])]
            if ev:
                lines.append("  evidence:")
                for s, t in ev:
                    lines.append(SENT_LINE.format(sid=s, text=clean_text(t)))
                    if s != "-":
                        sids_all.append(s)
        return "\n".join(lines), terms, sids_all

    def _render_existing(self, ctx: dict, terms: Sequence[str]) -> tuple[str, list[str]]:
        if not ctx["delta"]:
            return NO_EXISTING, []
        score: Counter = Counter()
        for t in terms:
            for tok in norm_key(t).split():
                for cid in ctx["tok_index"].get(tok, ()):
                    score[cid] += 1
        ids = [cid for cid, _ in sorted(score.items(), key=lambda kv: (-kv[1], kv[0]))][:ctx["existing_per_item"]]
        if not ids:
            return EXISTING_HEAD + "\n(none share a token with these terms)", []
        lines = [EXISTING_HEAD]
        for cid in ids:
            c = ctx["existing_by_id"][cid]
            lines.append(f"- {cid}: {c.get('label')} ({c.get('zh') or '-'}) [{c.get('type') or '-'}] "
                         f"{'; '.join(str(f) for f in (c.get('forms') or [])[:8])} | {'; '.join(str(f) for f in (c.get('case_forms') or [])[:6])}")
        return "\n".join(lines), ids

    def items(self, ctx: dict) -> list[Item]:
        out: list[Item] = []
        batch: list[list[int]] = []
        n_terms = 0

        def flush() -> None:
            nonlocal batch, n_terms
            if not batch:
                return
            rendered, terms, sids, idxs = [], [], [], []
            for j, g in enumerate(batch, 1):
                text, ts, ss = self._render_group(ctx, j, g)
                rendered.append(text)
                terms += ts
                sids += ss
                idxs += list(g)
            existing_text, ex_ids = self._render_existing(ctx, terms)
            key = hashlib.sha256("\n".join(sorted(terms)).encode("utf-8")).hexdigest()[:16]
            out.append(Item(key=key, payload={"existing": existing_text, "groups": "\n\n".join(rendered), "n_groups": len(batch)},
                            meta={"n_groups": len(batch), "terms": terms, "sids": sorted(set(sids)), "cand_idx": idxs,
                                  "existing_ids": ex_ids}))
            batch, n_terms = [], 0

        for g in ctx["groups"]:
            if batch and (len(batch) >= ctx["groups_per_item"] or n_terms + len(g) > ctx["terms_per_item"]):
                flush()
            batch.append(g)
            n_terms += len(g)
        flush()
        return out

    def schema(self, ctx: dict) -> dict:
        return SCHEMA

    def draft_name(self, ctx: dict, run_id: str) -> str:
        return f"{run_id}.delta.yaml" if ctx["delta"] else f"{run_id}.yaml"

    def finish(self, ctx: dict, items: Sequence[Item], results: Mapping[str, TaskResult], run: dict) -> dict:
        cand_by_key = {c["key"]: c for c in ctx["cands"]}
        taken: set[str] = set(ctx["existing_by_id"])
        concepts: list[dict] = []
        deltas: list[dict] = []
        unassigned: list[str] = []
        stats: Counter = Counter()
        for it in items:
            r = results.get(it.key)
            if r is None or not r.ok:
                continue
            item_terms = {norm_key(t) or t.lower(): t for t in it.meta["terms"]}
            allowed = set(it.meta["sids"])
            claimed: set[str] = set()
            for c in (r.data or {}).get("concepts") or []:
                merged = []
                for t in c.get("merged_terms") or []:
                    k = norm_key(str(t)) or str(t).lower()
                    if k in item_terms and k not in claimed and item_terms[k] not in merged:
                        merged.append(item_terms[k])
                if not merged:
                    stats["dropped_unknown_terms"] += 1
                    continue
                label = clean_text(c.get("label")) or merged[0]
                sids = [s for s in (c.get("sids") or []) if s in allowed]
                if not sids:
                    for t in merged:
                        sids += [s for s in (cand_by_key.get(norm_key(t) or t.lower(), {}).get("sids") or []) if s in allowed]
                sids = list(dict.fromkeys(sids))[:3]
                if not sids and allowed:
                    stats["dropped_no_evidence"] += 1
                    continue
                forms, case_forms, seen = [], [], set()
                for f in [label] + merged + [clean_text(x) for x in (c.get("forms") or [])]:
                    f = clean_text(f)
                    if not f or len(f) > 80:
                        continue
                    if is_case_form(f):
                        if f not in case_forms:
                            case_forms.append(f)
                        continue
                    k = norm_key(f)
                    if k and k not in seen:
                        seen.add(k)
                        forms.append(f)
                for f in c.get("case_forms") or []:
                    f = clean_text(f)
                    if f and CASE_FORM_RE.match(f) and any(ch.isupper() for ch in f) and f not in case_forms:
                        case_forms.append(f)
                if not forms and not case_forms:
                    stats["dropped_no_forms"] += 1
                    continue
                keys = set()
                for f in forms:
                    keys.update(form_keys(f))
                for f in case_forms:
                    keys.add(norm_key(f))
                hit = next((ctx["ex_keys"][k] for k in sorted(keys) if k in ctx["ex_keys"]), None)
                if hit:
                    for t in merged:
                        deltas.append({"action": "merge_into", "term": t, "concept_id": hit, "form": t,
                                       "note": f"rule: the forms of the proposed concept '{label}' collide with {hit}",
                                       "source": "rule", "sids": sids})
                    claimed.update(norm_key(t) or t.lower() for t in merged)
                    stats["collisions_to_delta"] += 1
                    continue
                cid = str(c.get("id") or "")
                if not ID_RE.match(cid) or cid in taken:
                    cid = concept_id_for(label, taken)
                taken.add(cid)
                typ = str(c.get("type") or cand_by_key.get(norm_key(merged[0]) or merged[0].lower(), {}).get("type") or "名词术语")
                zh = clean_text(c.get("zh")) or cand_by_key.get(norm_key(merged[0]) or merged[0].lower(), {}).get("zh") or ""
                concepts.append({"id": cid, "label": label, "zh": zh, "type": typ, "mode": "forms", "forms": forms,
                                 "gen_forms": [], "case_forms": case_forms, "rx": None, "probe": "", "readable": "",
                                 "domains": list(ctx["domains"]), "place": [], "shared_with": [],
                                 "origin": ORIGIN_BY_TYPE.get(typ, ORIGIN_DEFAULT), "merged_into": None, "source": "llm",
                                 "added_in": run["run_id"], "note": clean_text(c.get("note")), "merged_terms": merged,
                                 "example_sids": sids})
                claimed.update(norm_key(t) or t.lower() for t in merged)
            for d in (r.data or {}).get("deltas") or []:
                action = str(d.get("action") or "")
                if action not in ACTIONS:
                    continue
                term = clean_text(d.get("term"))
                cid = clean_text(d.get("concept_id"))
                tk = norm_key(term) or term.lower()
                if action == "add":
                    continue            # the concept itself is listed under "concepts"
                if action in ("merge_into", "new_form"):
                    if tk not in item_terms or cid not in ctx["existing_by_id"]:
                        stats["deltas_rejected"] += 1
                        continue
                    if tk in claimed:
                        continue
                    deltas.append({"action": "merge_into", "term": item_terms[tk], "concept_id": cid,
                                   "form": clean_text(d.get("form")) or item_terms[tk], "note": clean_text(d.get("note")),
                                   "source": "llm", "sids": [s for s in (cand_by_key.get(tk, {}).get("sids") or []) if s in allowed][:3]})
                    claimed.add(tk)
                else:
                    target = clean_text(d.get("target"))
                    if cid in ctx["existing_by_id"] and target in ctx["existing_by_id"] and cid != target:
                        deltas.append({"action": "retire", "concept_id": cid, "target": target,
                                       "note": clean_text(d.get("note")), "source": "llm"})
                    else:
                        stats["deltas_rejected"] += 1
            unassigned += [t for k, t in item_terms.items() if k not in claimed]
        # duplicate forms across the new concepts: the first concept keeps the form
        owner: dict[str, str] = {}
        conflicts: list[dict] = []
        kept: list[dict] = []
        for c in concepts:
            fs = []
            for f in c["forms"]:
                ks = form_keys(f)
                clash = next((owner[k] for k in ks if k in owner), None)
                if clash:
                    conflicts.append({"form": f, "kept_in": clash, "dropped_from": c["id"]})
                    continue
                for k in ks:
                    owner[k] = c["id"]
                fs.append(f)
            c["forms"] = fs
            if fs or c["case_forms"]:
                kept.append(c)
            else:
                stats["dropped_all_forms_taken"] += 1
        kept.sort(key=lambda c: c["id"])
        deltas.sort(key=lambda d: (d["action"], d.get("term") or "", d.get("concept_id") or ""))
        return {"mode": "delta" if ctx["delta"] else "base", "inputs": dict(ctx["sample_summary"]),
                "stats": {"n_concepts": len(kept), "n_deltas": len(deltas), "unassigned_terms": len(unassigned),
                          "form_conflicts": len(conflicts), **{k: int(v) for k, v in stats.items()}},
                "concepts": kept, "deltas": deltas, "patterns": dict(ctx["patterns"]),
                "unassigned_terms": sorted(set(unassigned)), "already_covered": list(ctx["covered"]),
                "form_conflicts": conflicts}

    def report_lines(self, ctx: dict, draft: Mapping) -> list[str]:
        st = draft.get("stats") or {}
        inp = draft.get("inputs") or {}
        lines = ["## Inputs", "", f"candidates {inp.get('candidates')} (already covered by existing forms {inp.get('already_covered')}), "
                 f"groups {inp.get('groups')}, mode {draft.get('mode')}, existing concepts {inp.get('existing_concepts')}",
                 "", "## Result", "", f"concepts {st.get('n_concepts')}, deltas {st.get('n_deltas')}, unassigned terms "
                 f"{st.get('unassigned_terms')}, form conflicts {st.get('form_conflicts')}", "",
                 "| id | label | zh | type | forms | case forms | merged terms |", "|---|---|---|---|---|---|---|"]
        for c in (draft.get("concepts") or [])[:60]:
            lines.append(f"| {c['id']} | {c['label']} | {c['zh']} | {c['type']} | {'; '.join(c['forms'])} | "
                         f"{'; '.join(c['case_forms'])} | {'; '.join(c['merged_terms'])} |")
        if draft.get("deltas"):
            lines += ["", "## Deltas", ""] + [f"- {d['action']}: {d.get('term') or d.get('concept_id')} → {d.get('concept_id') or d.get('target')} ({d.get('source')})"
                                              for d in draft["deltas"][:60]]
        if draft.get("unassigned_terms"):
            lines += ["", "## Unassigned terms", "", ", ".join(draft["unassigned_terms"][:80])]
        return lines
