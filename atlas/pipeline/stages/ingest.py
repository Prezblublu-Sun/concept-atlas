"""Stage 1 `ingest`: PDFs + manifest → evidence rows. Pure computation per document; no LLM.

Implemented by: increment 1, agent "ingest".

Per document (in doc_id order, `--limit N` for a sample, `--doc-ids a,b`):
 1. pdf_sha256 = sha256_file(path); doc_type from manifest column else project default; doctype = get_doctype(…).
 2. extractor_version = f"{SEGMENT_VERSION}+{doctype.name}:{doctype.version}+{extractor_tag()}+ovr:{sha of the
    document's entry in curated/units_override.yaml if any else '-'}"; ext_id = extraction_id(...).
    If the extraction exists and not `reextract` → skip (items_skipped).
 3. pages = extract_pages(); if text_ratio < thresholds.min_text_ratio → warning 'low text ratio' (still ingest).
 4. masks = doctype.drop_non_body(pages); units = doctype.segment(); marks = doctype.label_sections().
 5. Sentences, per page: remove masked spans (keep a map from the cleaned string back to original offsets),
    split_sentences() on the cleaned page text, map spans back to (cstart, cend) in the original page text, assign
    section by the latest SectionMark at or before (page_no, cstart), unit by page range (chapter whose
    page_from ≤ page ≤ page_to, else the document unit), is_body = section in BODY_SECTIONS, tags =
    tag_sentence(), flags = garbage_flags(). Sentences never cross pages. seq increases in reading order.
 6. sid = sentence_id(doc_id, text, k) where k counts earlier sentences in this extraction with the same norm().
 7. Aliases: if the document had a previous current extraction, for each old sentence find new sentences on the
    same page whose char span overlaps ≥ 0.5 of the old span (split and merge allowed → several rows);
    add_aliases().
 8. store.add_extraction(...) in one transaction; drop_ratio = masked chars / all chars; warn when above
    thresholds.max_drop_ratio.
Manifest: CSV with columns doc_id,title,year,authors,doi,venue,doc_type,file (file relative to pdf_dir); documents
not in the manifest but present in pdf_dir get doc_id 'D' + 4-digit sequence (persisted in the documents table,
so re-running assigns the same ids) and meta from pdf_metadata(). `atlas docs add` registers; `atlas run ingest`
extracts. The stage logs one line per document to work/logs/ingest.log and returns a Report.

Refinement of step 5 (PDF text has no blank lines between blocks, so a heading would otherwise merge into the
first sentence of its section): every SectionMark that is not a 'front' mark (the implicit (1, 0, 'front') and
the marks that end a keywords block are not headings) is a hard sentence boundary at the mark's offset and at
the end of that line — including a heading at offset 0 of a page (any page without a running header); the
heading line therefore becomes its own (short, flagged) sentence. A masked line leaves its newline behind, so
the lines around a mask stay separate.

`--workers N` > 1 runs steps 3–6 in a process pool (PyMuPDF is not thread-safe, so threads are never used);
database writes stay in the parent, in doc_id order.

Increment 2 (theses): `curated/units_override.yaml` = {doc_id: {chapters: [{heading, page_from, page_to,
unit_kind}]}} replaces the detected chapter units of that document (confidence 1.0, reason `override`; its sha is
part of extractor_version, so a changed override re-extracts). When a thesis has no chapter unit or any chapter's
confidence is below the doctype's `min_chapter_confidence`, the stage warns and writes
`work/drafts/chapters/<doc_id>.yaml` (the detected units with pages and reasons) for the user to correct.
"""
from __future__ import annotations

import csv
import logging
import re
from bisect import bisect_right
from collections.abc import Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from atlas.config import Project
from atlas.core.ids import canonical_json, extraction_id, norm, sentence_id, text_sha256, unit_id as make_unit_id
from atlas.core.schemas import BODY_SECTIONS, DocMeta, LineMask, PageText, SectionMark, SentenceRow, Unit
from atlas.core.segment import SEGMENT_VERSION, garbage_flags, split_sentences, tag_sentence
from atlas.core.text import hyphen_vocab, sentence_text
from atlas.doctypes import get_doctype
from atlas.ingest.pdf import extract_pages, extractor_tag, pdf_metadata, sha256_file, text_ratio
from atlas.pipeline.runner import Report
from atlas.store.curated import read_units_override
from atlas.store.db import transaction
from atlas.store.evidence import EvidenceStore

