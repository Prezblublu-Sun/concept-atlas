"""Stage 5 `match`: pinned sentences × curated concepts → `occurrences`, `topic_hits`, then `counts` and
`importance`. No LLM. Implemented by: increment 2 (derived layer).

Per document (doc_id order; `--doc-ids a,b` reruns those documents after deleting their outputs):
 1. sentences of the pinned extraction (ids + membership from the build, text from the attached evidence);
 2. `Matcher.sentence(text)` for every sentence → occurrence rows (cstart/cend index the stored text);
 3. topic fields per unit (`topic_hits`, the source of the title / keywords / abstract counts and of the
    sub-domain assignment). Document unit: `title` = `document_meta.title` (manifest or PDF); `keywords` =
    `document_meta.keywords` when the manifest/PDF provided one, else the pinned sentences of section 'keywords'
    (the 'Keywords:' label stripped); `abstract` = `document_meta.abstract` when provided, else the sentences of
    section 'abstract'. The fallback order avoids counting the same abstract twice (which would inflate the
    sub-domain 'core' rule of >= 2 abstract mentions). Chapter unit (theses): `title` = the chapter heading,
    `abstract` = the first 300 words of the chapter's body sentences. `Matcher.text` on each text; counts summed
    per (unit, concept, field).
 4. stage_items(match, doc_id) with the fingerprint `m<matcher version>:<sha of concepts+patterns>`; a document
    whose item is `done` with the same fingerprint is skipped (unless named in --doc-ids).
After the documents: `counts` is rebuilt from occurrences + topic hits ('body' = is_body sentences plus
`stats.body_extra_sections` (default: appendix) plus the title-field hits when `stats.body_includes_title` — the
previous system counted its full text, title line and appendices included, as "body") and `importance` is computed
per concept × scope with `atlas.core.stats` (parameters from `defaults.yaml` → `stats:`). Entries that are not
concepts (origin 句式短语 / 期刊名 / 通用句式 / 歧义缩写, `stats.is_tiered`) get their frequency columns but
`tier` NULL, as the previous system's frequency_importance.py kept them on a frequency-only sheet.

`--workers N` > 1 runs step 2–3 in a process pool (the matcher is built once per worker); writes stay in the
parent, in doc_id order, in batches.
"""
from __future__ import annotations

import hashlib
import logging
import re
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field

from atlas.config import Project
from atlas.core.matcher import MATCHER_VERSION, Matcher
from atlas.core.stats import DEFAULT_PARAMS, concept_metrics, params_with_defaults
from atlas.doctypes.paper import KEYWORDS_LINE
from atlas.pipeline.runner import Report
from atlas.store.build import BuildStore

log = logging.getLogger(__name__)

BATCH_DOCS = 32
CHAPTER_WORDS = 300
_WORD_RE = re.compile(r"\S+")
_MATCHER: Matcher | None = None


@dataclass
class _Job:
    doc_id: str
    sentences: list[tuple[str, str, str, str, int]]     # (sid, text, section, unit_id, is_body)
    units: list[dict]
    meta: dict
    occurrences: list[tuple] = field(default_factory=list)
    topic: list[tuple] = field(default_factory=list)
    n_sentences: int = 0
    error: str | None = None


def _init_worker(concepts: list[dict], patterns: dict) -> None:
    global _MATCHER
    _MATCHER = Matcher.from_curated(concepts, patterns)


def topic_texts(unit: dict, sentences: list[tuple[str, str, str, str, int]], meta: dict) -> dict[str, list[str]]:
    """{'title': [...], 'keywords': [...], 'abstract': [...]} for one unit (see the module docstring)."""
    def clean(s: str | None) -> str:
        return " ".join((s or "").split())
    if unit.get("kind") == "document":
        title = [clean(meta.get("title"))]
        kws = [clean(meta.get("keywords"))]
        abstract = [clean(meta.get("abstract"))]
        for sid, text, section, unit_id, is_body in sentences:
            if section == "keywords" and not kws[0]:
                t = clean(KEYWORDS_LINE.sub("", text, count=1))
                if t and not KEYWORDS_LINE.fullmatch(text.strip()):
                    kws.append(t)
            elif section == "abstract" and not abstract[0]:
                t = clean(text)
                if t and t.lower() not in ("abstract", "summary"):
                    abstract.append(t)
        return {"title": [t for t in title if t], "keywords": [t for t in kws if t], "abstract": [t for t in abstract if t]}
    # chapter unit: heading + first 300 words of the chapter
    words: list[str] = []
    texts: list[str] = []
    for sid, text, section, unit_id, is_body in sentences:
        if unit_id != unit["unit_id"] or not is_body:
            continue
        w = _WORD_RE.findall(text)
        if not w:
            continue
        take = w if len(words) + len(w) <= CHAPTER_WORDS else w[:max(0, CHAPTER_WORDS - len(words))]
        if not take:
            break
        words.extend(take)
        texts.append(" ".join(take))
        if len(words) >= CHAPTER_WORDS:
            break
    return {"title": [clean(unit.get("heading"))] if clean(unit.get("heading")) else [], "keywords": [],
            "abstract": texts}


