"""UserStore: notes, collections, annotations over user.sqlite. Implemented by: increment 1, agent "store".

Concurrency: the HTTP server, the MCP process and the CLI may all write; every method is one short
transaction (BEGIN IMMEDIATE … COMMIT) and relies on WAL + busy_timeout from atlas.store.db.

Timestamps are UTC ISO-8601 (seconds) taken from `time.time()` so tests can monkeypatch the clock.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

from atlas.core.ids import random_id
from atlas.core.schemas import find_refs
from atlas.store.db import open_user, transaction

log = logging.getLogger(__name__)


class ConflictError(Exception):
    """Raised by update_note when the client's rev is stale; carries the server copy."""
    def __init__(self, server_note: dict):
        super().__init__("note changed elsewhere")
        self.server_note = server_note


def _now() -> str:
    return datetime.fromtimestamp(time.time(), tz=timezone.utc).isoformat(timespec="seconds")


def _parse_ts(s: str) -> float:
    try:
        return datetime.fromisoformat(s).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _tags_str(tags: list[str] | None) -> str:
    """['Def', ' quant ', ''] → ',def,quant,' (lower-case, deduplicated, comma-wrapped); [] → ''."""
    out: list[str] = []
    for t in tags or []:
        t = str(t).strip().lower().replace(",", " ").strip()
        if t and t not in out:
            out.append(t)
    return f",{','.join(out)}," if out else ""


def _tags_list(s: str | None) -> list[str]:
    return [t for t in (s or "").split(",") if t]


def _fts_terms(q: str) -> str:
    toks = [t.replace('"', "") for t in (q or "").split()]
    return " ".join(f'"{t}"' for t in toks if t)


def _links(body_md: str) -> list[str]:
    seen: list[str] = []
    for r in find_refs(body_md or ""):
        s = str(r)
        if s not in seen:
            seen.append(s)
    return seen


def _json_or_none(v: dict | str | None) -> str | None:
    if v is None:
        return None
    if isinstance(v, str):
        return v
    return json.dumps(v, ensure_ascii=False, sort_keys=True)


def _loads(s: str | None):
    if not s:
        return None
    try:
        return json.loads(s)
    except ValueError:
        return None


def _chunks(items: list, n: int):
    for i in range(0, len(items), n):
        yield items[i:i + n]