BATCH_DOCS = 16        # documents per write transaction during `run ingest` (project key ingest.batch_docs)
CHECKPOINT_BATCHES = 32   # PASSIVE WAL checkpoint every 32 batches (~500 documents): the femstem corpus wrote a
                          # 6 GB WAL in one go before the final checkpoint; this keeps it near 1.5 GB

log = logging.getLogger(__name__)

MANIFEST_COLUMNS = ("doc_id", "title", "year", "authors", "doi", "venue", "doc_type", "file")
SCAN_ID_PREFIX = "D"
SCAN_ID_WIDTH = 4
ALIAS_MIN_OVERLAP = 0.5
UNITS_OVERRIDE_FILE = "units_override.yaml"
UNIT_KINDS = frozenset({"intro", "lit_review", "methods", "results", "discussion", "conclusion", "other"})
CHAPTER_DRAFTS = ("work", "drafts", "chapters")
INGEST_LOG = "ingest.log"
_FILE_LOGGER = "atlas.ingest.file"


# ---- pure helpers (page → sentences) --------------------------------------------------------------------------

def clean_page_text(text: str, masks: Sequence[LineMask]) -> tuple[str, list[int], list[int]]:
    """Remove the masked spans of one page. Returns (cleaned, to_orig, to_clean): to_orig[i] is the original
    offset of cleaned[i]; to_clean[j] (len(text)+1 entries) is the cleaned offset at which original offset j
    lands (a masked char maps to the position of the next kept char)."""
    n = len(text)
    kept: list[str] = []
    to_orig: list[int] = []
    to_clean: list[int] = [0] * (n + 1)
    pos = 0
    for m in sorted(masks, key=lambda m: (m.cstart, m.cend)):
        a, b = max(pos, min(m.cstart, n)), max(pos, min(m.cend, n))
        if a > pos:
            seg = text[pos:a]
            base = len(to_orig)
            kept.append(seg)
            to_orig.extend(range(pos, a))
            for j in range(pos, a):
                to_clean[j] = base + (j - pos)
            pos = a
        if b > pos:
            c = len(to_orig)
            for j in range(pos, b):
                to_clean[j] = c
            pos = b
    if pos < n:
        seg = text[pos:n]
        base = len(to_orig)
        kept.append(seg)
        to_orig.extend(range(pos, n))
        for j in range(pos, n):
            to_clean[j] = base + (j - pos)
    to_clean[n] = len(to_orig)
    return "".join(kept), to_orig, to_clean


def _line_end(text: str, pos: int) -> int:
    nl = text.find("\n", pos)
    return len(text) if nl < 0 else nl


def page_sentence_spans(page: PageText, masks: Sequence[LineMask], marks: Sequence[SectionMark],
                        min_chars: int = 3, vocab: frozenset[str] | None = None) -> list[tuple[int, int, str]]:
    """(cstart, cend, text) of the sentences of one page in reading order; offsets index into page.text, text is
    the cleaned (mask-free) slice normalised by atlas.core.text.sentence_text (dehyphenated with the document's
    `vocab` of hyphenated words, ligatures expanded, whitespace collapsed). Section marks on the page are hard
    boundaries (see the module docstring)."""
    text = page.text
    page_masks = [m for m in masks if m.page_no == page.page_no]
    cleaned, to_orig, to_clean = clean_page_text(text, page_masks)
    if not cleaned.strip():
        return []
    bounds: set[int] = {0, len(cleaned)}
    heading_segs: set[tuple[int, int]] = set()
    for m in marks:
        if m.page_no != page.page_no or m.section == "front":
            continue        # 'front' marks are never headings; a heading at offset 0 of a page still is
        if 0 <= m.cstart < len(text):
            s, e = to_clean[m.cstart], to_clean[_line_end(text, m.cstart)]
            bounds.update((s, e))
            heading_segs.add((s, e))
    cuts = sorted(bounds)
    out: list[tuple[int, int, str]] = []
    for s, e in zip(cuts, cuts[1:]):
        if e <= s:
            continue
        if (s, e) in heading_segs:
            # the heading (or 'Keywords:') line itself: one sentence, never split at its numbering ('1.')
            a, b = s, e
            while a < b and cleaned[a].isspace():
                a += 1
            while b > a and cleaned[b - 1].isspace():
                b -= 1
            spans = [(a - s, b - s)] if b - a >= min_chars else []
        else:
            spans = split_sentences(cleaned[s:e], min_chars)
        for a, b in spans:
            a, b = s + a, s + b
            out.append((to_orig[a], to_orig[b - 1] + 1, sentence_text(cleaned[a:b], vocab)))
    return out


