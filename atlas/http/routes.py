"""Route table: (method, path pattern) → service call. Implemented by: increment 1, agent "service+http".

All routes under /api/v1/. Path parameters in {braces}. JSON in/out (UTF-8), errors as {"error": msg} with
4xx/5xx. Query parameters are strings; the adapter converts ints/bools. Every response carries
`X-Atlas-Build: evidence` (increment 1) and `Cache-Control: no-cache` except PNG/PDF (max-age=600).

GET  info                                    -> service.info()
GET  i18n                                    -> service.i18n()
GET  docs?q=&limit=&offset=                  -> service.list_docs
GET  docs/{doc_id}                           -> service.get_doc
GET  docs/{doc_id}/pages/{n}                 -> service.page
GET  docs/{doc_id}/pages/{n}.png?hl=&spans=a-b,c-d&zoom=   -> image/png (+ X-Page, X-Pages, X-Hits, X-Hit-Y headers)
GET  docs/{doc_id}/search?phrase=&limit=&offset=           -> service.search(doc_id=…)
GET  pdf/{doc_id}                            -> the current PDF file (inline; ?download=1 → attachment)
GET  search?phrase=&limit=&offset=           -> service.search
GET  sentences/{sid}?context=                -> service.sentence
GET  resolve?ref=                            -> service.resolve
GET  notes?q=&tag=&limit=&offset=            -> service.list_notes
POST notes            {title, body_md, tags[], targets[]}     -> service.create_note (201)
GET  notes/{id}                              -> service.get_note
PUT  notes/{id}       {rev, title?, body_md?, tags?, explicit?} -> service.update_note (409 + server copy on conflict)
DELETE notes/{id}                            -> service.delete_note
GET  notes/{id}/history                      -> service.note_history
GET  notes/for?ref=                          -> service.notes_for
GET  collections                             -> service.collections
POST collections      {name, description}    -> service.create_collection (201)
GET  collections/{id}                        -> service.collection
POST collections/{id}/items {ref?, comment_md?, after_item?} -> service.add_to_collection (201)
PUT  collections/items/{item_id} {comment_md?, seq?}         -> service.update_item
DELETE collections/items/{item_id}           -> service.remove_item
GET  collections/{id}/export.md              -> text/markdown
Static: '/' → static/index.html; '/static/<file>' and bare '/<file>' for files that exist in static/ (explicit MIME
table: .html .js .mjs .css .json .png .svg .woff2 .wasm .map .ftl; unknown → application/octet-stream).

The table below is matched top to bottom (first match wins), so literal segments ('notes/for', 'collections/items')
come before the patterns that would swallow them. Parameter names in the patterns are the keyword names of the
service method they feed (`{n}` in the docstring above is `{page_no}` here; `{id}` is `{note_id}` / `{coll_id}`).
"""
from __future__ import annotations

import re

API_PREFIX = "/api/v1/"

ROUTES: list[tuple[str, str, str]] = [
    # (method, pattern, service method name) — the adapter in server.py dispatches on this table
    ("GET", "info", "info"),
    ("GET", "i18n", "i18n"),
    ("GET", "docs", "list_docs"),
    ("GET", "docs/{doc_id}", "get_doc"),
    ("GET", "docs/{doc_id}/pages/{page_no}.png", "page_png"),
    ("GET", "docs/{doc_id}/pages/{page_no}", "page"),
    ("GET", "docs/{doc_id}/search", "search"),
    ("GET", "pdf/{doc_id}", "pdf_path"),
    ("GET", "search", "search"),
    ("GET", "sentences/{sid}", "sentence"),
    ("GET", "resolve", "resolve"),
    ("GET", "notes/for", "notes_for"),
    ("GET", "notes", "list_notes"),
    ("POST", "notes", "create_note"),
    ("GET", "notes/{note_id}/history", "note_history"),
    ("GET", "notes/{note_id}", "get_note"),
    ("PUT", "notes/{note_id}", "update_note"),
    ("DELETE", "notes/{note_id}", "delete_note"),
    ("GET", "collections", "collections"),
    ("POST", "collections", "create_collection"),
    ("PUT", "collections/items/{item_id}", "update_item"),
    ("DELETE", "collections/items/{item_id}", "remove_item"),
    ("GET", "collections/{coll_id}/export.md", "export_collection_md"),
    ("POST", "collections/{coll_id}/items", "add_to_collection"),
    ("GET", "collections/{coll_id}", "collection"),
]

# Service methods whose JSON body / query parameters need renaming before the call.
BODY_ALIASES: dict[str, dict[str, str]] = {
    "create_note": {"targets": "target_refs"},
    "page_png": {"hl": "highlight"},
}

# Methods whose successful response is 201 Created.
CREATED: frozenset[str] = frozenset({"create_note", "create_collection", "add_to_collection"})

_PARAM_RE = re.compile(r"\{(\w+)\}")


def compile_pattern(pattern: str) -> re.Pattern[str]:
    """'docs/{doc_id}/pages/{page_no}.png' → anchored regex with named groups (one path segment each)."""
    parts: list[str] = []
    pos = 0
    for m in _PARAM_RE.finditer(pattern):
        parts.append(re.escape(pattern[pos:m.start()]))
        parts.append(f"(?P<{m.group(1)}>[^/]+?)")
        pos = m.end()
    parts.append(re.escape(pattern[pos:]))
    return re.compile("^" + "".join(parts) + "$")


COMPILED: list[tuple[str, re.Pattern[str], str]] = [(m, compile_pattern(p), name) for m, p, name in ROUTES]


def match(method: str, path: str) -> tuple[str | None, dict[str, str], bool]:
    """(service method name, path params, path_known). `path` is relative to API_PREFIX. When the path matches a
    pattern only under another HTTP method, the name is None and path_known is True (→ 405)."""
    known = False
    for m, rx, name in COMPILED:
        mo = rx.match(path)
        if mo is None:
            continue
        if m == method:
            return name, mo.groupdict(), True
        known = True
    return None, {}, known
