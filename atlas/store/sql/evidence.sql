-- evidence.sqlite: append-only evidence store. Schema version 1.
-- Only `document_meta` is ever updated; everything else is insert-only (deletes only via `atlas evidence gc`).

CREATE TABLE IF NOT EXISTS documents (
  doc_id     TEXT PRIMARY KEY,
  doc_type   TEXT NOT NULL DEFAULT 'paper',
  added      TEXT NOT NULL,                 -- ISO timestamp
  source     TEXT                           -- 'manifest' | 'scan' | 'legacy'
);

CREATE TABLE IF NOT EXISTS document_files (
  doc_id     TEXT NOT NULL REFERENCES documents(doc_id),
  pdf_sha256 TEXT NOT NULL,
  rel_path   TEXT NOT NULL,                 -- relative to the project's pdf_dir (or absolute if outside)
  size       INTEGER,
  added      TEXT NOT NULL,
  is_current INTEGER NOT NULL DEFAULT 1,    -- the file the reader opens
  PRIMARY KEY (doc_id, pdf_sha256)
);

CREATE TABLE IF NOT EXISTS document_meta (
  doc_id     TEXT PRIMARY KEY REFERENCES documents(doc_id),
  title      TEXT, year INTEGER, authors TEXT, doi TEXT, venue TEXT, keywords TEXT, abstract TEXT, lang TEXT,
  extra_json TEXT,
  updated    TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS document_fts USING fts5(
  doc_id UNINDEXED, title, authors, keywords, tokenize = 'unicode61 remove_diacritics 2'
);

CREATE TABLE IF NOT EXISTS extractions (
  ext_id            TEXT PRIMARY KEY,       -- atlas.core.ids.extraction_id(doc_id, pdf_sha256, extractor_version)
  doc_id            TEXT NOT NULL REFERENCES documents(doc_id),
  pdf_sha256        TEXT NOT NULL,
  extractor_version TEXT NOT NULL,
  doc_type          TEXT NOT NULL,
  n_pages           INTEGER NOT NULL,
  n_sentences       INTEGER NOT NULL,
  text_ratio        REAL,                   -- letters / chars over all pages (low → scanned)
  drop_ratio        REAL,                   -- chars removed by masks / chars
  created           TEXT NOT NULL,
  stats_json        TEXT,
  is_current        INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS extractions_doc ON extractions(doc_id, is_current);

-- page text is content-addressed and shared between extractions that produced identical text
CREATE TABLE IF NOT EXISTS page_text (
  text_sha256 TEXT PRIMARY KEY,
  text        TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS page_fts USING fts5(
  text, content = 'page_text', content_rowid = 'rowid', tokenize = 'unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS page_text_ai AFTER INSERT ON page_text BEGIN
  INSERT INTO page_fts(rowid, text) VALUES (new.rowid, new.text);
END;

CREATE TABLE IF NOT EXISTS pages (
  ext_id      TEXT NOT NULL REFERENCES extractions(ext_id),
  page_no     INTEGER NOT NULL,             -- 1-based physical page
  text_sha256 TEXT NOT NULL REFERENCES page_text(text_sha256),
  width       REAL, height REAL,
  PRIMARY KEY (ext_id, page_no)
);
CREATE INDEX IF NOT EXISTS pages_text ON pages(text_sha256);

CREATE TABLE IF NOT EXISTS line_masks (
  ext_id  TEXT NOT NULL REFERENCES extractions(ext_id),
  page_no INTEGER NOT NULL,
  cstart  INTEGER NOT NULL,
  cend    INTEGER NOT NULL,
  reason  TEXT NOT NULL,
  PRIMARY KEY (ext_id, page_no, cstart)
);

CREATE TABLE IF NOT EXISTS units (
  ext_id      TEXT NOT NULL REFERENCES extractions(ext_id),
  unit_id     TEXT NOT NULL,                -- atlas.core.ids.unit_id
  doc_id      TEXT NOT NULL,
  kind        TEXT NOT NULL,                -- document | chapter
  ordinal     INTEGER NOT NULL,
  heading     TEXT,
  page_from   INTEGER, page_to INTEGER,
  confidence  REAL, reasons TEXT,
  parent_unit TEXT, unit_kind TEXT,
  PRIMARY KEY (ext_id, unit_id)
);

CREATE TABLE IF NOT EXISTS sentences (
  sid       TEXT PRIMARY KEY,               -- atlas.core.ids.sentence_id
  doc_id    TEXT NOT NULL REFERENCES documents(doc_id),
  text      TEXT NOT NULL,                  -- raw text of the first occurrence
  first_ext TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS sentences_doc ON sentences(doc_id);
CREATE VIRTUAL TABLE IF NOT EXISTS sentence_fts USING fts5(
  text, content = 'sentences', content_rowid = 'rowid', tokenize = 'unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS sentences_ai AFTER INSERT ON sentences BEGIN
  INSERT INTO sentence_fts(rowid, text) VALUES (new.rowid, new.text);
END;

-- membership of a sentence in one extraction: where it is and what it is
CREATE TABLE IF NOT EXISTS extraction_sentences (
  ext_id  TEXT NOT NULL REFERENCES extractions(ext_id),
  sid     TEXT NOT NULL REFERENCES sentences(sid),
  seq     INTEGER NOT NULL,                 -- reading order, 0-based
  unit_id TEXT NOT NULL,
  page_no INTEGER NOT NULL,
  cstart  INTEGER NOT NULL, cend INTEGER NOT NULL,   -- offsets into the page text
  section TEXT NOT NULL,
  is_body INTEGER NOT NULL DEFAULT 1,
  tags    TEXT NOT NULL DEFAULT '',          -- ',def,quant,' (comma-wrapped for LIKE)
  flags   TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (ext_id, sid)
);
CREATE INDEX IF NOT EXISTS es_seq  ON extraction_sentences(ext_id, seq);
CREATE INDEX IF NOT EXISTS es_page ON extraction_sentences(ext_id, page_no, cstart);
CREATE INDEX IF NOT EXISTS es_sid  ON extraction_sentences(sid);

-- when a document is re-extracted, old sentence ids map to new ones (same page, char-span overlap >= 0.5)
CREATE TABLE IF NOT EXISTS sentence_aliases (
  old_sid  TEXT NOT NULL,
  new_sid  TEXT NOT NULL,
  from_ext TEXT NOT NULL,
  to_ext   TEXT NOT NULL,
  overlap  REAL,
  PRIMARY KEY (old_sid, new_sid)
);
CREATE INDEX IF NOT EXISTS sa_new ON sentence_aliases(new_sid);

CREATE TABLE IF NOT EXISTS vector_rows (
  sid   TEXT NOT NULL,
  model TEXT NOT NULL,
  row   INTEGER NOT NULL,
  PRIMARY KEY (sid, model)
);
