"""BuildStore: repository over one `builds/b<date>_<seq>/build.sqlite` (increment 2 §2).

A build folder holds `build.sqlite`, `manifest.json`, `curated/` (snapshot of the project's curated files) and,
after `finish`, `report.md`. The store pins the evidence (sentence ids, units, document meta — no text), loads
the scheme tables from the snapshot, receives the stage outputs and answers every read query of the service.
Sentence text is never copied: readers that need it attach `evidence.sqlite` read-only (`evidence_path`).

Implemented by: increment 2 (derived layer). Only `store/db.py` connects; this module uses its helpers.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import stat
import time
from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
from pathlib import Path

from atlas.core.schemas import SECTIONS
from atlas.core.stats import is_tiered
from atlas.store.curated import CURATED_FILES, curated_shas, load_curated
from atlas.store.db import BUILD_SCHEMA_VERSION, attach, open_build, transaction, upgrade_build, user_version

log = logging.getLogger(__name__)

MANIFEST = "manifest.json"
DB_FILE = "build.sqlite"
REPORT = "report.md"
CURRENT_FILE = "current.txt"
BUILD_ID_RE = re.compile(r"^b(\d{4}-\d{2}-\d{2})_(\d{3})$")
TOPIC_FIELDS = ("title", "keywords", "abstract")
SECTION_ORDER = {s: i for i, s in enumerate(SECTIONS)}
SECTION_ORDER["body"] = len(SECTIONS)
_IN_CHUNK = 400


def _now() -> str:
    return datetime.fromtimestamp(time.time(), tz=timezone.utc).isoformat(timespec="seconds")


def _loads(s: str | None, default):
    if not s:
        return default
    try:
        return json.loads(s)
    except ValueError:
        return default


def _dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _like(s: str) -> str:
    return "%" + s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def new_build_id(builds_dir: str | Path, today: str | None = None) -> str:
    """b<YYYY-MM-DD>_<seq3>, seq = 1 + the highest sequence number among the existing folders of that day (not
    their count: after b<day>_001 is deleted while _002 exists, the next id is _003 — the folder is write-once)."""
    day = today or datetime.now().strftime("%Y-%m-%d")
    d = Path(builds_dir)
    seqs: list[int] = []
    if d.is_dir():
        for p in d.iterdir():
            m = BUILD_ID_RE.match(p.name)
            if p.is_dir() and m and m.group(1) == day:
                seqs.append(int(m.group(2)))
    return f"b{day}_{max(seqs, default=0) + 1:03d}"


def list_build_dirs(builds_dir: str | Path) -> list[Path]:
    d = Path(builds_dir)
    if not d.is_dir():
        return []
    return sorted((p for p in d.iterdir() if p.is_dir() and BUILD_ID_RE.match(p.name)), key=lambda p: p.name)


def read_manifest(build_dir: str | Path) -> dict | None:
    p = Path(build_dir) / MANIFEST
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return None


def write_manifest(build_dir: str | Path, manifest: dict) -> None:
    p = Path(build_dir) / MANIFEST
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, p)


def build_schema_version(build_dir: str | Path) -> int:
    """`PRAGMA user_version` of a build folder's build.sqlite (read-only, no migration)."""
    conn = open_build(Path(build_dir) / DB_FILE, "ro")
    try:
        return user_version(conn)
    finally:
        conn.close()


def current_build_id(builds_dir: str | Path) -> str | None:
    p = Path(builds_dir) / CURRENT_FILE
    if not p.is_file():
        return None
    s = p.read_text(encoding="utf-8").strip()
    return s or None


def set_current_build(builds_dir: str | Path, build_id: str) -> None:
    d = Path(builds_dir)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / (CURRENT_FILE + ".tmp")
    tmp.write_text(build_id + "\n", encoding="utf-8")
    os.replace(tmp, d / CURRENT_FILE)


