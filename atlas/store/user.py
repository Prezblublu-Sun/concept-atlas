"""UserStore: notes, collections, annotations over user.sqlite. Implemented by: increment 1, agent "store".

Concurrency: the HTTP server, the MCP process and the CLI may all write; every method is one short
transaction (BEGIN IMMEDIATE … COMMIT) and relies on WAL + busy_timeout from atlas.store.db.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path


class ConflictError(Exception):
    """Raised by update_note when the client's rev is stale; carries the server copy."""
    def __init__(self, server_note: dict):
        super().__init__("note changed elsewhere")
        self.server_note = server_note


class UserStore:
    HISTORY_THROTTLE_MIN = 10
    ANNOTATION_VERSIONS = 10

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    @classmethod
    def open(cls, path: str | Path, mode: str = "rw") -> "UserStore":
        raise NotImplementedError("increment 1 / store")

    # ---- notes --------------------------------------------------------------------------------------------
    def create_note(self, *, title: str = "", body_md: str = "", tags: list[str] | None = None,
                    targets: list[dict] | None = None) -> dict:
        """targets: [{ref, snapshot_text, snapshot_json, snapshot_build}]. Parses [[refs]] in body into
        note_links. Writes a note_history row (reason='explicit'). Returns get_note()."""
        raise NotImplementedError

    def get_note(self, note_id: str) -> dict | None:
        """{note_id, title, body_md, tags:[...], rev, created, updated, targets:[{ref, snapshot_text, status,
        resolved_ref}], links:[refs]}"""
        raise NotImplementedError

    def update_note(self, note_id: str, *, rev: int, title: str | None = None, body_md: str | None = None,
                    tags: list[str] | None = None, explicit: bool = False) -> dict:
        """Optimistic concurrency: if `rev` != stored rev → ConflictError(server copy) and the attempted body is
        saved to note_history with reason='conflict'. Otherwise bump rev, update, re-parse links, and write
        note_history when `explicit` or when the last history row is older than HISTORY_THROTTLE_MIN minutes."""
        raise NotImplementedError

    def delete_note(self, note_id: str) -> None:
        """Soft delete (deleted=1) — the FTS trigger removes it from search."""
        raise NotImplementedError

    def add_target(self, note_id: str, ref: str, snapshot_text: str | None = None, snapshot_json: dict | None = None,
                   snapshot_build: str | None = None) -> None:
        raise NotImplementedError

    def remove_target(self, note_id: str, ref: str) -> None:
        raise NotImplementedError

    def notes_for_ref(self, ref: str) -> list[dict]:
        """Notes targeting `ref` or linking to it in their body, newest first (not deleted)."""
        raise NotImplementedError

    def list_notes(self, q: str | None = None, tag: str | None = None, limit: int = 50, offset: int = 0) -> dict:
        """{'rows': [note summaries with targets], 'total': n}; `q` uses note_fts, `tag` filters tags."""
        raise NotImplementedError

    def history(self, note_id: str) -> list[dict]:
        raise NotImplementedError

    # ---- collections --------------------------------------------------------------------------------------
    def create_collection(self, name: str, description: str = "") -> dict:
        raise NotImplementedError

    def list_collections(self) -> list[dict]:
        raise NotImplementedError

    def get_collection(self, coll_id: str) -> dict | None:
        """{coll_id, name, description, items:[{item_id, seq, ref, snapshot, comment_md, added}] ordered by seq}"""
        raise NotImplementedError

    def add_item(self, coll_id: str, ref: str | None, snapshot: dict | None, comment_md: str = "",
                 after_item: str | None = None) -> dict:
        raise NotImplementedError

    def update_item(self, item_id: str, comment_md: str | None = None, seq: float | None = None) -> None:
        raise NotImplementedError

    def remove_item(self, item_id: str) -> None:
        raise NotImplementedError

    # ---- annotations (PDF.js JSON) --------------------------------------------------------------------------
    def save_annotations(self, doc_id: str, annots_json: str, base_version: int, device: str | None = None) -> dict:
        """Append version base_version+1 if base_version is the latest, else ConflictError-like {'conflict': True,
        'version': latest}. Keep only the last ANNOTATION_VERSIONS versions. Returns {version, saved}."""
        raise NotImplementedError

    def get_annotations(self, doc_id: str, version: int | None = None) -> dict | None:
        """{doc_id, version, annots_json, saved, versions:[{version, saved}]}"""
        raise NotImplementedError

    # ---- aliases --------------------------------------------------------------------------------------------
    def resolve_ref(self, ref: str) -> tuple[str, str]:
        """Follow ref_aliases chains: (newest_ref, 'live'|'aliased')."""
        raise NotImplementedError