class _MarkIndex:
    def __init__(self, marks: Sequence[SectionMark]):
        ms = sorted(marks, key=lambda m: (m.page_no, m.cstart))
        self.keys = [(m.page_no, m.cstart) for m in ms]
        self.sections = [m.section for m in ms]

    def section_at(self, page_no: int, cstart: int) -> str:
        i = bisect_right(self.keys, (page_no, cstart)) - 1
        return self.sections[i] if i >= 0 else "front"


def _unit_for(units: Sequence[Unit], page_no: int, doc_id: str) -> str:
    for u in units:
        if u.kind == "chapter" and u.page_from <= page_no <= u.page_to:
            return u.unit_id
    for u in units:
        if u.kind == "document":
            return u.unit_id
    return doc_id


def build_sentences(doc_id: str, ext_id: str, pages: Sequence[PageText], masks: Sequence[LineMask],
                    units: Sequence[Unit], marks: Sequence[SectionMark], min_chars: int = 3) -> list[SentenceRow]:
    """Steps 5–6 of the module docstring for a whole document (pure)."""
    index = _MarkIndex(marks)
    vocab = hyphen_vocab("\n".join(p.text for p in pages))
    rows: list[SentenceRow] = []
    seen: dict[str, int] = {}
    seq = 0
    for page in sorted(pages, key=lambda p: p.page_no):
        unit = _unit_for(units, page.page_no, doc_id)
        for cstart, cend, text in page_sentence_spans(page, masks, marks, min_chars, vocab):
            key = norm(text)
            k = seen.get(key, 0)
            seen[key] = k + 1
            section = index.section_at(page.page_no, cstart)
            rows.append(SentenceRow(
                sid=sentence_id(doc_id, text, k), doc_id=doc_id, text=text, ext_id=ext_id, seq=seq, unit_id=unit,
                page_no=page.page_no, cstart=cstart, cend=cend, section=section,
                is_body=section in BODY_SECTIONS, tags=tuple(tag_sentence(text)),
                flags=tuple(garbage_flags(text))))
            seq += 1
    return rows


def compute_aliases(old: Sequence[SentenceRow], new: Sequence[SentenceRow], from_ext: str, to_ext: str,
                    min_overlap: float = ALIAS_MIN_OVERLAP) -> list[tuple[str, str, str, str, float]]:
    """Step 7 (pure): (old_sid, new_sid, from_ext, to_ext, overlap) for every new sentence on the same page whose
    span covers ≥ min_overlap of the old span."""
    by_page: dict[int, list[SentenceRow]] = {}
    for s in new:
        by_page.setdefault(s.page_no, []).append(s)
    out: list[tuple[str, str, str, str, float]] = []
    for o in old:
        length = max(1, o.cend - o.cstart)
        for s in by_page.get(o.page_no, []):
            inter = min(o.cend, s.cend) - max(o.cstart, s.cstart)
            if inter <= 0:
                continue
            ratio = inter / length
            if ratio >= min_overlap:
                out.append((o.sid, s.sid, from_ext, to_ext, round(min(1.0, ratio), 4)))
    return out


def drop_ratio_of(pages: Sequence[PageText], masks: Sequence[LineMask]) -> float:
    total = sum(len(p.text) for p in pages)
    if total == 0:
        return 0.0
    dropped = sum(max(0, m.cend - m.cstart) for m in masks)
    return min(1.0, dropped / total)


def load_units_overrides(project: Project, report: Report | None = None) -> dict[str, dict]:
    """{doc_id: entry} from `curated/units_override.yaml` (read once per run); an unreadable file is reported as a
    warning and ignored, entries that are not mappings are dropped."""
    try:
        data = read_units_override(project.curated_dir)
    except Exception as e:
        msg = f"cannot read {project.curated_dir / UNITS_OVERRIDE_FILE}: {type(e).__name__}: {e}"
        log.warning(msg)
        if report is not None:
            report.warnings.append(msg)
        return {}
    return {str(k): v for k, v in data.items() if isinstance(v, dict)}


def _override_sha(entry: dict | None) -> str:
    return "-" if entry is None else text_sha256(canonical_json(entry))[:12]


