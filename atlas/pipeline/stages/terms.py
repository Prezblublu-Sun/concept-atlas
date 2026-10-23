"""Stage 2 `terms` (version `terms1`): a stratified sample of body sentences → `terms_extract` per chunk →
`work/drafts/terms/<run>.yaml` (term → votes, types, sections, example sids, documents) + report. LLM stage;
implemented by: increment 4 (work package S).

Items: chunks of ≤ 40 sentences of one section type (`core/sample.chunk_rows`), from `core/sample.stratified_sample`
over the body sentences of the current extractions (strata `unit_kind|section` × `venue` × `year_bin`, per-document
cap, seeded); `--pilot N` restricts the pool to N documents chosen by `extra.proximity_tier` then doc id. The payload
of a chunk lists its sentences as `[sid] text` in sid order (pure: no doc ids, no timestamps). The answer is
`{terms: [{term, zh, type ∈ TERM_TYPES, gloss, sids}]}`, capped at 25 terms × 3 sids by code; a term whose sids are
not all from its chunk keeps the valid ones and is dropped when none is left (`dropped_no_evidence`).

Aggregation: terms are keyed by `core/matcher.norm_key` (so "Femoral stems" and "femoral stem" are one line);
votes = number of chunks that proposed the term; `types` / `sections` are counters; `example_sids` the first five
distinct citations; `docs` the documents of those citations. The accept gate: a full run (no `--pilot`) needs a
passing blind review (`atlas qa report`) for this task and prompt sha.

Project keys (`curation.terms.*`, none in defaults.yaml): sample_n 16000, per_doc_cap 160, chunk_size 40, seed 1,
strata, max_gloss_words 20, examples_per_term 5.
"""
from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Mapping, Sequence

from atlas.config import Project
from atlas.core.matcher import norm_key
from atlas.core.sample import CHUNK_SIZE, DEFAULT_STRATA, TERM_TYPES, chunk_rows, composition, pilot_docs, stratified_sample
from atlas.llm.tasks import TaskResult
from atlas.pipeline.llm_stage import Item, LlmStage, clean_text
from atlas.pipeline.runner import StageError
from atlas.store.evidence import EvidenceStore

log = logging.getLogger(__name__)

SENT_LINE = "[{sid}] {text}"
DEFAULTS = {"sample_n": 16000, "per_doc_cap": 160, "chunk_size": CHUNK_SIZE, "seed": 1, "max_gloss_words": 20,
            "examples_per_term": 5}
MAX_TERMS = 25
MAX_SIDS = 3
SCHEMA: dict = {
    "type": "object", "required": ["terms"],
    "properties": {"terms": {"type": "array", "maxItems": MAX_TERMS, "items": {
        "type": "object", "required": ["term", "type", "sids"],
        "properties": {"term": {"type": "string", "maxLength": 100}, "zh": {"type": "string", "maxLength": 60},
                       "type": {"type": "string", "enum": list(TERM_TYPES)}, "gloss": {"type": "string", "maxLength": 240},
                       "sids": {"type": "array", "maxItems": MAX_SIDS, "items": {"type": "string", "maxLength": 40}}}}}},
}


def cfg(project: Project, key: str, default):
    v = project.get(f"curation.terms.{key}")
    return default if v is None else v


def trim_words(s: str | None, n: int) -> str:
    words = clean_text(s).split()
    return " ".join(words[:n])


