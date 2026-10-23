"""AtlasService — the single façade. Implemented by: increment 1, agent "service+http".

Every method returns JSON-serialisable dicts/lists. Every evidence pointer is a *ref object*:
    {"ref": "S:S1…", "doc_id": "P123", "title": "...", "year": 2021, "doi": "...", "unit_id": "P123",
     "page": 4, "sid": "S1…", "text": "...", "status": "live", "flags": []}
Every label is a {"zh": ..., "en": ...} pair. Thread-safety: one EvidenceStore/UserStore connection per thread
(threading.local inside the service); the service is constructed once per server process. Because
ThreadingHTTPServer runs every request on a fresh thread, the HTTP adapter wraps each request in `lease()`,
which binds a pooled (EvidenceStore, UserStore) pair to the request thread and returns it to the pool
afterwards, so connections (and the user-schema migration check) are not re-opened per request.

Error convention (the HTTP adapter maps these): KeyError → 404, ValueError → 400, NoBuildError → 409,
everything else → 500.

Increment 2: `build()` is the BuildStore of the promoted build (`builds/current.txt`), pooled like the evidence
store and re-opened when current.txt changes; the concept / framework routes answer 409 {"error": "no build
promoted"} without one, the document / reader / notes routes keep working.
"""
from __future__ import annotations

import json
import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import atlas
from atlas.config import Project
from atlas.core.schemas import REF_KINDS, SECTION_ZH, SECTIONS, TAGS, parse_ref
from atlas.ingest import pdf as pdf_io
from atlas.core.stats import TIER_EN, TIER_ZH, TIERS
from atlas.pipeline.build import read_manifest, resolve_build
from atlas.store.build import BuildStore, current_build_id
from atlas.store.evidence import EvidenceStore
from atlas.store.user import ConflictError, UserStore

log = logging.getLogger(__name__)

BUILD = "evidence"
I18N_PATH = Path(__file__).resolve().parents[1] / "http" / "static" / "i18n.json"

TAG_ZH = {"def": "定义", "quant": "定量", "method": "方法", "mech": "机制", "cmp": "比较", "limit": "局限",
          "result": "结果"}
TAG_EN = {"def": "Definition", "quant": "Quantitative", "method": "Method", "mech": "Mechanism",
          "cmp": "Comparison", "limit": "Limitation", "result": "Result"}
KIND_ZH = {"document": "文献", "unit": "单元", "sentence": "句子", "concept": "概念", "statement": "陈述",
           "subdomain": "子领域", "answer": "回答", "note": "笔记", "collection": "摘录册", "query": "查询"}

_RESOLVABLE = ("D", "U", "S", "N", "K", "C", "SD")
_NEEDS_BUILD = ("C", "SD")
_IDS_PAGE = 1000            # page size when the service walks a whole concept list (ids mode, type facet)
CONCEPT_SORTS = ("papers", "coverage", "topic_rate", "label", "tier")   # contract §6; anything else is a 400
CONCEPT_KINDS = ("concept", "other", "all")                             # list_concepts(kind=…)


class NoBuildError(Exception):
    """A concept/framework route was called while no build is promoted (HTTP 409) — or the promoted build was
    written by an older schema than this code serves (the message then says to rebuild)."""

    def __init__(self, msg: str = "no build promoted"):
        super().__init__(msg)


def _ref_object(**kw) -> dict:
    base = {"ref": None, "kind": None, "doc_id": None, "title": None, "year": None, "doi": None, "unit_id": None,
            "page": None, "sid": None, "text": None, "status": "orphan", "flags": []}
    base.update(kw)
    return base


def _clip(text: str | None, n: int = 240) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