def apply_units_override(doc_id: str, units: list[Unit], override: dict | None, n_pages: int) -> list[Unit]:
    """Replace the chapter units by the override's chapters (document unit kept). Pure. Chapters are taken in the
    listed order; pages are clipped to 1..n_pages (page_to defaults to page_from), unknown unit kinds become
    `other`, entries without a usable page_from are skipped."""
    chapters = (override or {}).get("chapters") if override else None
    if not chapters or not isinstance(chapters, list):
        return units
    doc_unit = next((u for u in units if u.kind == "document"), None) or Unit(doc_id, doc_id, "document", 0, None, 1, n_pages)
    out = [doc_unit]
    taken: set[str] = set()
    for ch in chapters:
        if not isinstance(ch, dict):
            continue
        try:
            pf = int(ch.get("page_from") or 0)
            pt = int(ch.get("page_to") or pf)
        except (TypeError, ValueError):
            continue
        if pf < 1:
            continue
        pf = min(pf, max(1, n_pages))
        pt = min(max(pf, pt), max(1, n_pages))
        k = len(out)
        heading = " ".join(str(ch.get("heading") or f"Chapter {k}").split())
        uid = make_unit_id(doc_id, heading, taken)
        taken.add(uid)
        kind = str(ch.get("unit_kind") or "other")
        out.append(Unit(unit_id=uid, doc_id=doc_id, kind="chapter", ordinal=k, heading=heading, page_from=pf,
                        page_to=pt, confidence=1.0, reasons=("override",), parent_unit=doc_id,
                        unit_kind=kind if kind in UNIT_KINDS else "other"))
    return out if len(out) > 1 else units


def chapter_draft(job) -> dict:
    """The YAML draft written for a thesis whose chapters need the user's eye."""
    return {"doc_id": job.doc_id, "doc_type": job.doc_type, "n_pages": len(job.pages),
            "chapters": [{"heading": u.heading, "page_from": u.page_from, "page_to": u.page_to,
                          "unit_kind": u.unit_kind, "confidence": u.confidence, "reasons": list(u.reasons)}
                         for u in job.units if u.kind == "chapter"],
            "reasons": list(next((u.reasons for u in job.units if u.kind == "document"), ())),
            "hint": "copy the chapters list into curated/units_override.yaml under this doc_id and re-run ingest"}


def _doc_path(project: Project, rel_path: str | None) -> Path | None:
    if not rel_path:
        return None
    p = Path(rel_path)
    return p if p.is_absolute() else project.pdf_dir / p


