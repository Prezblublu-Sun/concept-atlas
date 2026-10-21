"""PyMuPDF adapter: page text with stable offsets, metadata, page rendering with highlights.

Implemented by: increment 1, agent "ingest". Port the proven highlight logic from
H:/UCL_theses/2610 opt/术语阅览系统_v3_2026-10-20/server.py (render_page: try the phrase, then hyphen/space
variants, then the longest word ≥ 3 chars; yellow highlight annots; X-Hit-Y as the first hit's relative y).
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from atlas.core.schemas import DocMeta, PageText


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def extractor_tag() -> str:
    """'pymupdf:<fitz.VersionBind>' — part of extractor_version."""
    raise NotImplementedError("increment 1 / ingest")


def extract_pages(path: str | Path) -> list[PageText]:
    """page.get_text('text') for every page, 1-based page_no, width/height in points. The text is stored verbatim
    (offsets index into it); only '\\x00' and '\\r' are removed."""
    raise NotImplementedError("increment 1 / ingest")


def pdf_metadata(path: str | Path) -> DocMeta:
    """Title/author/subject/keywords from the PDF info dictionary plus a first-page DOI regex
    (10\\.\\d{4,9}/[^\\s]+) and a first-page title guess (largest-font line of page 1 via get_text('dict'))
    placed in extra={'title_guess': …, 'doi_guess': …}. Never raises on a broken PDF: returns an empty DocMeta
    with extra={'error': str}."""
    raise NotImplementedError("increment 1 / ingest")


def render_page(path: str | Path, page_no: int, zoom: float = 1.6, highlight: str | None = None,
                spans: list[tuple[int, int]] | None = None) -> tuple[bytes, dict]:
    """PNG bytes of the page and info {page, pages, hits, hit_y}. `highlight` is a phrase searched with
    page.search_for (with the fallbacks described in the module docstring); `spans` are (cstart, cend) character
    offsets into the page text to highlight instead (map them to rects via get_text('rawdict')/chars; a best-effort
    quad search on the span text is acceptable)."""
    raise NotImplementedError("increment 1 / ingest")


def text_ratio(pages: list[PageText]) -> float:
    """letters / all chars over the document (0 when empty)."""
    raise NotImplementedError("increment 1 / ingest")