def match_document(job: _Job, matcher: Matcher) -> _Job:
    """Steps 2–3 (pure given the matcher)."""
    try:
        occ: list[tuple] = []
        for sid, text, section, unit_id, is_body in job.sentences:
            for o in matcher.sentence(text):
                occ.append((sid, o.concept_id, o.form, o.cstart, o.cend, o.nested_in))
        topic: list[tuple] = []
        for u in job.units:
            fields = topic_texts(u, job.sentences, job.meta)
            for fname, texts in fields.items():
                agg: dict[str, list[int]] = {}
                for t in texts:
                    for cid, (n, nn) in matcher.counts(t).items():
                        a = agg.setdefault(cid, [0, 0])
                        a[0] += n
                        a[1] += nn
                for cid, (n, nn) in agg.items():
                    topic.append((u["unit_id"], cid, fname, n, nn))
        job.occurrences = occ
        job.topic = topic
        job.n_sentences = len(job.sentences)
    except Exception as e:  # noqa: BLE001 - recorded per document
        log.exception("match failed for %s", job.doc_id)
        job.error = f"{type(e).__name__}: {e}"
    job.sentences = []       # do not ship the text back
    return job


def _match_in_worker(job: _Job) -> _Job:
    assert _MATCHER is not None
    return match_document(job, _MATCHER)


def fingerprint(manifest: dict, concepts: list[dict], patterns: dict) -> str:
    cur = (manifest or {}).get("curated") or {}
    h = hashlib.sha256()
    h.update((cur.get("concepts") or "").encode())
    h.update((cur.get("patterns") or "").encode())
    if not cur.get("concepts"):
        h.update(repr(sorted((c["id"], tuple(c.get("forms") or [])) for c in concepts)).encode())
        h.update(repr(sorted(patterns.items())).encode())
    return f"{MATCHER_VERSION}:{h.hexdigest()[:16]}"


def compute_importance(store: BuildStore, params: dict | None = None) -> list[dict]:
    """One row per concept × scope ('all' + every domain with scope = true)."""
    p = params_with_defaults(params)
    meta = store.doc_meta_all()
    all_ids = sorted(meta)
    doc_years = {d: m.get("year") for d, m in meta.items()}
    scopes: dict[str, list[str]] = {"all": all_ids}
    for d in store.domains():
        if d["scope"]:
            scopes[d["id"]] = [x for x in all_ids if d["id"] in (meta[x].get("domains") or [])]
    lits = store.literal_ids()
    tiered = {cid: cs["tiered"] for cid, cs in store.concept_scope().items()}
    rows: list[dict] = []
    seen: set[str] = set()

    def emit(cid: str, per_doc: dict) -> None:
        for scope, ids in scopes.items():
            m = concept_metrics(per_doc, ids, doc_years, cid in lits, p, complement_ids=None if scope == "all" else all_ids)
            m.pop("sec_papers", None)
            if not tiered.get(cid, True):
                m["tier"] = None            # frequency only: the previous system never tiered phrases / journal names
            rows.append({"concept_id": cid, "scope": scope, **m})

    for cid, per_doc in store.iter_counts():
        seen.add(cid)
        emit(cid, per_doc)
    for cid in store.concept_ids():
        if cid not in seen:
            emit(cid, {})
    return rows


