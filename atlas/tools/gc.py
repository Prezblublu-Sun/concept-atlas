"""`atlas evidence gc [--vacuum] [--dry-run]`: remove superseded extractions from evidence.sqlite.

The evidence store is append-only: every re-ingest with a changed rule version adds a generation of extractions
(pages, line masks, units, extraction_sentences) and keeps the old one. On a hard disk those superseded rows are
pure cost — the femstem store held three generations (1.5 GB) of which the server ever read one. This tool deletes
what nothing references any more:

- an extraction is **kept** when it is a document's current extraction (`is_current = 1`), when any build pins it
  (`builds/*/build.sqlite` → `build_docs.ext_id`; every build folder with a build.sqlite counts, promoted or not —
  delete the build folder to release its generation), or when a note / collection refers to one of its sentences
  (`user.sqlite`: `S:<sid>` in note_targets.ref / resolved_ref, note_links.ref, collection_items.ref) and that
  sentence belongs to no kept extraction (the newest such extraction is kept, so the text and one membership
  survive and `resolve` keeps working);
- every other extraction is removed: its pages / line_masks / units / extraction_sentences rows, then the
  sentences whose sid no longer has any extraction_sentences row (their FTS rows go through the existing AFTER
  DELETE triggers), then the page_text rows no page references any more, then the extractions row;
- one `BEGIN IMMEDIATE … COMMIT` per extraction, so an interrupted run leaves a consistent store and the next
  run continues where it stopped;
- `--vacuum` runs VACUUM afterwards and then `EvidenceStore.rebuild_fts()`: VACUUM may renumber the rowids of
  `sentences` / `page_text` (TEXT primary keys), on which the external-content FTS tables are keyed. Stop
  `atlas serve` first (VACUUM needs the file to itself). If a run is interrupted between the VACUUM and the
  rebuild, `atlas evidence gc --rebuild-fts` repairs the index.
- refuses while any build is `open` (a stage may still be reading the evidence it pinned).

`sentence_aliases` rows are kept (they let old sentence ids in notes resolve to live ones)."""
from __future__ import annotations

import logging
import time
from pathlib import Path

from atlas.config import Project
from atlas.store.build import DB_FILE, list_build_dirs, read_manifest
from atlas.store.db import connect, transaction
from atlas.store.evidence import EvidenceStore
from atlas.store.user import UserStore

log = logging.getLogger(__name__)

_IN_CHUNK = 900


class GcError(Exception):
    pass


def _chunks(items: list, n: int = _IN_CHUNK):
    for i in range(0, len(items), n):
        yield items[i:i + n]


def pinned_extractions(project: Project) -> dict[str, set[str]]:
    """{build_id: set(ext_id)} for every build folder that has a build.sqlite (open or finished, promoted or not)."""
    out: dict[str, set[str]] = {}
    for d in list_build_dirs(project.builds_dir):
        db = d / DB_FILE
        if not db.is_file():
            continue
        m = read_manifest(d) or {}
        mode = "immutable" if m.get("status") == "finished" else "ro"
        conn = connect(db, mode)
        try:
            out[d.name] = {r[0] for r in conn.execute("SELECT ext_id FROM build_docs")}
        finally:
            conn.close()
    return out


def open_builds(project: Project) -> list[str]:
    return [d.name for d in list_build_dirs(project.builds_dir) if (read_manifest(d) or {}).get("status") == "open"]


def referenced_sids(project: Project) -> set[str]:
    """Sentence ids that notes / collections point at (user.sqlite), or an empty set without a user store."""
    if not project.user_db.is_file():
        return set()
    us = UserStore.open(project.user_db, "ro")      # the planner only reads: no migration, no write lock, no WAL
    try:
        sids: set[str] = set()
        for sql in ("SELECT ref FROM note_targets UNION SELECT resolved_ref FROM note_targets",
                    "SELECT ref FROM note_links", "SELECT ref FROM collection_items"):
            for r in us.conn.execute(sql):
                ref = r[0]
                if isinstance(ref, str) and ref.startswith("S:") and len(ref) > 2:
                    sids.add(ref[2:])
        return sids
    finally:
        us.close()


