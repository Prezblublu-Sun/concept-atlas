# Increment 1 — evidence store, reader, notes (no LLM, 0 RMB)

Goal: on a folder of PDFs, run `atlas init` → `docs add` → `run ingest` → `serve` and get a bilingual web UI with
a document list, a page reader (rendered page with highlight / text view), corpus-wide phrase search, and notes
attached to sentences, documents or free-standing, plus collections exportable to Markdown. Sentence ids are
content-derived and stable; re-ingesting the same PDFs yields identical ids.

## Contracts (already written — do not change signatures without updating every caller)
- `atlas/core/ids.py`, `atlas/core/schemas.py`, `atlas/config.py` — complete.
- `atlas/store/sql/evidence.sql`, `atlas/store/sql/user.sql` — complete schemas.
- Stubs with docstring specs: `core/text.py`, `core/segment.py`, `doctypes/paper.py`, `store/db.py`,
  `store/evidence.py`, `store/user.py`, `ingest/pdf.py`, `pipeline/stages/ingest.py`, `pipeline/runner.py`,
  `service/api.py`, `http/routes.py`, `http/server.py`, `tools/fixture.py`, `cli.py`.

## Work packages (independent file sets; implement, then unit-test your own package)
| Agent | Files | Tests |
|---|---|---|
| core | `core/text.py`, `core/segment.py` | `tests/unit/test_text.py`, `tests/unit/test_segment.py`, `tests/unit/test_ids.py` |
| doctypes | `doctypes/paper.py` | `tests/unit/test_paper_doctype.py` (synthetic page texts) |
| store | `store/db.py`, `store/evidence.py`, `store/user.py` | `tests/unit/test_store_evidence.py`, `tests/unit/test_store_user.py` (tmp_path DBs, hand-made rows) |
| ingest | `ingest/pdf.py`, `pipeline/stages/ingest.py`, `tools/fixture.py`, `cli.py` | `tests/unit/test_pdf.py`, `tests/test_fixture_ingest.py` (make_fixture → docs add → ingest → assert counts/ids) |
| service+http | `service/api.py`, `http/server.py`, `http/routes.py`, `http/static/*` | `tests/test_http.py` (start server on a free port in a thread against an ingested fixture; exercise every route) |

Shared conventions: Python 3.12, type hints, no third-party deps beyond pymupdf/pyyaml/numpy/openpyxl; UTF-8
everywhere; never `print` from library code (use `logging`); Windows paths; keep functions pure where the
contract says pure. Write files with proper tools (no shell heredocs with backslashes).

## UI (service+http agent)
Vanilla JS ES module SPA in `atlas/http/static/`: `index.html`, `app.js`, `style.css`, `i18n.json`.
- Header: project name, phrase search box, zh/en toggle (persisted in localStorage), links: Documents / Notes / Collections.
- Documents view: searchable list (title, year, authors, doc_id, pages, notes count) → document view.
- Document view (reader): page list; center: rendered page PNG (`/pages/{n}.png`) with prev/next and zoom, toggle
  to text view that lists the page's sentences (section label, tags, is_body dimmed for references) and lets the
  user click a sentence to select it; right: notes panel for the selection (`notes/for?ref=S:…`), new-note form
  (title, body, tags) with autosave (debounced PUT with rev; on 409 show "changed elsewhere" and offer reload),
  "add to collection" button; link "open PDF" (`/pdf/{id}`).
- Search view: results (snippet with <mark>, doc title/year, page, section) → click opens the reader at that page
  with the phrase highlighted (`?hl=`) and the sentence selected.
- Notes view: list/search notes; note page with Markdown rendered (minimal renderer: headings, paragraphs, lists,
  bold/italic/code, [[refs]] rendered as links that resolve via `/resolve?ref=`), targets list with "go to".
- Collections view: list, create, open: ordered items with comment editing and reorder (up/down), export .md.
- Bilingual: every chrome string from `i18n.json` `{key: {zh, en}}`; data labels (sections, tags) from `/i18n`.
- Style: reuse the look of the previous viewer (system font, light/dark via prefers-color-scheme, cards, badges);
  no frameworks, no build step.
