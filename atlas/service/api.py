"""AtlasService — the single façade. Implemented by: increment 1, agent "service+http".

Every method returns JSON-serialisable dicts/lists. Every evidence pointer is a *ref object*:
    {"ref": "S:S1…", "doc_id": "P123", "title": "...", "year": 2021, "doi": "...", "unit_id": "P123",
     "page": 4, "sid": "S1…", "text": "...", "status": "live", "flags": []}
Every label is a {"zh": ..., "en": ...} pair. Thread-safety: one EvidenceStore/UserStore connection per thread
(use threading.local inside the service); the service is constructed once per server process.

Error convention (the HTTP adapter maps these): KeyError → 404, ValueError → 400, everything else → 500.
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

import atlas
from atlas.config import Project
from atlas.core.schemas import REF_KINDS, SECTION_ZH, SECTIONS, TAGS, parse_ref
from atlas.ingest import pdf as pdf_io
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

_RESOLVABLE = ("D", "U", "S", "N", "K")


def _ref_object(**kw) -> dict:
    base = {"ref": None, "kind": None, "doc_id": None, "title": None, "year": None, "doi": None, "unit_id": None,
            "page": None, "sid": None, "text": None, "status": "orphan", "flags": []}
    base.update(kw)
    return base


def _clip(text: str | None, n: int = 240) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


class AtlasService:
    def __init__(self, project: Project):
        self.project = project
        self._local = threading.local()
        self._ui_strings = self._load_ui_strings()
        log.info("service for project %s at %s", project.name, project.root)

    # ---- per-thread stores ----------------------------------------------------------------------------------
    def evidence(self) -> EvidenceStore:
        st = getattr(self._local, "evidence", None)
        if st is None:
            path = self.project.evidence_db
            # the server reads evidence read-only; a project that has not been ingested yet gets an empty DB
            st = EvidenceStore.open(path, "ro") if path.exists() else EvidenceStore.open(path, "rw")
            self._local.evidence = st
        return st

    def user(self) -> UserStore:
        st = getattr(self._local, "user", None)
        if st is None:
            st = UserStore.open(self.project.user_db, "rw")
            self._local.user = st
        return st

    def close(self) -> None:
        """Close this thread's connections (tests; the server's threads close theirs on exit)."""
        for name in ("evidence", "user"):
            st = getattr(self._local, name, None)
            if st is not None:
                try:
                    st.close()
                except Exception:  # pragma: no cover - defensive
                    log.debug("close %s failed", name, exc_info=True)
                setattr(self._local, name, None)

    # ---- meta
    def info(self) -> dict:
        """{project, build: 'evidence' (no builds yet), stats: EvidenceStore.stats(), version}"""
        return {"project": self.project.name, "build": BUILD, "stats": self.evidence().stats(),
                "version": atlas.__version__, "doc_type": self.project.get("doc_type", "paper")}

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
                "kinds": {ref kind letter: {zh, en}}}"""
        return {
            "ui": self._ui_strings,
            "sections": {s: {"zh": SECTION_ZH.get(s, s), "en": s.capitalize()} for s in SECTIONS},
            "tags": {t: {"zh": TAG_ZH.get(t, t), "en": TAG_EN.get(t, t.capitalize())} for t in TAGS},
            "kinds": {k: {"zh": KIND_ZH.get(v, v), "en": v.capitalize()} for k, v in REF_KINDS.items()},
        }

    # ---- documents and reading
    def _notes_count(self, ref: str) -> int:
        return len(self.user().notes_for_ref(ref))

    def list_docs(self, q: str | None = None, limit: int = 100, offset: int = 0) -> dict:
        res = self.evidence().list_documents(q, int(limit), int(offset))
        for row in res["rows"]:
            row["notes_count"] = self._notes_count(f"D:{row['doc_id']}")
        return res

    def _doc(self, doc_id: str) -> dict:
        d = self.evidence().get_document(doc_id)
        if d is None:
            raise KeyError(f"unknown document {doc_id!r}")
        return d

    def get_doc(self, doc_id: str) -> dict:
        """Document dict + pages:[{page_no, n_sentences}] + notes_count."""
        d = self._doc(doc_id)
        d["pages"] = self.evidence().pages_of(doc_id)
        d["notes_count"] = self._notes_count(f"D:{doc_id}")
        d["has_pdf"] = self.pdf_path(doc_id) is not None
        return d

    def page(self, doc_id: str, page_no: int) -> dict:
        """EvidenceStore.page() + for each sentence the number of notes targeting it (and its text)."""
        d = self._doc(doc_id)
        p = self.evidence().page(doc_id, int(page_no))
        if p is None:
            raise KeyError(f"no page {page_no} in {doc_id!r}")
        text = p.get("text") or ""
        for s in p["sentences"]:
            s["text"] = text[s["cstart"]:s["cend"]]
            s["notes"] = self._notes_count(f"S:{s['sid']}")
        p["n_pages"] = int(d.get("n_pages") or 0)
        p["title"] = d.get("title")
        return p

    def page_png(self, doc_id: str, page_no: int, highlight: str | None = None,
                 spans: list[tuple[int, int]] | None = None, zoom: float | None = None) -> tuple[bytes, dict]:
        path = self.pdf_path(doc_id)
        if path is None:
            raise KeyError(f"no PDF for {doc_id!r}")
        n_pages = int(self._doc(doc_id).get("n_pages") or 0)
        if int(page_no) < 1 or (n_pages and int(page_no) > n_pages):
            raise KeyError(f"no page {page_no} in {doc_id!r}")
        z = float(zoom) if zoom else float(self.project.get("ingest.page_render_zoom", 1.6))
        z = max(0.2, min(z, 5.0))
        return pdf_io.render_page(path, int(page_no), zoom=z, highlight=highlight or None, spans=spans or None)

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
        """Sentence phrase search (EvidenceStore.phrase_search) with each row expanded to a ref object."""
        phrase = (phrase or "").strip()
        res = self.evidence().phrase_search(phrase, doc_id or None, int(limit), int(offset))
        rows = []
        for r in res["rows"]:
            rows.append(_ref_object(ref=f"S:{r['sid']}", kind="S", doc_id=r["doc_id"], title=r.get("title"),
                                    year=r.get("year"), page=r["page_no"], sid=r["sid"], status="live",
                                    section=r.get("section"), is_body=r.get("is_body"), snippet=r.get("snippet")))
        return {"phrase": phrase, "doc_id": doc_id, "rows": rows, "total": res["total"],
                "documents": res["documents"]}

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

    # ---- notes and collections (thin pass-through to UserStore, snapshots taken here)
    def _snapshot(self, ref: str) -> dict:
        obj = self.resolve(ref)
        return {"ref": ref, "snapshot_text": obj.get("text"), "snapshot_json": obj, "snapshot_build": BUILD,
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
