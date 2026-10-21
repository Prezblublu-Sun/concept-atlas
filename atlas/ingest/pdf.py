"""PyMuPDF adapter: page text with stable offsets, metadata, page rendering with highlights.

Implemented by: increment 1, agent "ingest". Port the proven highlight logic from
H:/UCL_theses/2610 opt/术语阅览系统_v3_2026-10-20/server.py (render_page: try the phrase, then hyphen/space
variants, then the longest word ≥ 3 chars; yellow highlight annots; X-Hit-Y as the first hit's relative y).

Offsets: `extract_pages` returns `page.get_text('text')` with only '\\x00' and '\\r' removed; every (cstart, cend)
in the evidence store indexes into that string. `render_page(spans=…)` maps such offsets back to glyph boxes
through `get_text('rawdict')`, whose characters joined with one '\\n' per line reproduce the 'text' output
exactly (verified for PyMuPDF 1.24–1.27); when the reconstruction differs, it falls back to `search_for` on the
span text.
"""
from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Sequence
from pathlib import Path

import fitz

from atlas.core.schemas import DocMeta, PageText

log = logging.getLogger(__name__)

#: first-page DOI guess (trailing sentence punctuation is stripped from the match)
DOI_RE = re.compile(r"10\.\d{4,9}/[^\s]+")
_DOI_TRAIL = ".,;:)]}'\">"
HIGHLIGHT_COLOR = (1.0, 0.85, 0.1)
_DROP_CHARS = ("\x00", "\r")


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def extractor_tag() -> str:
    """'pymupdf:<fitz.VersionBind>' — part of extractor_version."""
    return f"pymupdf:{fitz.VersionBind}"


def _clean(text: str | None) -> str:
    s = text or ""
    for ch in _DROP_CHARS:
        if ch in s:
            s = s.replace(ch, "")
    return s


def extract_pages(path: str | Path) -> list[PageText]:
    """page.get_text('text') for every page, 1-based page_no, width/height in points. The text is stored verbatim
    (offsets index into it); only '\\x00' and '\\r' are removed."""
    out: list[PageText] = []
    with fitz.open(str(path)) as doc:
        for i in range(doc.page_count):
            page = doc[i]
            rect = page.rect
            out.append(PageText(page_no=i + 1, text=_clean(page.get_text("text")),
                                width=float(rect.width), height=float(rect.height)))
    return out


def _largest_font_line(page) -> str | None:
    """Text of the line(s) set in the largest font on the page: the first run of consecutive lines whose
    biggest span size equals the page maximum, joined with spaces."""
    try:
        d = page.get_text("dict")
    except Exception as e:  # pragma: no cover - defensive
        log.debug("get_text(dict) failed: %s", e)
        return None
    lines: list[tuple[float, str]] = []
    for b in d.get("blocks", []):
        for ln in b.get("lines", []):
            spans = ln.get("spans", [])
            text = " ".join("".join(sp.get("text", "") for sp in spans).split())
            if not text:
                continue
            size = max((float(sp.get("size", 0.0)) for sp in spans), default=0.0)
            lines.append((size, text))
    if not lines:
        return None
    top = max(s for s, _ in lines)
    parts: list[str] = []
    for size, text in lines:
        if size >= top - 0.05:
            parts.append(text)
        elif parts:
            break
    return " ".join(parts) or None


def _str_or_none(v) -> str | None:
    s = " ".join(str(v).split()) if v is not None else ""
    return s or None


def pdf_metadata(path: str | Path) -> DocMeta:
    """Title/author/subject/keywords from the PDF info dictionary plus a first-page DOI regex
    (10\\.\\d{4,9}/[^\\s]+) and a first-page title guess (largest-font line of page 1 via get_text('dict'))
    placed in extra={'title_guess': …, 'doi_guess': …}. Never raises on a broken PDF: returns an empty DocMeta
    with extra={'error': str}."""
    try:
        with fitz.open(str(path)) as doc:
            info = doc.metadata or {}
            meta = DocMeta(title=_str_or_none(info.get("title")), authors=_str_or_none(info.get("author")),
                           keywords=_str_or_none(info.get("keywords")))
            subject = _str_or_none(info.get("subject"))
            if subject:
                meta.extra["subject"] = subject
            created = _str_or_none(info.get("creationDate"))
            if created:
                meta.extra["creation_date"] = created
            meta.extra["pages"] = int(doc.page_count)
            title_guess: str | None = None
            doi_guess: str | None = None
            if doc.page_count:
                page = doc[0]
                m = DOI_RE.search(_clean(page.get_text("text")))
                if m:
                    doi_guess = m.group(0).rstrip(_DOI_TRAIL) or None
                title_guess = _largest_font_line(page)
            meta.extra["title_guess"] = title_guess
            meta.extra["doi_guess"] = doi_guess
            return meta
    except Exception as e:
        log.warning("pdf_metadata failed for %s: %s", path, e)
        return DocMeta(extra={"error": str(e)})