def plan(project: Project) -> dict:
    """What a run would do: {kept: {current, pinned, protected}, remove: [extraction dicts], builds: {id: n_pinned},
    open_builds: [...]} — raises GcError while a build is open or without an evidence store."""
    if not project.evidence_db.is_file():
        raise GcError(f"no evidence store at {project.evidence_db}")
    opened = open_builds(project)
    if opened:
        raise GcError(f"build(s) still open: {', '.join(opened)} — finish them (`atlas run finish`) or delete the folder")
    pins = pinned_extractions(project)
    pinned = set().union(*pins.values()) if pins else set()
    store = EvidenceStore.open(project.evidence_db, "ro")
    try:
        exts = [dict(r) for r in store.conn.execute(
            "SELECT ext_id, doc_id, extractor_version, created, n_pages, n_sentences, is_current FROM extractions "
            "ORDER BY doc_id, created")]
        current = {e["ext_id"] for e in exts if e["is_current"]}
        kept = current | pinned
        protected: set[str] = set()
        refs = sorted(referenced_sids(project))
        for part in _chunks(refs):
            marks = ",".join("?" * len(part))
            by_sid: dict[str, list[tuple[str, str]]] = {}
            for r in store.conn.execute(
                    f"SELECT es.sid, es.ext_id, e.created FROM extraction_sentences es JOIN extractions e ON e.ext_id = es.ext_id "
                    f"WHERE es.sid IN ({marks})", part):
                by_sid.setdefault(r["sid"], []).append((r["created"], r["ext_id"]))
            for sid, members in by_sid.items():
                if any(x in kept for _, x in members):
                    continue
                protected.add(max(members)[1])         # the newest extraction that still holds the sentence
        kept |= protected
        remove = [e for e in exts if e["ext_id"] not in kept]
    finally:
        store.close()
    return {"kept": {"current": len(current), "pinned": len(pinned - current), "protected": len(protected - current - pinned)},
            "remove": remove, "builds": {b: len(s) for b, s in pins.items()}, "open_builds": opened,
            "extractions_total": len(exts)}


def _delete_extraction(store: EvidenceStore, ext_id: str) -> dict[str, int]:
    c = store.conn
    n: dict[str, int] = {}
    with transaction(c):
        sids = [r[0] for r in c.execute("SELECT sid FROM extraction_sentences WHERE ext_id=?", (ext_id,))]
        shas = [r[0] for r in c.execute("SELECT DISTINCT text_sha256 FROM pages WHERE ext_id=?", (ext_id,))]
        n["extraction_sentences"] = c.execute("DELETE FROM extraction_sentences WHERE ext_id=?", (ext_id,)).rowcount
        n["sentences"] = 0
        for part in _chunks(sids):
            marks = ",".join("?" * len(part))
            n["sentences"] += c.execute(
                f"DELETE FROM sentences WHERE sid IN ({marks}) AND NOT EXISTS "
                "(SELECT 1 FROM extraction_sentences es WHERE es.sid = sentences.sid)", part).rowcount
        n["units"] = c.execute("DELETE FROM units WHERE ext_id=?", (ext_id,)).rowcount
        n["line_masks"] = c.execute("DELETE FROM line_masks WHERE ext_id=?", (ext_id,)).rowcount
        n["pages"] = c.execute("DELETE FROM pages WHERE ext_id=?", (ext_id,)).rowcount
        n["page_text"] = 0
        for part in _chunks(shas):
            marks = ",".join("?" * len(part))
            n["page_text"] += c.execute(
                f"DELETE FROM page_text WHERE text_sha256 IN ({marks}) AND NOT EXISTS "
                "(SELECT 1 FROM pages p WHERE p.text_sha256 = page_text.text_sha256)", part).rowcount
        n["extractions"] = c.execute("DELETE FROM extractions WHERE ext_id=?", (ext_id,)).rowcount
    return n


