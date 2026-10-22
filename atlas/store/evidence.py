"""EvidenceStore: repository over evidence.sqlite. Implemented by: increment 1, agent "store".

All methods return plain dicts/lists (JSON-serialisable) or dataclasses from atlas.core.schemas; no sqlite3.Row
leaks out. Write methods are used only by the ingest stage; the server opens the store read-only.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from atlas.core.ids import text_sha256
from atlas.core.schemas import DocMeta, LineMask, PageText, SentenceRow, Unit
from atlas.store.db import open_evidence, transaction

log = logging.getLogger(__name__)

_META_FIELDS = ("title", "year", "authors", "doi", "venue", "keywords", "abstract", "lang")
_SNIPPET_TOKENS = 40


def _now() -> str:
    return datetime.fromtimestamp(time.time(), tz=timezone.utc).isoformat(timespec="seconds")


def _wrap(items: Iterable[str]) -> str:
    """('a', 'b') → ',a,b,' ; () → ''."""
    xs = [str(x).strip() for x in items if str(x).strip()]
    return f",{','.join(xs)}," if xs else ""


def _unwrap(s: str | None) -> list[str]:
    return [x for x in (s or "").split(",") if x]


def _fts_phrase(phrase: str) -> str:
    """Quote a user phrase for FTS5 MATCH (no operator syntax leaks through)."""
    return '"' + (phrase or "").replace('"', "").strip() + '"'


def _fts_terms(q: str) -> str:
    """Each whitespace token quoted; implicit AND. '' when nothing is left."""
    toks = [t.replace('"', "") for t in (q or "").split()]
    toks = [t for t in toks if t]
    return " ".join(f'"{t}"' for t in toks)


def _row(r: sqlite3.Row | None) -> dict | None:
    return dict(r) if r is not None else None


class EvidenceStore:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    @classmethod
    def open(cls, path: str | Path, mode: str = "rw") -> "EvidenceStore":
        return cls(open_evidence(path, mode))

    def close(self) -> None:
        self.conn.close()

    # ---- documents ----------------------------------------------------------------------------------------
    def add_document(self, doc_id: str, doc_type: str, source: str, pdf_path: Path, rel_path: str,
                     pdf_sha256: str, meta: DocMeta | None = None) -> None:
        """Insert documents/document_files/document_meta/document_fts (idempotent: a known doc_id with a new
        pdf_sha256 adds a document_files row and marks it current; a known sha becomes the current file again
        (its rel_path refreshed) — e.g. when the PDF on disk was reverted to an earlier version)."""
        now = _now()
        size: int | None = None
        try:
            if pdf_path is not None and Path(pdf_path).is_file():
                size = Path(pdf_path).stat().st_size
        except OSError:
            size = None
        with transaction(self.conn) as c:
            c.execute("INSERT OR IGNORE INTO documents(doc_id, doc_type, added, source) VALUES (?,?,?,?)",
                      (doc_id, doc_type or "paper", now, source))
            have = c.execute("SELECT 1 FROM document_files WHERE doc_id=? AND pdf_sha256=?",
                             (doc_id, pdf_sha256)).fetchone()
            # the file with this sha is the current one, whether or not it is already known (review fix)
            c.execute("UPDATE document_files SET is_current = CASE WHEN pdf_sha256 = ? THEN 1 ELSE 0 END "
                      "WHERE doc_id=?", (pdf_sha256, doc_id))
            if have is None:
                c.execute("INSERT INTO document_files(doc_id, pdf_sha256, rel_path, size, added, is_current) "
                          "VALUES (?,?,?,?,?,1)", (doc_id, pdf_sha256, rel_path, size, now))
            else:
                c.execute("UPDATE document_files SET rel_path=? WHERE doc_id=? AND pdf_sha256=?",
                          (rel_path, doc_id, pdf_sha256))
            c.execute("INSERT OR IGNORE INTO document_meta(doc_id, updated) VALUES (?,?)", (doc_id, now))
            if c.execute("SELECT 1 FROM document_fts WHERE doc_id=?", (doc_id,)).fetchone() is None:
                c.execute("INSERT INTO document_fts(doc_id, title, authors, keywords) VALUES (?,?,?,?)",
                          (doc_id, "", "", ""))
            if meta is not None:
                self._merge_meta(c, doc_id, meta, now)

    def _merge_meta(self, c: sqlite3.Connection, doc_id: str, meta: DocMeta, now: str) -> None:
        sets, vals = [], []
        for f in _META_FIELDS:
            v = getattr(meta, f, None)
            if v is not None:
                sets.append(f"{f}=?")
                vals.append(v)
        if meta.extra:
            cur = c.execute("SELECT extra_json FROM document_meta WHERE doc_id=?", (doc_id,)).fetchone()
            try:
                old = json.loads(cur["extra_json"]) if cur and cur["extra_json"] else {}
            except ValueError:
                old = {}
            if not isinstance(old, dict):
                old = {}
            old.update(meta.extra)
            sets.append("extra_json=?")
            vals.append(json.dumps(old, ensure_ascii=False, sort_keys=True))
        sets.append("updated=?")
        vals.append(now)
        c.execute("INSERT OR IGNORE INTO document_meta(doc_id, updated) VALUES (?,?)", (doc_id, now))
        c.execute(f"UPDATE document_meta SET {', '.join(sets)} WHERE doc_id=?", (*vals, doc_id))
        m = c.execute("SELECT title, authors, keywords FROM document_meta WHERE doc_id=?", (doc_id,)).fetchone()
        c.execute("DELETE FROM document_fts WHERE doc_id=?", (doc_id,))
        c.execute("INSERT INTO document_fts(doc_id, title, authors, keywords) VALUES (?,?,?,?)",
                  (doc_id, m["title"] or "", m["authors"] or "", m["keywords"] or ""))

    def upsert_meta(self, doc_id: str, meta: DocMeta) -> None:
        """Merge non-None fields into document_meta and refresh document_fts."""
        with transaction(self.conn) as c:
            self._merge_meta(c, doc_id, meta, _now())

    _DOC_SQL = """
        SELECT d.doc_id, d.doc_type, d.added, d.source,
               m.title, m.year, m.authors, m.doi, m.venue, m.keywords, m.abstract, m.lang, m.extra_json,
               f.pdf_sha256, f.rel_path,
               e.ext_id, e.n_pages, e.n_sentences
        FROM documents d
        LEFT JOIN document_meta m ON m.doc_id = d.doc_id
        LEFT JOIN document_files f ON f.doc_id = d.doc_id AND f.is_current = 1
        LEFT JOIN extractions e ON e.doc_id = d.doc_id AND e.is_current = 1
    """

    @staticmethod
    def _doc_dict(r: sqlite3.Row) -> dict:
        d = dict(r)
        try:
            extra = json.loads(d.pop("extra_json") or "{}")
        except ValueError:
            extra = {}
        d["extra"] = extra if isinstance(extra, dict) else {}
        d["n_pages"] = int(d["n_pages"] or 0)
        d["n_sentences"] = int(d["n_sentences"] or 0)
        return d

    def get_document(self, doc_id: str) -> dict | None:
        """{doc_id, doc_type, added, source, title, year, authors, doi, venue, keywords, abstract, lang,
        pdf_sha256, rel_path, ext_id (current extraction or None), n_pages, n_sentences}"""
        r = self.conn.execute(self._DOC_SQL + " WHERE d.doc_id = ?", (doc_id,)).fetchone()
        return self._doc_dict(r) if r is not None else None

    def list_documents(self, q: str | None = None, limit: int = 100, offset: int = 0) -> dict:
        """{'rows': [get_document-shaped dicts], 'total': n}; `q` searches document_fts (title/authors/keywords)
        and falls back to LIKE on doc_id; sorted by year desc, doc_id."""
        order = " ORDER BY m.year DESC, d.doc_id LIMIT ? OFFSET ?"
        q = (q or "").strip()
        if not q:
            total = self.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
            rows = self.conn.execute(self._DOC_SQL + order, (limit, offset)).fetchall()
            return {"rows": [self._doc_dict(r) for r in rows], "total": int(total)}
        ids: set[str] = set()
        terms = _fts_terms(q)
        if terms:
            try:
                ids.update(r[0] for r in self.conn.execute(
                    "SELECT doc_id FROM document_fts WHERE document_fts MATCH ?", (terms,)))
            except sqlite3.OperationalError as e:  # pragma: no cover - defensive
                log.warning("document_fts MATCH failed for %r: %s", q, e)
        like = "%" + q.replace("%", "\\%").replace("_", "\\_") + "%"
        ids.update(r[0] for r in self.conn.execute(
            "SELECT doc_id FROM documents WHERE doc_id LIKE ? ESCAPE '\\'", (like,)))
        if not ids:
            return {"rows": [], "total": 0}
        marks = ",".join("?" * len(ids))
        id_list = sorted(ids)
        rows = self.conn.execute(self._DOC_SQL + f" WHERE d.doc_id IN ({marks})" + order,
                                 (*id_list, limit, offset)).fetchall()
        return {"rows": [self._doc_dict(r) for r in rows], "total": len(ids)}

    def known_files(self) -> dict[str, str]:
        """{rel_path: doc_id} over document_files (current files only); used by `atlas docs add` to skip PDFs that
        are already registered."""
        rows = self.conn.execute("SELECT rel_path, doc_id FROM document_files WHERE is_current=1").fetchall()
        return {r["rel_path"]: r["doc_id"] for r in rows}

    def max_doc_seq(self, prefix: str = "D") -> int:
        """Largest numeric suffix among doc_ids of the form <prefix><digits> (0 when none); the next scanned
        document gets prefix + 4-digit (seq + 1)."""
        best = 0
        rows = self.conn.execute("SELECT doc_id FROM documents WHERE doc_id LIKE ?", (prefix + "%",)).fetchall()
        for r in rows:
            tail = r["doc_id"][len(prefix):]
            if tail.isdigit():
                best = max(best, int(tail))
        return best

    def stats(self) -> dict:
        """{documents, extractions, pages, sentences, body_sentences}"""
        c = self.conn
        one = lambda sql, *a: int(c.execute(sql, a).fetchone()[0])  # noqa: E731
        return {
            "documents": one("SELECT COUNT(*) FROM documents"),
            "extractions": one("SELECT COUNT(*) FROM extractions"),
            "pages": one("SELECT COUNT(*) FROM pages p JOIN extractions e ON e.ext_id=p.ext_id "
                         "WHERE e.is_current=1"),
            "sentences": one("SELECT COUNT(*) FROM extraction_sentences es JOIN extractions e "
                             "ON e.ext_id=es.ext_id WHERE e.is_current=1"),
            "body_sentences": one("SELECT COUNT(*) FROM extraction_sentences es JOIN extractions e "
                                  "ON e.ext_id=es.ext_id WHERE e.is_current=1 AND es.is_body=1"),
        }

    # ---- extractions --------------------------------------------------------------------------------------
    def extraction_exists(self, ext_id: str) -> bool:
        return self.conn.execute("SELECT 1 FROM extractions WHERE ext_id=?", (ext_id,)).fetchone() is not None

    @staticmethod
    def _ext_dict(r: sqlite3.Row) -> dict:
        d = dict(r)
        try:
            d["stats"] = json.loads(d.get("stats_json") or "{}")
        except ValueError:
            d["stats"] = {}
        d["is_current"] = bool(d.get("is_current"))
        return d

    def current_extraction(self, doc_id: str) -> dict | None:
        """The is_current extraction row of a document (dict) or None."""
        r = self.conn.execute("SELECT * FROM extractions WHERE doc_id=? AND is_current=1 "
                              "ORDER BY created DESC LIMIT 1", (doc_id,)).fetchone()
        return self._ext_dict(r) if r is not None else None

    def get_extraction(self, ext_id: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM extractions WHERE ext_id=?", (ext_id,)).fetchone()
        return self._ext_dict(r) if r is not None else None

    def add_extraction(self, *, ext_id: str, doc_id: str, pdf_sha256: str, extractor_version: str, doc_type: str,
                       pages: Sequence[PageText], masks: Sequence[LineMask], units: Sequence[Unit],
                       sentences: Sequence[SentenceRow], text_ratio: float, drop_ratio: float,
                       stats: dict | None = None) -> None:
        """One transaction: extractions row; page_text (INSERT OR IGNORE by sha) + pages; line_masks; units;
        sentences (INSERT OR IGNORE: first occurrence wins) + extraction_sentences; previous extractions of the
        document get is_current = 0.

        Re-adding an existing ext_id (`--reextract` with unchanged inputs) replaces that extraction's rows."""
        now = _now()
        with transaction(self.conn) as c:
            if c.execute("SELECT 1 FROM documents WHERE doc_id=?", (doc_id,)).fetchone() is None:
                raise KeyError(f"unknown document {doc_id!r}; call add_document first")
            if c.execute("SELECT 1 FROM extractions WHERE ext_id=?", (ext_id,)).fetchone() is not None:
                log.info("extraction %s exists; replacing its rows", ext_id)
                for t in ("extraction_sentences", "units", "line_masks", "pages"):
                    c.execute(f"DELETE FROM {t} WHERE ext_id=?", (ext_id,))
                c.execute("DELETE FROM extractions WHERE ext_id=?", (ext_id,))
            c.execute("UPDATE extractions SET is_current=0 WHERE doc_id=?", (doc_id,))
            c.execute(
                "INSERT INTO extractions(ext_id, doc_id, pdf_sha256, extractor_version, doc_type, n_pages, "
                "n_sentences, text_ratio, drop_ratio, created, stats_json, is_current) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,1)",
                (ext_id, doc_id, pdf_sha256, extractor_version, doc_type, len(pages), len(sentences),
                 float(text_ratio), float(drop_ratio), now,
                 json.dumps(stats or {}, ensure_ascii=False, sort_keys=True)))
            for p in pages:
                sha = text_sha256(p.text)
                c.execute("INSERT OR IGNORE INTO page_text(text_sha256, text) VALUES (?,?)", (sha, p.text))
                c.execute("INSERT INTO pages(ext_id, page_no, text_sha256, width, height) VALUES (?,?,?,?,?)",
                          (ext_id, int(p.page_no), sha, float(p.width), float(p.height)))
            c.executemany("INSERT OR IGNORE INTO line_masks(ext_id, page_no, cstart, cend, reason) "
                          "VALUES (?,?,?,?,?)",
                          [(ext_id, m.page_no, m.cstart, m.cend, m.reason) for m in masks])
            c.executemany(
                "INSERT INTO units(ext_id, unit_id, doc_id, kind, ordinal, heading, page_from, page_to, "
                "confidence, reasons, parent_unit, unit_kind) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                [(ext_id, u.unit_id, u.doc_id, u.kind, u.ordinal, u.heading, u.page_from, u.page_to,
                  u.confidence, ",".join(u.reasons) if u.reasons else None, u.parent_unit, u.unit_kind)
                 for u in units])
            c.executemany("INSERT OR IGNORE INTO sentences(sid, doc_id, text, first_ext) VALUES (?,?,?,?)",
                          [(s.sid, s.doc_id, s.text, ext_id) for s in sentences])
            c.executemany(
                "INSERT OR IGNORE INTO extraction_sentences(ext_id, sid, seq, unit_id, page_no, cstart, cend, "
                "section, is_body, tags, flags) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                [(ext_id, s.sid, s.seq, s.unit_id, s.page_no, s.cstart, s.cend, s.section,
                  1 if s.is_body else 0, _wrap(s.tags), _wrap(s.flags)) for s in sentences])
        log.info("extraction %s for %s: %d pages, %d sentences", ext_id, doc_id, len(pages), len(sentences))

    def rebuild_fts(self) -> None:
        """Rebuild sentence_fts and page_fts from their content tables. Only needed after a VACUUM, which may
        renumber the rowids the external-content FTS tables are keyed on (see evidence.sql)."""
        with transaction(self.conn) as c:
            c.execute("INSERT INTO sentence_fts(sentence_fts) VALUES ('rebuild')")
            c.execute("INSERT INTO page_fts(page_fts) VALUES ('rebuild')")
        log.info("FTS indexes rebuilt")

    @contextmanager
    def bulk_writes(self) -> Iterator[None]:
        """Bulk-load mode for many add_extraction() calls: the WAL auto-checkpoint is switched off (each
        per-document checkpoint rewrote thousands of FTS pages into the main file — seconds on a hard disk) and
        one TRUNCATE checkpoint runs at the end; the normal setting is restored even on error."""
        self.conn.execute("PRAGMA wal_autocheckpoint = 0")
        try:
            yield
        finally:
            try:
                self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                self.conn.execute("PRAGMA wal_autocheckpoint = 1000")

    def add_aliases(self, rows: Iterable[tuple[str, str, str, str, float]]) -> None:
        """(old_sid, new_sid, from_ext, to_ext, overlap) rows, INSERT OR IGNORE."""
        with transaction(self.conn) as c:
            c.executemany("INSERT OR IGNORE INTO sentence_aliases(old_sid, new_sid, from_ext, to_ext, overlap) "
                          "VALUES (?,?,?,?,?)", [tuple(r) for r in rows])

    def sentences_of_extraction(self, ext_id: str) -> list[SentenceRow]:
        rows = self.conn.execute(
            "SELECT s.sid, s.doc_id, s.text, es.ext_id, es.seq, es.unit_id, es.page_no, es.cstart, es.cend, "
            "es.section, es.is_body, es.tags, es.flags "
            "FROM extraction_sentences es JOIN sentences s ON s.sid = es.sid "
            "WHERE es.ext_id = ? ORDER BY es.seq", (ext_id,)).fetchall()
        return [SentenceRow(sid=r["sid"], doc_id=r["doc_id"], text=r["text"], ext_id=r["ext_id"], seq=r["seq"],
                            unit_id=r["unit_id"], page_no=r["page_no"], cstart=r["cstart"], cend=r["cend"],
                            section=r["section"], is_body=bool(r["is_body"]), tags=tuple(_unwrap(r["tags"])),
                            flags=tuple(_unwrap(r["flags"]))) for r in rows]

    # ---- reading ------------------------------------------------------------------------------------------
    def _current_ext_id(self, doc_id: str) -> str | None:
        r = self.conn.execute("SELECT ext_id FROM extractions WHERE doc_id=? AND is_current=1 "
                              "ORDER BY created DESC LIMIT 1", (doc_id,)).fetchone()
        return r[0] if r else None

    def pages_of(self, doc_id: str) -> list[dict]:
        """[{page_no, width, height, n_sentences}] for the current extraction."""
        ext_id = self._current_ext_id(doc_id)
        if ext_id is None:
            return []
        rows = self.conn.execute(
            "SELECT p.page_no, p.width, p.height, "
            "(SELECT COUNT(*) FROM extraction_sentences es WHERE es.ext_id=p.ext_id AND es.page_no=p.page_no) "
            "AS n_sentences FROM pages p WHERE p.ext_id=? ORDER BY p.page_no", (ext_id,)).fetchall()
        return [dict(r) for r in rows]

    def page(self, doc_id: str, page_no: int) -> dict | None:
        """{doc_id, ext_id, page_no, text, width, height, masks:[{cstart,cend,reason}],
        sentences:[{sid, seq, cstart, cend, section, is_body, tags:[...], flags:[...], text}]} — `text` of a
        sentence is the stored (normalised) sentence text, not the raw page slice."""
        ext_id = self._current_ext_id(doc_id)
        if ext_id is None:
            return None
        p = self.conn.execute(
            "SELECT p.page_no, p.width, p.height, t.text FROM pages p JOIN page_text t "
            "ON t.text_sha256 = p.text_sha256 WHERE p.ext_id=? AND p.page_no=?", (ext_id, int(page_no))).fetchone()
        if p is None:
            return None
        masks = self.conn.execute("SELECT cstart, cend, reason FROM line_masks WHERE ext_id=? AND page_no=? "
                                  "ORDER BY cstart", (ext_id, int(page_no))).fetchall()
        sents = self.conn.execute(
            "SELECT es.sid, es.seq, es.cstart, es.cend, es.section, es.is_body, es.tags, es.flags, s.text "
            "FROM extraction_sentences es JOIN sentences s ON s.sid = es.sid "
            "WHERE es.ext_id=? AND es.page_no=? ORDER BY es.cstart, es.seq", (ext_id, int(page_no))).fetchall()
        return {
            "doc_id": doc_id, "ext_id": ext_id, "page_no": int(p["page_no"]), "text": p["text"],
            "width": p["width"], "height": p["height"],
            "masks": [dict(m) for m in masks],
            "sentences": [{"sid": s["sid"], "seq": s["seq"], "cstart": s["cstart"], "cend": s["cend"],
                           "section": s["section"], "is_body": bool(s["is_body"]),
                           "tags": _unwrap(s["tags"]), "flags": _unwrap(s["flags"]), "text": s["text"]}
                          for s in sents],
        }

    def resolve_sid(self, sid: str) -> tuple[str, str]:
        """(newest_sid, status) following alias chains; ('', 'missing') when unknown."""
        c = self.conn
        known = lambda s: c.execute("SELECT 1 FROM sentences WHERE sid=?", (s,)).fetchone() is not None  # noqa: E731
        if not sid:
            return "", "missing"
        cur = sid
        seen = {cur}
        hops = 0
        while True:
            nxt = c.execute(
                "SELECT a.new_sid FROM sentence_aliases a LEFT JOIN extractions e ON e.ext_id = a.to_ext "
                "WHERE a.old_sid=? AND a.new_sid<>a.old_sid "
                "ORDER BY e.is_current DESC, a.overlap DESC, e.created DESC, a.new_sid LIMIT 1", (cur,)).fetchone()
            if nxt is None or nxt[0] in seen:
                break
            cur = nxt[0]
            seen.add(cur)
            hops += 1
        if not known(cur):
            return "", "missing"
        return cur, ("aliased" if hops else "live")

    def _membership(self, sid: str) -> sqlite3.Row | None:
        """The extraction_sentences row of `sid`, preferring the document's current extraction."""
        return self.conn.execute(
            "SELECT es.*, e.is_current, e.created FROM extraction_sentences es JOIN extractions e "
            "ON e.ext_id = es.ext_id WHERE es.sid=? ORDER BY e.is_current DESC, e.created DESC LIMIT 1",
            (sid,)).fetchone()

    def sentence(self, sid: str, context: int = 1) -> dict | None:
        """Resolve a sid (following sentence_aliases to the newest sid first):
        {sid, requested_sid, status live|aliased|missing, doc_id, ext_id, unit_id, page_no, cstart, cend, section,
        is_body, tags, flags, text, before:[texts], after:[texts], doc:{title, year, doi}}"""
        new_sid, status = self.resolve_sid(sid)
        if status == "missing":
            return None
        s = self.conn.execute("SELECT sid, doc_id, text FROM sentences WHERE sid=?", (new_sid,)).fetchone()
        es = self._membership(new_sid)
        out: dict = {"sid": new_sid, "requested_sid": sid, "status": status, "doc_id": s["doc_id"],
                     "ext_id": None, "unit_id": None, "page_no": None, "cstart": None, "cend": None,
                     "section": None, "is_body": None, "tags": [], "flags": [], "text": s["text"],
                     "before": [], "after": []}
        if es is not None:
            out.update({"ext_id": es["ext_id"], "unit_id": es["unit_id"], "page_no": es["page_no"],
                        "cstart": es["cstart"], "cend": es["cend"], "section": es["section"],
                        "is_body": bool(es["is_body"]), "tags": _unwrap(es["tags"]),
                        "flags": _unwrap(es["flags"])})
            k = max(0, int(context))
            if k:
                out["before"] = [r[0] for r in self.conn.execute(
                    "SELECT s.text FROM extraction_sentences x JOIN sentences s ON s.sid=x.sid "
                    "WHERE x.ext_id=? AND x.seq>=? AND x.seq<? ORDER BY x.seq",
                    (es["ext_id"], es["seq"] - k, es["seq"]))]
                out["after"] = [r[0] for r in self.conn.execute(
                    "SELECT s.text FROM extraction_sentences x JOIN sentences s ON s.sid=x.sid "
                    "WHERE x.ext_id=? AND x.seq>? AND x.seq<=? ORDER BY x.seq",
                    (es["ext_id"], es["seq"], es["seq"] + k))]
        m = self.conn.execute("SELECT title, year, doi FROM document_meta WHERE doc_id=?",
                              (s["doc_id"],)).fetchone()
        out["doc"] = dict(m) if m is not None else {"title": None, "year": None, "doi": None}
        return out

    def phrase_search(self, phrase: str, doc_id: str | None = None, limit: int = 50, offset: int = 0) -> dict:
        """FTS5 phrase search over sentences (current extractions only): {'rows': [{sid, doc_id, page_no, section,
        is_body, snippet, title, year}], 'total': n, 'documents': n_distinct_docs}. The phrase is quoted (no FTS
        syntax from users)."""
        empty = {"rows": [], "total": 0, "documents": 0}
        q = _fts_phrase(phrase)
        if q == '""':
            return empty
        base = (
            "FROM (SELECT rowid AS rid, snippet(sentence_fts, 0, '<mark>', '</mark>', '…', ?) AS snip "
            "      FROM sentence_fts WHERE sentence_fts MATCH ?) f "
            "JOIN sentences s ON s.rowid = f.rid "
            "JOIN extraction_sentences es ON es.sid = s.sid "
            "JOIN extractions e ON e.ext_id = es.ext_id AND e.is_current = 1 "
            "LEFT JOIN document_meta m ON m.doc_id = s.doc_id "
        )
        params: list = [_SNIPPET_TOKENS, q]
        where = ""
        if doc_id:
            where = "WHERE s.doc_id = ? "
            params.append(doc_id)
        try:
            tot = self.conn.execute("SELECT COUNT(*), COUNT(DISTINCT s.doc_id) " + base + where, params).fetchone()
            rows = self.conn.execute(
                "SELECT s.sid, s.doc_id, es.page_no, es.section, es.is_body, f.snip AS snippet, m.title, m.year "
                + base + where + "ORDER BY m.year DESC, s.doc_id, es.page_no, es.cstart LIMIT ? OFFSET ?",
                (*params, limit, offset)).fetchall()
        except sqlite3.OperationalError as e:
            log.warning("phrase_search failed for %r: %s", phrase, e)
            return empty
        return {"rows": [{"sid": r["sid"], "doc_id": r["doc_id"], "page_no": r["page_no"], "section": r["section"],
                          "is_body": bool(r["is_body"]), "snippet": r["snippet"], "title": r["title"],
                          "year": r["year"]} for r in rows],
                "total": int(tot[0]), "documents": int(tot[1])}

    def page_search(self, phrase: str, doc_id: str | None = None, limit: int = 50, offset: int = 0) -> dict:
        """Same over page_fts (for text the sentence splitter may have cut): rows {doc_id, page_no, snippet}."""
        empty = {"rows": [], "total": 0, "documents": 0}
        q = _fts_phrase(phrase)
        if q == '""':
            return empty
        base = (
            "FROM (SELECT rowid AS rid, snippet(page_fts, 0, '<mark>', '</mark>', '…', ?) AS snip "
            "      FROM page_fts WHERE page_fts MATCH ?) f "
            "JOIN page_text t ON t.rowid = f.rid "
            "JOIN pages p ON p.text_sha256 = t.text_sha256 "
            "JOIN extractions e ON e.ext_id = p.ext_id AND e.is_current = 1 "
        )
        params: list = [_SNIPPET_TOKENS, q]
        where = ""
        if doc_id:
            where = "WHERE e.doc_id = ? "
            params.append(doc_id)
        try:
            tot = self.conn.execute("SELECT COUNT(*), COUNT(DISTINCT e.doc_id) " + base + where, params).fetchone()
            rows = self.conn.execute(
                "SELECT e.doc_id, p.page_no, f.snip AS snippet " + base + where +
                "ORDER BY e.doc_id, p.page_no LIMIT ? OFFSET ?", (*params, limit, offset)).fetchall()
        except sqlite3.OperationalError as e:
            log.warning("page_search failed for %r: %s", phrase, e)
            return empty
        return {"rows": [dict(r) for r in rows], "total": int(tot[0]), "documents": int(tot[1])}