def _file_logger(project: Project) -> tuple[logging.Logger, logging.Handler]:
    project.logs_dir.mkdir(parents=True, exist_ok=True)
    lg = logging.getLogger(_FILE_LOGGER)
    lg.setLevel(logging.INFO)
    lg.propagate = False
    h = logging.FileHandler(project.logs_dir / INGEST_LOG, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    lg.addHandler(h)
    return lg, h


@dataclass
class _Job:
    doc_id: str
    doc_type: str
    path: Path
    pdf_sha256: str
    extractor_version: str
    ext_id: str
    doctype: object
    min_chars: int
    override: dict | None = None
    # filled by _compute
    pages: list[PageText] = field(default_factory=list)
    masks: list[LineMask] = field(default_factory=list)
    units: list[Unit] = field(default_factory=list)
    marks: list[SectionMark] = field(default_factory=list)
    sentences: list[SentenceRow] = field(default_factory=list)
    text_ratio: float = 0.0
    drop_ratio: float = 0.0
    error: str | None = None
    draft: dict | None = None


def _compute(job: _Job) -> _Job:
    """Steps 3–6 (no database access; runs in a worker process when --workers > 1)."""
    try:
        dt = job.doctype
        job.pages = extract_pages(job.path)
        job.text_ratio = text_ratio(job.pages)
        job.masks = list(dt.drop_non_body(job.pages))
        job.units = apply_units_override(job.doc_id, list(dt.segment(job.doc_id, job.pages, job.masks)), job.override,
                                         len(job.pages))
        job.marks = list(dt.label_sections(job.doc_id, job.pages, job.masks, job.units))
        job.sentences = build_sentences(job.doc_id, job.ext_id, job.pages, job.masks, job.units, job.marks,
                                        job.min_chars)
        job.drop_ratio = drop_ratio_of(job.pages, job.masks)
    except Exception as e:
        log.exception("ingest failed for %s", job.doc_id)
        job.error = f"{type(e).__name__}: {e}"
    return job


def _stats(job: _Job) -> dict:
    sections: dict[str, int] = {}
    flags: dict[str, int] = {}
    reasons: dict[str, int] = {}
    for s in job.sentences:
        sections[s.section] = sections.get(s.section, 0) + 1
        for f in s.flags:
            flags[f] = flags.get(f, 0) + 1
    for m in job.masks:
        reasons[m.reason] = reasons.get(m.reason, 0) + 1
    return {"sections": sections, "flags": flags, "masks": reasons, "n_masks": len(job.masks),
            "n_units": len(job.units), "n_marks": len(job.marks),
            "body_sentences": sum(1 for s in job.sentences if s.is_body)}


class IngestStage:
    name = "ingest"
    version = "ingest1"
    llm = False
    requires: tuple[str, ...] = ()
    gate: tuple[str, ...] = ()

    def run(self, project: Project, build=None, *, reextract: bool = False, limit: int | None = None,
            doc_ids: list[str] | None = None, workers: int = 1) -> Report:
        """`build` is ignored: ingest writes the evidence store, not a build."""
        report = Report(stage=self.name)
        details: dict[str, dict] = {}
        report.details["documents"] = details
        min_chars = int(project.get("ingest.sentence_min_chars", 3) or 3)
        default_type = str(project.get("doc_type", "paper") or "paper")
        flog, handler = _file_logger(project)
        overrides = load_units_overrides(project, report)
        store = EvidenceStore.open(project.evidence_db, "rw")
        try:
            docs = sorted(store.list_documents(limit=10 ** 9)["rows"], key=lambda d: d["doc_id"])
            if doc_ids:
                wanted = {d.strip() for d in doc_ids if d and d.strip()}
                docs = [d for d in docs if d["doc_id"] in wanted]
            if limit is not None and limit >= 0:
                docs = docs[:limit]
            report.items_total = len(docs)

            # ---- steps 1–2: plan (needs the store for the skip check)
            jobs: list[_Job] = []
            for d in docs:
                doc_id = d["doc_id"]
                path = _doc_path(project, d.get("rel_path"))
                if path is None or not path.is_file():
                    report.items_failed += 1
                    msg = f"{doc_id}: PDF not found: {path}"
                    report.warnings.append(msg)
                    details[doc_id] = {"status": "failed", "error": "PDF not found"}
                    flog.info("%s failed pdf-not-found %s", doc_id, path)
                    continue
                try:
                    sha = sha256_file(path)
                    doc_type = d.get("doc_type") or default_type
                    doctype = get_doctype(doc_type)
                    if sha != d.get("pdf_sha256"):
                        log.info("%s: file changed on disk; registering new sha", doc_id)
                        store.add_document(doc_id, doc_type, d.get("source") or "scan", path,
                                           d.get("rel_path") or str(path), sha)
                    version = (f"{SEGMENT_VERSION}+{doctype.name}:{doctype.version}+{extractor_tag()}"
                               f"+ovr:{_override_sha(overrides.get(doc_id))}")
                    ext_id = extraction_id(doc_id, sha, version)
                except Exception as e:
                    log.exception("ingest planning failed for %s", doc_id)
                    report.items_failed += 1
                    report.warnings.append(f"{doc_id}: {type(e).__name__}: {e}")
                    details[doc_id] = {"status": "failed", "error": str(e)}
                    flog.info("%s failed %s", doc_id, e)
                    continue
                if store.extraction_exists(ext_id) and not reextract:
                    report.items_skipped += 1
                    details[doc_id] = {"status": "skipped", "ext_id": ext_id}
                    flog.info("%s skipped ext=%s (exists)", doc_id, ext_id)
                    continue
                jobs.append(_Job(doc_id=doc_id, doc_type=doc_type, path=path, pdf_sha256=sha,
                                 extractor_version=version, ext_id=ext_id, doctype=doctype, min_chars=min_chars,
                                 override=overrides.get(doc_id)))

            # ---- steps 3–6: compute (optionally in worker processes), steps 7–8: write in doc_id order
            if workers and workers > 1 and len(jobs) > 1:
                pool = ProcessPoolExecutor(max_workers=int(workers))
                results = pool.map(_compute, jobs)
            else:
                pool = None
                results = map(_compute, jobs)
            batch_size = max(1, int(project.get("ingest.batch_docs", BATCH_DOCS) or BATCH_DOCS))
            try:
                with store.bulk_writes():
                    batch: list[_Job] = []
                    n_batches = 0
                    for job in results:
                        batch.append(job)
                        if len(batch) >= batch_size:
                            self._finish_batch(batch, store, report, details, flog, project)
                            batch = []
                            n_batches += 1
                            if n_batches % CHECKPOINT_BATCHES == 0:
                                busy, frames, done_frames = store.checkpoint("PASSIVE")
                                flog.info("checkpoint after %d batches: wal_frames=%d checkpointed=%d busy=%d",
                                          n_batches, frames, done_frames, busy)
                    if batch:
                        self._finish_batch(batch, store, report, details, flog, project)
            finally:
                if pool is not None:
                    pool.shutdown(wait=True)
        finally:
            store.close()
            flog.removeHandler(handler)
            handler.close()
        log.info(report.summary())
        return report

    @classmethod
    def _finish_batch(cls, batch: list[_Job], store: EvidenceStore, report: Report, details: dict,
                      flog: logging.Logger, project: Project) -> None:
        """Steps 7–8 for several documents in ONE transaction (fewer commits, fewer FTS segments). Bookkeeping
        happens only after the commit; if anything fails the whole batch rolls back and every document is
        stored again on its own through _finish, which records the failure of the bad one."""
        if len(batch) == 1:
            cls._finish(batch[0], store, report, details, flog, project)
            return
        stored: list[tuple[_Job, list[str], int]] = []
        try:
            with transaction(store.conn):
                for job in batch:
                    if job.error:
                        continue
                    warns, n_aliases = cls._store(job, store)
                    stored.append((job, warns, n_aliases))
        except Exception as e:
            log.warning("batch of %d documents rolled back (%s: %s); storing them one by one",
                        len(batch), type(e).__name__, e)
            for job in batch:
                cls._finish(job, store, report, details, flog, project)
            return
        for job in batch:
            if job.error:
                cls._record_failure(job, report, details, flog, job.error)
        for job, warns, n_aliases in stored:
            cls._record_done(job, warns, n_aliases, report, details, flog)
            path = cls._write_draft(job, project)
            if path:
                flog.info("%s chapter draft %s", job.doc_id, path)

    @staticmethod
    def _record_failure(job: _Job, report: Report, details: dict, flog: logging.Logger, error: str) -> None:
        report.items_failed += 1
        report.warnings.append(f"{job.doc_id}: {error}")
        details[job.doc_id] = {"status": "failed", "error": error, "ext_id": job.ext_id}
        flog.info("%s failed ext=%s %s", job.doc_id, job.ext_id, error)

    @staticmethod
    def _store(job: _Job, store: EvidenceStore) -> tuple[list[str], int]:
        """Steps 7–8 for one computed job inside the caller's transaction: (warnings, number of aliases).
        Raises on storage errors."""
        warns: list[str] = []
        th = job.doctype.thresholds
        if job.text_ratio < th.min_text_ratio:
            warns.append(f"low text ratio {job.text_ratio:.2f} < {th.min_text_ratio:.2f}")
        if job.drop_ratio > th.max_drop_ratio:
            warns.append(f"drop ratio {job.drop_ratio:.2f} > {th.max_drop_ratio:.2f}")
        if not job.sentences:
            warns.append("no sentences")
        elif not any(m.section not in ("front", "keywords", "other") for m in job.marks):
            warns.append("no headings detected (whole document labelled 'other')")
        chapters = [u for u in job.units if u.kind == "chapter"]
        if job.doc_type == "thesis":
            low = [u for u in chapters if (u.confidence or 0) < th.min_chapter_confidence]
            if not chapters:
                warns.append("no chapter units detected (document unit only)")
            elif low:
                warns.append(f"{len(low)} of {len(chapters)} chapter units below confidence {th.min_chapter_confidence:.1f}")
            if not chapters or low:
                job.draft = chapter_draft(job)
        prev = store.current_extraction(job.doc_id)
        aliases: list[tuple[str, str, str, str, float]] = []
        if prev is not None and prev["ext_id"] != job.ext_id:
            old = store.sentences_of_extraction(prev["ext_id"])
            aliases = compute_aliases(old, job.sentences, prev["ext_id"], job.ext_id)
        stats = _stats(job)
        stats["warnings"] = warns
        with transaction(store.conn):
            store.add_extraction(ext_id=job.ext_id, doc_id=job.doc_id, pdf_sha256=job.pdf_sha256,
                                 extractor_version=job.extractor_version, doc_type=job.doc_type,
                                 pages=job.pages, masks=job.masks, units=job.units, sentences=job.sentences,
                                 text_ratio=job.text_ratio, drop_ratio=job.drop_ratio, stats=stats)
            if aliases:
                store.add_aliases(aliases)
        return warns, len(aliases)

    @staticmethod
    def _write_draft(job: _Job, project: Project) -> str | None:
        """Write `work/drafts/chapters/<doc_id>.yaml` for a thesis whose chapters need the user's eye; its path."""
        if not job.draft:
            return None
        d = project.root.joinpath(*CHAPTER_DRAFTS)
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{job.doc_id}.yaml"
        with open(p, "w", encoding="utf-8") as f:
            yaml.safe_dump(job.draft, f, allow_unicode=True, sort_keys=False)
        return str(p)

    @staticmethod
    def _record_done(job: _Job, warns: list[str], n_aliases: int, report: Report, details: dict,
                     flog: logging.Logger) -> None:
        doc_id = job.doc_id
        report.items_done += 1
        for w in warns:
            report.warnings.append(f"{doc_id}: {w}")
        details[doc_id] = {"status": "done", "ext_id": job.ext_id, "n_pages": len(job.pages),
                           "n_sentences": len(job.sentences), "n_aliases": n_aliases,
                           "text_ratio": round(job.text_ratio, 4), "drop_ratio": round(job.drop_ratio, 4),
                           "warnings": warns, "n_chapters": sum(1 for u in job.units if u.kind == "chapter"),
                           "chapter_confidence": min((u.confidence for u in job.units if u.kind == "chapter"), default=None),
                           "draft": job.draft is not None}
        flog.info("%s done ext=%s pages=%d sentences=%d masks=%d aliases=%d text_ratio=%.3f drop_ratio=%.3f%s",
                  doc_id, job.ext_id, len(job.pages), len(job.sentences), len(job.masks), n_aliases,
                  job.text_ratio, job.drop_ratio, (" warnings=" + "; ".join(warns)) if warns else "")
        log.info("%s: %d pages, %d sentences (ext %s)", doc_id, len(job.pages), len(job.sentences), job.ext_id)

    @classmethod
    def _finish(cls, job: _Job, store: EvidenceStore, report: Report, details: dict, flog: logging.Logger,
                project: Project) -> None:
        """Steps 7–8 for one document in its own transaction; a storage error is recorded, never raised."""
        if job.error:
            cls._record_failure(job, report, details, flog, job.error)
            return
        try:
            warns, n_aliases = cls._store(job, store)
        except Exception as e:
            log.exception("storing extraction failed for %s", job.doc_id)
            cls._record_failure(job, report, details, flog, f"{type(e).__name__}: {e}")
            return
        cls._record_done(job, warns, n_aliases, report, details, flog)
        path = cls._write_draft(job, project)
        if path:
            flog.info("%s chapter draft %s", job.doc_id, path)


# ---- registration (`atlas docs add`) ---------------------------------------------------------------------------

def _rel_path(project: Project, path: Path) -> str:
    try:
        return path.resolve().relative_to(project.pdf_dir.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def _int_or_none(v: str | None) -> int | None:
    s = (v or "").strip()
    m = re.search(r"\d{4}", s)
    if m:
        return int(m.group(0))
    return int(s) if s.isdigit() else None


def _meta_from_row(row: dict) -> DocMeta:
    g = lambda k: (" ".join((row.get(k) or "").split()) or None)  # noqa: E731
    return DocMeta(title=g("title"), year=_int_or_none(row.get("year")), authors=g("authors"), doi=g("doi"),
                   venue=g("venue"))


def _fill_from_pdf(meta: DocMeta, path: Path) -> DocMeta:
    """Fill title/authors/keywords/doi that the manifest did not provide from the PDF (info dict, then guesses)."""
    pm = pdf_metadata(path)
    if meta.title is None:
        meta.title = pm.title or pm.extra.get("title_guess")
    if meta.authors is None:
        meta.authors = pm.authors
    if meta.keywords is None:
        meta.keywords = pm.keywords
    if meta.doi is None:
        meta.doi = pm.doi or pm.extra.get("doi_guess")
    meta.extra.update({k: v for k, v in pm.extra.items() if v is not None})
    return meta


def _meta_changed(existing: dict, meta: DocMeta) -> bool:
    for f in ("title", "year", "authors", "doi", "venue"):
        v = getattr(meta, f)
        if v is not None and existing.get(f) != v:
            return True
    return False


def read_manifest(path: Path) -> list[dict]:
    """Rows of manifest.csv (UTF-8 with optional BOM), header names lower-cased and stripped."""
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        rows: list[dict] = []
        for raw in reader:
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items() if k is not None}
            if any(row.values()):
                rows.append(row)
    return rows


def register_documents(project: Project, manifest: str | None = None, pattern: str = "**/*.pdf",
                       scan: bool | None = None) -> Report:
    """`atlas docs add`: read manifest.csv (if present or given) and, when `scan` is true, also scan pdf_dir for
    PDFs not in the manifest; add documents and files to the evidence store; never extracts. `scan` defaults to
    True only when there is no manifest (a pdf_dir may be a large library folder that contains far more than the
    corpus). Report.details = {added, updated, unchanged, missing_files}."""
    report = Report(stage="docs add")
    added: list[str] = []
    updated: list[str] = []
    unchanged: list[str] = []
    missing: list[str] = []
    report.details.update({"added": added, "updated": updated, "unchanged": unchanged, "missing_files": missing})
    default_type = str(project.get("doc_type", "paper") or "paper")
    pdf_dir = project.pdf_dir

    mpath: Path | None = None
    if manifest:
        mpath = Path(manifest)
        if not mpath.is_absolute():
            mpath = project.root / mpath
        if not mpath.is_file():
            raise FileNotFoundError(f"manifest not found: {mpath}")
    elif project.manifest_path.is_file():
        mpath = project.manifest_path
    if scan is None:
        scan = mpath is None

    store = EvidenceStore.open(project.evidence_db, "rw")
    try:
        known = store.known_files()
        seq = store.max_doc_seq(SCAN_ID_PREFIX)

        def next_id() -> str:
            nonlocal seq
            seq += 1
            return f"{SCAN_ID_PREFIX}{seq:0{SCAN_ID_WIDTH}d}"

        def register(doc_id: str, doc_type: str, source: str, path: Path, meta: DocMeta) -> None:
            rel = _rel_path(project, path)
            sha = sha256_file(path)
            existing = store.get_document(doc_id)
            store.add_document(doc_id, doc_type, source, path, rel, sha, meta)
            known[rel] = doc_id
            if existing is None:
                added.append(doc_id)
            elif existing.get("pdf_sha256") != sha or _meta_changed(existing, meta):
                updated.append(doc_id)
            else:
                unchanged.append(doc_id)

        manifest_files: set[str] = set()
        if mpath is not None:
            rows = read_manifest(mpath)
            report.items_total += len(rows)
            for i, row in enumerate(rows, 1):
                file = row.get("file", "")
                if not file:
                    report.items_failed += 1
                    report.warnings.append(f"manifest row {i}: no file")
                    continue
                p = Path(file)
                path = p if p.is_absolute() else pdf_dir / p
                if not path.is_file():
                    report.items_failed += 1
                    missing.append(file)
                    report.warnings.append(f"manifest row {i}: file not found: {path}")
                    continue
                manifest_files.add(_rel_path(project, path))
                doc_id = row.get("doc_id") or ""
                if not doc_id:
                    rel = _rel_path(project, path)
                    doc_id = known.get(rel) or next_id()
                doc_type = row.get("doc_type") or default_type
                meta = _meta_from_row(row)
                if meta.title is None:
                    meta = _fill_from_pdf(meta, path)
                try:
                    register(doc_id, doc_type, "manifest", path, meta)
                    report.items_done += 1
                except Exception as e:
                    log.exception("registering %s failed", doc_id)
                    report.items_failed += 1
                    report.warnings.append(f"{doc_id}: {type(e).__name__}: {e}")

        if scan and pdf_dir.is_dir():
            for path in sorted(p for p in pdf_dir.glob(pattern) if p.is_file()):
                rel = _rel_path(project, path)
                if rel in manifest_files:
                    continue
                report.items_total += 1
                if rel in known:
                    unchanged.append(known[rel])
                    report.items_skipped += 1
                    continue
                doc_id = next_id()
                meta = _fill_from_pdf(DocMeta(), path)
                try:
                    register(doc_id, default_type, "scan", path, meta)
                    report.items_done += 1
                except Exception as e:
                    log.exception("registering %s failed", doc_id)
                    report.items_failed += 1
                    report.warnings.append(f"{doc_id}: {type(e).__name__}: {e}")
        elif scan and mpath is None:
            report.warnings.append(f"pdf_dir does not exist: {pdf_dir}")
    finally:
        store.close()
    log.info("%s: added %d, updated %d, unchanged %d, missing %d", report.stage, len(added), len(updated),
             len(unchanged), len(missing))
    return report