class UserStore:
    HISTORY_THROTTLE_MIN = 10
    ANNOTATION_VERSIONS = 10
    IN_CHUNK = 300              # refs/ids per IN (...) list (well under SQLite's bound-variable limit)

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    @classmethod
    def open(cls, path: str | Path, mode: str = "rw") -> "UserStore":
        return cls(open_user(path, mode))

    def close(self) -> None:
        self.conn.close()

    # ---- notes --------------------------------------------------------------------------------------------
    def _set_links(self, c: sqlite3.Connection, note_id: str, body_md: str) -> None:
        c.execute("DELETE FROM note_links WHERE note_id=?", (note_id,))
        c.executemany("INSERT OR IGNORE INTO note_links(note_id, ref) VALUES (?,?)",
                      [(note_id, ref) for ref in _links(body_md)])

    def _add_history(self, c: sqlite3.Connection, note_id: str, rev: int, title: str, body_md: str, tags: str,
                     reason: str, saved: str) -> None:
        c.execute("INSERT INTO note_history(note_id, rev, title, body_md, tags, saved, reason) "
                  "VALUES (?,?,?,?,?,?,?)", (note_id, rev, title, body_md, tags, saved, reason))

    def create_note(self, *, title: str = "", body_md: str = "", tags: list[str] | None = None,
                    targets: list[dict] | None = None) -> dict:
        """targets: [{ref, snapshot_text, snapshot_json, snapshot_build}]. Parses [[refs]] in body into
        note_links. Writes a note_history row (reason='explicit'). Returns get_note()."""
        note_id = random_id("N")
        now = _now()
        tg = _tags_str(tags)
        with transaction(self.conn) as c:
            c.execute("INSERT INTO notes(note_id, title, body_md, tags, rev, created, updated, deleted) "
                      "VALUES (?,?,?,?,1,?,?,0)", (note_id, title or "", body_md or "", tg, now, now))
            for t in targets or []:
                if not t or not t.get("ref"):
                    continue
                c.execute("INSERT OR REPLACE INTO note_targets(note_id, ref, snapshot_text, snapshot_json, "
                          "snapshot_build, status, resolved_ref) VALUES (?,?,?,?,?,'live',NULL)",
                          (note_id, t["ref"], t.get("snapshot_text"), _json_or_none(t.get("snapshot_json")),
                           t.get("snapshot_build")))
            self._set_links(c, note_id, body_md or "")
            self._add_history(c, note_id, 1, title or "", body_md or "", tg, "explicit", now)
        log.debug("note %s created", note_id)
        note = self.get_note(note_id)
        assert note is not None
        return note

    @staticmethod
    def _target_dict(r: sqlite3.Row) -> dict:
        return {"ref": r["ref"], "snapshot_text": r["snapshot_text"], "snapshot_json": _loads(r["snapshot_json"]),
                "snapshot_build": r["snapshot_build"], "status": r["status"], "resolved_ref": r["resolved_ref"]}

    def _targets(self, note_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT ref, snapshot_text, snapshot_json, snapshot_build, status, resolved_ref FROM note_targets "
            "WHERE note_id=? ORDER BY rowid", (note_id,)).fetchall()
        return [self._target_dict(r) for r in rows]

    def _targets_many(self, note_ids: list[str]) -> dict[str, list[dict]]:
        """{note_id: targets} for a page of notes in one query per IN_CHUNK ids (no per-note round trips)."""
        out: dict[str, list[dict]] = {n: [] for n in note_ids}
        for chunk in _chunks(note_ids, self.IN_CHUNK):
            marks = ",".join("?" * len(chunk))
            rows = self.conn.execute(
                "SELECT note_id, ref, snapshot_text, snapshot_json, snapshot_build, status, resolved_ref "
                f"FROM note_targets WHERE note_id IN ({marks}) ORDER BY rowid", chunk).fetchall()
            for r in rows:
                out[r["note_id"]].append(self._target_dict(r))
        return out

    def _note_dicts(self, rows: list[sqlite3.Row], with_body: bool = True) -> list[dict]:
        targets = self._targets_many([r["note_id"] for r in rows])
        return [self._note_dict(r, with_body, targets[r["note_id"]]) for r in rows]

    def _note_dict(self, r: sqlite3.Row, with_body: bool = True, targets: list[dict] | None = None) -> dict:
        d = {"note_id": r["note_id"], "title": r["title"], "tags": _tags_list(r["tags"]), "rev": r["rev"],
             "created": r["created"], "updated": r["updated"],
             "targets": self._targets(r["note_id"]) if targets is None else targets}
        if with_body:
            d["body_md"] = r["body_md"]
            d["links"] = [x[0] for x in self.conn.execute(
                "SELECT ref FROM note_links WHERE note_id=? ORDER BY rowid", (r["note_id"],))]
        else:
            body = r["body_md"] or ""
            d["excerpt"] = body[:200]
        return d

    def get_note(self, note_id: str) -> dict | None:
        """{note_id, title, body_md, tags:[...], rev, created, updated, targets:[{ref, snapshot_text, status,
        resolved_ref}], links:[refs]}"""
        r = self.conn.execute("SELECT * FROM notes WHERE note_id=? AND deleted=0", (note_id,)).fetchone()
        return self._note_dict(r) if r is not None else None

    def update_note(self, note_id: str, *, rev: int, title: str | None = None, body_md: str | None = None,
                    tags: list[str] | None = None, explicit: bool = False) -> dict:
        """Optimistic concurrency: if `rev` != stored rev → ConflictError(server copy) and the attempted body is
        saved to note_history with reason='conflict'. Otherwise bump rev, update, re-parse links, and write
        note_history when `explicit` or when the last history row is older than HISTORY_THROTTLE_MIN minutes."""
        now = _now()
        with transaction(self.conn) as c:
            r = c.execute("SELECT * FROM notes WHERE note_id=? AND deleted=0", (note_id,)).fetchone()
            if r is None:
                raise KeyError(f"unknown note {note_id!r}")
            if int(rev) != int(r["rev"]):
                self._add_history(c, note_id, int(rev), title if title is not None else r["title"],
                                  body_md if body_md is not None else r["body_md"],
                                  _tags_str(tags) if tags is not None else r["tags"], "conflict", now)
                server = self._note_dict(r)
                log.info("note %s: stale rev %s (server %s)", note_id, rev, r["rev"])
                conflict = ConflictError(server)
            else:
                conflict = None
            if conflict is None:
                new_rev = int(r["rev"]) + 1
                new_title = r["title"] if title is None else title
                new_body = r["body_md"] if body_md is None else body_md
                new_tags = r["tags"] if tags is None else _tags_str(tags)
                c.execute("UPDATE notes SET title=?, body_md=?, tags=?, rev=?, updated=? WHERE note_id=?",
                          (new_title, new_body, new_tags, new_rev, now, note_id))
                if body_md is not None:
                    self._set_links(c, note_id, new_body)
                last = c.execute("SELECT saved FROM note_history WHERE note_id=? AND reason<>'conflict' "
                                 "ORDER BY id DESC LIMIT 1", (note_id,)).fetchone()
                stale = last is None or (
                    _parse_ts(now) - _parse_ts(last["saved"]) >= self.HISTORY_THROTTLE_MIN * 60)
                if explicit or stale:
                    self._add_history(c, note_id, new_rev, new_title, new_body, new_tags,
                                      "explicit" if explicit else "throttle", now)
        if conflict is not None:
            raise conflict
        note = self.get_note(note_id)
        assert note is not None
        return note

    def delete_note(self, note_id: str) -> None:
        """Soft delete (deleted=1) — the FTS trigger removes it from search."""
        with transaction(self.conn) as c:
            c.execute("UPDATE notes SET deleted=1, updated=? WHERE note_id=? AND deleted=0", (_now(), note_id))

    def add_target(self, note_id: str, ref: str, snapshot_text: str | None = None, snapshot_json: dict | None = None,
                   snapshot_build: str | None = None) -> None:
        with transaction(self.conn) as c:
            if c.execute("SELECT 1 FROM notes WHERE note_id=?", (note_id,)).fetchone() is None:
                raise KeyError(f"unknown note {note_id!r}")
            c.execute("INSERT OR REPLACE INTO note_targets(note_id, ref, snapshot_text, snapshot_json, "
                      "snapshot_build, status, resolved_ref) VALUES (?,?,?,?,?,'live',NULL)",
                      (note_id, ref, snapshot_text, _json_or_none(snapshot_json), snapshot_build))
            c.execute("UPDATE notes SET updated=? WHERE note_id=?", (_now(), note_id))

    def remove_target(self, note_id: str, ref: str) -> None:
        with transaction(self.conn) as c:
            c.execute("DELETE FROM note_targets WHERE note_id=? AND ref=?", (note_id, ref))
            c.execute("UPDATE notes SET updated=? WHERE note_id=?", (_now(), note_id))

    def set_target_status(self, note_id: str, ref: str, status: str, resolved_ref: str | None = None) -> None:
        """live | aliased | orphan, as determined by the service when it resolves the target."""
        with transaction(self.conn) as c:
            c.execute("UPDATE note_targets SET status=?, resolved_ref=? WHERE note_id=? AND ref=?",
                      (status, resolved_ref, note_id, ref))

    def notes_for_ref(self, ref: str) -> list[dict]:
        """Notes targeting `ref` or linking to it in their body, newest first (not deleted)."""
        rows = self.conn.execute(
            "SELECT n.* FROM notes n WHERE n.deleted=0 AND n.note_id IN ("
            "  SELECT note_id FROM note_targets WHERE ref=? OR resolved_ref=? "
            "  UNION SELECT note_id FROM note_links WHERE ref=?) "
            "ORDER BY n.updated DESC, n.note_id DESC", (ref, ref, ref)).fetchall()
        return self._note_dicts(rows)

    def note_counts(self, refs: Iterable[str]) -> dict[str, int]:
        """{ref: number of live notes that target it (ref or resolved_ref) or link to it} for every ref given
        (zero included) — one query per IN_CHUNK refs instead of a notes_for_ref() call per ref (review fix)."""
        wanted = list(dict.fromkeys(r for r in refs if r))
        out: dict[str, int] = {r: 0 for r in wanted}
        for chunk in _chunks(wanted, self.IN_CHUNK):
            marks = ",".join("?" * len(chunk))
            rows = self.conn.execute(
                "SELECT x.ref AS ref, COUNT(DISTINCT x.note_id) AS n FROM ("
                f"  SELECT t.ref AS ref, t.note_id FROM note_targets t WHERE t.ref IN ({marks})"
                f"  UNION SELECT t.resolved_ref, t.note_id FROM note_targets t WHERE t.resolved_ref IN ({marks})"
                f"  UNION SELECT l.ref, l.note_id FROM note_links l WHERE l.ref IN ({marks})"
                ") x JOIN notes n ON n.note_id = x.note_id AND n.deleted = 0 GROUP BY ref",
                (*chunk, *chunk, *chunk)).fetchall()
            for r in rows:
                out[r["ref"]] = int(r["n"])
        return out

    def list_notes(self, q: str | None = None, tag: str | None = None, limit: int = 50, offset: int = 0) -> dict:
        """{'rows': [note summaries with targets], 'total': n}; `q` uses note_fts, `tag` filters tags."""
        where = ["n.deleted=0"]
        params: list = []
        terms = _fts_terms(q or "")
        if terms:
            where.append("n.note_id IN (SELECT note_id FROM note_fts WHERE note_fts MATCH ?)")
            params.append(terms)
        if tag:
            t = str(tag).strip().lower()
            if t:
                where.append("n.tags LIKE ?")
                params.append(f"%,{t},%")
        w = " WHERE " + " AND ".join(where)
        try:
            total = self.conn.execute("SELECT COUNT(*) FROM notes n" + w, params).fetchone()[0]
            rows = self.conn.execute("SELECT n.* FROM notes n" + w + " ORDER BY n.updated DESC, n.note_id DESC "
                                     "LIMIT ? OFFSET ?", (*params, limit, offset)).fetchall()
        except sqlite3.OperationalError as e:
            log.warning("list_notes failed for q=%r: %s", q, e)
            return {"rows": [], "total": 0}
        return {"rows": self._note_dicts(rows, with_body=False), "total": int(total)}

    def history(self, note_id: str) -> list[dict]:
        rows = self.conn.execute("SELECT id, note_id, rev, title, body_md, tags, saved, reason FROM note_history "
                                 "WHERE note_id=? ORDER BY id DESC", (note_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["tags"] = _tags_list(d["tags"])
            out.append(d)
        return out

    # ---- collections --------------------------------------------------------------------------------------
    def create_collection(self, name: str, description: str = "") -> dict:
        coll_id = random_id("K")
        now = _now()
        with transaction(self.conn) as c:
            c.execute("INSERT INTO collections(coll_id, name, description, created, updated, deleted) "
                      "VALUES (?,?,?,?,?,0)", (coll_id, name or "", description or "", now, now))
        coll = self.get_collection(coll_id)
        assert coll is not None
        return coll

    def list_collections(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT k.coll_id, k.name, k.description, k.created, k.updated, "
            "(SELECT COUNT(*) FROM collection_items i WHERE i.coll_id=k.coll_id) AS n_items "
            "FROM collections k WHERE k.deleted=0 ORDER BY k.updated DESC, k.coll_id DESC").fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def _item_dict(r: sqlite3.Row) -> dict:
        return {"item_id": r["item_id"], "coll_id": r["coll_id"], "seq": r["seq"], "ref": r["ref"],
                "snapshot": _loads(r["snapshot_json"]), "comment_md": r["comment_md"], "added": r["added"]}

    def get_collection(self, coll_id: str) -> dict | None:
        """{coll_id, name, description, items:[{item_id, seq, ref, snapshot, comment_md, added}] ordered by seq}"""
        r = self.conn.execute("SELECT * FROM collections WHERE coll_id=? AND deleted=0", (coll_id,)).fetchone()
        if r is None:
            return None
        items = self.conn.execute("SELECT * FROM collection_items WHERE coll_id=? ORDER BY seq, added, item_id",
                                  (coll_id,)).fetchall()
        return {"coll_id": r["coll_id"], "name": r["name"], "description": r["description"],
                "created": r["created"], "updated": r["updated"], "items": [self._item_dict(i) for i in items]}

    def delete_collection(self, coll_id: str) -> None:
        with transaction(self.conn) as c:
            c.execute("UPDATE collections SET deleted=1, updated=? WHERE coll_id=?", (_now(), coll_id))

    def add_item(self, coll_id: str, ref: str | None, snapshot: dict | None, comment_md: str = "",
                 after_item: str | None = None) -> dict:
        """Append at the end, or right after `after_item` (seq = midpoint to the following item)."""
        item_id = random_id("KI")
        now = _now()
        with transaction(self.conn) as c:
            if c.execute("SELECT 1 FROM collections WHERE coll_id=? AND deleted=0", (coll_id,)).fetchone() is None:
                raise KeyError(f"unknown collection {coll_id!r}")
            if after_item:
                a = c.execute("SELECT seq FROM collection_items WHERE item_id=? AND coll_id=?",
                              (after_item, coll_id)).fetchone()
                if a is None:
                    raise KeyError(f"unknown item {after_item!r} in collection {coll_id!r}")
                prev = float(a["seq"])
                nxt = c.execute("SELECT MIN(seq) FROM collection_items WHERE coll_id=? AND seq>?",
                                (coll_id, prev)).fetchone()[0]
                seq = (prev + float(nxt)) / 2.0 if nxt is not None else prev + 1.0
            else:
                mx = c.execute("SELECT MAX(seq) FROM collection_items WHERE coll_id=?", (coll_id,)).fetchone()[0]
                seq = float(mx) + 1.0 if mx is not None else 1.0
            c.execute("INSERT INTO collection_items(item_id, coll_id, seq, ref, snapshot_json, comment_md, added) "
                      "VALUES (?,?,?,?,?,?,?)",
                      (item_id, coll_id, seq, ref, _json_or_none(snapshot), comment_md or "", now))
            c.execute("UPDATE collections SET updated=? WHERE coll_id=?", (now, coll_id))
        r = self.conn.execute("SELECT * FROM collection_items WHERE item_id=?", (item_id,)).fetchone()
        return self._item_dict(r)

    def get_item(self, item_id: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM collection_items WHERE item_id=?", (item_id,)).fetchone()
        return self._item_dict(r) if r is not None else None

    def update_item(self, item_id: str, comment_md: str | None = None, seq: float | None = None) -> None:
        sets, vals = [], []
        if comment_md is not None:
            sets.append("comment_md=?")
            vals.append(comment_md)
        if seq is not None:
            sets.append("seq=?")
            vals.append(float(seq))
        if not sets:
            return
        with transaction(self.conn) as c:
            r = c.execute("SELECT coll_id FROM collection_items WHERE item_id=?", (item_id,)).fetchone()
            if r is None:
                raise KeyError(f"unknown item {item_id!r}")
            c.execute(f"UPDATE collection_items SET {', '.join(sets)} WHERE item_id=?", (*vals, item_id))
            c.execute("UPDATE collections SET updated=? WHERE coll_id=?", (_now(), r["coll_id"]))

    def remove_item(self, item_id: str) -> None:
        with transaction(self.conn) as c:
            r = c.execute("SELECT coll_id FROM collection_items WHERE item_id=?", (item_id,)).fetchone()
            if r is None:
                return
            c.execute("DELETE FROM collection_items WHERE item_id=?", (item_id,))
            c.execute("UPDATE collections SET updated=? WHERE coll_id=?", (_now(), r["coll_id"]))

    # ---- annotations (PDF.js JSON) --------------------------------------------------------------------------
    def save_annotations(self, doc_id: str, annots_json: str, base_version: int, device: str | None = None) -> dict:
        """Append version base_version+1 if base_version is the latest, else ConflictError-like {'conflict': True,
        'version': latest}. Keep only the last ANNOTATION_VERSIONS versions. Returns {version, saved}."""
        if not isinstance(annots_json, str):
            annots_json = json.dumps(annots_json, ensure_ascii=False)
        now = _now()
        with transaction(self.conn) as c:
            latest = c.execute("SELECT MAX(version) FROM annotations WHERE doc_id=?", (doc_id,)).fetchone()[0]
            latest = int(latest) if latest is not None else 0
            if int(base_version) != latest:
                log.info("annotations %s: base %s != latest %s", doc_id, base_version, latest)
                return {"conflict": True, "version": latest}
            version = latest + 1
            c.execute("INSERT INTO annotations(doc_id, version, annots_json, saved, device) VALUES (?,?,?,?,?)",
                      (doc_id, version, annots_json, now, device))
            c.execute("DELETE FROM annotations WHERE doc_id=? AND version<=?",
                      (doc_id, version - self.ANNOTATION_VERSIONS))
        return {"version": version, "saved": now}

    def get_annotations(self, doc_id: str, version: int | None = None) -> dict | None:
        """{doc_id, version, annots_json, saved, versions:[{version, saved}]}"""
        if version is None:
            r = self.conn.execute("SELECT * FROM annotations WHERE doc_id=? ORDER BY version DESC LIMIT 1",
                                  (doc_id,)).fetchone()
        else:
            r = self.conn.execute("SELECT * FROM annotations WHERE doc_id=? AND version=?",
                                  (doc_id, int(version))).fetchone()
        if r is None:
            return None
        versions = self.conn.execute("SELECT version, saved FROM annotations WHERE doc_id=? ORDER BY version",
                                     (doc_id,)).fetchall()
        return {"doc_id": doc_id, "version": r["version"], "annots_json": r["annots_json"], "saved": r["saved"],
                "device": r["device"], "versions": [dict(v) for v in versions]}

    # ---- aliases --------------------------------------------------------------------------------------------
    def add_ref_alias(self, old_ref: str, new_ref: str, method: str | None = None, build_id: str | None = None) -> None:
        with transaction(self.conn) as c:
            c.execute("INSERT OR IGNORE INTO ref_aliases(old_ref, new_ref, method, build_id, ts) VALUES (?,?,?,?,?)",
                      (old_ref, new_ref, method, build_id, _now()))

    def resolve_ref(self, ref: str) -> tuple[str, str]:
        """Follow ref_aliases chains: (newest_ref, 'live'|'aliased')."""
        cur = ref
        seen = {cur}
        hops = 0
        while True:
            nxt = self.conn.execute("SELECT new_ref FROM ref_aliases WHERE old_ref=? AND new_ref<>old_ref "
                                    "ORDER BY ts DESC, new_ref LIMIT 1", (cur,)).fetchone()
            if nxt is None or nxt[0] in seen:
                break
            cur = nxt[0]
            seen.add(cur)
            hops += 1
        return cur, ("aliased" if hops else "live")