# ---- rendering ----------------------------------------------------------------------------------------------

def _collapse(s: str) -> str:
    return " ".join((s or "").split())


def _search(page, needle: str) -> list:
    needle = _collapse(needle)
    if not needle:
        return []
    try:
        return list(page.search_for(needle))
    except Exception as e:  # pragma: no cover - defensive (mupdf limits)
        log.debug("search_for(%r) failed: %s", needle[:60], e)
        return []


def _rects_for_phrase(page, phrase: str) -> list:
    """The ported fallback chain: phrase, hyphen→space, space→hyphen, U+2010→'-', then the longest word ≥ 3."""
    q = _collapse(phrase)
    if not q:
        return []
    tries = [q, q.replace("-", " "), q.replace(" ", "-"), q.replace("\u2010", "-")]
    seen: set[str] = set()
    for t in tries:
        if t in seen:
            continue
        seen.add(t)
        rects = _search(page, t)
        if rects:
            return rects
    words = [w for w in re.split(r"[\s\-]+", q) if len(w) >= 3]
    if words:
        return _search(page, max(words, key=len))
    return []


def _char_map(page, text: str) -> list[tuple[str, object | None]] | None:
    """[(char, Rect|None)] in 'text' order (one '\\n' per line, no rect), or None when the rawdict
    reconstruction does not reproduce `text` exactly."""
    try:
        rd = page.get_text("rawdict")
    except Exception as e:  # pragma: no cover - defensive
        log.debug("get_text(rawdict) failed: %s", e)
        return None
    out: list[tuple[str, object | None]] = []
    for b in rd.get("blocks", []):
        if b.get("type", 0) != 0:
            continue
        for ln in b.get("lines", []):
            for sp in ln.get("spans", []):
                for ch in sp.get("chars", []):
                    c = ch.get("c", "")
                    if c in _DROP_CHARS or not c:
                        continue
                    out.append((c, fitz.Rect(ch["bbox"])))
            out.append(("\n", None))
    if "".join(c for c, _ in out) != text:
        return None
    return out


def _rects_for_spans(page, text: str, spans: Sequence[tuple[int, int]]) -> list:
    cmap = _char_map(page, text)
    rects: list = []
    n = len(text)
    for a, b in spans:
        a, b = max(0, int(a)), min(n, int(b))
        if b <= a:
            continue
        if cmap is not None:
            cur = None
            for c, r in cmap[a:b]:
                if r is None:
                    if cur is not None:
                        rects.append(cur)
                    cur = None
                    continue
                cur = fitz.Rect(r) if cur is None else cur | r
            if cur is not None:
                rects.append(cur)
        else:
            rects.extend(_rects_for_phrase(page, text[a:b]))
    return rects


def render_page(path: str | Path, page_no: int, zoom: float = 1.6, highlight: str | None = None,
                spans: list[tuple[int, int]] | None = None) -> tuple[bytes, dict]:
    """PNG bytes of the page and info {page, pages, hits, hit_y}. `highlight` is a phrase searched with
    page.search_for (with the fallbacks described in the module docstring); `spans` are (cstart, cend) character
    offsets into the page text to highlight instead (map them to rects via get_text('rawdict')/chars; a best-effort
    quad search on the span text is acceptable)."""
    with fitz.open(str(path)) as doc:
        pages = doc.page_count
        if pages == 0:
            raise ValueError(f"PDF has no pages: {path}")
        page_no = max(1, min(int(page_no), pages))
        pg = doc[page_no - 1]
        rects: list = []
        if spans:
            rects = _rects_for_spans(pg, _clean(pg.get_text("text")), spans)
        if not rects and highlight:
            rects = _rects_for_phrase(pg, highlight)
        for r in rects:
            try:
                annot = pg.add_highlight_annot(r)
                annot.set_colors(stroke=HIGHLIGHT_COLOR)
                annot.update()
            except Exception as e:  # pragma: no cover - a degenerate rect must not break rendering
                log.debug("highlight annot failed: %s", e)
        z = float(zoom) if zoom and zoom > 0 else 1.6
        pix = pg.get_pixmap(matrix=fitz.Matrix(z, z), annots=True)
        png = pix.tobytes("png")
        height = pg.rect.height or 1.0
        hit_y = round(min(r.y0 for r in rects) / height, 4) if rects else None
        return png, {"page": page_no, "pages": pages, "hits": len(rects), "hit_y": hit_y}


def text_ratio(pages: list[PageText]) -> float:
    """letters / all chars over the document (0 when empty)."""
    total = sum(len(p.text) for p in pages)
    if total == 0:
        return 0.0
    letters = sum(sum(1 for ch in p.text if ch.isalpha()) for p in pages)
    return letters / total