class AtlasService:
    POOL_SIZE = 8           # idle (EvidenceStore, UserStore) pairs kept for request threads

    def __init__(self, project: Project):
        self.project = project
        self._local = threading.local()
        self._pool: list[tuple[EvidenceStore, UserStore, BuildStore | None]] = []
        self._pool_lock = threading.Lock()
        self._ui_strings = self._load_ui_strings()
        self._build_id: str | None = current_build_id(project.builds_dir)
        self._types_cache: tuple[str, list[dict]] | None = None   # (build id, concept type facet) — builds are immutable
        self._build_stale: str | None = None                       # promoted build id refused for its schema
        # /info is called on every page load; its evidence aggregates are cached per evidence file state and
        # its build counts per build id (builds are immutable) — see _evidence_key()
        self._stats_cache: tuple[tuple, dict] | None = None
        self._build_stats_cache: tuple[str, dict] | None = None
        self._year_totals_cache: tuple[str, dict] | None = None
        self._cache_lock = threading.Lock()
        log.info("service for project %s at %s (build %s)", project.name, project.root, self._build_id)

    # ---- per-thread stores ----------------------------------------------------------------------------------
    def _open_evidence(self) -> EvidenceStore:
        path = self.project.evidence_db
        # the server reads evidence read-only; a project that has not been ingested yet gets an empty DB
        return EvidenceStore.open(path, "ro") if path.exists() else EvidenceStore.open(path, "rw")

    def evidence(self) -> EvidenceStore:
        st = getattr(self._local, "evidence", None)
        if st is None:
            st = self._open_evidence()
            self._local.evidence = st
        return st

    def user(self) -> UserStore:
        st = getattr(self._local, "user", None)
        if st is None:
            st = UserStore.open(self.project.user_db, "rw")
            self._local.user = st
        return st

    # ---- promoted build -----------------------------------------------------------------------------------
    def _refresh_build_id(self) -> None:
        """Re-read builds/current.txt (cheap) and drop pooled build stores when it changed."""
        bid = current_build_id(self.project.builds_dir)
        if bid != self._build_id:
            log.info("promoted build changed: %s -> %s", self._build_id, bid)
            self._build_id = bid
            with self._pool_lock:
                pairs, self._pool = self._pool, []
            for pair in pairs:
                for st in pair:
                    if st is not None:
                        try:
                            st.close()
                        except Exception:  # pragma: no cover - defensive
                            pass
            b = getattr(self._local, "build", None)
            if b is not None:
                try:
                    b.close()
                except Exception:  # pragma: no cover - defensive
                    pass
                self._local.build = None

    def _open_build(self) -> BuildStore | None:
        bid = self._build_id
        if not bid:
            return None
        try:
            d = resolve_build(self.project, bid)
        except Exception as e:
            log.warning("promoted build %s cannot be opened: %s", bid, e)
            return None
        m = read_manifest(d) or {}
        mode = "immutable" if m.get("status") == "finished" else "ro"
        b = BuildStore.open(d, mode, evidence_path=self.project.evidence_db)
        if not b.schema_current:
            # a build file of an older schema: its occurrences table lacks the columns the queries use
            if self._build_stale != bid:
                log.warning("promoted build %s has schema %s; rebuild it (`atlas build all`)", bid, b.schema_version)
                self._build_stale = bid
            b.close()
            return None
        return b

    def build_optional(self) -> BuildStore | None:
        """The promoted build's store for this thread, or None when no build is promoted (or the promoted build
        cannot be served, see `build()`)."""
        if getattr(self._local, "build_bid", None) != self._build_id:
            b = getattr(self._local, "build", None)
            if b is not None:
                try:
                    b.close()
                except Exception:  # pragma: no cover - defensive
                    pass
            self._local.build = self._open_build()
            self._local.build_bid = self._build_id
        return getattr(self._local, "build", None)

    def build(self) -> BuildStore:
        b = self.build_optional()
        if b is None:
            if self._build_id and self._build_stale == self._build_id:
                raise NoBuildError(f"promoted build {self._build_id} was written by an older schema; "
                                   f"run `atlas build upgrade {self._build_id}` (seconds) or `atlas build all`")
            raise NoBuildError()
        return b

    def build_label(self) -> str:
        """Value of the X-Atlas-Build header: the promoted build id, else 'evidence'."""
        return self._build_id or BUILD

    @contextmanager
    def lease(self) -> Iterator[None]:
        """Bind a pooled (EvidenceStore, UserStore) pair to the calling thread for the duration of a request and
        hand it back afterwards (review fix: ThreadingHTTPServer uses one thread per request, so plain
        thread-local caching opened two fresh connections per request). A thread that already holds stores
        keeps them."""
        self._refresh_build_id()
        if getattr(self._local, "evidence", None) is not None or getattr(self._local, "user", None) is not None:
            yield
            return
        with self._pool_lock:
            pair = self._pool.pop() if self._pool else None
        if pair is None:
            pair = (self._open_evidence(), UserStore.open(self.project.user_db, "rw"), self._open_build())
        self._local.evidence, self._local.user, self._local.build = pair
        self._local.build_bid = self._build_id
        try:
            yield
        finally:
            self._local.evidence = self._local.user = self._local.build = None
            self._local.build_bid = None
            with self._pool_lock:
                keep = len(self._pool) < self.POOL_SIZE
                if keep:
                    self._pool.append(pair)
            if not keep:
                for st in pair:
                    if st is None:
                        continue
                    try:
                        st.close()
                    except Exception:  # pragma: no cover - defensive
                        log.debug("close pooled store failed", exc_info=True)

    def pool_size(self) -> int:
        with self._pool_lock:
            return len(self._pool)

    def close(self) -> None:
        """Close this thread's connections (tests; the server's threads close theirs on exit)."""
        for name in ("evidence", "user", "build"):
            st = getattr(self._local, name, None)
            if st is not None:
                try:
                    st.close()
                except Exception:  # pragma: no cover - defensive
                    log.debug("close %s failed", name, exc_info=True)
                setattr(self._local, name, None)

    def shutdown(self) -> None:
        """Close this thread's connections and every idle pooled pair (server exit)."""
        self.close()
        with self._pool_lock:
            pairs, self._pool = self._pool, []
        for pair in pairs:
            for st in pair:
                if st is None:
                    continue
                try:
                    st.close()
                except Exception:  # pragma: no cover - defensive
                    log.debug("close pooled store failed", exc_info=True)

    # ---- meta
    def _evidence_key(self) -> tuple:
        """Cache key for anything derived from evidence.sqlite: (size, mtime_ns) of the main file and of its WAL
        (ingest writes land in the WAL first; the main file only changes at a checkpoint)."""
        key: list = []
        for suffix in ("", "-wal"):
            p = Path(str(self.project.evidence_db) + suffix)
            try:
                st = p.stat()
                # an empty WAL is what a reader leaves behind when it opens the file: not a change
                key.append((st.st_size, st.st_mtime_ns) if st.st_size or not suffix else None)
            except OSError:
                key.append(None)
        return tuple(key)

    def evidence_stats(self) -> dict:
        """EvidenceStore.stats(), cached until the evidence file (or its WAL) changes."""
        ev = self.evidence()            # opening the connection may create the WAL/SHM files: key afterwards
        key = self._evidence_key()
        with self._cache_lock:
            cached = self._stats_cache
        if cached is not None and cached[0] == key:
            return dict(cached[1])
        stats = ev.stats()
        with self._cache_lock:
            self._stats_cache = (key, dict(stats))
        return stats

    def build_stats(self, b: BuildStore) -> dict:
        with self._cache_lock:
            cached = self._build_stats_cache
        if cached is not None and cached[0] == b.build_id:
            return dict(cached[1])
        st = b.stats()
        with self._cache_lock:
            self._build_stats_cache = (b.build_id, dict(st))
        return st

    def year_totals(self, b: BuildStore) -> dict[str, int]:
        with self._cache_lock:
            cached = self._year_totals_cache
        if cached is not None and cached[0] == b.build_id:
            return dict(cached[1])
        yt = b.year_totals()
        with self._cache_lock:
            self._year_totals_cache = (b.build_id, dict(yt))
        return yt

    def info(self) -> dict:
        """{project, build: build id | 'evidence', build_info: {id, created, docs, sentences, concepts} | None,
        build_stale: None | {id, reason} (a promoted build this code refuses: older schema — the UI says so
        instead of 'no build'), stats: EvidenceStore.stats(), version}. The evidence stats are cached per evidence
        file state and the build counts per build id (the SPA calls this route on every load)."""
        b = self.build_optional()
        binfo = None
        stale = None
        if b is not None:
            st = self.build_stats(b)
            binfo = {"id": b.build_id, "created": st.get("created"), "docs": st["docs"], "sentences": st["sentences"],
                     "concepts": st["concepts"], "occurrences": st.get("occurrences", 0)}
        elif self._build_id and self._build_stale == self._build_id:
            stale = {"id": self._build_id, "reason": "schema",
                     "message": f"promoted build {self._build_id} was written by an older schema; "
                                f"run `atlas build upgrade {self._build_id}` or `atlas build all`"}
        return {"project": self.project.name, "build": self.build_label(), "build_info": binfo, "build_stale": stale,
                "stats": self.evidence_stats(), "version": atlas.__version__,
                "doc_type": self.project.get("doc_type", "paper")}

    @staticmethod
    def _load_ui_strings() -> dict:
        try:
            with open(I18N_PATH, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError) as e:
            log.warning("i18n.json not loaded (%s)", e)
            return {}

    def i18n(self) -> dict:
        """UI chrome strings {key: {zh, en}} loaded from http/static/i18n.json plus section/tag labels from
        atlas.core.schemas (SECTION_ZH; English = the key capitalised).

        Shape: {"ui": {key: {zh, en}}, "sections": {section: {zh, en}}, "tags": {tag: {zh, en}},
                "kinds": {ref kind letter: {zh, en}}, "tiers": {A: {zh, en}}, "data": {zh: en} (the promoted
                build's i18n table: domain / category / sub-domain names and descriptions), "domains": [...],
                "types": [{type, n}] (the concept types of the build, most frequent first; the list view's filter)}"""
        out = {
            "ui": self._ui_strings,
            "sections": {s: {"zh": SECTION_ZH.get(s, s), "en": s.capitalize()} for s in SECTIONS},
            "tags": {t: {"zh": TAG_ZH.get(t, t), "en": TAG_EN.get(t, t.capitalize())} for t in TAGS},
            "kinds": {k: {"zh": KIND_ZH.get(v, v), "en": v.capitalize()} for k, v in REF_KINDS.items()},
            "tiers": {t: {"zh": TIER_ZH[t], "en": TIER_EN[t]} for t in TIERS},
            "data": {}, "domains": [], "types": [],
        }
        out["sections"]["body"] = {"zh": "正文", "en": "Body"}
        b = self.build_optional()
        if b is not None:
            out["data"] = b.i18n_strings()
            out["domains"] = b.domains()
            out["types"] = self._concept_types(b)
        return out

    def _all_concepts(self, b: BuildStore, **filters) -> dict:
        """BuildStore.list_concepts() result with *every* row matching `filters` in list order (the service walks
        the store's pages)."""
        rows: list[dict] = []
        while True:
            res = b.list_concepts(limit=_IDS_PAGE, offset=len(rows), **filters)
            rows += res["rows"]
            if not res["rows"] or len(rows) >= res["total"]:
                res["rows"] = rows
                return res

    def _concept_types(self, b: BuildStore) -> list[dict]:
        cached = self._types_cache
        if cached is not None and cached[0] == b.build_id:
            return cached[1]
        counts: dict[str, int] = {}
        for r in self._all_concepts(b)["rows"]:
            if r.get("type"):
                counts[r["type"]] = counts.get(r["type"], 0) + 1
        types = [{"type": k, "n": n} for k, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]
        self._types_cache = (b.build_id, types)
        return types

    # ---- documents and reading
    def _notes_count(self, ref: str) -> int:
        return self.user().note_counts([ref]).get(ref, 0)

    def list_docs(self, q: str | None = None, limit: int = 100, offset: int = 0) -> dict:
        res = self.evidence().list_documents(q, int(limit), int(offset))
        counts = self.user().note_counts([f"D:{row['doc_id']}" for row in res["rows"]])
        for row in res["rows"]:
            row["notes_count"] = counts.get(f"D:{row['doc_id']}", 0)
        return res

    def _doc(self, doc_id: str) -> dict:
        d = self.evidence().get_document(doc_id)
        if d is None:
            raise KeyError(f"unknown document {doc_id!r}")
        return d

    def get_doc(self, doc_id: str) -> dict:
        """Document dict + pages:[{page_no, n_sentences}] + notes_count + has_pdf."""
        d = self._doc(doc_id)
        d["pages"] = self.evidence().pages_of(doc_id)
        d["notes_count"] = self._notes_count(f"D:{doc_id}")
        d["has_pdf"] = self.pdf_path(doc_id) is not None
        d["units"] = self.evidence().units_of(doc_id)
        d["domains"], d["subdomains"], d["top_concepts"] = [], [], []
        b = self.build_optional()
        if b is not None:
            ext = b.doc_extension(doc_id)
            if ext:
                d["domains"] = ext["domains"]
                d["subdomains"] = ext["subdomains"]
                d["top_concepts"] = ext["top_concepts"]
                if ext.get("units"):
                    d["units"] = ext["units"]
                d["in_build"] = True
            else:
                d["in_build"] = False
        return d

    def page(self, doc_id: str, page_no: int, c: str | None = None) -> dict:
        """EvidenceStore.page() (sentences carry their stored text) + for each sentence the number of notes
        targeting it; with `c` (comma-separated concept ids) every sentence gets `concepts: [{id, cstart, cend}]`
        from the promoted build (empty without one)."""
        d = self._doc(doc_id)
        p = self.evidence().page(doc_id, int(page_no))
        if p is None:
            raise KeyError(f"no page {page_no} in {doc_id!r}")
        counts = self.user().note_counts([f"S:{s['sid']}" for s in p["sentences"]])
        for s in p["sentences"]:
            s["notes"] = counts.get(f"S:{s['sid']}", 0)
        p["n_pages"] = int(d.get("n_pages") or 0)
        p["title"] = d.get("title")
        ids = [x.strip() for x in (c or "").split(",") if x.strip()]
        if ids:
            b = self.build_optional()
            marks = b.page_concepts(doc_id, int(page_no), ids) if b is not None else {}
            for s in p["sentences"]:
                s["concepts"] = marks.get(s["sid"], [])
            p["concepts"] = ids
        return p

    def page_png(self, doc_id: str, page_no: int, highlight: str | None = None,
                 spans: list[tuple[int, int]] | None = None, zoom: float | None = None) -> tuple[bytes, dict]:
        """PNG of a page; KeyError (404) when the document has no PDF, the page is out of range or the PDF cannot
        be rendered (empty, encrypted or damaged file — the message never carries the file path)."""
        path = self.pdf_path(doc_id)
        if path is None:
            raise KeyError(f"no PDF for {doc_id!r}")
        n_pages = int(self._doc(doc_id).get("n_pages") or 0)
        page_no = int(page_no)
        if page_no < 1 or (n_pages and page_no > n_pages):
            raise KeyError(f"no page {page_no} in {doc_id!r}")
        z = float(zoom) if zoom else float(self.project.get("ingest.page_render_zoom", 1.6))
        z = max(0.2, min(z, 5.0))
        try:
            png, info = pdf_io.render_page(path, page_no, zoom=z, highlight=highlight or None, spans=spans or None)
        except (ValueError, RuntimeError, OSError) as e:   # fitz.FileDataError is a RuntimeError
            log.warning("cannot render %s page %s: %s: %s", doc_id, page_no, type(e).__name__, e)
            raise KeyError(f"page {page_no} of {doc_id!r} cannot be rendered") from e
        if int(info.get("pages") or 0) and page_no > int(info["pages"]):
            raise KeyError(f"no page {page_no} in {doc_id!r}")
        return png, info

    def pdf_path(self, doc_id: str) -> str | None:
        d = self._doc(doc_id)
        rel = d.get("rel_path")
        if not rel:
            return None
        p = Path(rel)
        if not p.is_absolute():
            p = self.project.pdf_dir / p
        return str(p) if p.is_file() else None

    def _doc_meta(self, doc_id: str) -> dict:
        d = self.evidence().get_document(doc_id) or {}
        return {"title": d.get("title"), "year": d.get("year"), "doi": d.get("doi"), "authors": d.get("authors"),
                "venue": d.get("venue")}

    def search(self, phrase: str, doc_id: str | None = None, limit: int = 50, offset: int = 0) -> dict:
        """Sentence phrase search (EvidenceStore.phrase_search) with each row expanded to a ref object; an unknown
        doc_id is a KeyError (404) like every other docs/{id} route."""
        phrase = (phrase or "").strip()
        if doc_id:
            self._doc(doc_id)
        res = self.evidence().phrase_search(phrase, doc_id or None, int(limit), int(offset))
        rows = []
        for r in res["rows"]:
            rows.append(_ref_object(ref=f"S:{r['sid']}", kind="S", doc_id=r["doc_id"], title=r.get("title"),
                                    year=r.get("year"), page=r["page_no"], sid=r["sid"], status="live",
                                    section=r.get("section"), is_body=r.get("is_body"), snippet=r.get("snippet")))
        return {"phrase": phrase, "doc_id": doc_id, "rows": rows, "total": res["total"],
                "documents": res["documents"], "total_capped": bool(res.get("total_capped")),
                "listed": int(res.get("listed", len(rows)))}

    def _sentence_ref(self, s: dict) -> dict:
        doc = s.get("doc") or {}
        return _ref_object(ref=f"S:{s['sid']}", kind="S", doc_id=s["doc_id"], title=doc.get("title"),
                           year=doc.get("year"), doi=doc.get("doi"), unit_id=s.get("unit_id"), page=s.get("page_no"),
                           sid=s["sid"], text=s.get("text"), status=s.get("status", "live"),
                           flags=list(s.get("flags") or []), section=s.get("section"), is_body=s.get("is_body"),
                           tags=list(s.get("tags") or []), cstart=s.get("cstart"), cend=s.get("cend"),
                           requested_sid=s.get("requested_sid"))

    def sentence(self, sid: str, context: int = 1) -> dict:
        """Ref object + before/after + notes targeting it."""
        s = self.evidence().sentence(sid, int(context))
        if s is None:
            raise KeyError(f"unknown sentence {sid!r}")
        out = self._sentence_ref(s)
        out["before"] = s.get("before") or []
        out["after"] = s.get("after") or []
        refs = [f"S:{sid}"]
        if s["sid"] != sid:
            refs.append(f"S:{s['sid']}")
        seen: set[str] = set()
        notes = []
        for ref in refs:
            for n in self.user().notes_for_ref(ref):
                if n["note_id"] not in seen:
                    seen.add(n["note_id"])
                    notes.append(n)
        out["notes"] = notes
        return out

    def resolve(self, ref: str) -> dict:
        """Any ref → ref object (status live|aliased|orphan). Only D:, U:, S:, N:, K: resolve in increment 1."""
        r = parse_ref(ref)  # ValueError → 400
        new_ref, alias_status = self.user().resolve_ref(str(r))
        if new_ref != str(r):
            try:
                r = parse_ref(new_ref)
            except ValueError:
                log.warning("ref alias %s → %s is not a ref", ref, new_ref)
        out = _ref_object(ref=str(r), kind=r.kind, requested_ref=ref)
        if r.kind not in _RESOLVABLE:
            out["status"] = "orphan"
            out["flags"] = ["unsupported_kind"]
            return out
        if r.kind in _NEEDS_BUILD:
            b = self.build_optional()
            if b is None:
                out["flags"] = ["no_build"]
                return out
            if r.kind == "C":
                c = b.concept(r.id)
                if c is None:
                    return out
                out.update({"title": c["label"], "text": c["label"], "zh": c.get("zh"), "status": alias_status,
                            "concept_id": r.id, "type": c.get("type")})
                return out
            sd = b.subdomain_label(r.id)
            if sd is None:
                return out
            out.update({"title": sd.get("zh"), "text": sd.get("zh"), "zh": sd.get("zh"), "en": sd.get("en"),
                        "status": alias_status, "sub_id": r.id, "sub_kind": sd.get("kind")})
            return out
        ev = self.evidence()
        if r.kind == "S":
            s = ev.sentence(r.id, 0)
            if s is None:
                return out
            out = self._sentence_ref(s)
            out["requested_ref"] = ref
            if alias_status == "aliased" and out["status"] == "live":
                out["status"] = "aliased"
            return out
        if r.kind in ("D", "U"):
            doc_id = r.id if r.kind == "D" else r.id.split("/", 1)[0]
            d = ev.get_document(doc_id)
            if d is None:
                return out
            out.update({"doc_id": doc_id, "title": d.get("title"), "year": d.get("year"), "doi": d.get("doi"),
                        "unit_id": r.id if r.kind == "U" else doc_id,
                        "text": d.get("title") if r.kind == "D" else r.id.split("/", 1)[-1],
                        "status": alias_status, "authors": d.get("authors"), "venue": d.get("venue")})
            if r.kind == "U":
                units = {u["unit_id"]: u for u in ev.units_of(doc_id)}
                u = units.get(r.id)
                if u is None:
                    b = self.build_optional()
                    u = b.unit(r.id) if b is not None else None
                if u:
                    out.update({"heading": u.get("heading"), "page": u.get("page_from"), "unit_kind": u.get("unit_kind"),
                                "text": u.get("heading") or out["text"]})
            return out
        if r.kind == "N":
            n = self.user().get_note(r.id)
            if n is None:
                return out
            out.update({"title": n.get("title"), "text": _clip(n.get("title") or n.get("body_md")),
                        "status": alias_status, "note_id": r.id})
            return out
        k = self.user().get_collection(r.id)
        if k is None:
            return out
        out.update({"title": k.get("name"), "text": k.get("name"), "status": alias_status, "coll_id": r.id,
                    "n_items": len(k.get("items") or [])})
        return out

    # ---- concepts and frameworks (promoted build; NoBuildError → 409) ---------------------------------------
    def list_concepts(self, q: str | None = None, domain: str | None = None, tier: str | None = None,
                      type: str | None = None, sub: str | None = None, cat: str | None = None, scope: str = "all",
                      sort: str = "papers", dir: str | None = None, limit: int = 100, offset: int = 0,
                      ids: bool = False, kind: str = "concept") -> dict:
        """{rows: [{id, label, zh, type, mode, papers, coverage, topic_rate, tier, main_section, notes}], total, scope,
        sort, dir, kind}. `kind` = concept (default: the tiered concepts, origin 概念…) | other (the frequency-only
        phrase / journal-name entries) | all. With `ids` the whole filtered list in list order is returned instead
        of a page (`limit`/`offset` ignored): {ids: [...], labels: [...], total, scope, sort, dir} — what the UI
        keeps for prev/next."""
        b = self.build()
        if scope and scope != "all" and scope not in b.scope_ids():
            raise ValueError(f"unknown scope {scope!r}")
        if sort and sort not in CONCEPT_SORTS:
            raise ValueError(f"sort must be one of {', '.join(CONCEPT_SORTS)}")
        if dir and dir.lower() not in ("asc", "desc"):
            raise ValueError("dir must be asc | desc")
        if kind and kind not in CONCEPT_KINDS:
            raise ValueError(f"kind must be one of {', '.join(CONCEPT_KINDS)}")
        filters = dict(q=q, domain=domain, tier=tier, type=type, sub=sub, cat=cat, scope=scope or "all",
                       sort=sort or "papers", dir=dir or None, kind=kind or "concept")
        if ids:
            res = self._all_concepts(b, **filters)
            return {"ids": [r["id"] for r in res["rows"]], "labels": [r["label"] for r in res["rows"]],
                    "total": res["total"], "scope": res["scope"], "sort": res["sort"], "dir": res["dir"],
                    "build": b.build_id}
        res = b.list_concepts(limit=int(limit), offset=int(offset), **filters)
        counts = self.user().note_counts([f"C:{r['id']}" for r in res["rows"]])
        for r in res["rows"]:
            r["notes"] = counts.get(f"C:{r['id']}", 0)
        res["build"] = b.build_id
        return res

    def get_concept(self, concept_id: str, scope: str | None = None) -> dict:
        b = self.build()
        c = b.concept(concept_id)
        if c is None:
            raise KeyError(f"unknown concept {concept_id!r}")
        if scope and scope != "all" and scope not in b.scope_ids():
            raise ValueError(f"unknown scope {scope!r}")
        c["importance"] = b.importance_of(concept_id)
        c["scope"] = scope or "all"
        c["sections"] = b.sections_of(concept_id)
        c["domains_use"] = b.domains_use(concept_id)
        c["years"] = b.years_of(concept_id)
        c["year_totals"] = self.year_totals(b)
        c["categories"] = b.categories_of(concept_id)
        c["subdomains"] = b.subdomains_of(concept_id)
        c["anchor_of"] = b.anchor_of(concept_id)
        c["term_tree_places"] = b.term_tree_places(concept_id)
        c["examples"] = [self._example_ref(e, concept_id) for e in b.examples(concept_id, 6)]
        # what the "all sentences" view will report: sentences with any occurrence (nested included) and the
        # standalone ones — importance.occ counts standalone body occurrences, not sentences
        c["sentences"] = b.sentence_count(concept_id)
        c["sentences_standalone"] = b.sentence_count(concept_id, standalone=True)
        c["notes"] = self._notes_count(f"C:{concept_id}")
        c["build"] = b.build_id
        return c

    def _example_ref(self, e: dict, concept_id: str) -> dict:
        meta = self.build().doc_meta(e["doc_id"]) or {}
        return _ref_object(ref=f"S:{e['sid']}", kind="S", doc_id=e["doc_id"], title=meta.get("title"),
                           year=meta.get("year"), page=e.get("page_no"), sid=e["sid"], text=e.get("text"),
                           status="live", section=e.get("section"), spans=e.get("spans") or [],
                           tags=[t for t in (e.get("tags") or "").split(",") if t], concept_id=concept_id)

    def concept_sentences(self, concept_id: str, section: str | None = None, domain: str | None = None,
                          sub: str | None = None, doc: str | None = None, tag: str | None = None,
                          standalone: bool = False, sort: str = "doc", limit: int = 50, offset: int = 0) -> dict:
        """Every sentence with an occurrence of the concept: {rows: [{sid, doc_id, title, year, page, section, text,
        spans: [[cstart, cend, form, nested_in]], tags}], total}."""
        b = self.build()
        if b.concept(concept_id) is None:
            raise KeyError(f"unknown concept {concept_id!r}")
        if section and section not in SECTIONS:
            raise ValueError(f"unknown section {section!r}")
        res = b.concept_sentences(concept_id, section=section, domain=domain, sub=sub, doc=doc, tag=tag,
                                  standalone=bool(standalone), sort=sort or "doc", limit=int(limit), offset=int(offset))
        for r in res["rows"]:
            r["ref"] = f"S:{r['sid']}"
        res["concept_id"] = concept_id
        return res

    def concept_docs(self, concept_id: str, scope: str | None = None, use: str = "body", sort: str = "count",
                     limit: int = 50, offset: int = 0) -> dict:
        b = self.build()
        if b.concept(concept_id) is None:
            raise KeyError(f"unknown concept {concept_id!r}")
        if use not in ("body", "any", "refs"):
            raise ValueError("use must be body | any | refs")
        res = b.concept_docs(concept_id, scope=scope, use=use, sort=sort or "count", limit=int(limit), offset=int(offset))
        res["concept_id"] = concept_id
        return res

    def framework_terms(self) -> dict:
        b = self.build()
        return {"domains": b.term_tree(), "build": b.build_id}

    def framework_categories(self, domain: str | None = None, scope: str = "all") -> dict:
        b = self.build()
        return {"domains": b.categories_tree(domain or None, scope or "all"), "build": b.build_id}

    def framework_subdomains(self) -> dict:
        b = self.build()
        return {"groups": b.subdomain_tree(), "build": b.build_id}

    def get_subdomain(self, sub_id: str) -> dict:
        b = self.build()
        s = b.subdomain(sub_id)
        if s is None:
            raise KeyError(f"unknown sub-domain {sub_id!r}")
        s["notes"] = self._notes_count(f"SD:{sub_id}")
        s["build"] = b.build_id
        return s

    def subdomain_docs(self, sub_id: str, tier: str | None = None, q: str | None = None, sort: str = "hits",
                       limit: int = 50, offset: int = 0) -> dict:
        b = self.build()
        if b.subdomain_label(sub_id) is None:
            raise KeyError(f"unknown sub-domain {sub_id!r}")
        return b.subdomain_docs(sub_id, tier=tier, q=q, sort=sort or "hits", limit=int(limit), offset=int(offset))

    # ---- notes and collections (thin pass-through to UserStore, snapshots taken here)
    def _snapshot(self, ref: str) -> dict:
        obj = self.resolve(ref)
        return {"ref": ref, "snapshot_text": obj.get("text"), "snapshot_json": obj, "snapshot_build": self.build_label(),
                "status": obj.get("status")}

    def create_note(self, title: str, body_md: str, tags: list[str], target_refs: list[str]) -> dict:
        """Takes a snapshot (text, doc, page) for every target ref by resolving it first."""
        targets = []
        for ref in target_refs or []:
            ref = (ref or "").strip()
            if not ref:
                continue
            parse_ref(ref)
            targets.append(self._snapshot(ref))
        us = self.user()
        note = us.create_note(title=title or "", body_md=body_md or "", tags=list(tags or []), targets=targets)
        for t in targets:
            if t["status"] in ("aliased", "orphan"):
                resolved = t["snapshot_json"].get("ref") if t["status"] == "aliased" else None
                us.set_target_status(note["note_id"], t["ref"], t["status"], resolved)
        return us.get_note(note["note_id"]) or note

    def update_note(self, note_id: str, rev: int, title: str | None, body_md: str | None,
                    tags: list[str] | None, explicit: bool) -> dict:
        """Returns {'note': …} or {'conflict': True, 'server': …} (never raises for a stale rev)."""
        try:
            note = self.user().update_note(note_id, rev=int(rev), title=title, body_md=body_md,
                                           tags=list(tags) if tags is not None else None, explicit=bool(explicit))
        except ConflictError as e:
            return {"conflict": True, "server": e.server_note}
        return {"note": note}

    def get_note(self, note_id: str) -> dict:
        n = self.user().get_note(note_id)
        if n is None:
            raise KeyError(f"unknown note {note_id!r}")
        return n

    def delete_note(self, note_id: str) -> dict:
        self.get_note(note_id)
        self.user().delete_note(note_id)
        return {"deleted": note_id}

    def list_notes(self, q: str | None = None, tag: str | None = None, limit: int = 50, offset: int = 0) -> dict:
        return self.user().list_notes(q or None, tag or None, int(limit), int(offset))

    def notes_for(self, ref: str) -> list[dict]:
        ref = (ref or "").strip()
        parse_ref(ref)
        us = self.user()
        rows = us.notes_for_ref(ref)
        new_ref, status = us.resolve_ref(ref)
        if status == "aliased":
            seen = {n["note_id"] for n in rows}
            rows += [n for n in us.notes_for_ref(new_ref) if n["note_id"] not in seen]
        return rows

    def note_history(self, note_id: str) -> list[dict]:
        self.get_note(note_id)
        return self.user().history(note_id)

    def collections(self) -> list[dict]:
        return self.user().list_collections()

    def create_collection(self, name: str, description: str = "") -> dict:
        name = (name or "").strip()
        if not name:
            raise ValueError("collection name is required")
        return self.user().create_collection(name, description or "")

    def collection(self, coll_id: str) -> dict:
        k = self.user().get_collection(coll_id)
        if k is None:
            raise KeyError(f"unknown collection {coll_id!r}")
        return k

    def add_to_collection(self, coll_id: str, ref: str | None, comment_md: str = "", after_item: str | None = None) -> dict:
        """Snapshot = resolve(ref) when ref is given."""
        ref = (ref or "").strip() or None
        snapshot = self.resolve(ref) if ref else None
        return self.user().add_item(coll_id, ref, snapshot, comment_md or "", after_item or None)

    def update_item(self, item_id: str, comment_md: str | None = None, seq: float | None = None) -> dict:
        us = self.user()
        if us.get_item(item_id) is None:
            raise KeyError(f"unknown item {item_id!r}")
        us.update_item(item_id, comment_md=comment_md, seq=float(seq) if seq is not None else None)
        return us.get_item(item_id) or {}

    def remove_item(self, item_id: str) -> dict:
        us = self.user()
        if us.get_item(item_id) is None:
            raise KeyError(f"unknown item {item_id!r}")
        us.remove_item(item_id)
        return {"removed": item_id}

    def export_collection_md(self, coll_id: str) -> str:
        """Markdown: title, description, items in order (quoted text with '[doc_id p.N]' pointers and the comment),
        then a numbered reference list (authors, year, title, venue, DOI link) built from document_meta."""
        k = self.collection(coll_id)
        ref_order: list[str] = []
        lines = [f"# {k['name'] or coll_id}", ""]
        if k.get("description"):
            lines += [k["description"].strip(), ""]
        lines += [f"<!-- collection {coll_id}; exported from concept-atlas {atlas.__version__} -->", ""]
        for i, item in enumerate(k.get("items") or [], start=1):
            snap = item.get("snapshot") or {}
            doc_id = snap.get("doc_id")
            if doc_id and doc_id not in ref_order:
                ref_order.append(doc_id)
            head = f"{i}."
            if item.get("ref"):
                pointer = ""
                if doc_id:
                    pointer = f"[{doc_id}"
                    if snap.get("page"):
                        pointer += f" p.{snap['page']}"
                    pointer += "]"
                    pointer += f" [{ref_order.index(doc_id) + 1}]"
                lines.append(f"{head} `{item['ref']}` {pointer}".rstrip())
                text = snap.get("text") or snap.get("title")
                if text:
                    for ln in str(text).splitlines() or [""]:
                        lines.append(f"   > {ln}")
            else:
                lines.append(f"{head} (comment)")
            comment = (item.get("comment_md") or "").strip()
            if comment:
                lines.append("")
                lines += ["   " + ln if ln else "" for ln in comment.splitlines()]
            lines.append("")
        lines += ["## References", ""]
        if not ref_order:
            lines.append("(none)")
        for n, doc_id in enumerate(ref_order, start=1):
            m = self._doc_meta(doc_id)
            parts = []
            if m.get("authors"):
                parts.append(str(m["authors"]).strip())
            if m.get("year"):
                parts.append(f"({m['year']})")
            parts.append((m.get("title") or doc_id).strip() + ".")
            if m.get("venue"):
                parts.append(str(m["venue"]).strip() + ".")
            if m.get("doi"):
                doi = str(m["doi"]).strip()
                url = doi if doi.startswith("http") else f"https://doi.org/{doi}"
                parts.append(f"[{doi}]({url})")
            parts.append(f"`{doc_id}`")
            lines.append(f"{n}. " + " ".join(parts))
        lines.append("")
        return "\n".join(lines)