class BuildStore:
    def __init__(self, conn: sqlite3.Connection, build_dir: Path, evidence_attached: bool = False):
        self.conn = conn
        self.build_dir = Path(build_dir)
        self.evidence_attached = evidence_attached
        self.schema_version = user_version(conn)

    @property
    def schema_current(self) -> bool:
        """False for a build file written by an older schema (the service refuses to serve it until
        `atlas build upgrade <id>` has brought it to the current schema)."""
        return self.schema_version >= BUILD_SCHEMA_VERSION

    # ---- open / create --------------------------------------------------------------------------------------
    @classmethod
    def open(cls, build_dir: str | Path, mode: str = "rw", evidence_path: str | Path | None = None) -> "BuildStore":
        build_dir = Path(build_dir)
        conn = open_build(build_dir / DB_FILE, mode)
        attached = False
        if evidence_path is not None and Path(evidence_path).is_file():
            attach(conn, "ev", evidence_path, "ro")
            attached = True
        return cls(conn, build_dir, attached)

    @classmethod
    def create(cls, builds_dir: str | Path, build_id: str, project_name: str, curated_dir: str | Path,
               note: str | None = None) -> "BuildStore":
        """Make the folder (refuses to overwrite), copy the curated snapshot, write manifest.json (status open)
        and open the empty build.sqlite."""
        build_dir = Path(builds_dir) / build_id
        if build_dir.exists():
            raise FileExistsError(f"build folder exists: {build_dir}")
        build_dir.mkdir(parents=True)
        snap = build_dir / "curated"
        snap.mkdir()
        src = Path(curated_dir)
        for name, fn in CURATED_FILES.items():
            if (src / fn).is_file():
                shutil.copy2(src / fn, snap / fn)
        if (src / "confirmations.json").is_file():
            shutil.copy2(src / "confirmations.json", snap / "confirmations.json")
        manifest = {"build_id": build_id, "created": _now(), "status": "open", "project": project_name,
                    "note": note or "", "evidence_sha_at_pin": None, "curated": curated_shas(snap),
                    "docs": 0, "sentences": 0, "stages": {}}
        write_manifest(build_dir, manifest)
        st = cls.open(build_dir, "rw")
        with transaction(st.conn) as c:
            c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('build_id', ?)", (build_id,))
            c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('created', ?)", (manifest["created"],))
        return st

    def close(self) -> None:
        self.conn.close()

    @property
    def build_id(self) -> str:
        return self.build_dir.name

    @property
    def db_path(self) -> Path:
        return self.build_dir / DB_FILE

    @property
    def curated_dir(self) -> Path:
        return self.build_dir / "curated"

    def manifest(self) -> dict:
        return read_manifest(self.build_dir) or {"build_id": self.build_id, "status": "unknown"}

    def update_manifest(self, **fields) -> dict:
        m = self.manifest()
        m.update(fields)
        write_manifest(self.build_dir, m)
        return m

    def meta_get(self, key: str) -> str | None:
        r = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r[0] if r else None

    def meta_set(self, key: str, value: str) -> None:
        with transaction(self.conn) as c:
            c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)", (key, value))

    # ---- pin the evidence -----------------------------------------------------------------------------------
    def pin_evidence(self, evidence_path: str | Path, doc_domains: dict[str, list[str]] | None = None) -> dict:
        """Copy every document's current extraction membership (ids only) from evidence.sqlite. Returns
        {docs, sentences, units}."""
        if not self.evidence_attached:
            attach(self.conn, "ev", evidence_path, "ro")
            self.evidence_attached = True
        dd = doc_domains or {}
        with transaction(self.conn) as c:
            c.execute("DELETE FROM build_doc_meta")
            c.execute("DELETE FROM build_units")
            c.execute("DELETE FROM build_sentences")
            c.execute("DELETE FROM build_docs")
            c.execute("INSERT INTO build_docs(doc_id, ext_id) SELECT doc_id, ext_id FROM "
                      "(SELECT e.doc_id, e.ext_id, MAX(e.created) AS created FROM ev.extractions e "
                      " WHERE e.is_current = 1 GROUP BY e.doc_id)")
            c.execute("INSERT OR IGNORE INTO build_sentences(sid, doc_id, unit_id, page_no, section, is_body, tags, "
                      "seq, cstart, cend) "
                      "SELECT es.sid, b.doc_id, es.unit_id, es.page_no, es.section, es.is_body, es.tags, es.seq, "
                      "es.cstart, es.cend FROM ev.extraction_sentences es JOIN build_docs b ON b.ext_id = es.ext_id")
            c.execute("INSERT OR IGNORE INTO build_units(unit_id, doc_id, kind, ordinal, heading, page_from, page_to, "
                      "unit_kind, confidence) "
                      "SELECT u.unit_id, b.doc_id, u.kind, u.ordinal, u.heading, u.page_from, u.page_to, u.unit_kind, "
                      "u.confidence FROM ev.units u JOIN build_docs b ON b.ext_id = u.ext_id")
            c.execute("INSERT INTO build_doc_meta(doc_id, title, year, authors, venue, doc_type, keywords, abstract, "
                      "domains_json) "
                      "SELECT b.doc_id, m.title, m.year, m.authors, m.venue, d.doc_type, m.keywords, m.abstract, '[]' "
                      "FROM build_docs b JOIN ev.documents d ON d.doc_id = b.doc_id "
                      "LEFT JOIN ev.document_meta m ON m.doc_id = b.doc_id")
            if dd:
                c.executemany("UPDATE build_doc_meta SET domains_json=? WHERE doc_id=?",
                              [(_dumps(list(v or [])), k) for k, v in dd.items()])
            # documents without a unit row (defensive): one document unit
            c.execute("INSERT OR IGNORE INTO build_units(unit_id, doc_id, kind, ordinal, heading, page_from, page_to, "
                      "unit_kind, confidence) SELECT doc_id, doc_id, 'document', 0, NULL, 1, "
                      "(SELECT MAX(page_no) FROM build_sentences s WHERE s.doc_id = b.doc_id), NULL, 1.0 "
                      "FROM build_docs b WHERE NOT EXISTS (SELECT 1 FROM build_units u WHERE u.doc_id = b.doc_id)")
        out = self.pin_counts()
        self.meta_set("pinned", _now())
        return out

    def pin_counts(self) -> dict:
        one = lambda sql: int(self.conn.execute(sql).fetchone()[0])  # noqa: E731
        return {"docs": one("SELECT COUNT(*) FROM build_docs"),
                "sentences": one("SELECT COUNT(*) FROM build_sentences"),
                "units": one("SELECT COUNT(*) FROM build_units")}

    # ---- scheme ---------------------------------------------------------------------------------------------
    def load_scheme(self, curated: dict | None = None) -> dict:
        """Load the scheme tables from the build's own curated snapshot (or the given dict). Returns counts."""
        cur = curated if curated is not None else load_curated(self.curated_dir)
        with transaction(self.conn) as c:
            for t in ("concept_forms", "anchors", "concept_categories", "term_tree", "concepts", "patterns",
                      "domains", "categories", "subdomains", "i18n"):
                c.execute(f"DELETE FROM {t}")
            for d in (cur.get("domains") or {}).get("domains", []) or []:
                c.execute("INSERT INTO domains(id, zh, en, ord, scope) VALUES (?,?,?,?,?)",
                          (d["id"], d.get("zh"), d.get("en"), int(d.get("order", 0) or 0),
                           1 if d.get("scope", True) else 0))
            for k, v in (cur.get("patterns") or {}).items():
                c.execute("INSERT INTO patterns(key, json) VALUES (?,?)", (k, _dumps(v)))
            for x in (cur.get("concepts") or {}).get("concepts", []) or []:
                c.execute("INSERT INTO concepts(id, label, zh, type, mode, domains_json, place_json, origin, probe, rx, "
                          "readable, shared_json, merged_into, source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (x["id"], x.get("label") or x["id"], x.get("zh"), x.get("type"), x.get("mode") or "forms",
                           _dumps(list(x.get("domains") or [])), _dumps(list(x.get("place") or [])), x.get("origin"),
                           x.get("probe"), x.get("rx"), x.get("readable"), _dumps(list(x.get("shared_with") or [])),
                           x.get("merged_into"), x.get("source")))
                rows = [(x["id"], f, "form", None) for f in x.get("forms") or []]
                rows += [(x["id"], f, "gen", None) for f in x.get("gen_forms") or []]
                rows += [(x["id"], f, "case", None) for f in x.get("case_forms") or []]
                if x.get("rx"):
                    rows.append((x["id"], x.get("readable") or x.get("label") or x["id"], "literal", x["rx"]))
                c.executemany("INSERT OR IGNORE INTO concept_forms(concept_id, form, kind, rx) VALUES (?,?,?,?)", rows)
            for k in (cur.get("categories") or {}).get("categories", []) or []:
                c.execute("INSERT INTO categories(id, domain, zh, en, desc_zh, desc_en, ord) VALUES (?,?,?,?,?,?,?)",
                          (k["id"], k.get("domain"), k.get("name_zh"), k.get("name_en"), k.get("desc_zh"),
                           k.get("desc_en"), int(k.get("order", 0) or 0)))
            for r in (cur.get("concept_categories") or {}).get("rows", []) or []:
                c.execute("INSERT OR REPLACE INTO concept_categories(concept_id, domain, primary_cat, secondary_cat, reason) "
                          "VALUES (?,?,?,?,?)", (r["concept_id"], r.get("domain"), r.get("primary"),
                                                 r.get("secondary") or None, r.get("reason")))
            for s in (cur.get("subdomains") or {}).get("subdomains", []) or []:
                c.execute("INSERT INTO subdomains(id, kind, domain, zh, en, desc_zh, desc_en, ord, notes) "
                          "VALUES (?,?,?,?,?,?,?,?,?)",
                          (s["id"], s.get("kind") or "within", s.get("domain"), s.get("name_zh"), s.get("name_en"),
                           s.get("desc_zh"), s.get("desc_en"), int(s.get("order", 0) or 0), s.get("notes")))
                groups = s.get("groups") if s.get("kind") == "cross" else [s.get("anchors") or []]
                for g, ids in enumerate(groups or []):
                    c.executemany("INSERT OR IGNORE INTO anchors(sub_id, group_no, concept_id) VALUES (?,?,?)",
                                  [(s["id"], g, cid) for cid in ids or []])
            ord_ = 0
            for d in (cur.get("term_tree") or {}).get("domains", []) or []:
                for sec in d.get("sections", []) or []:
                    for sub in sec.get("subs", []) or []:
                        for t in sub.get("terms", []) or []:
                            ord_ += 1
                            c.execute("INSERT INTO term_tree(domain, section, sub_name, sub_ord, term, zh, type, "
                                      "concept_id, note, example, example_doc, ord) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                                      (d["id"], sec.get("section"), sub.get("name"), int(sub.get("order", 0) or 0),
                                       t.get("term"), t.get("zh"), t.get("type"), t.get("concept_id"), t.get("note"),
                                       t.get("example"), t.get("example_doc"), ord_))
            for s in (cur.get("i18n") or {}).get("strings", []) or []:
                if s.get("zh"):
                    c.execute("INSERT OR REPLACE INTO i18n(zh, en, grp) VALUES (?,?,?)",
                              (s["zh"], s.get("en"), s.get("grp")))
        return self.scheme_counts()

    def scheme_counts(self) -> dict:
        one = lambda t: int(self.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])  # noqa: E731
        return {t: one(t) for t in ("concepts", "concept_forms", "domains", "categories", "concept_categories",
                                    "subdomains", "anchors", "term_tree", "i18n")}

    def matcher_data(self) -> tuple[list[dict], dict]:
        """(concept dicts, patterns dict) as `Matcher.from_curated` expects, from the scheme tables."""
        concepts: dict[str, dict] = {}
        for r in self.conn.execute("SELECT id, mode, rx, probe, merged_into FROM concepts"):
            concepts[r["id"]] = {"id": r["id"], "mode": r["mode"], "rx": r["rx"], "probe": r["probe"],
                                 "merged_into": r["merged_into"], "forms": [], "gen_forms": [], "case_forms": []}
        for r in self.conn.execute("SELECT concept_id, form, kind FROM concept_forms WHERE kind != 'literal'"):
            key = {"form": "forms", "gen": "gen_forms", "case": "case_forms"}[r["kind"]]
            concepts[r["concept_id"]][key].append(r["form"])
        patterns = {r["key"]: _loads(r["json"], None) for r in self.conn.execute("SELECT key, json FROM patterns")}
        return list(concepts.values()), patterns

    def literal_ids(self) -> set[str]:
        return {r[0] for r in self.conn.execute("SELECT id FROM concepts WHERE mode='literal'")}

    def domains(self) -> list[dict]:
        return _rows(self.conn.execute("SELECT id, zh, en, ord, scope FROM domains ORDER BY ord, id"))

    def scope_ids(self) -> list[str]:
        return ["all"] + [d["id"] for d in self.domains() if d["scope"]]

    def i18n_strings(self) -> dict[str, str]:
        return {r["zh"]: r["en"] for r in self.conn.execute("SELECT zh, en FROM i18n WHERE en IS NOT NULL")}

    # ---- documents / units ----------------------------------------------------------------------------------
    def doc_ids(self) -> list[str]:
        return [r[0] for r in self.conn.execute("SELECT doc_id FROM build_docs ORDER BY doc_id")]

    def doc_meta(self, doc_id: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM build_doc_meta WHERE doc_id=?", (doc_id,)).fetchone()
        if r is None:
            return None
        d = dict(r)
        d["domains"] = _loads(d.pop("domains_json"), [])
        return d

    def doc_meta_all(self) -> dict[str, dict]:
        out = {}
        for r in self.conn.execute("SELECT doc_id, title, year, domains_json, doc_type FROM build_doc_meta"):
            out[r["doc_id"]] = {"title": r["title"], "year": r["year"], "doc_type": r["doc_type"],
                                "domains": _loads(r["domains_json"], [])}
        return out

    def units_of(self, doc_id: str) -> list[dict]:
        return _rows(self.conn.execute("SELECT * FROM build_units WHERE doc_id=? ORDER BY ordinal", (doc_id,)))

    def unit(self, unit_id: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM build_units WHERE unit_id=?", (unit_id,)).fetchone()
        return dict(r) if r else None

    def units_all(self) -> list[dict]:
        return _rows(self.conn.execute("SELECT * FROM build_units ORDER BY doc_id, ordinal"))

    def sentences_of_doc(self, doc_id: str) -> list[dict]:
        """Pinned membership rows of a document in reading order (plus `text` when the evidence is attached)."""
        if self.evidence_attached:
            sql = ("SELECT bs.sid, bs.doc_id, bs.unit_id, bs.page_no, bs.section, bs.is_body, bs.tags, bs.seq, "
                   "bs.cstart, bs.cend, s.text FROM build_sentences bs JOIN ev.sentences s ON s.sid = bs.sid "
                   "WHERE bs.doc_id=? ORDER BY bs.seq")
        else:
            sql = ("SELECT sid, doc_id, unit_id, page_no, section, is_body, tags, seq, cstart, cend, NULL AS text "
                   "FROM build_sentences WHERE doc_id=? ORDER BY seq")
        return _rows(self.conn.execute(sql, (doc_id,)))

    # ---- stage items ----------------------------------------------------------------------------------------
    def set_item(self, stage: str, item_key: str, status: str, fingerprint: str | None = None,
                 detail: dict | None = None, started: str | None = None) -> None:
        now = _now()
        with transaction(self.conn) as c:
            c.execute("INSERT OR REPLACE INTO stage_items(stage, item_key, fingerprint, status, started, finished, "
                      "detail_json) VALUES (?,?,?,?,?,?,?)",
                      (stage, item_key, fingerprint, status, started or now,
                       now if status in ("done", "failed", "skipped") else None,
                       _dumps(detail) if detail is not None else None))

    def set_items(self, stage: str, rows: Iterable[tuple[str, str, str | None, dict | None]]) -> None:
        now = _now()
        with transaction(self.conn) as c:
            c.executemany("INSERT OR REPLACE INTO stage_items(stage, item_key, fingerprint, status, started, finished, "
                          "detail_json) VALUES (?,?,?,?,?,?,?)",
                          [(stage, k, fp, st, now, now if st in ("done", "failed", "skipped") else None,
                            _dumps(d) if d is not None else None) for k, st, fp, d in rows])

    def stage_status(self, stage: str) -> str | None:
        r = self.conn.execute("SELECT status FROM stage_items WHERE stage=? AND item_key='*'", (stage,)).fetchone()
        return r[0] if r else None

    def stage_items(self, stage: str | None = None) -> list[dict]:
        if stage:
            cur = self.conn.execute("SELECT * FROM stage_items WHERE stage=? ORDER BY item_key", (stage,))
        else:
            cur = self.conn.execute("SELECT * FROM stage_items ORDER BY stage, item_key")
        out = []
        for r in cur:
            d = dict(r)
            d["detail"] = _loads(d.pop("detail_json"), None)
            out.append(d)
        return out

    def stage_summary(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for r in self.conn.execute("SELECT stage, status, COUNT(*) n FROM stage_items WHERE item_key != '*' "
                                   "GROUP BY stage, status"):
            out.setdefault(r["stage"], {})[r["status"]] = r["n"]
        for r in self.conn.execute("SELECT stage, status, started, finished, detail_json FROM stage_items "
                                   "WHERE item_key = '*'"):
            d = out.setdefault(r["stage"], {})
            d["status"] = r["status"]
            d["started"], d["finished"] = r["started"], r["finished"]
            d["detail"] = _loads(r["detail_json"], None)
        return out

    # ---- match outputs --------------------------------------------------------------------------------------
    def delete_doc_outputs(self, doc_ids: Sequence[str]) -> None:
        with transaction(self.conn) as c:
            for i in range(0, len(doc_ids), _IN_CHUNK):
                part = list(doc_ids[i:i + _IN_CHUNK])
                marks = ",".join("?" * len(part))
                c.execute(f"DELETE FROM occurrences WHERE sid IN (SELECT sid FROM build_sentences WHERE doc_id IN ({marks}))", part)
                c.execute(f"DELETE FROM topic_hits WHERE unit_id IN (SELECT unit_id FROM build_units WHERE doc_id IN ({marks}))", part)

    def add_occurrences(self, rows: Iterable[tuple[str, str, str, int, int, str | None]]) -> int:
        """(sid, concept_id, form, cstart, cend, nested_in) rows; INSERT OR IGNORE (PK sid, concept_id, cstart).
        The denormalised columns (doc_id, seq, page_no, section, is_body) are copied from the pinned
        build_sentences row by the insert itself, so callers keep the six-tuple shape; a sid that is not pinned
        inserts nothing (the finish check "every occurrence sid is pinned" stays true by construction)."""
        rows = list(rows)
        with transaction(self.conn) as c:
            c.executemany("INSERT OR IGNORE INTO occurrences(sid, concept_id, form, cstart, cend, nested_in, doc_id, seq, "
                          "page_no, section, is_body, tags) "
                          "SELECT ?, ?, ?, ?, ?, ?, bs.doc_id, bs.seq, bs.page_no, bs.section, bs.is_body, bs.tags "
                          "FROM build_sentences bs WHERE bs.sid = ?",
                          [(sid, cid, form, cs, ce, nested, sid) for sid, cid, form, cs, ce, nested in rows])
        return len(rows)

    def add_topic_hits(self, rows: Iterable[tuple[str, str, str, int, int]]) -> int:
        rows = list(rows)
        with transaction(self.conn) as c:
            c.executemany("INSERT OR REPLACE INTO topic_hits(unit_id, concept_id, field, n, n_nested) VALUES (?,?,?,?,?)",
                          rows)
        return len(rows)

    def rebuild_counts(self, body_extra_sections: Sequence[str] = ("appendix",), body_includes_title: bool = True) -> int:
        """`counts` from occurrences (sentence sections; 'body' = is_body sentences plus `body_extra_sections`,
        which the previous system counted as full text) and topic_hits of the document units (title / keywords /
        abstract); with `body_includes_title` the title-field hits are added to 'body' (the previous system's full
        text started with the title line). n_sa = standalone."""
        extra = [s for s in body_extra_sections or () if s in SECTION_ORDER]
        body_where = "o.is_body = 1" + (" OR o.section IN (%s)" % ",".join("?" * len(extra)) if extra else "")
        with transaction(self.conn) as c:
            c.execute("DELETE FROM counts")
            c.execute("INSERT INTO counts(concept_id, doc_id, section, n, n_nested, n_sa) "
                      "SELECT o.concept_id, o.doc_id, o.section, COUNT(*), SUM(o.nested_in IS NOT NULL), 0 "
                      "FROM occurrences o "
                      "WHERE o.section NOT IN ('title', 'keywords', 'abstract') GROUP BY o.concept_id, o.doc_id, o.section")
            c.execute("INSERT INTO counts(concept_id, doc_id, section, n, n_nested, n_sa) "
                      "SELECT o.concept_id, o.doc_id, 'body', COUNT(*), SUM(o.nested_in IS NOT NULL), 0 "
                      f"FROM occurrences o WHERE {body_where} "
                      "GROUP BY o.concept_id, o.doc_id", tuple(extra))
            c.execute("INSERT OR REPLACE INTO counts(concept_id, doc_id, section, n, n_nested, n_sa) "
                      "SELECT th.concept_id, u.doc_id, th.field, th.n, th.n_nested, 0 FROM topic_hits th "
                      "JOIN build_units u ON u.unit_id = th.unit_id AND u.kind = 'document'")
            if body_includes_title:
                c.execute("INSERT INTO counts(concept_id, doc_id, section, n, n_nested, n_sa) "
                          "SELECT th.concept_id, u.doc_id, 'body', th.n, th.n_nested, 0 FROM topic_hits th "
                          "JOIN build_units u ON u.unit_id = th.unit_id AND u.kind = 'document' WHERE th.field = 'title' "
                          "ON CONFLICT(concept_id, doc_id, section) DO UPDATE SET n = n + excluded.n, "
                          "n_nested = n_nested + excluded.n_nested")
            c.execute("UPDATE counts SET n_sa = CASE WHEN concept_id IN (SELECT id FROM concepts WHERE mode='literal') "
                      "THEN n ELSE MAX(0, n - n_nested) END")
        return int(self.conn.execute("SELECT COUNT(*) FROM counts").fetchone()[0])

    def iter_counts(self):
        """Yields (concept_id, {doc_id: {section: (n, n_nested)}}) in concept order (streaming)."""
        cur = self.conn.execute("SELECT concept_id, doc_id, section, n, n_nested FROM counts ORDER BY concept_id, doc_id")
        current: str | None = None
        per_doc: dict[str, dict[str, tuple[int, int]]] = {}
        for r in cur:
            if r["concept_id"] != current:
                if current is not None:
                    yield current, per_doc
                current, per_doc = r["concept_id"], {}
            per_doc.setdefault(r["doc_id"], {})[r["section"]] = (r["n"], r["n_nested"])
        if current is not None:
            yield current, per_doc

    def topic_hits_by_unit(self, standalone: bool = False) -> dict[str, dict[str, tuple[int, int, int]]]:
        """{unit_id: {concept_id: (title, keywords, abstract)}} counts. Default: every occurrence, nested ones
        included — the sub-domain assignment of the previous system read the first version's `term_paper`
        title / keywords / abstract columns, which had no nested distinction. `standalone=True` gives n - n_nested
        (literal concepts: n), the counting used by `importance`."""
        lits = self.literal_ids()
        out: dict[str, dict[str, list[int]]] = {}
        for r in self.conn.execute("SELECT unit_id, concept_id, field, n, n_nested FROM topic_hits"):
            if r["field"] not in TOPIC_FIELDS:
                continue
            n = r["n"]
            if standalone and r["concept_id"] not in lits:
                n = max(0, n - r["n_nested"])
            if n <= 0:
                continue
            out.setdefault(r["unit_id"], {}).setdefault(r["concept_id"], [0, 0, 0])[TOPIC_FIELDS.index(r["field"])] = n
        return {u: {c: tuple(v) for c, v in d.items()} for u, d in out.items()}

    def write_importance(self, rows: Iterable[dict]) -> int:
        rows = list(rows)
        with transaction(self.conn) as c:
            c.execute("DELETE FROM importance")
            c.executemany(
                "INSERT INTO importance(concept_id, scope, papers, any_papers, coverage, topic_papers, topic_rate, "
                "abstract_rate, occ, median_per_paper, refs_papers, main_section, lift, year_cov_json, trend, trend_flag, "
                "tier) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(r["concept_id"], r["scope"], r["papers"], r["any_papers"], r["coverage"], r["topic_papers"],
                  r["topic_rate"], r["abstract_rate"], r["occ"], r["median_per_paper"], r["refs_papers"],
                  r.get("main_section"), r.get("lift"), _dumps(r.get("year_cov") or {}), r.get("trend"),
                  r.get("trend_flag") or "", r["tier"]) for r in rows])
        return len(rows)

    # ---- assign outputs -------------------------------------------------------------------------------------
    def anchors_all(self) -> dict[str, list[list[str]]]:
        """{sub_id: [[concept ids of group 0], [group 1], …]}."""
        out: dict[str, dict[int, list[str]]] = {}
        for r in self.conn.execute("SELECT sub_id, group_no, concept_id FROM anchors ORDER BY sub_id, group_no, concept_id"):
            out.setdefault(r["sub_id"], {}).setdefault(r["group_no"], []).append(r["concept_id"])
        return {s: [g[k] for k in sorted(g)] for s, g in out.items()}

    def subdomains_all(self) -> list[dict]:
        return _rows(self.conn.execute("SELECT * FROM subdomains ORDER BY ord, id"))

    def write_unit_subdomains(self, rows: Iterable[tuple[str, str, str, dict, int]]) -> int:
        rows = list(rows)
        with transaction(self.conn) as c:
            c.execute("DELETE FROM unit_subdomains")
            c.executemany("INSERT OR REPLACE INTO unit_subdomains(unit_id, sub_id, tier, hits_json, n_hits) VALUES (?,?,?,?,?)",
                          [(u, s, t, _dumps(h), n) for u, s, t, h, n in rows])
        return len(rows)

    def write_subdomain_concepts(self, rows: Iterable[dict]) -> int:
        rows = list(rows)
        with transaction(self.conn) as c:
            c.execute("DELETE FROM subdomain_concepts")
            c.executemany("INSERT OR REPLACE INTO subdomain_concepts(sub_id, concept_id, rank, papers, coverage, coverage_rest, "
                          "lift, topic_rate, score) VALUES (?,?,?,?,?,?,?,?,?)",
                          [(r["sub_id"], r["concept_id"], r["rank"], r["papers"], r["coverage"], r["coverage_rest"],
                            r["lift"], r["topic_rate"], r["score"]) for r in rows])
        return len(rows)

    def write_framework_stats(self, rows: Iterable[dict]) -> int:
        rows = list(rows)
        with transaction(self.conn) as c:
            c.execute("DELETE FROM framework_stats")
            c.executemany("INSERT OR REPLACE INTO framework_stats(kind, id, scope, papers, core, base, years_json, "
                          "overlaps_json, concepts_json) VALUES (?,?,?,?,?,?,?,?,?)",
                          [(r["kind"], r["id"], r.get("scope", "all"), r.get("papers", 0), r.get("core", 0),
                            r.get("base", 0), _dumps(r.get("years") or {}), _dumps(r.get("overlaps") or []),
                            _dumps(r.get("concepts") or [])) for r in rows])
        return len(rows)

    def body_users(self) -> dict[str, set[str]]:
        """{concept_id: set(doc_id)} of standalone body users (for sub-domain concept ranking)."""
        out: dict[str, set[str]] = {}
        for r in self.conn.execute("SELECT concept_id, doc_id FROM counts WHERE section='body' AND n_sa > 0"):
            out.setdefault(r["concept_id"], set()).add(r["doc_id"])
        return out

    def topic_users(self, standalone: bool = True) -> dict[str, set[str]]:
        """{concept_id: set(doc_id)} with a title/keywords hit: standalone hits (the importance table's
        topic_rate) or, with `standalone=False`, any hit, nested ones included (the sub-domain framework of the
        previous system read the first version's title/keywords columns, which had no nested distinction)."""
        col = "n_sa" if standalone else "n"
        out: dict[str, set[str]] = {}
        for r in self.conn.execute(f"SELECT concept_id, doc_id FROM counts WHERE section IN ('title','keywords') AND {col} > 0"):
            out.setdefault(r["concept_id"], set()).add(r["doc_id"])
        return out

    def concept_scope(self) -> dict[str, dict]:
        """{concept_id: {'domains': [DOM ids], 'tiered': bool}} for every unmerged concept: the data behind the
        candidate filters of the sub-domain ranking and the tier rule (`atlas.core.stats.is_tiered`)."""
        out: dict[str, dict] = {}
        for r in self.conn.execute("SELECT id, domains_json, origin FROM concepts WHERE merged_into IS NULL"):
            out[r["id"]] = {"domains": _loads(r["domains_json"], []), "tiered": is_tiered(r["origin"])}
        return out

    def concept_categories_all(self) -> list[dict]:
        return _rows(self.conn.execute("SELECT concept_id, domain, primary_cat, secondary_cat FROM concept_categories"))

    def categories_all(self) -> list[dict]:
        return _rows(self.conn.execute("SELECT * FROM categories ORDER BY ord, id"))

    def tier_histogram(self) -> dict[str, dict[str, int]]:
        """{scope: {tier: n}} over the tiered concepts only (frequency-only items carry tier NULL)."""
        out: dict[str, dict[str, int]] = {}
        for r in self.conn.execute("SELECT scope, tier, COUNT(*) n FROM importance WHERE tier IS NOT NULL GROUP BY scope, tier"):
            out.setdefault(r["scope"], {})[r["tier"]] = r["n"]
        return out

    def top_concepts(self, scope: str = "all", n: int = 20) -> list[dict]:
        return _rows(self.conn.execute(
            "SELECT i.concept_id AS id, c.label, c.zh, i.papers, i.coverage, i.topic_rate, i.tier FROM importance i "
            "JOIN concepts c ON c.id = i.concept_id WHERE i.scope=? ORDER BY i.papers DESC, c.label LIMIT ?", (scope, int(n))))

    def orphan_occurrence_sids(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM occurrences o LEFT JOIN build_sentences bs ON bs.sid = o.sid "
                                     "WHERE bs.sid IS NULL").fetchone()[0])

    def unresolved_anchors(self) -> list[str]:
        return [r[0] for r in self.conn.execute(
            "SELECT DISTINCT a.concept_id FROM anchors a LEFT JOIN concepts c ON c.id = a.concept_id WHERE c.id IS NULL")]

    def recount_body(self, concept_id: str, body_extra_sections: Sequence[str] = ("appendix",),
                     body_includes_title: bool = True) -> dict[str, tuple[int, int]]:
        """{doc_id: (n, n_nested)} of body occurrences straight from `occurrences` + title topic hits (finish check;
        same definition as rebuild_counts)."""
        extra = [s for s in body_extra_sections or () if s in SECTION_ORDER]
        body_where = "o.is_body = 1" + (" OR o.section IN (%s)" % ",".join("?" * len(extra)) if extra else "")
        out = {r["doc_id"]: [r["n"], r["nn"]] for r in self.conn.execute(
            "SELECT o.doc_id, COUNT(*) n, SUM(o.nested_in IS NOT NULL) nn FROM occurrences o "
            f"WHERE o.concept_id=? AND ({body_where}) GROUP BY o.doc_id",
            (concept_id, *extra))}
        if body_includes_title:
            for r in self.conn.execute("SELECT u.doc_id, th.n, th.n_nested FROM topic_hits th JOIN build_units u "
                                       "ON u.unit_id = th.unit_id AND u.kind = 'document' WHERE th.concept_id=? AND th.field='title'",
                                       (concept_id,)):
                v = out.setdefault(r["doc_id"], [0, 0])
                v[0] += r["n"]
                v[1] += r["n_nested"]
        return {k: (v[0], v[1]) for k, v in out.items()}

    def body_counts(self, concept_id: str) -> dict[str, tuple[int, int]]:
        return {r["doc_id"]: (r["n"], r["n_nested"]) for r in self.conn.execute(
            "SELECT doc_id, n, n_nested FROM counts WHERE concept_id=? AND section='body'", (concept_id,))}

    def concept_ids(self) -> list[str]:
        return [r[0] for r in self.conn.execute("SELECT id FROM concepts WHERE merged_into IS NULL ORDER BY id")]

    def sampled_concepts(self, n: int = 20) -> list[str]:
        """Deterministic sample: every k-th concept with counts, by id."""
        ids = [r[0] for r in self.conn.execute("SELECT DISTINCT concept_id FROM counts ORDER BY concept_id")]
        if len(ids) <= n:
            return ids
        step = max(1, len(ids) // n)
        return ids[::step][:n]

    def total_occurrences(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM occurrences").fetchone()[0])

    # ---- checks and freeze ----------------------------------------------------------------------------------
    def add_checks(self, rows: Iterable[tuple[str, str, str, str, str | None]]) -> int:
        rows = list(rows)
        with transaction(self.conn) as c:
            for kind, tid, check, sev, detail in rows:
                seq = c.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM check_results WHERE target_kind=? AND target_id=?",
                                (kind, tid)).fetchone()[0]
                c.execute('INSERT INTO check_results(target_kind, target_id, seq, "check", severity, detail) VALUES (?,?,?,?,?,?)',
                          (kind, tid, seq, check, sev, detail))
        return len(rows)

    def checks(self) -> list[dict]:
        return _rows(self.conn.execute("SELECT * FROM check_results ORDER BY target_kind, target_id, seq"))

    @classmethod
    def upgrade(cls, build_dir: str | Path) -> dict:
        """Bring a finished build of schema 1 to the current schema in place (`atlas build upgrade <id>`): clear the
        read-only attribute, fill the denormalised occurrence columns from build_sentences and create the covering
        index (db.upgrade_build), then re-freeze (VACUUM, journal DELETE, read-only, new sha256 in manifest.json).
        Seconds on a 2.3 M-row build; a rebuild is minutes. Returns {build_id, from, to, sha256, seconds}."""
        build_dir = Path(build_dir)
        db = build_dir / DB_FILE
        if not db.is_file():
            raise FileNotFoundError(f"no build.sqlite in {build_dir}")
        m = read_manifest(build_dir) or {}
        if m.get("status") != "finished":
            raise ValueError(f"build {build_dir.name} is not finished (status {m.get('status')}); only finished builds are upgraded")
        t0 = time.perf_counter()
        os.chmod(db, stat.S_IWRITE | stat.S_IREAD)
        before, after = upgrade_build(db)
        if before == after:
            os.chmod(db, stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)
            return {"build_id": build_dir.name, "from": before, "to": after, "sha256": m.get("sha256"), "seconds": 0.0,
                    "unchanged": True}
        st = cls.open(build_dir, "rw")
        try:
            st.meta_set("schema_upgraded", _now())
        except Exception:
            st.close()
            raise
        sha = st.freeze()
        write_manifest(build_dir, dict(m, sha256=sha, schema_version=after, upgraded=_now()))
        return {"build_id": build_dir.name, "from": before, "to": after, "sha256": sha,
                "seconds": round(time.perf_counter() - t0, 2), "unchanged": False}

    def freeze(self) -> str:
        """wal_checkpoint(TRUNCATE) → VACUUM → journal_mode=DELETE → close → read-only attribute → sha256."""
        c = self.conn
        if self.evidence_attached:
            c.execute("DETACH DATABASE ev")
            self.evidence_attached = False
        c.execute("PRAGMA main.wal_checkpoint(TRUNCATE)")
        c.execute("VACUUM")
        c.execute("PRAGMA main.journal_mode = DELETE")
        c.close()
        for ext in ("-wal", "-shm"):
            p = Path(str(self.db_path) + ext)
            if p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass
        os.chmod(self.db_path, stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)
        return sha256_file(self.db_path)

    # ---- counts for the service ---------------------------------------------------------------------------------
    def stats(self) -> dict:
        one = lambda sql: int(self.conn.execute(sql).fetchone()[0])  # noqa: E731
        m = {r["key"]: r["value"] for r in self.conn.execute("SELECT key, value FROM meta")}
        return {"docs": int(m.get("n_docs") or one("SELECT COUNT(*) FROM build_docs")),
                "sentences": int(m.get("n_sentences") or one("SELECT COUNT(*) FROM build_sentences")),
                "concepts": int(m.get("n_concepts") or one("SELECT COUNT(*) FROM concepts")),
                "occurrences": int(m.get("n_occurrences") or 0),
                "created": m.get("created")}

    # ---- concept queries ------------------------------------------------------------------------------------
    _SORT = {"papers": ("COALESCE(i.papers, 0)", "DESC"), "coverage": ("COALESCE(i.coverage, 0)", "DESC"),
             "topic_rate": ("COALESCE(i.topic_rate, 0)", "DESC"), "label": ("c.label COLLATE NOCASE", "ASC"),
             "tier": ("COALESCE(i.tier, 'Z')", "ASC"), "occ": ("COALESCE(i.occ, 0)", "DESC"), "id": ("c.id", "ASC")}

    #: list_concepts(kind=…): 'concept' = the tiered concepts (origin empty or 概念…, the previous system's 1,308),
    #: 'other' = the frequency-only entries (phrases, journal names, ambiguous abbreviations), 'all' = both
    _KINDS = {"concept": "(c.origin IS NULL OR c.origin = '' OR c.origin LIKE '概念%')",
              "other": "NOT (c.origin IS NULL OR c.origin = '' OR c.origin LIKE '概念%')", "all": "1"}

    def list_concepts(self, q: str | None = None, domain: str | None = None, tier: str | None = None,
                      type: str | None = None, sub: str | None = None, cat: str | None = None, scope: str = "all",
                      sort: str = "papers", dir: str | None = None, limit: int = 100, offset: int = 0,
                      kind: str = "concept") -> dict:
        where = ["c.merged_into IS NULL", self._KINDS.get(kind or "concept", self._KINDS["concept"])]
        args: list = [scope or "all"]
        if q:
            like = _like(q.strip())
            where.append("(c.label LIKE ? ESCAPE '\\' OR c.zh LIKE ? ESCAPE '\\' OR c.id LIKE ? ESCAPE '\\')")
            args += [like, like, like]
        if domain:
            where.append("c.domains_json LIKE ?")
            args.append(f'%"{domain}"%')
        if tier:
            where.append("i.tier = ?")
            args.append(tier)
        if type:
            where.append("c.type = ?")
            args.append(type)
        if sub:
            where.append("c.id IN (SELECT concept_id FROM subdomain_concepts WHERE sub_id = ?)")
            args.append(sub)
        if cat:
            where.append("c.id IN (SELECT concept_id FROM concept_categories WHERE primary_cat = ? OR secondary_cat = ?)")
            args += [cat, cat]
        col, default_dir = self._SORT.get(sort or "papers", self._SORT["papers"])
        d = (dir or default_dir).upper()
        if d not in ("ASC", "DESC"):
            d = default_dir
        base = ("FROM concepts c LEFT JOIN importance i ON i.concept_id = c.id AND i.scope = ? WHERE " + " AND ".join(where))
        total = int(self.conn.execute("SELECT COUNT(*) " + base, args).fetchone()[0])
        rows = _rows(self.conn.execute(
            "SELECT c.id, c.label, c.zh, c.type, c.mode, c.domains_json, COALESCE(i.papers, 0) AS papers, "
            "COALESCE(i.coverage, 0) AS coverage, COALESCE(i.topic_rate, 0) AS topic_rate, i.tier, i.main_section, "
            f"COALESCE(i.occ, 0) AS occ {base} ORDER BY {col} {d}, c.label COLLATE NOCASE, c.id LIMIT ? OFFSET ?",
            (*args, int(limit), int(offset))))
        for r in rows:
            r["domains"] = _loads(r.pop("domains_json"), [])
        return {"rows": rows, "total": total, "scope": scope or "all", "sort": sort or "papers", "dir": d.lower(),
                "kind": kind or "concept"}

    def concept(self, concept_id: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM concepts WHERE id=?", (concept_id,)).fetchone()
        if r is None:
            return None
        d = dict(r)
        d["domains"] = _loads(d.pop("domains_json"), [])
        d["place"] = _loads(d.pop("place_json"), [])
        d["shared_with"] = _loads(d.pop("shared_json"), [])
        d["tiered"] = is_tiered(d.get("origin"))
        d["forms"], d["gen_forms"], d["case_forms"] = [], [], []
        for f in self.conn.execute("SELECT form, kind FROM concept_forms WHERE concept_id=? ORDER BY kind, form", (concept_id,)):
            key = {"form": "forms", "gen": "gen_forms", "case": "case_forms"}.get(f["kind"])
            if key:
                d[key].append(f["form"])
        return d

    def concept_labels(self, ids: Iterable[str]) -> dict[str, dict]:
        ids = list(dict.fromkeys(ids))
        out: dict[str, dict] = {}
        for i in range(0, len(ids), _IN_CHUNK):
            part = ids[i:i + _IN_CHUNK]
            marks = ",".join("?" * len(part))
            for r in self.conn.execute(f"SELECT id, label, zh, type FROM concepts WHERE id IN ({marks})", part):
                out[r["id"]] = {"id": r["id"], "label": r["label"], "zh": r["zh"], "type": r["type"]}
        return out

    def importance_of(self, concept_id: str) -> list[dict]:
        order = {s: i for i, s in enumerate(self.scope_ids())}
        rows = _rows(self.conn.execute("SELECT * FROM importance WHERE concept_id=?", (concept_id,)))
        for r in rows:
            r["year_cov"] = _loads(r.pop("year_cov_json"), {})
        rows.sort(key=lambda r: order.get(r["scope"], 99))
        return rows

    def sections_of(self, concept_id: str) -> list[dict]:
        rows = _rows(self.conn.execute(
            "SELECT section, SUM(n_sa > 0) AS papers, SUM(n_sa) AS occ, SUM(n > 0) AS any_papers, SUM(n) AS any_occ "
            "FROM counts WHERE concept_id=? GROUP BY section", (concept_id,)))
        rows.sort(key=lambda r: SECTION_ORDER.get(r["section"], 99))
        return rows

    def domains_use(self, concept_id: str) -> list[dict]:
        out = []
        for d in self.domains():
            like = f'%"{d["id"]}"%'
            n = self.conn.execute("SELECT COUNT(*) FROM counts k JOIN build_doc_meta m ON m.doc_id = k.doc_id "
                                  "WHERE k.concept_id=? AND k.section='body' AND k.n_sa > 0 AND m.domains_json LIKE ?",
                                  (concept_id, like)).fetchone()[0]
            tot = self.conn.execute("SELECT COUNT(*) FROM build_doc_meta WHERE domains_json LIKE ?", (like,)).fetchone()[0]
            out.append({"domain": d["id"], "zh": d["zh"], "en": d["en"], "papers": int(n), "total": int(tot)})
        n = self.conn.execute("SELECT COUNT(*) FROM counts k JOIN build_doc_meta m ON m.doc_id = k.doc_id "
                              "WHERE k.concept_id=? AND k.section='body' AND k.n_sa > 0 AND m.domains_json = '[]'",
                              (concept_id,)).fetchone()[0]
        tot = self.conn.execute("SELECT COUNT(*) FROM build_doc_meta WHERE domains_json = '[]'").fetchone()[0]
        out.append({"domain": None, "zh": "未归入", "en": "unassigned", "papers": int(n), "total": int(tot)})
        return out

    def years_of(self, concept_id: str) -> list[dict]:
        return _rows(self.conn.execute(
            "SELECT m.year AS year, COUNT(*) AS n FROM counts k JOIN build_doc_meta m ON m.doc_id = k.doc_id "
            "WHERE k.concept_id=? AND k.section='body' AND k.n_sa > 0 AND m.year IS NOT NULL GROUP BY m.year ORDER BY m.year",
            (concept_id,)))

    def year_totals(self) -> dict[str, int]:
        return {str(r["year"]): r["n"] for r in self.conn.execute(
            "SELECT year, COUNT(*) n FROM build_doc_meta WHERE year IS NOT NULL GROUP BY year ORDER BY year")}

    def categories_of(self, concept_id: str) -> list[dict]:
        return _rows(self.conn.execute(
            "SELECT cc.domain, cc.primary_cat, cc.secondary_cat, cc.reason, c1.zh AS primary_zh, c1.en AS primary_en, "
            "c2.zh AS secondary_zh, c2.en AS secondary_en FROM concept_categories cc "
            "LEFT JOIN categories c1 ON c1.id = cc.primary_cat LEFT JOIN categories c2 ON c2.id = cc.secondary_cat "
            "WHERE cc.concept_id=? ORDER BY cc.domain", (concept_id,)))

    def subdomains_of(self, concept_id: str) -> list[dict]:
        return _rows(self.conn.execute(
            "SELECT sc.sub_id, s.zh, s.en, s.kind, s.domain, sc.rank, sc.papers, sc.coverage, sc.coverage_rest, sc.lift, "
            "sc.topic_rate, sc.score, f.papers AS sub_papers FROM subdomain_concepts sc JOIN subdomains s ON s.id = sc.sub_id "
            "LEFT JOIN framework_stats f ON f.kind='subdomain' AND f.id = sc.sub_id AND f.scope='all' "
            "WHERE sc.concept_id=? ORDER BY sc.score DESC, sc.sub_id", (concept_id,)))

    def anchor_of(self, concept_id: str) -> list[dict]:
        return _rows(self.conn.execute(
            "SELECT DISTINCT a.sub_id, s.zh, s.en, s.kind FROM anchors a JOIN subdomains s ON s.id = a.sub_id "
            "WHERE a.concept_id=? ORDER BY s.ord", (concept_id,)))

    def sentence_count(self, concept_id: str, standalone: bool = False) -> int:
        """Number of distinct sentences with an occurrence of the concept (nested ones included unless
        `standalone`): the total of `concept_sentences` without filters, from the covering index alone."""
        cond = "o.concept_id = ?" + (" AND o.nested_in IS NULL" if standalone else "")
        return int(self.conn.execute(
            f"SELECT COUNT(*) FROM (SELECT 1 FROM occurrences o WHERE {cond} GROUP BY o.doc_id, o.seq)",
            (concept_id,)).fetchone()[0])

    def term_tree_places(self, concept_id: str) -> list[dict]:
        return _rows(self.conn.execute(
            "SELECT domain, section, sub_name, term, zh, note, example, example_doc FROM term_tree WHERE concept_id=? ORDER BY ord",
            (concept_id,)))

    def examples(self, concept_id: str, n: int = 6) -> list[dict]:
        """Body sentences with a standalone occurrence: one per document, documents with the most standalone body
        occurrences first (ties by doc_id), the first such sentence of each in reading order, preferring running
        text over keyword lines / unlabelled sections (deterministic)."""
        # counts first (one row per document), then the covering index of occurrences (no table reads), text last
        docs = [r[0] for r in self.conn.execute(
            "SELECT doc_id FROM counts WHERE concept_id=? AND section='body' AND n_sa > 0 ORDER BY n_sa DESC, doc_id LIMIT ?",
            (concept_id, int(n)))]
        out = []
        for doc_id in docs:
            r = self.conn.execute(
                "SELECT o.sid, o.doc_id, o.page_no, o.section, o.tags, o.seq FROM occurrences o "
                "WHERE o.concept_id=? AND o.doc_id=? AND o.is_body=1 AND o.nested_in IS NULL "
                "ORDER BY CASE WHEN o.section IN ('keywords', 'title', 'other', 'front') THEN 1 ELSE 0 END, o.seq LIMIT 1",
                (concept_id, doc_id)).fetchone()
            if r is not None:
                out.append(dict(r, text=None))
        if out:
            texts = self._texts([d["sid"] for d in out])
            spans = self.spans_of(concept_id, [d["sid"] for d in out])
            for d in out:
                d["text"] = texts.get(d["sid"])
                d["spans"] = spans.get(d["sid"], [])
        return out

    def _texts(self, sids: Sequence[str]) -> dict[str, str]:
        """{sid: text} from the attached evidence (empty when it is not attached)."""
        out: dict[str, str] = {}
        if not self.evidence_attached:
            return out
        for i in range(0, len(sids), _IN_CHUNK):
            part = list(sids[i:i + _IN_CHUNK])
            marks = ",".join("?" * len(part))
            for r in self.conn.execute(f"SELECT sid, text FROM ev.sentences WHERE sid IN ({marks})", part):
                out[r["sid"]] = r["text"]
        return out

    def spans_of(self, concept_id: str, sids: Sequence[str]) -> dict[str, list[list]]:
        out: dict[str, list[list]] = {}
        for i in range(0, len(sids), _IN_CHUNK):
            part = list(sids[i:i + _IN_CHUNK])
            marks = ",".join("?" * len(part))
            for r in self.conn.execute(
                    f"SELECT sid, cstart, cend, form, nested_in FROM occurrences WHERE concept_id=? AND sid IN ({marks}) "
                    "ORDER BY sid, cstart", (concept_id, *part)):
                out.setdefault(r["sid"], []).append([r["cstart"], r["cend"], r["form"], r["nested_in"]])
        return out

    def concept_sentences(self, concept_id: str, section: str | None = None, domain: str | None = None,
                          sub: str | None = None, doc: str | None = None, tag: str | None = None,
                          standalone: bool = False, sort: str = "doc", limit: int = 50, offset: int = 0) -> dict:
        """Every sentence with an occurrence of the concept, filtered / sorted / paged on the covering index of
        `occurrences` alone (schema 2: doc_id, seq, section, is_body, tags live in the index); build_doc_meta,
        build_sentences and the evidence text are read for the page rows only. One row per sentence (GROUP BY
        doc_id, seq walks the index in order, so the default sort needs no temporary b-tree)."""
        where = ["o.concept_id = ?"]
        args: list = [concept_id]
        if standalone:
            where.append("o.nested_in IS NULL")
        if section:
            where.append("o.section = ?")
            args.append(section)
        if doc:
            where.append("o.doc_id = ?")
            args.append(doc)
        if domain:
            where.append("o.doc_id IN (SELECT doc_id FROM build_doc_meta WHERE domains_json LIKE ?)")
            args.append(f'%"{domain}"%')
        if sub:
            # document units carry the doc id; chapter units (theses) need the sentence's unit through build_sentences
            where.append("(o.doc_id IN (SELECT unit_id FROM unit_subdomains WHERE sub_id = ?) OR o.sid IN ("
                         "SELECT bs.sid FROM build_sentences bs WHERE bs.unit_id IN (SELECT us.unit_id FROM unit_subdomains us "
                         "JOIN build_units u ON u.unit_id = us.unit_id WHERE us.sub_id = ? AND u.kind <> 'document')))")
            args += [sub, sub]
        if tag:
            where.append("o.tags LIKE ?")
            args.append(f"%,{tag},%")
        cond = " AND ".join(where)
        order = {"doc": "o.doc_id, o.seq", "year": "year DESC, o.doc_id, o.seq",
                 "section": "o.section, o.doc_id, o.seq", "page": "o.doc_id, o.page_no, o.seq"}.get(sort or "doc", "o.doc_id, o.seq")
        year_col = ", (SELECT m.year FROM build_doc_meta m WHERE m.doc_id = o.doc_id) AS year" if (sort == "year") else ", NULL AS year"
        total = int(self.conn.execute(
            f"SELECT COUNT(*) FROM (SELECT 1 FROM occurrences o WHERE {cond} GROUP BY o.doc_id, o.seq)", args).fetchone()[0])
        rows = _rows(self.conn.execute(
            f"SELECT o.sid, o.doc_id, o.page_no AS page, o.section, o.tags, o.seq, o.is_body{year_col} "
            f"FROM occurrences o WHERE {cond} GROUP BY o.doc_id, o.seq ORDER BY {order} LIMIT ? OFFSET ?",
            (*args, int(limit), int(offset))))
        sids = [r["sid"] for r in rows]
        spans = self.spans_of(concept_id, sids)
        texts = self._texts(sids)
        units: dict[str, str] = {}
        for i in range(0, len(sids), _IN_CHUNK):
            part = sids[i:i + _IN_CHUNK]
            marks = ",".join("?" * len(part))
            for u in self.conn.execute(f"SELECT sid, unit_id FROM build_sentences WHERE sid IN ({marks})", part):
                units[u["sid"]] = u["unit_id"]
        meta: dict[str, dict] = {}
        for d in dict.fromkeys(r["doc_id"] for r in rows):
            m = self.conn.execute("SELECT title, year FROM build_doc_meta WHERE doc_id=?", (d,)).fetchone()
            meta[d] = dict(m) if m else {"title": None, "year": None}
        for r in rows:
            r["title"] = meta[r["doc_id"]]["title"]
            r["year"] = meta[r["doc_id"]]["year"]
            r["unit_id"] = units.get(r["sid"], r["doc_id"])
            r["text"] = texts.get(r["sid"])
            r["spans"] = spans.get(r["sid"], [])
            r["tags"] = [t for t in (r["tags"] or "").split(",") if t]
            r["is_body"] = bool(r["is_body"])
        return {"rows": rows, "total": total}

    def concept_docs(self, concept_id: str, scope: str | None = None, use: str = "body", sort: str = "count",
                     limit: int = 50, offset: int = 0) -> dict:
        where = ["k.concept_id = ?"]
        args: list = [concept_id]
        if scope and scope != "all":
            where.append("m.domains_json LIKE ?")
            args.append(f'%"{scope}"%')
        having = {"body": "SUM(CASE WHEN k.section='body' THEN k.n_sa END) > 0",
                  "any": "SUM(k.n) > 0", "refs": "SUM(CASE WHEN k.section='references' THEN k.n END) > 0"}.get(use or "body")
        cols = ", ".join(f"COALESCE(SUM(CASE WHEN k.section='{s}' THEN k.n_sa END), 0) AS {s}"
                         for s in ("title", "keywords", "abstract", "introduction", "methods", "results", "discussion",
                                   "conclusion", "other", "appendix", "body"))
        cols += ", COALESCE(SUM(CASE WHEN k.section='references' THEN k.n END), 0) AS refs"
        cols += ", COALESCE(SUM(CASE WHEN k.section='body' THEN k.n_nested END), 0) AS body_nested"
        base = (f"FROM counts k JOIN build_doc_meta m ON m.doc_id = k.doc_id WHERE {' AND '.join(where)} "
                f"GROUP BY k.doc_id HAVING {having}")
        order = {"count": "body DESC, m.year DESC, k.doc_id", "year": "m.year DESC, body DESC, k.doc_id",
                 "year_asc": "m.year ASC, body DESC, k.doc_id", "title": "m.title COLLATE NOCASE, k.doc_id"}.get(sort or "count", "body DESC, k.doc_id")
        total = int(self.conn.execute(f"SELECT COUNT(*) FROM (SELECT k.doc_id {base})", args).fetchone()[0])
        rows = _rows(self.conn.execute(
            f"SELECT k.doc_id, m.title, m.year, m.venue, m.doc_type, m.domains_json, {cols} {base} ORDER BY {order} "
            "LIMIT ? OFFSET ?", (*args, int(limit), int(offset))))
        for r in rows:
            r["domains"] = _loads(r.pop("domains_json"), [])
        return {"rows": rows, "total": total}

    # ---- framework queries ----------------------------------------------------------------------------------
    def term_tree(self, full: bool = False) -> list[dict]:
        """The 分结构术语库 tree. The tree view renders term / zh / type / note only, so the example sentences (45 % of
        the payload) are left out unless `full`; the concept page gets them through term_tree_places()."""
        doms = {d["id"]: d for d in self.domains()}
        out: dict[str, dict] = {}
        cols = "domain, section, sub_name, sub_ord, term, zh, type, concept_id, note" + (", example, example_doc" if full else "")
        for r in self.conn.execute(f"SELECT {cols} FROM term_tree ORDER BY ord"):
            dom = out.setdefault(r["domain"], {"id": r["domain"], "zh": doms.get(r["domain"], {}).get("zh"),
                                               "en": doms.get(r["domain"], {}).get("en"),
                                               "ord": doms.get(r["domain"], {}).get("ord", 99), "n": 0, "sections": {}})
            sec = dom["sections"].setdefault(r["section"], {"section": r["section"], "n": 0, "subs": {}})
            sub = sec["subs"].setdefault(r["sub_name"], {"name": r["sub_name"], "order": r["sub_ord"], "n": 0, "terms": []})
            term = {"term": r["term"], "zh": r["zh"], "type": r["type"], "concept_id": r["concept_id"], "note": r["note"]}
            if full:
                term["example"], term["example_doc"] = r["example"], r["example_doc"]
            sub["terms"].append(term)
            sub["n"] += 1
            sec["n"] += 1
            dom["n"] += 1
        res = []
        for dom in sorted(out.values(), key=lambda d: (d["ord"] if d["ord"] is not None else 99, d["id"])):
            secs = []
            for sec in sorted(dom["sections"].values(), key=lambda s: SECTION_ORDER.get(s["section"], 99)):
                sec["subs"] = sorted(sec["subs"].values(), key=lambda s: (s["order"], s["name"]))
                secs.append(sec)
            dom["sections"] = secs
            res.append(dom)
        return res

    def categories_tree(self, domain: str | None = None, scope: str = "all") -> list[dict]:
        cats = _rows(self.conn.execute("SELECT * FROM categories" + (" WHERE domain = ?" if domain else "") + " ORDER BY ord, id",
                                       (domain,) if domain else ()))
        by_id = {c["id"]: dict(c, concepts=[], n=0, papers=0) for c in cats}
        rows = self.conn.execute(
            "SELECT cc.primary_cat, cc.secondary_cat, c.id, c.label, c.zh, c.type, COALESCE(i.papers, 0) AS papers, i.tier "
            "FROM concept_categories cc JOIN concepts c ON c.id = cc.concept_id "
            "LEFT JOIN importance i ON i.concept_id = c.id AND i.scope = ? ORDER BY papers DESC, c.label COLLATE NOCASE",
            (scope,)).fetchall()
        for r in rows:
            for cat_id, secondary in ((r["primary_cat"], False), (r["secondary_cat"], True)):
                if cat_id and cat_id in by_id:
                    by_id[cat_id]["concepts"].append({"id": r["id"], "label": r["label"], "zh": r["zh"], "type": r["type"],
                                                      "papers": r["papers"], "tier": r["tier"], "secondary": secondary})
                    by_id[cat_id]["n"] += 1
        fs = {r["id"]: dict(r) for r in self.conn.execute(
            "SELECT id, papers, core, base FROM framework_stats WHERE kind='category' AND scope=?", (scope,))}
        for c in by_id.values():
            if c["id"] in fs:
                c["papers"], c["base"] = fs[c["id"]]["papers"], fs[c["id"]]["base"]
        doms = self.domains()
        out = []
        for d in doms:
            if domain and d["id"] != domain:
                continue
            out.append({"id": d["id"], "zh": d["zh"], "en": d["en"],
                        "categories": [c for c in by_id.values() if c["domain"] == d["id"]]})
        return out

    def subdomain_tree(self, scope: str = "all") -> list[dict]:
        fs = {r["id"]: dict(r) for r in self.conn.execute(
            "SELECT id, papers, core, base FROM framework_stats WHERE kind='subdomain' AND scope=?", (scope,))}
        subs = self.subdomains_all()
        groups: dict[str, dict] = {}
        doms = {d["id"]: d for d in self.domains()}
        for s in subs:
            key = s["domain"] if s["kind"] == "within" and s["domain"] else "cross"
            g = groups.setdefault(key, {"id": key, "zh": doms.get(key, {}).get("zh") or ("交叉" if key == "cross" else key),
                                        "en": doms.get(key, {}).get("en") or ("Cross-domain" if key == "cross" else key),
                                        "ord": doms.get(key, {}).get("ord", 99) if key != "cross" else 999,
                                        "papers": None, "subs": []})
            st = fs.get(s["id"], {})
            g["subs"].append({"id": s["id"], "zh": s["zh"], "en": s["en"], "kind": s["kind"], "domain": s["domain"],
                              "desc_zh": s["desc_zh"], "desc_en": s["desc_en"], "papers": st.get("papers", 0),
                              "core": st.get("core", 0), "base": st.get("base", 0)})
            if st.get("base") and key != "cross":
                g["papers"] = st["base"]
        return sorted(groups.values(), key=lambda g: g["ord"])

    def subdomain(self, sub_id: str, top: int = 60, docs: int = 50) -> dict | None:
        r = self.conn.execute("SELECT * FROM subdomains WHERE id=?", (sub_id,)).fetchone()
        if r is None:
            return None
        s = dict(r)
        groups = self.anchors_all().get(sub_id, [])
        labels = self.concept_labels([c for g in groups for c in g])
        s["anchors"] = [[labels.get(c, {"id": c, "label": c}) for c in g] for g in groups]
        st = self.conn.execute("SELECT * FROM framework_stats WHERE kind='subdomain' AND id=? AND scope='all'", (sub_id,)).fetchone()
        st = dict(st) if st else {}
        s["papers"], s["core"], s["base"] = st.get("papers", 0), st.get("core", 0), st.get("base", 0)
        s["years"] = _loads(st.get("years_json"), {})
        subnames = {x["id"]: x for x in self.subdomains_all()}
        s["overlaps"] = [{"id": o, "zh": subnames.get(o, {}).get("zh"), "en": subnames.get(o, {}).get("en"), "papers": n}
                         for o, n in _loads(st.get("overlaps_json"), [])]
        s["n_concepts"] = int(self.conn.execute("SELECT COUNT(*) FROM subdomain_concepts WHERE sub_id=?", (sub_id,)).fetchone()[0])
        s["concepts"] = _rows(self.conn.execute(
            "SELECT sc.*, c.label, c.zh, c.type, cc.primary_cat AS category, cc.domain AS cat_domain FROM subdomain_concepts sc "
            "JOIN concepts c ON c.id = sc.concept_id LEFT JOIN concept_categories cc ON cc.concept_id = sc.concept_id "
            "AND cc.domain = ? WHERE sc.sub_id=? ORDER BY sc.rank LIMIT ?", (s["domain"] or "", sub_id, int(top))))
        s["tiers"] = {x["tier"]: x["n"] for x in self.conn.execute(
            "SELECT tier, COUNT(*) n FROM unit_subdomains WHERE sub_id=? GROUP BY tier", (sub_id,))}
        s["docs"] = self.subdomain_docs(sub_id, limit=docs)["rows"]
        return s

    def subdomain_docs(self, sub_id: str, tier: str | None = None, q: str | None = None, sort: str = "hits",
                       limit: int = 50, offset: int = 0) -> dict:
        where = ["us.sub_id = ?"]
        args: list = [sub_id]
        if tier in ("core", "related"):
            where.append("us.tier = ?")
            args.append(tier)
        if q:
            where.append("m.title LIKE ? ESCAPE '\\'")
            args.append(_like(q))
        base = ("FROM unit_subdomains us JOIN build_units u ON u.unit_id = us.unit_id "
                "JOIN build_doc_meta m ON m.doc_id = u.doc_id WHERE " + " AND ".join(where))
        order = {"hits": "us.n_hits DESC, m.year DESC, u.doc_id", "year": "m.year DESC, us.n_hits DESC, u.doc_id",
                 "year_asc": "m.year ASC, u.doc_id", "title": "m.title COLLATE NOCASE"}.get(sort or "hits", "us.n_hits DESC, u.doc_id")
        total = int(self.conn.execute("SELECT COUNT(*) " + base, args).fetchone()[0])
        rows = _rows(self.conn.execute(
            f"SELECT u.unit_id, u.doc_id, u.kind, u.heading, m.title, m.year, m.venue, m.doc_type, us.tier, us.hits_json, us.n_hits "
            f"{base} ORDER BY {order} LIMIT ? OFFSET ?", (*args, int(limit), int(offset))))
        ids = set()
        for r in rows:
            r["hits"] = _loads(r.pop("hits_json"), {})
            ids.update(r["hits"])
        labels = self.concept_labels(ids)
        for r in rows:
            r["hits"] = [{"id": k, "label": labels.get(k, {}).get("label", k), "n": n}
                         for k, n in sorted(r["hits"].items(), key=lambda kv: (-kv[1], kv[0]))]
        return {"rows": rows, "total": total}

    # ---- document extension ----------------------------------------------------------------------------------
    def doc_extension(self, doc_id: str, top: int = 12) -> dict | None:
        m = self.doc_meta(doc_id)
        if m is None:
            return None
        subs = _rows(self.conn.execute(
            "SELECT us.unit_id, us.sub_id, us.tier, us.n_hits, s.zh, s.en, s.kind FROM unit_subdomains us "
            "JOIN build_units u ON u.unit_id = us.unit_id JOIN subdomains s ON s.id = us.sub_id WHERE u.doc_id=? "
            "ORDER BY us.tier = 'core' DESC, s.ord", (doc_id,)))
        tops = _rows(self.conn.execute(
            "SELECT k.concept_id AS id, c.label, c.zh, k.n_sa AS n FROM counts k JOIN concepts c ON c.id = k.concept_id "
            "WHERE k.doc_id=? AND k.section='body' AND k.n_sa > 0 ORDER BY k.n_sa DESC, c.label LIMIT ?", (doc_id, int(top))))
        return {"domains": m["domains"], "subdomains": subs, "top_concepts": tops, "units": self.units_of(doc_id)}

    def page_concepts(self, doc_id: str, page_no: int, concept_ids: Sequence[str]) -> dict[str, list[dict]]:
        ids = list(dict.fromkeys(concept_ids))[:_IN_CHUNK]
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        out: dict[str, list[dict]] = {}
        for r in self.conn.execute(
                f"SELECT o.sid, o.concept_id, o.cstart, o.cend, o.nested_in FROM occurrences o "
                f"WHERE o.doc_id=? AND o.page_no=? AND o.concept_id IN ({marks}) "
                "ORDER BY o.sid, o.cstart", (doc_id, int(page_no), *ids)):
            out.setdefault(r["sid"], []).append({"id": r["concept_id"], "cstart": r["cstart"], "cend": r["cend"],
                                                 "nested_in": r["nested_in"]})
        return out

    def subdomain_label(self, sub_id: str) -> dict | None:
        r = self.conn.execute("SELECT id, zh, en, kind, domain FROM subdomains WHERE id=?", (sub_id,)).fetchone()
        return dict(r) if r else None