def _file_bytes(path: Path) -> int:
    total = 0
    for suffix in ("", "-wal"):
        p = Path(str(path) + suffix)
        try:
            total += p.stat().st_size
        except OSError:
            pass
    return total


def rebuild_fts(project: Project) -> float:
    """`EvidenceStore.rebuild_fts()` on its own (recovery after an interrupted `--vacuum`)."""
    t0 = time.perf_counter()
    store = EvidenceStore.open(project.evidence_db, "rw")
    try:
        store.rebuild_fts()
    finally:
        store.close()
    return time.perf_counter() - t0


def run(project: Project, vacuum: bool = False, dry_run: bool = False) -> dict:
    """Execute the plan. Returns the plan plus {removed: n, rows: {table: n}, by_version: {version: n},
    bytes_before, bytes_after, seconds, vacuum: bool, fts_rebuilt: bool}."""
    t0 = time.perf_counter()
    p = plan(project)
    rows: dict[str, int] = {}
    by_version: dict[str, int] = {}
    before = _file_bytes(project.evidence_db)
    report = dict(p, removed=0, rows=rows, by_version=by_version, bytes_before=before, bytes_after=before,
                  seconds=0.0, vacuum=False, fts_rebuilt=False, dry_run=dry_run)
    for e in p["remove"]:
        by_version[e["extractor_version"]] = by_version.get(e["extractor_version"], 0) + 1
    if dry_run:
        report["seconds"] = round(time.perf_counter() - t0, 2)
        return report
    store = EvidenceStore.open(project.evidence_db, "rw")
    try:
        with store.bulk_writes():
            for i, e in enumerate(p["remove"], start=1):
                n = _delete_extraction(store, e["ext_id"])
                for k, v in n.items():
                    rows[k] = rows.get(k, 0) + int(v)
                report["removed"] += 1
                if i % 200 == 0:
                    log.info("gc: %d/%d extractions removed", i, len(p["remove"]))
                    store.checkpoint("PASSIVE")
        if vacuum:
            t1 = time.perf_counter()
            store.conn.execute("VACUUM")
            log.info("gc: VACUUM in %.1f s", time.perf_counter() - t1)
            report["vacuum"] = True
            t1 = time.perf_counter()
            store.rebuild_fts()
            store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            log.info("gc: FTS rebuilt in %.1f s", time.perf_counter() - t1)
            report["fts_rebuilt"] = True
    finally:
        store.close()
    report["bytes_after"] = _file_bytes(project.evidence_db)
    report["seconds"] = round(time.perf_counter() - t0, 2)
    return report


def format_report(r: dict) -> str:
    mb = lambda b: f"{b / 1048576:.0f} MB"  # noqa: E731
    lines = [f"evidence gc{' (dry run)' if r.get('dry_run') else ''}: {r['extractions_total']} extractions; kept "
             f"{r['kept']['current']} current, {r['kept']['pinned']} pinned by builds, {r['kept']['protected']} protected by notes; "
             f"{'would remove' if r.get('dry_run') else 'removed'} {len(r['remove']) if r.get('dry_run') else r['removed']}"]
    for b, n in sorted(r.get("builds", {}).items()):
        lines.append(f"  build {b}: pins {n} extractions")
    for v, n in sorted(r.get("by_version", {}).items()):
        lines.append(f"  {v}: {n}")
    if not r.get("dry_run"):
        for t, n in sorted(r.get("rows", {}).items()):
            lines.append(f"  rows deleted from {t}: {n}")
        lines.append(f"  file: {mb(r['bytes_before'])} -> {mb(r['bytes_after'])}"
                     + (" (VACUUM + FTS rebuild)" if r.get("vacuum") else " (no VACUUM: pages are reused by the next ingest)"))
    lines.append(f"  seconds: {r['seconds']}")
    return "\n".join(lines)