class MatchStage:
    name = "match"
    version = "match4"      # match1 summed meta abstract/keywords with the same sentences; match2 falls back instead;
                            # match3 writes the denormalised occurrence columns (build schema 2); match4 tiers only
                            # the 概念 items (tier NULL for phrases / journal names, as the previous system)
    llm = False
    requires: tuple[str, ...] = ()
    gate: tuple[str, ...] = ("concepts", "patterns")

    def run(self, project: Project, build: BuildStore, *, doc_ids: list[str] | None = None, workers: int = 1,
            force: bool = False) -> Report:
        report = Report(stage=self.name)
        t0 = time.perf_counter()
        concepts, patterns = build.matcher_data()
        fp = fingerprint(build.manifest(), concepts, patterns)
        matcher = Matcher.from_curated(concepts, patterns)
        docs = build.doc_ids()
        wanted = {d.strip() for d in (doc_ids or []) if d and d.strip()}
        if wanted:
            docs = [d for d in docs if d in wanted]
        done = {it["item_key"]: it for it in build.stage_items(self.name) if it["status"] == "done"}
        todo: list[str] = []
        for d in docs:
            it = done.get(d)
            if it and it.get("fingerprint") == fp and not wanted and not force:
                report.items_skipped += 1
            else:
                todo.append(d)
        report.items_total = len(docs)
        if todo:
            build.delete_doc_outputs(todo)
        units_by_doc: dict[str, list[dict]] = {}
        for u in build.units_all():
            units_by_doc.setdefault(u["doc_id"], []).append(u)
        def job_for(doc_id: str) -> _Job:
            sents = [(r["sid"], r["text"] or "", r["section"], r["unit_id"], int(r["is_body"]))
                     for r in build.sentences_of_doc(doc_id)]
            return _Job(doc_id=doc_id, sentences=sents, units=units_by_doc.get(doc_id) or
                        [{"unit_id": doc_id, "doc_id": doc_id, "kind": "document", "heading": None}],
                        meta=build.doc_meta(doc_id) or {})

        n_occ = n_topic = 0
        batch: list[_Job] = []

        def flush() -> None:
            nonlocal n_occ, n_topic
            if not batch:
                return
            rows_o = [r for j in batch if not j.error for r in j.occurrences]
            rows_t = [r for j in batch if not j.error for r in j.topic]
            build.add_occurrences(rows_o)
            build.add_topic_hits(rows_t)
            n_occ += len(rows_o)
            n_topic += len(rows_t)
            items = []
            for j in batch:
                if j.error:
                    report.items_failed += 1
                    report.warnings.append(f"{j.doc_id}: {j.error}")
                    items.append((j.doc_id, "failed", fp, {"error": j.error}))
                else:
                    report.items_done += 1
                    items.append((j.doc_id, "done", fp, {"sentences": j.n_sentences, "occurrences": len(j.occurrences),
                                                        "topic_hits": len(j.topic)}))
            build.set_items(self.name, items)
            batch.clear()

        if workers and int(workers) > 1 and len(todo) > 1:
            window = int(workers) * 4
            with ProcessPoolExecutor(max_workers=int(workers), initializer=_init_worker,
                                     initargs=(concepts, patterns)) as pool:
                pending = []
                it = iter(todo)
                exhausted = False
                while pending or not exhausted:
                    while len(pending) < window and not exhausted:
                        try:
                            d = next(it)
                        except StopIteration:
                            exhausted = True
                            break
                        pending.append(pool.submit(_match_in_worker, job_for(d)))
                    if not pending:
                        break
                    fut = pending.pop(0)
                    batch.append(fut.result())
                    if len(batch) >= BATCH_DOCS:
                        flush()
        else:
            for d in todo:
                batch.append(match_document(job_for(d), matcher))
                if len(batch) >= BATCH_DOCS:
                    flush()
        flush()
        t_docs = time.perf_counter() - t0
        n_counts = build.rebuild_counts(body_extra_sections=tuple(project.get("stats.body_extra_sections", ["appendix"]) or ()),
                                        body_includes_title=bool(project.get("stats.body_includes_title", True)))
        t_counts = time.perf_counter() - t0 - t_docs
        rows = compute_importance(build, project.get("stats", DEFAULT_PARAMS))
        n_imp = build.write_importance(rows)
        t_imp = time.perf_counter() - t0 - t_docs - t_counts
        total_occ = build.total_occurrences()
        build.meta_set("n_occurrences", str(total_occ))
        detail = {"fingerprint": fp, "documents": len(docs), "occurrences_added": n_occ, "topic_hits_added": n_topic,
                  "occurrences_total": total_occ, "counts_rows": n_counts, "importance_rows": n_imp,
                  "seconds": {"documents": round(t_docs, 2), "counts": round(t_counts, 2), "importance": round(t_imp, 2)}}
        build.set_item(self.name, "*", "failed" if report.items_failed else "done", fp, detail)
        m = build.manifest()
        stages = m.get("stages") or {}
        stages[self.name] = {"status": "done" if not report.items_failed else "failed", "version": self.version,
                             "seconds": round(time.perf_counter() - t0, 2), **detail}
        build.update_manifest(stages=stages)
        report.details.update(detail)
        log.info(report.summary())
        return report
