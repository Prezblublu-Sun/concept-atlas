"""AtlasService — the single façade. Implemented by: increment 1, agent "service+http".

Every method returns JSON-serialisable dicts/lists. Every evidence pointer is a *ref object*:
    {"ref": "S:S1…", "doc_id": "P123", "title": "...", "year": 2021, "doi": "...", "unit_id": "P123",
     "page": 4, "sid": "S1…", "text": "...", "status": "live", "flags": []}
Every label is a {"zh": ..., "en": ...} pair. Thread-safety: one EvidenceStore/UserStore connection per thread
(use threading.local inside the service); the service is constructed once per server process.
"""
from __future__ import annotations

from atlas.config import Project


class AtlasService:
    def __init__(self, project: Project):
        self.project = project

    # ---- meta
    def info(self) -> dict:
        """{project, build: 'evidence' (no builds yet), stats: EvidenceStore.stats(), version}"""
        raise NotImplementedError

    def i18n(self) -> dict:
        """UI chrome strings {key: {zh, en}} loaded from http/static/i18n.json plus section/tag labels from
        atlas.core.schemas (SECTION_ZH; English = the key capitalised)."""
        raise NotImplementedError

    # ---- documents and reading
    def list_docs(self, q: str | None = None, limit: int = 100, offset: int = 0) -> dict: ...
    def get_doc(self, doc_id: str) -> dict:
        """Document dict + pages:[{page_no, n_sentences}] + notes_count."""
        raise NotImplementedError

    def page(self, doc_id: str, page_no: int) -> dict:
        """EvidenceStore.page() + for each sentence the number of notes targeting it."""
        raise NotImplementedError

    def page_png(self, doc_id: str, page_no: int, highlight: str | None = None,
                 spans: list[tuple[int, int]] | None = None, zoom: float | None = None) -> tuple[bytes, dict]: ...

    def pdf_path(self, doc_id: str) -> str | None: ...

    def search(self, phrase: str, doc_id: str | None = None, limit: int = 50, offset: int = 0) -> dict:
        """Sentence phrase search (EvidenceStore.phrase_search) with each row expanded to a ref object."""
        raise NotImplementedError

    def sentence(self, sid: str, context: int = 1) -> dict:
        """Ref object + before/after + notes targeting it."""
        raise NotImplementedError

    def resolve(self, ref: str) -> dict:
        """Any ref → ref object (status live|aliased|orphan). Only D:, U:, S:, N:, K: resolve in increment 1."""
        raise NotImplementedError

    # ---- notes and collections (thin pass-through to UserStore, snapshots taken here)
    def create_note(self, title: str, body_md: str, tags: list[str], target_refs: list[str]) -> dict:
        """Takes a snapshot (text, doc, page) for every target ref by resolving it first."""
        raise NotImplementedError

    def update_note(self, note_id: str, rev: int, title: str | None, body_md: str | None,
                    tags: list[str] | None, explicit: bool) -> dict:
        """Returns {'note': …} or {'conflict': True, 'server': …} (never raises for a stale rev)."""
        raise NotImplementedError

    def get_note(self, note_id: str) -> dict: ...
    def delete_note(self, note_id: str) -> dict: ...
    def list_notes(self, q: str | None = None, tag: str | None = None, limit: int = 50, offset: int = 0) -> dict: ...
    def notes_for(self, ref: str) -> list[dict]: ...
    def note_history(self, note_id: str) -> list[dict]: ...

    def collections(self) -> list[dict]: ...
    def create_collection(self, name: str, description: str = "") -> dict: ...
    def collection(self, coll_id: str) -> dict: ...
    def add_to_collection(self, coll_id: str, ref: str | None, comment_md: str = "", after_item: str | None = None) -> dict:
        """Snapshot = resolve(ref) when ref is given."""
        raise NotImplementedError

    def update_item(self, item_id: str, comment_md: str | None = None, seq: float | None = None) -> dict: ...
    def remove_item(self, item_id: str) -> dict: ...
    def export_collection_md(self, coll_id: str) -> str:
        """Markdown: title, description, items in order (quoted text with '[doc_id p.N]' pointers and the comment),
        then a numbered reference list (authors, year, title, venue, DOI link) built from document_meta."""
        raise NotImplementedError
