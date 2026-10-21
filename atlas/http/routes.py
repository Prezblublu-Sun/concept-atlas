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
"""
from __future__ import annotations

ROUTES: list[tuple[str, str, str]] = [
    # (method, pattern, service method name) — the adapter in server.py dispatches on this table
]
