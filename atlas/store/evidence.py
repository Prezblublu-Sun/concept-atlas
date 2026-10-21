"""EvidenceStore: repository over evidence.sqlite. Implemented by: increment 1, agent "store".

All methods return plain dicts/lists (JSON-serialisable) or dataclasses from atlas.core.schemas; no sqlite3.Row
leaks out. Write methods are used only by the ingest stage; the server opens the store read-only.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from pathlib import Path

from atlas.core.schemas import DocMeta, LineMask, PageText, SentenceRow, Unit


class EvidenceStore:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    @classmethod
    def open(cls, path: str | Path, mode: str = "rw") -> "EvidenceStore":
        raise NotImplementedError("increment 1 / store")

    # ---- documents ----------------------------------------------------------------------------------------
    def add_document(self, doc_id: str, doc_type: str, source: str, pdf_path: Path, rel_path: str,
                     pdf_sha256: str, meta: DocMeta | None = None) -> None:
        """Insert documents/document_files/document_meta/document_fts (idempotent: a known doc_id with a new
        pdf_sha256 adds a document_files row and marks it current; identical sha is a no-op)."""
        raise NotImplementedError

    def upsert_meta(self, doc_id: str, meta: DocMeta) -> None:
        """Merge non-None fields into document_meta and refresh document_fts."""
        raise NotImplementedError

    def get_document(self, doc_id: str) -> dict | None:
        """{doc_id, doc_type, added, source, title, year, authors, doi, venue, keywords, abstract, lang,
        pdf_sha256, rel_path, ext_id (current extraction or None), n_pages, n_sentences}"""
        raise NotImplementedError

    def list_documents(self, q: str | None = None, limit: int = 100, offset: int = 0) -> dict:
        """{'rows': [get_document-shaped dicts], 'total': n}; `q` searches document_fts (title/authors/keywords)
        and falls back to LIKE on doc_id; sorted by year desc, doc_id."""
        raise NotImplementedError

    def stats(self) -> dict:
        """{documents, extractions, pages, sentences, body_sentences}"""
        raise NotImplementedError

    # ---- extractions --------------------------------------------------------------------------------------
    def extraction_exists(self, ext_id: str) -> bool:
        raise NotImplementedError

    def current_extraction(self, doc_id: str) -> dict | None:
        """The is_current extraction row of a document (dict) or None."""
        raise NotImplementedError

    def add_extraction(self, *, ext_id: str, doc_id: str, pdf_sha256: str, extractor_version: str, doc_type: str,
                       pages: Sequence[PageText], masks: Sequence[LineMask], units: Sequence[Unit],
                       sentences: Sequence[SentenceRow], text_ratio: float, drop_ratio: float,
                       stats: dict | None = None) -> None:
        """One transaction: extractions row; page_text (INSERT OR IGNORE by sha) + pages; line_masks; units;
        sentences (INSERT OR IGNORE: first occurrence wins) + extraction_sentences; previous extractions of the
        document get is_current = 0."""
        raise NotImplementedError

    def add_aliases(self, rows: Iterable[tuple[str, str, str, str, float]]) -> None:
        """(old_sid, new_sid, from_ext, to_ext, overlap) rows, INSERT OR IGNORE."""
        raise NotImplementedError

    def sentences_of_extraction(self, ext_id: str) -> list[SentenceRow]:
        raise NotImplementedError

    # ---- reading ------------------------------------------------------------------------------------------
    def pages_of(self, doc_id: str) -> list[dict]:
        """[{page_no, width, height, n_sentences}] for the current extraction."""
        raise NotImplementedError

    def page(self, doc_id: str, page_no: int) -> dict | None:
        """{doc_id, ext_id, page_no, text, width, height, masks:[{cstart,cend,reason}],
        sentences:[{sid, seq, cstart, cend, section, is_body, tags:[...], flags:[...]}]}"""
        raise NotImplementedError

    def sentence(self, sid: str, context: int = 1) -> dict | None:
        """Resolve a sid (following sentence_aliases to the newest sid first):
        {sid, requested_sid, status live|aliased|missing, doc_id, ext_id, unit_id, page_no, cstart, cend, section,
        is_body, tags, flags, text, before:[texts], after:[texts], doc:{title, year, doi}}"""
        raise NotImplementedError

    def resolve_sid(self, sid: str) -> tuple[str, str]:
        """(newest_sid, status) following alias chains; ('', 'missing') when unknown."""
        raise NotImplementedError

    def phrase_search(self, phrase: str, doc_id: str | None = None, limit: int = 50, offset: int = 0) -> dict:
        """FTS5 phrase search over sentences (current extractions only): {'rows': [{sid, doc_id, page_no, section,
        is_body, snippet, title, year}], 'total': n, 'documents': n_distinct_docs}. The phrase is quoted (no FTS
        syntax from users)."""
        raise NotImplementedError

    def page_search(self, phrase: str, doc_id: str | None = None, limit: int = 50, offset: int = 0) -> dict:
        """Same over page_fts (for text the sentence splitter may have cut): rows {doc_id, page_no, snippet}."""
        raise NotImplementedError
