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
Vanilla JS ES module SPA in `atlas/http/static/`: `index.html`, `app.js` (router + shell), `core.js` (API client, i18n, helpers), `views.js` (document list, search, notes, collections), `reader.js` (page image / text reader with highlights), `editor.js` (note editor with autosave and rev conflicts), `style.css`, `i18n.json`.
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


## Validation on a real corpus (femstem, 2026-10-22)

Project `H:\projects\atlas-projects\femstem`: 2,545 PDFs registered from a manifest (absolute paths, ids from the
previous term viewer), ingested with SEGMENT_VERSION seg6 / doctype paper4, `--workers 4`.

**Result.** 2,545/2,545 documents, 0 failures, 53 warnings (22 "no headings detected" — garbled fonts, OCR scans
and two-page editorials; 6 "no sentences"; the rest low text ratio or high drop ratio). 1,228,662 sentences:
references 33.6 %, methods 18.8 %, results 16.7 %, introduction 12.3 %, discussion 5.3 %, appendix 4.1 %,
conclusion 3.4 %, abstract 2.5 %, acknowledgements 1.4 %, front 1.3 %, keywords 0.3 %, other 0.3 %. No document
is without body text; 40 documents have an introduction/abstract only, 11 are more than half `front`.

**Section agreement.** 40 sample papers compared sentence by sentence with the previous system's Markdown-based
section split: 85 % exact label agreement on comparable labels, 99 % body/non-body agreement (the previous
split is itself not ground truth; its vocabulary has no `conclusion`). Starting point before the seg3–seg6 rules
was 68 % / 85 %.

**What the real corpus taught the paper doctype** (all in `atlas/core/segment.py` and `atlas/doctypes/paper.py`,
each with tests in `tests/unit/test_headings_seg3.py`):

- ML/engineering papers use descriptive headings: `HEADINGS_LOOSE` ('Proposed methodology', 'Numerical Results
  and Analysis', 'Kriging method (KG)'), numbered-sequence headings ('IV. Computational Methodology',
  '3 Extrapolating from a pretrained PRNN' → classified by keyword, 'methods' when keyword-less after the
  introduction), SIAM run-in headings ('1. Introduction. Operator learning …').
- Structured abstracts (Background / Methods / Results / Conclusions on page 1) are retracted when the body
  'Introduction' or 'Methods' arrives and become `abstract`; a strict heading retracts earlier loose ones.
- 'Abstract' only counts on the first pages; a late 'Summary' is a conclusion; lower-case single words
  ('results' as the last word of a wrapped sentence) are never headings.
- Springer PDFs hide a BEL glyph inside headings ('2\u2002 \x07Material and Methods'); ACS glues a square bullet
  ('■INTRODUCTION'); both are stripped by `segment.heading_line()`.
- Papers with an abstract/keywords block but no detectable heading afterwards get `other` (body) for the rest,
  not `front`.
- Stored sentence text is normalised by `atlas.core.text.sentence_text`: soft hyphens join, a hard hyphen at a
  line end joins unless the document writes the hyphenated word elsewhere, ligatures expand, control characters
  go, whitespace collapses; page offsets stay verbatim. The page route returns this stored text.

**Throughput.** Extraction in a 4-process pool is ~7 documents/s; the write side was the bottleneck: one
transaction per document triggered a WAL auto-checkpoint almost every time (3–4 s per document on a hard disk).
`EvidenceStore.bulk_writes()` + `BATCH_DOCS` = 16 documents per transaction + a PASSIVE checkpoint every 32
batches brought the full re-ingest to 519 s (4.9 documents/s including alias computation against the previous
extraction of every document). `evidence.sqlite` is 1.5 GB after three extraction generations (append-only; a
prune/VACUUM tool for superseded extractions is a later increment).

**Known limits.** Headings that are only distinguishable by font (unnumbered, keyword-less: 'Metamodelling
techniques', 'Solutions') are not detected — a font-size hint from `get_text('dict')` is the next lever. OCR'd
scans and garbled-font PDFs (11 with text ratio < 0.5) give no usable sections.
