-- user.sqlite: notes, collections, saved queries, answers, PDF annotations, ref aliases. Schema version 1.
-- Never written by any pipeline stage. Lives outside builds; survives every rebuild.

CREATE TABLE IF NOT EXISTS notes (
  note_id  TEXT PRIMARY KEY,                -- N_...
  title    TEXT NOT NULL DEFAULT '',
  body_md  TEXT NOT NULL DEFAULT '',
  tags     TEXT NOT NULL DEFAULT '',        -- ',tag1,tag2,' comma-wrapped, lower-case
  rev      INTEGER NOT NULL DEFAULT 1,      -- bumped on every save; clients send the rev they loaded
  created  TEXT NOT NULL,
  updated  TEXT NOT NULL,
  deleted  INTEGER NOT NULL DEFAULT 0
);

-- what a note is attached to; the snapshot keeps the note meaningful if the target later vanishes
CREATE TABLE IF NOT EXISTS note_targets (
  note_id        TEXT NOT NULL REFERENCES notes(note_id),
  ref            TEXT NOT NULL,             -- 'S:S1…', 'D:P123', 'C:C_x', 'ST:…', 'SD:…', 'U:…'
  snapshot_text  TEXT,
  snapshot_json  TEXT,
  snapshot_build TEXT,
  status         TEXT NOT NULL DEFAULT 'live',   -- live | aliased | orphan
  resolved_ref   TEXT,
  PRIMARY KEY (note_id, ref)
);
CREATE INDEX IF NOT EXISTS nt_ref ON note_targets(ref);

CREATE TABLE IF NOT EXISTS note_history (
  id      INTEGER PRIMARY KEY,
  note_id TEXT NOT NULL,
  rev     INTEGER NOT NULL,
  title   TEXT, body_md TEXT NOT NULL, tags TEXT,
  saved   TEXT NOT NULL,
  reason  TEXT                              -- explicit | throttle | conflict
);
CREATE INDEX IF NOT EXISTS nh_note ON note_history(note_id, rev);

-- [[ref]] links found in note bodies (backlinks are a query over this table)
CREATE TABLE IF NOT EXISTS note_links (
  note_id TEXT NOT NULL REFERENCES notes(note_id),
  ref     TEXT NOT NULL,
  PRIMARY KEY (note_id, ref)
);
CREATE INDEX IF NOT EXISTS nl_ref ON note_links(ref);

CREATE VIRTUAL TABLE IF NOT EXISTS note_fts USING fts5(
  note_id UNINDEXED, title, body, tags, tokenize = 'unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS notes_ai AFTER INSERT ON notes BEGIN
  INSERT INTO note_fts(note_id, title, body, tags) VALUES (new.note_id, new.title, new.body_md, new.tags);
END;
CREATE TRIGGER IF NOT EXISTS notes_au AFTER UPDATE ON notes BEGIN
  DELETE FROM note_fts WHERE note_id = old.note_id;
  INSERT INTO note_fts(note_id, title, body, tags)
    SELECT new.note_id, new.title, new.body_md, new.tags WHERE new.deleted = 0;
END;
CREATE TRIGGER IF NOT EXISTS notes_ad AFTER DELETE ON notes BEGIN
  DELETE FROM note_fts WHERE note_id = old.note_id;
END;

CREATE TABLE IF NOT EXISTS collections (
  coll_id     TEXT PRIMARY KEY,             -- K_...
  name        TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  created     TEXT NOT NULL, updated TEXT NOT NULL,
  deleted     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS collection_items (
  item_id       TEXT PRIMARY KEY,           -- K_... (random_id('KI'))
  coll_id       TEXT NOT NULL REFERENCES collections(coll_id),
  seq           REAL NOT NULL,              -- ordering key; reorder by assigning midpoints
  ref           TEXT,                       -- S:/ST:/A:/N:/D:… or NULL for a free comment
  snapshot_json TEXT,                       -- {text, doc_id, page, title, year, doi, …} taken at add time
  comment_md    TEXT NOT NULL DEFAULT '',
  added         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ci_coll ON collection_items(coll_id, seq);

CREATE TABLE IF NOT EXISTS saved_queries (
  query_id  TEXT PRIMARY KEY,               -- Q_...
  name      TEXT NOT NULL,
  canonical TEXT NOT NULL,                  -- canonical JSON of atlas.core.query.Query
  created   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS answers (
  answer_id         TEXT PRIMARY KEY,       -- A_...
  build_id          TEXT,
  question          TEXT NOT NULL,
  canonical         TEXT,
  context_sids_json TEXT NOT NULL,
  answer_json       TEXT NOT NULL,
  flags_json        TEXT,
  request_hash      TEXT,
  cost_rmb          REAL,
  ts                TEXT NOT NULL
);

-- PDF annotations as JSON (PDF.js annotation storage), one row per saved version, last N kept by the store
CREATE TABLE IF NOT EXISTS annotations (
  doc_id      TEXT NOT NULL,
  version     INTEGER NOT NULL,
  annots_json TEXT NOT NULL,
  saved       TEXT NOT NULL,
  device      TEXT,
  PRIMARY KEY (doc_id, version)
);

-- refs that moved (statement regenerated, unit re-detected, concept merged); written by `atlas build promote`
CREATE TABLE IF NOT EXISTS ref_aliases (
  old_ref  TEXT NOT NULL,
  new_ref  TEXT NOT NULL,
  method   TEXT,
  build_id TEXT,
  ts       TEXT NOT NULL,
  PRIMARY KEY (old_ref, new_ref)
);
