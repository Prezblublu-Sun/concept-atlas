-- llm.sqlite: every model call of a project (increment 4 §1.4). Per project, outside builds; never written by
-- anything but the LLM layer (atlas/llm/*). Schema version 1.
--
-- calls     one row per distinct request (request_hash = sha256 of provider/model/system/user/max_tokens/schema/
--           effort): the response and its validation state. The task layer reads a call only when
--           status = 'ok' AND validated = 1 (then the ledger gets a cache_local row at zero cost).
-- ledger    one row per attempt (live call, batch request, local cache hit, budget stop). Inserted as `pending`
--           with the high estimate BEFORE the HTTP call / batch submit and updated afterwards; a `pending` row
--           that is never updated is unknown spend and reported at startup / by `atlas cost report`.
-- batches   one row per Message Batch submitted (`atlas llm batches list|status|collect`).
-- balance_probes  DeepSeek account balance readings (the Anthropic API has no balance endpoint).
-- unit_costs      per-item cost medians per (task, prompt_sha) refreshed by `atlas cost calibrate`; the low
--                 estimate of a dry run reads them.

CREATE TABLE IF NOT EXISTS calls (
  request_hash  TEXT PRIMARY KEY,
  provider      TEXT NOT NULL,
  model         TEXT NOT NULL,
  task          TEXT NOT NULL DEFAULT '',
  prompt_sha    TEXT NOT NULL DEFAULT '',
  request_json  TEXT NOT NULL,             -- canonical request + task + key (see atlas.llm.base.canonical_request)
  response_text TEXT NOT NULL DEFAULT '',
  stop_reason   TEXT NOT NULL DEFAULT '',  -- end_turn | max_tokens | refusal | error | '' (pending)
  usage_json    TEXT NOT NULL DEFAULT '{}',
  cost          REAL NOT NULL DEFAULT 0,
  currency      TEXT NOT NULL DEFAULT 'USD',
  status        TEXT NOT NULL,             -- pending | ok | truncated | malformed | refused | error
  validated     INTEGER NOT NULL DEFAULT 0,  -- 1: parsed JSON passed the local schema check
  created       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS calls_task ON calls(task, prompt_sha);

CREATE TABLE IF NOT EXISTS ledger (
  id                 INTEGER PRIMARY KEY,
  ts                 TEXT NOT NULL,
  run_id             TEXT NOT NULL DEFAULT '',   -- settings of the run: <project>/work/runs/<run_id>/llm_settings.json
  build_id           TEXT,
  stage              TEXT NOT NULL DEFAULT '',
  item_key           TEXT NOT NULL DEFAULT '',
  task               TEXT NOT NULL DEFAULT '',
  prompt_sha         TEXT NOT NULL DEFAULT '',
  request_hash       TEXT NOT NULL DEFAULT '',
  provider           TEXT NOT NULL DEFAULT '',   -- anthropic | deepseek | fake (the provider split assigns items)
  failover           INTEGER NOT NULL DEFAULT 0, -- 1: the item was re-run here after its assigned provider failed
  attempt            INTEGER NOT NULL DEFAULT 1,
  status             TEXT NOT NULL,        -- pending | ok | cache_local | cache_batch | truncated | malformed |
                                           -- refused | error | budget_stop
  in_tokens          INTEGER NOT NULL DEFAULT 0,
  cache_write_tokens INTEGER NOT NULL DEFAULT 0,
  cache_read_tokens  INTEGER NOT NULL DEFAULT 0,
  out_tokens         INTEGER NOT NULL DEFAULT 0,
  est_cost           REAL NOT NULL DEFAULT 0,   -- the high estimate reserved before the call
  cost               REAL NOT NULL DEFAULT 0,   -- actual, in `currency` (batch rows: × batch_multiplier)
  currency           TEXT NOT NULL DEFAULT 'USD',
  latency_ms         INTEGER NOT NULL DEFAULT 0,
  model_reported     TEXT,
  batch_id           TEXT
);
CREATE INDEX IF NOT EXISTS ledger_run    ON ledger(run_id);
CREATE INDEX IF NOT EXISTS ledger_status ON ledger(status);
CREATE INDEX IF NOT EXISTS ledger_batch  ON ledger(batch_id);
CREATE INDEX IF NOT EXISTS ledger_ts     ON ledger(ts);
CREATE INDEX IF NOT EXISTS ledger_task   ON ledger(task, prompt_sha, status);
CREATE INDEX IF NOT EXISTS ledger_prov   ON ledger(provider);

CREATE TABLE IF NOT EXISTS batches (
  batch_id   TEXT PRIMARY KEY,
  provider   TEXT NOT NULL,
  model      TEXT NOT NULL,
  created    TEXT NOT NULL,
  status     TEXT NOT NULL,                -- in_progress | canceling | ended | collected | unknown
  n_requests INTEGER NOT NULL DEFAULT 0,
  n_done     INTEGER NOT NULL DEFAULT 0,
  n_failed   INTEGER NOT NULL DEFAULT 0,
  cost       REAL NOT NULL DEFAULT 0,
  run_id     TEXT NOT NULL DEFAULT '',
  stage      TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS balance_probes (
  id       INTEGER PRIMARY KEY,
  ts       TEXT NOT NULL,
  provider TEXT NOT NULL,
  balance  REAL NOT NULL,
  currency TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS unit_costs (
  task        TEXT NOT NULL,
  prompt_sha  TEXT NOT NULL,
  unit        TEXT NOT NULL DEFAULT 'item',
  cost_median REAL NOT NULL,
  n           INTEGER NOT NULL,
  measured_at TEXT NOT NULL,
  PRIMARY KEY (task, prompt_sha, unit)
);
