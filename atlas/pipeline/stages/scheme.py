"""Stage 4 `scheme` (version `scheme1`): one item — a deterministic sample of documents (title, year, venue,
abstract) plus the top-N confirmed concepts by document frequency — → `scheme_draft` → `work/drafts/scheme/<run>.yaml`
with `domains`, `categories`, `subdomains` in the exact shapes of `domains.yaml`, `categories.yaml`,
`subdomains.yaml`. LLM stage, gated on confirmed `concepts`; implemented by: increment 4 (work package S).

Document frequency comes from the newest build whose `match` stage is done (`importance.papers`, scope all); without
a build the documents of the newest terms draft are counted through the concepts' forms; without either the first N
concepts by id are listed. Documents are chosen by `core/sample.pilot_docs` (proximity tier, then id); the abstract
is `document_meta.abstract` or the sentences of section `abstract`, cut to 600 characters.

Code after the model: domain ids `DOM_<slug>`, category ids `<letters><number>`, sub-domain ids `ABBR-ABBR` (the
model's ids are kept when well-formed, else generated); a category of an unknown domain is dropped; anchors and
group members must resolve to concept ids (unresolved ones are removed and listed in `stats.unresolved_anchors`;
a sub-domain left without anchors, or a cross sub-domain with fewer than two groups, is dropped); `order` is the
position in the answer. The draft keeps `evidence_docs` per sub-domain; `atlas curate merge` strips draft-only keys.
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from collections.abc import Mapping, Sequence

from atlas.config import Project
from atlas.core.ids import slug
from atlas.core.matcher import form_keys, norm_key
from atlas.core.sample import pilot_docs
from atlas.pipeline.llm_shim import TaskResult
from atlas.pipeline.llm_stage import Item, LlmStage, clean_text, newest_draft, read_draft
from atlas.pipeline.runner import StageError
from atlas.store.build import BuildStore, list_build_dirs, read_manifest
from atlas.store.curated import read_curated
from atlas.store.evidence import EvidenceStore

log = logging.getLogger(__name__)

DEFAULTS = {"n_docs": 60, "n_concepts": 150, "abstract_chars": 600}
DOC_LINE = "[{doc_id}] {year} | {venue} | {title} — {abstract}"
CONCEPT_LINE = "{id}: {label} ({zh}) [{type}] docs={n}"
DOM_RE = re.compile(r"^DOM_[a-z0-9_]+$")
CAT_RE = re.compile(r"^[A-Z]{1,3}\d{1,2}$")
SUB_RE = re.compile(r"^[A-Z0-9]{1,4}-[A-Z0-9]{1,6}$")
_NAME = {"type": "string", "maxLength": 80}
_DESC = {"type": "string", "maxLength": 300}
SCHEMA: dict = {
    "type": "object", "required": ["domains", "categories", "subdomains"],
    "properties": {
        "domains": {"type": "array", "minItems": 1, "maxItems": 8, "items": {
            "type": "object", "required": ["id", "zh", "en"],
            "properties": {"id": {"type": "string", "maxLength": 40}, "zh": _NAME, "en": _NAME, "scope": {"type": "boolean"}}}},
        "categories": {"type": "array", "maxItems": 80, "items": {
            "type": "object", "required": ["id", "domain", "name_zh", "name_en"],
            "properties": {"id": {"type": "string", "maxLength": 12}, "domain": {"type": "string", "maxLength": 40},
                           "name_zh": _NAME, "name_en": _NAME, "desc_zh": _DESC, "desc_en": _DESC}}},
        "subdomains": {"type": "array", "maxItems": 40, "items": {
            "type": "object", "required": ["id", "kind", "name_zh", "name_en"],
            "properties": {"id": {"type": "string", "maxLength": 16}, "kind": {"type": "string", "enum": ["within", "cross"]},
                           "domain": {"type": ["string", "null"], "maxLength": 40}, "name_zh": _NAME, "name_en": _NAME,
                           "desc_zh": _DESC, "desc_en": _DESC,
                           "anchors": {"type": "array", "maxItems": 12, "items": {"type": "string", "maxLength": 48}},
                           "groups": {"type": "array", "maxItems": 4, "items": {"type": "array", "maxItems": 12, "items": {"type": "string", "maxLength": 48}}},
                           "evidence_docs": {"type": "array", "maxItems": 5, "items": {"type": "string", "maxLength": 40}}}}},
    },
}


def cfg(project: Project, key: str, default):
    v = project.get(f"curation.scheme.{key}")
    return default if v is None else v


def concept_doc_freq(project: Project, concepts: Sequence[Mapping]) -> tuple[dict[str, int], str]:
    """{concept_id: documents} and the source: a build with `match` done, else the newest terms draft, else none."""
    for d in reversed(list_build_dirs(project.builds_dir)):
        m = read_manifest(d) or {}
        if "match" not in (m.get("stages") or {}):
            continue
        try:
            st = BuildStore.open(d, "immutable" if m.get("status") == "finished" else "ro")
        except Exception as e:  # noqa: BLE001 - an unreadable build is skipped
            log.warning("cannot open build %s: %s", d.name, e)
            continue
        try:
            rows = st.top_concepts("all", 10 ** 6)
        finally:
            st.close()
        return {r["id"]: int(r["papers"] or 0) for r in rows}, f"build:{d.name}"
    p = newest_draft(project, "terms")
    if p is not None:
        try:
            terms = read_draft(p).get("terms") or []
        except Exception:
            terms = []
        by_key = {}
        for t in terms:
            by_key[norm_key(str(t.get("term") or ""))] = set(t.get("docs") or [])
        freq: dict[str, int] = {}
        for c in concepts:
            docs: set[str] = set()
            for f in [c.get("label")] + list(c.get("forms") or []):
                for k in form_keys(str(f or "")):
                    docs |= by_key.get(k, set())
            freq[str(c["id"])] = len(docs)
        return freq, f"terms draft:{p.name}"
    return {}, "none"


def category_id(domain_id: str, n: int, taken: set[str]) -> str:
    letters = re.sub(r"[^A-Z]", "", domain_id[4:].upper())[:2] or "X"
    cand, k = f"{letters}{n}", n
    while cand in taken:
        k += 1
        cand = f"{letters}{k}"
    return cand


def subdomain_id(name_en: str, kind: str, domain_id: str | None, taken: set[str]) -> str:
    left = "X" if kind == "cross" else (re.sub(r"[^A-Z]", "", (domain_id or "")[4:].upper())[:3] or "D")
    right = "".join(w[0] for w in re.findall(r"[A-Za-z]+", name_en or "")).upper()[:4] or "SD"
    cand, k = f"{left}-{right}", 1
    while cand in taken:
        k += 1
        cand = f"{left}-{right}{k}"
    return cand


class SchemeStage(LlmStage):
    name = "scheme"
    version = "scheme1"
    task = "scheme_draft"
    prompt_file = "scheme_draft"
    requires: tuple[str, ...] = ()
    gate: tuple[str, ...] = ("concepts",)
    max_tokens = 8000
    out_low = 3000
    caps = {"domains": 8, "categories": 80, "subdomains": 40, "subdomains.anchors": 12, "subdomains.groups": 4,
            "subdomains.evidence_docs": 5}

    def prepare(self, project: Project, build=None, *, pilot: int | None = None, seed: int | None = None, **_) -> dict:
        data = read_curated(project.curated_dir, "concepts") or {}
        concepts = [c for c in (data.get("concepts") or []) if not c.get("merged_into")]
        if not concepts:
            raise StageError("`scheme` needs confirmed concepts in curated/concepts.yaml (run `concepts`, merge, confirm)")
        n_docs = int(pilot if pilot is not None else cfg(project, "n_docs", DEFAULTS["n_docs"]))
        n_concepts = int(cfg(project, "n_concepts", DEFAULTS["n_concepts"]))
        max_chars = int(cfg(project, "abstract_chars", DEFAULTS["abstract_chars"]))
        freq, freq_source = concept_doc_freq(project, concepts)
        ranked = sorted(concepts, key=lambda c: (-freq.get(str(c["id"]), 0), str(c["id"])))[:n_concepts]
        docs: list[dict] = []
        if project.evidence_db.is_file():
            store = EvidenceStore.open(project.evidence_db, "ro")
            try:
                meta = store.doc_meta_all()
                ids = pilot_docs(meta, n_docs)
                need = [d for d in ids if not clean_text((meta[d] or {}).get("abstract"))]
                abstracts: dict[str, list[str]] = {}
                if need:
                    rows = list(store.iter_membership(doc_ids=need, sections=["abstract"]))
                    texts = store.texts_of([r["sid"] for r in rows])
                    for r in rows:
                        t = clean_text(texts.get(r["sid"]))
                        if t and t.lower() not in ("abstract", "summary"):
                            abstracts.setdefault(r["doc_id"], []).append(t)
                for d in ids:
                    m = meta[d] or {}
                    ab = clean_text(m.get("abstract")) or " ".join(abstracts.get(d, []))
                    docs.append({"doc_id": d, "year": m.get("year") or "-", "venue": clean_text(m.get("venue")) or "-",
                                 "title": clean_text(m.get("title")) or "-", "abstract": ab[:max_chars]})
            finally:
                store.close()
        summary = {"documents": len(docs), "concepts": len(ranked), "concepts_total": len(concepts), "freq_source": freq_source}
        return {"project": project, "docs": docs, "concepts": ranked, "freq": freq, "sample_summary": summary, "pilot": pilot}

    def items(self, ctx: dict) -> list[Item]:
        doc_lines = [DOC_LINE.format(**d) for d in sorted(ctx["docs"], key=lambda d: d["doc_id"])]
        con_lines = [CONCEPT_LINE.format(id=c["id"], label=c.get("label") or c["id"], zh=c.get("zh") or "-",
                                         type=c.get("type") or "-", n=ctx["freq"].get(str(c["id"]), 0))
                     for c in sorted(ctx["concepts"], key=lambda c: str(c["id"]))]
        payload = {"concepts": "\n".join(con_lines), "documents": "\n".join(doc_lines), "n_concepts": len(con_lines),
                   "n_docs": len(doc_lines)}
        return [Item(key="scheme", payload=payload, meta={"doc_ids": sorted(d["doc_id"] for d in ctx["docs"]),
                                                          "concept_ids": sorted(str(c["id"]) for c in ctx["concepts"])})]

    def schema(self, ctx: dict) -> dict:
        return SCHEMA

    def finish(self, ctx: dict, items: Sequence[Item], results: Mapping[str, TaskResult], run: dict) -> dict:
        res = results.get("scheme")
        data = (res["data"] if res and res.ok else None) or {}
        known = set(items[0].meta["concept_ids"]) if items else set()
        known_docs = set(items[0].meta["doc_ids"]) if items else set()
        stats: Counter = Counter()
        unresolved: list[str] = []
        src = {"source": "llm", "added_in": run["run_id"]}
        domains: list[dict] = []
        dom_ids: set[str] = set()
        for i, d in enumerate(data.get("domains") or []):
            did = clean_text(d.get("id"))
            if not DOM_RE.match(did):
                did = "DOM_" + (slug(d.get("en") or d.get("zh") or f"d{i}", 20).replace("-", "_") or f"d{i}")
            if did in dom_ids:
                stats["duplicate_domains"] += 1
                continue
            dom_ids.add(did)
            domains.append({"id": did, "zh": clean_text(d.get("zh")), "en": clean_text(d.get("en")), "order": len(domains),
                            "scope": bool(d.get("scope", True)), **src})
        categories: list[dict] = []
        cat_ids: set[str] = set()
        per_dom: Counter = Counter()
        for c in data.get("categories") or []:
            dom = clean_text(c.get("domain"))
            if dom not in dom_ids:
                alt = next((x["id"] for x in domains if x["zh"] == dom or x["en"] == dom), None)
                if alt is None:
                    stats["categories_unknown_domain"] += 1
                    continue
                dom = alt
            per_dom[dom] += 1
            cid = clean_text(c.get("id"))
            if not CAT_RE.match(cid) or cid in cat_ids:
                cid = category_id(dom, per_dom[dom], cat_ids)
            cat_ids.add(cid)
            categories.append({"id": cid, "domain": dom, "name_zh": clean_text(c.get("name_zh")), "name_en": clean_text(c.get("name_en")),
                               "desc_zh": clean_text(c.get("desc_zh")), "desc_en": clean_text(c.get("desc_en")),
                               "order": per_dom[dom] - 1, **src})
        subdomains: list[dict] = []
        sub_ids: set[str] = set()
        for s in data.get("subdomains") or []:
            kind = "cross" if str(s.get("kind")) == "cross" else "within"
            dom = clean_text(s.get("domain")) or None
            if kind == "within" and dom not in dom_ids:
                alt = next((x["id"] for x in domains if x["zh"] == dom or x["en"] == dom), None)
                if alt is None:
                    stats["subdomains_unknown_domain"] += 1
                    continue
                dom = alt
            row = {"id": "", "kind": kind, "domain": dom if kind == "within" else None,
                   "name_zh": clean_text(s.get("name_zh")), "name_en": clean_text(s.get("name_en")),
                   "desc_zh": clean_text(s.get("desc_zh")), "desc_en": clean_text(s.get("desc_en")), "order": len(subdomains),
                   "notes": "", **src}
            if kind == "within":
                anchors = []
                for a in s.get("anchors") or []:
                    a = clean_text(a)
                    if a in known:
                        if a not in anchors:
                            anchors.append(a)
                    elif a:
                        unresolved.append(a)
                if not anchors:
                    stats["subdomains_without_anchors"] += 1
                    continue
                row["anchors"] = anchors
            else:
                groups = []
                for g in s.get("groups") or []:
                    ids = []
                    for a in g or []:
                        a = clean_text(a)
                        if a in known:
                            if a not in ids:
                                ids.append(a)
                        elif a:
                            unresolved.append(a)
                    if ids:
                        groups.append(ids)
                if len(groups) < 2:
                    stats["cross_without_two_groups"] += 1
                    continue
                row["groups"] = groups
            sid = clean_text(s.get("id"))
            if not SUB_RE.match(sid) or sid in sub_ids:
                sid = subdomain_id(row["name_en"], kind, dom, sub_ids)
            sub_ids.add(sid)
            row["id"] = sid
            row["evidence_docs"] = [d for d in (s.get("evidence_docs") or []) if d in known_docs][:5]
            subdomains.append(row)
        if not domains and res is not None and res.ok:
            stats["empty_answer"] += 1
        return {"inputs": dict(ctx["sample_summary"]),
                "stats": {"n_domains": len(domains), "n_categories": len(categories), "n_subdomains": len(subdomains),
                          "unresolved_anchors": sorted(set(unresolved)), **{k: int(v) for k, v in stats.items()}},
                "domains": domains, "categories": categories, "subdomains": subdomains}

    def report_lines(self, ctx: dict, draft: Mapping) -> list[str]:
        st = draft.get("stats") or {}
        lines = ["## Scheme", "", f"domains {st.get('n_domains')}, categories {st.get('n_categories')}, sub-domains "
                 f"{st.get('n_subdomains')}; unresolved anchors {len(st.get('unresolved_anchors') or [])}", ""]
        for d in draft.get("domains") or []:
            lines.append(f"- {d['id']}: {d['zh']} / {d['en']} (scope {d['scope']})")
            for c in draft.get("categories") or []:
                if c["domain"] == d["id"]:
                    lines.append(f"  - {c['id']} {c['name_zh']} / {c['name_en']}: {c['desc_en'][:80]}")
        lines += ["", "## Sub-domains", ""]
        for s in draft.get("subdomains") or []:
            target = ", ".join(s.get("anchors") or []) if s["kind"] == "within" else " × ".join("[" + ", ".join(g) + "]" for g in s.get("groups") or [])
            lines.append(f"- {s['id']} ({s['kind']}, {s.get('domain') or '-'}): {s['name_zh']} / {s['name_en']} — {target}")
        return lines
