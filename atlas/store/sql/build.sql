-- build.sqlite: one disposable derived snapshot (increment 2: pinned evidence + curated scheme + match/assign
-- outputs). Written by the build stages only; frozen (read-only, journal_mode=DELETE) by `atlas run finish`.
-- Schema version 1.

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

-- ---- pinned evidence --------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS build_docs (
  doc_id  TEXT PRIMARY KEY,
  ext_id  TEXT NOT NULL                     -- the document's current extraction at pin time
);

CREATE TABLE IF NOT EXISTS build_sentences (
  sid      TEXT PRIMARY KEY,                -- text lives in evidence.sqlite (sentences.text)
  doc_id   TEXT NOT NULL REFERENCES build_docs(doc_id),
  unit_id  TEXT NOT NULL,
  page_no  INTEGER NOT NULL,
  section  TEXT NOT NULL,
  is_body  INTEGER NOT NULL DEFAULT 1,
  tags     TEXT NOT NULL DEFAULT '',        -- ',def,quant,' comma-wrapped
  seq      INTEGER NOT NULL DEFAULT 0,      -- reading order inside the document
  cstart   INTEGER NOT NULL DEFAULT 0,      -- offsets into the page text (reader marks)
  cend     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS bs_doc  ON build_sentences(doc_id, seq);
CREATE INDEX IF NOT EXISTS bs_unit ON build_sentences(unit_id);
CREATE INDEX IF NOT EXISTS bs_page ON build_sentences(doc_id, page_no);

CREATE TABLE IF NOT EXISTS build_units (
  unit_id    TEXT PRIMARY KEY,
  doc_id     TEXT NOT NULL REFERENCES build_docs(doc_id),
  kind       TEXT NOT NULL,                 -- document | chapter
  ordinal    INTEGER NOT NULL,
  heading    TEXT,
  page_from  INTEGER, page_to INTEGER,
  unit_kind  TEXT,
  confidence REAL
);
CREATE INDEX IF NOT EXISTS bu_doc ON build_units(doc_id, ordinal);

CREATE TABLE IF NOT EXISTS build_doc_meta (
  doc_id       TEXT PRIMARY KEY REFERENCES build_docs(doc_id),
  title TEXT, year INTEGER, authors TEXT, venue TEXT, doc_type TEXT,
  keywords TEXT, abstract TEXT,
  domains_json TEXT NOT NULL DEFAULT '[]'   -- from curated/doc_domains.yaml
);
CREATE INDEX IF NOT EXISTS bdm_year ON build_doc_meta(year);

-- ---- scheme (loaded from the build's own curated/ snapshot) -------------------------------------------------
CREATE TABLE IF NOT EXISTS concepts (
  id           TEXT PRIMARY KEY,            -- C_<slug> (legacy ids kept verbatim)
  label        TEXT NOT NULL,
  zh           TEXT,
  type         TEXT,
  mode         TEXT NOT NULL DEFAULT 'forms',   -- forms | literal
  domains_json TEXT NOT NULL DEFAULT '[]',
  place_json   TEXT NOT NULL DEFAULT '[]',
  origin       TEXT,
  probe        TEXT,
  rx           TEXT,
  readable     TEXT,
  shared_json  TEXT NOT NULL DEFAULT '[]',
  merged_into  TEXT,
  source       TEXT
);
CREATE INDEX IF NOT EXISTS concepts_label ON concepts(label COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS concept_forms (
  concept_id TEXT NOT NULL REFERENCES concepts(id),
  form       TEXT NOT NULL,
  kind       TEXT NOT NULL,                 -- form | gen | case | literal
  rx         TEXT,
  PRIMARY KEY (concept_id, form, kind)
);

CREATE TABLE IF NOT EXISTS patterns (key TEXT PRIMARY KEY, json TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS domains (
  id TEXT PRIMARY KEY, zh TEXT, en TEXT, ord INTEGER NOT NULL DEFAULT 0, scope INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS categories (
  id TEXT PRIMARY KEY, domain TEXT NOT NULL, zh TEXT, en TEXT, desc_zh TEXT, desc_en TEXT, ord INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS concept_categories (
  concept_id TEXT NOT NULL REFERENCES concepts(id),
  domain TEXT NOT NULL, primary_cat TEXT NOT NULL, secondary_cat TEXT, reason TEXT,
  PRIMARY KEY (concept_id, domain)
);
CREATE INDEX IF NOT EXISTS cc_cat ON concept_categories(primary_cat);

CREATE TABLE IF NOT EXISTS subdomains (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, domain TEXT, zh TEXT, en TEXT, desc_zh TEXT, desc_en TEXT,
  ord INTEGER NOT NULL DEFAULT 0, notes TEXT
);

CREATE TABLE IF NOT EXISTS anchors (
  sub_id TEXT NOT NULL REFERENCES subdomains(id),
  group_no INTEGER NOT NULL DEFAULT 0,
  concept_id TEXT NOT NULL REFERENCES concepts(id),
  PRIMARY KEY (sub_id, group_no, concept_id)
);
CREATE INDEX IF NOT EXISTS anchors_concept ON anchors(concept_id);

CREATE TABLE IF NOT EXISTS term_tree (
  id INTEGER PRIMARY KEY,
  domain TEXT NOT NULL, section TEXT NOT NULL, sub_name TEXT NOT NULL, sub_ord INTEGER NOT NULL DEFAULT 0,
  term TEXT NOT NULL, zh TEXT, type TEXT, concept_id TEXT, note TEXT, example TEXT, example_doc TEXT,
  ord INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS tt_concept ON term_tree(concept_id);

CREATE TABLE IF NOT EXISTS i18n (zh TEXT PRIMARY KEY, en TEXT, grp TEXT);

-- ---- stage outputs ----------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS occurrences (
  sid        TEXT NOT NULL REFERENCES build_sentences(sid),
  concept_id TEXT NOT NULL REFERENCES concepts(id),
  form       TEXT,
  cstart     INTEGER NOT NULL,              -- offsets into the stored sentence text
  cend       INTEGER NOT NULL,
  nested_in  TEXT,                          -- concept id of the longer match containing this one
  PRIMARY KEY (sid, concept_id, cstart)
);
CREATE INDEX IF NOT EXISTS occ_concept ON occurrences(concept_id, sid);

CREATE TABLE IF NOT EXISTS topic_hits (
  unit_id    TEXT NOT NULL,
  concept_id TEXT NOT NULL REFERENCES concepts(id),
  field      TEXT NOT NULL,                 -- title | keywords | abstract
  n          INTEGER NOT NULL,
  n_nested   INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (unit_id, concept_id, field)
);
CREATE INDEX IF NOT EXISTS th_concept ON topic_hits(concept_id);

CREATE TABLE IF NOT EXISTS unit_subdomains (
  unit_id   TEXT NOT NULL,
  sub_id    TEXT NOT NULL REFERENCES subdomains(id),
  tier      TEXT NOT NULL,                  -- core | related
  hits_json TEXT NOT NULL DEFAULT '{}',
  n_hits    INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (unit_id, sub_id)
);
CREATE INDEX IF NOT EXISTS us_sub ON unit_subdomains(sub_id, tier);

-- per concept × document × section; section ∈ SECTIONS ∪ {'body'}; n_sa = standalone (n - n_nested; literal: n)
CREATE TABLE IF NOT EXISTS counts (
  concept_id TEXT NOT NULL REFERENCES concepts(id),
  doc_id     TEXT NOT NULL REFERENCES build_docs(doc_id),
  section    TEXT NOT NULL,
  n          INTEGER NOT NULL,
  n_nested   INTEGER NOT NULL DEFAULT 0,
  n_sa       INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (concept_id, doc_id, section)
);
CREATE INDEX IF NOT EXISTS counts_doc ON counts(doc_id, section);

CREATE TABLE IF NOT EXISTS importance (
  concept_id       TEXT NOT NULL REFERENCES concepts(id),
  scope            TEXT NOT NULL,           -- 'all' | DOM_x
  papers           INTEGER NOT NULL,
  any_papers       INTEGER NOT NULL,
  coverage         REAL NOT NULL,
  topic_papers     INTEGER NOT NULL,
  topic_rate       REAL NOT NULL,
  abstract_rate    REAL NOT NULL,
  occ              INTEGER NOT NULL,
  median_per_paper REAL NOT NULL,
  refs_papers      INTEGER NOT NULL,
  main_section     TEXT,
  lift             REAL,
  year_cov_json    TEXT,
  trend            REAL,
  trend_flag       TEXT,
  tier             TEXT NOT NULL,
  PRIMARY KEY (concept_id, scope)
);
CREATE INDEX IF NOT EXISTS imp_scope ON importance(scope, papers DESC);

CREATE TABLE IF NOT EXISTS framework_stats (
  kind          TEXT NOT NULL,              -- subdomain | category
  id            TEXT NOT NULL,
  scope         TEXT NOT NULL,
  papers        INTEGER NOT NULL DEFAULT 0,
  core          INTEGER NOT NULL DEFAULT 0,
  base          INTEGER NOT NULL DEFAULT 0,
  years_json    TEXT,
  overlaps_json TEXT,
  concepts_json TEXT,
  PRIMARY KEY (kind, id, scope)
);

-- ranked concepts of a sub-domain (v2 subdomain framework); the top rows are also in framework_stats.concepts_json
CREATE TABLE IF NOT EXISTS subdomain_concepts (
  sub_id        TEXT NOT NULL REFERENCES subdomains(id),
  concept_id    TEXT NOT NULL REFERENCES concepts(id),
  rank          INTEGER NOT NULL,
  papers        INTEGER NOT NULL,
  coverage      REAL NOT NULL,
  coverage_rest REAL NOT NULL,
  lift          REAL NOT NULL,
  topic_rate    REAL NOT NULL,
  score         REAL NOT NULL,
  PRIMARY KEY (sub_id, concept_id)
);
CREATE INDEX IF NOT EXISTS sc_concept ON subdomain_concepts(concept_id);

CREATE TABLE IF NOT EXISTS stage_items (
  stage       TEXT NOT NULL,
  item_key    TEXT NOT NULL,                -- '*' = the stage as a whole, else a doc id
  fingerprint TEXT,
  status      TEXT NOT NULL,                -- running | done | failed | skipped
  started     TEXT, finished TEXT,
  detail_json TEXT,
  PRIMARY KEY (stage, item_key)
);

CREATE TABLE IF NOT EXISTS check_results (
  target_kind TEXT NOT NULL,
  target_id   TEXT NOT NULL,
  seq         INTEGER NOT NULL,
  "check"     TEXT NOT NULL,
  severity    TEXT NOT NULL,                -- info | warning | error
  detail      TEXT,
  PRIMARY KEY (target_kind, target_id, seq)
);