class TermsStage(LlmStage):
    name = "terms"
    version = "terms1"
    task = "terms_extract"
    prompt_file = "terms_extract"
    requires: tuple[str, ...] = ()
    gate: tuple[str, ...] = ()
    accept_gate = True
    max_tokens = 2000
    caps = {"terms": MAX_TERMS, "terms[].sids": MAX_SIDS}

    def prepare(self, project: Project, build=None, *, pilot: int | None = None, seed: int | None = None,
                doc_ids: Sequence[str] | None = None, sample_n: int | None = None, per_doc_cap: int | None = None,
                **_) -> dict:
        if not project.evidence_db.is_file():
            raise StageError(f"no evidence store at {project.evidence_db}; run `atlas run ingest` first")
        n = int(sample_n or cfg(project, "sample_n", DEFAULTS["sample_n"]))
        cap = int(per_doc_cap or cfg(project, "per_doc_cap", DEFAULTS["per_doc_cap"]))
        size = int(cfg(project, "chunk_size", DEFAULTS["chunk_size"]))
        seed = int(seed if seed is not None else cfg(project, "seed", DEFAULTS["seed"]))
        strata = tuple(cfg(project, "strata", DEFAULT_STRATA))
        bins = project.get("stats.year_bins")
        store = EvidenceStore.open(project.evidence_db, "ro")
        try:
            meta = store.doc_meta_all()
            kinds = store.unit_kinds()
            docs = sorted(meta)
            if doc_ids:
                wanted = {str(d).strip() for d in doc_ids if str(d).strip()}
                docs = [d for d in docs if d in wanted]
            if pilot is not None:
                docs = pilot_docs({d: meta[d] for d in docs}, int(pilot))
            restrict = docs if (pilot is not None or doc_ids) else None
            rows: list[dict] = []
            for r in store.iter_membership(doc_ids=restrict, body_only=True):
                m = meta.get(r["doc_id"]) or {}
                r["unit_kind"] = kinds.get(r["unit_id"])
                r["venue"] = m.get("venue")
                r["year"] = m.get("year")
                rows.append(r)
            sample = stratified_sample(rows, strata, n, cap, seed, year_bins=bins)
            pool_comp = composition(rows, ("unit_kind|section",), bins)
            n_pool = len(rows)
            del rows
            texts = store.texts_of([r["sid"] for r in sample])
        finally:
            store.close()
        chunks = chunk_rows(sample, size)
        comp = composition(sample, strata, bins)
        comp["section"] = dict(Counter(str(r["section"]) for r in sample).most_common())
        summary = {"documents": len(docs), "pool_sentences": n_pool, "sentences": len(sample), "chunks": len(chunks),
                   "sample_n": n, "per_doc_cap": cap, "chunk_size": size, "seed": seed, "strata": list(strata),
                   "composition": comp, "pool_composition": pool_comp}
        return {"project": project, "docs": docs, "sample": sample, "texts": texts, "chunks": chunks,
                "sample_summary": summary, "pilot": pilot, "seed": seed,
                "max_gloss_words": int(cfg(project, "max_gloss_words", DEFAULTS["max_gloss_words"])),
                "examples_per_term": int(cfg(project, "examples_per_term", DEFAULTS["examples_per_term"]))}

    def items(self, ctx: dict) -> list[Item]:
        texts = ctx["texts"]
        out: list[Item] = []
        for ch in ctx["chunks"]:
            sids = sorted(s for s in ch.sids if texts.get(s))
            lines = [SENT_LINE.format(sid=s, text=clean_text(texts[s])) for s in sids]
            if not lines:
                continue
            out.append(Item(key=ch.key, payload={"n_sentences": len(lines), "section": ch.section, "sentences": "\n".join(lines)},
                            meta={"section": ch.section, "sids": sids, "n_sentences": len(lines), "doc_ids": ch.doc_ids}))
        return out

    def schema(self, ctx: dict) -> dict:
        return SCHEMA

    def finish(self, ctx: dict, items: Sequence[Item], results: Mapping[str, TaskResult], run: dict) -> dict:
        sid_doc = {r["sid"]: r["doc_id"] for r in ctx["sample"]}
        max_words = ctx["max_gloss_words"]
        n_examples = ctx["examples_per_term"]
        agg: dict[str, dict] = {}
        stats: Counter = Counter()
        for it in items:
            r = results.get(it.key)
            if r is None or not r.ok:
                continue
            allowed = set(it.meta["sids"])
            for t in (r.data or {}).get("terms") or []:
                term = clean_text(t.get("term"))
                if not term:
                    stats["dropped_empty"] += 1
                    continue
                sids = list(dict.fromkeys(s for s in (t.get("sids") or []) if s in allowed))
                if len(sids) < len(t.get("sids") or []):
                    stats["foreign_sids"] += len(t.get("sids") or []) - len(sids)
                if not sids:
                    stats["dropped_no_evidence"] += 1
                    continue
                key = norm_key(term) or term.lower()
                a = agg.setdefault(key, {"surface": Counter(), "zh": Counter(), "types": Counter(), "gloss": "",
                                         "votes": 0, "sections": Counter(), "example_sids": [], "docs": set()})
                a["surface"][term] += 1
                a["votes"] += 1
                a["types"][str(t.get("type"))] += 1
                zh = clean_text(t.get("zh"))
                if zh:
                    a["zh"][zh] += 1
                gloss = trim_words(t.get("gloss"), max_words)
                if gloss and not a["gloss"]:
                    a["gloss"] = gloss
                if len(clean_text(t.get("gloss")).split()) > max_words:
                    stats["gloss_trimmed"] += 1
                a["sections"][it.meta["section"]] += 1
                for s in sids:
                    if s not in a["example_sids"] and len(a["example_sids"]) < n_examples:
                        a["example_sids"].append(s)
                    if s in sid_doc:
                        a["docs"].add(sid_doc[s])
                stats["proposed"] += 1
        terms = []
        for key, a in agg.items():
            terms.append({"term": a["surface"].most_common(1)[0][0], "zh": a["zh"].most_common(1)[0][0] if a["zh"] else "",
                          "type": a["types"].most_common(1)[0][0], "types": dict(a["types"].most_common()),
                          "gloss": a["gloss"], "votes": a["votes"], "sections": dict(a["sections"].most_common()),
                          "example_sids": list(a["example_sids"]), "docs": sorted(a["docs"]), "n_docs": len(a["docs"]),
                          "key": key})
        terms.sort(key=lambda t: (-t["votes"], -t["n_docs"], t["term"]))
        by_type = Counter(t["type"] for t in terms)
        return {"sample": dict(ctx["sample_summary"]),
                "stats": {"n_terms": len(terms), "proposed": stats["proposed"], "dropped_no_evidence": stats["dropped_no_evidence"],
                          "dropped_empty": stats["dropped_empty"], "foreign_sids": stats["foreign_sids"],
                          "gloss_trimmed": stats["gloss_trimmed"], "by_type": dict(by_type.most_common())},
                "terms": terms}

    def report_lines(self, ctx: dict, draft: Mapping) -> list[str]:
        s = draft.get("sample") or {}
        st = draft.get("stats") or {}
        lines = ["## Sample", "", f"documents {s.get('documents')}, pool {s.get('pool_sentences')} body sentences, "
                 f"sampled {s.get('sentences')}, chunks {s.get('chunks')} (seed {s.get('seed')}, cap {s.get('per_doc_cap')}/doc)"]
        for dim, comp in (s.get("composition") or {}).items():
            lines.append(f"- by {dim}: " + ", ".join(f"{v} {n}" for v, n in list(comp.items())[:10]))
        lines += ["", "## Terms", "", f"{st.get('n_terms')} distinct terms from {st.get('proposed')} proposals; "
                  f"dropped without evidence {st.get('dropped_no_evidence')}; foreign sids {st.get('foreign_sids')}",
                  "- by type: " + ", ".join(f"{k} {v}" for k, v in (st.get("by_type") or {}).items()), "",
                  "| term | zh | type | votes | docs | sections |", "|---|---|---|---|---|---|"]
        for t in (draft.get("terms") or [])[:40]:
            lines.append(f"| {t['term']} | {t['zh']} | {t['type']} | {t['votes']} | {t['n_docs']} | "
                         + ", ".join(f"{k} {v}" for k, v in t["sections"].items()) + " |")
        return lines
