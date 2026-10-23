"""LlmStore: repository over `<project>/llm/llm.sqlite` (increment 4 §1.4): calls, ledger, batches, balance
probes, unit costs. Implemented by: increment 4, work package L.

Rules:
- the connection comes from `store/db.py` (`connect` + `migrate('llm.sql')`); nothing else connects;
- every method returns plain dicts / numbers (no sqlite3.Row leaks out); every write is one transaction;
- `calls` is the exact-hash cache: `lookup_valid` answers only `status = 'ok' AND validated = 1`;
- a ledger row is inserted as `pending` (with the reserved high estimate) before the call and finished afterwards;
  `pending_rows()` is the unknown spend reported at startup and by `atlas cost report`;
- every ledger row names its `provider` (the provider split assigns items) and `failover` = 1 when the item was
  re-run there after its assigned provider failed; sums across providers convert currencies with `rates`
  ({currency: value of one unit in the target currency}) — a currency without a rate counts 1:1;
- timestamps are UTC ISO-8601 seconds, like the other stores; the daily budget scope uses `spent(since=...)`.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import statistics
import time
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

from atlas.store.db import connect, migrate, transaction

log = logging.getLogger(__name__)

LLM_SCHEMA_VERSION = 1
LEDGER_STATUSES = ("pending", "ok", "cache_local", "cache_batch", "truncated", "malformed", "refused", "error",
                   "budget_stop")
#: ledger rows whose `cost` is real spend (everything that was answered by a provider, ok or not)
SPENT_STATUSES = ("ok", "cache_batch", "truncated", "malformed", "refused", "error")
CALL_STATUSES = ("pending", "ok", "truncated", "malformed", "refused", "error")


def _now() -> str:
    return datetime.fromtimestamp(time.time(), tz=timezone.utc).isoformat(timespec="seconds")


def _dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _loads(s: str | None, default):
    if not s:
        return default
    try:
        return json.loads(s)
    except ValueError:
        return default


def _rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


def open_llm(path: str | Path, mode: str = "rw") -> sqlite3.Connection:
    """connect + migrate('llm.sql') when mode == 'rw' (same shape as db.open_evidence / open_user)."""
    conn = connect(path, mode)
    if mode == "rw":
        migrate(conn, "llm.sql", LLM_SCHEMA_VERSION)
    return conn


class LlmStore:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    @classmethod
    def open(cls, path: str | Path, mode: str = "rw") -> "LlmStore":
        return cls(open_llm(path, mode))

    def close(self) -> None:
        self.conn.close()

    # ---- calls (exact-hash cache) -----------------------------------------------------------------------------
    @staticmethod
    def _call_dict(r: sqlite3.Row) -> dict:
        d = dict(r)
        d["request"] = _loads(d.pop("request_json"), {})
        d["usage"] = _loads(d.pop("usage_json"), {})
        d["validated"] = bool(d["validated"])
        return d

    def get_call(self, request_hash: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM calls WHERE request_hash=?", (request_hash,)).fetchone()
        return self._call_dict(r) if r is not None else None

    def lookup_valid(self, request_hash: str) -> dict | None:
        """The cached call for this hash, only when it succeeded and its JSON passed validation."""
        r = self.conn.execute("SELECT * FROM calls WHERE request_hash=? AND status='ok' AND validated=1",
                              (request_hash,)).fetchone()
        return self._call_dict(r) if r is not None else None

    def put_call(self, *, request_hash: str, provider: str, model: str, task: str, prompt_sha: str, request: dict,
                 response_text: str = "", stop_reason: str = "", usage: dict | None = None, cost: float = 0.0,
                 currency: str = "USD", status: str = "pending", validated: bool = False,
                 keep_valid: bool = True) -> bool:
        """INSERT OR REPLACE the call row. With `keep_valid` (default) an existing ok+validated row is never
        overwritten by a worse one (a batch result collected after a live run answered the same hash).
        Returns True when the row was written."""
        if status not in CALL_STATUSES:
            raise ValueError(f"bad call status {status!r}")
        with transaction(self.conn) as c:
            if keep_valid and not validated:
                have = c.execute("SELECT 1 FROM calls WHERE request_hash=? AND status='ok' AND validated=1",
                                 (request_hash,)).fetchone()
                if have is not None:
                    return False
            c.execute(
                "INSERT OR REPLACE INTO calls(request_hash, provider, model, task, prompt_sha, request_json, "
                "response_text, stop_reason, usage_json, cost, currency, status, validated, created) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (request_hash, provider, model, task, prompt_sha, _dumps(request), response_text, stop_reason,
                 _dumps(usage or {}), float(cost), currency, status, 1 if validated else 0, _now()))
        return True

    # ---- ledger ---------------------------------------------------------------------------------------------------
    def add_pending(self, *, run_id: str, build_id: str | None, stage: str, item_key: str, task: str,
                    prompt_sha: str, request_hash: str, attempt: int, est_cost: float, currency: str,
                    provider: str = "", failover: bool = False, batch_id: str | None = None) -> int:
        """The `pending` row written BEFORE a call / batch submit; returns its id."""
        with transaction(self.conn) as c:
            cur = c.execute(
                "INSERT INTO ledger(ts, run_id, build_id, stage, item_key, task, prompt_sha, request_hash, provider, "
                "failover, attempt, status, est_cost, currency, batch_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,'pending',?,?,?)",
                (_now(), run_id, build_id, stage, item_key, task, prompt_sha, request_hash, provider,
                 1 if failover else 0, int(attempt), float(est_cost), currency, batch_id))
            return int(cur.lastrowid)

    def finish_row(self, row_id: int, *, status: str, in_tokens: int = 0, cache_write_tokens: int = 0,
                   cache_read_tokens: int = 0, out_tokens: int = 0, cost: float = 0.0, latency_ms: int = 0,
                   model_reported: str | None = None, request_hash: str | None = None) -> None:
        if status not in LEDGER_STATUSES or status == "pending":
            raise ValueError(f"bad ledger status {status!r}")
        sets = ["status=?", "in_tokens=?", "cache_write_tokens=?", "cache_read_tokens=?", "out_tokens=?", "cost=?",
                "latency_ms=?", "model_reported=?"]
        vals: list = [status, int(in_tokens), int(cache_write_tokens), int(cache_read_tokens), int(out_tokens),
                      float(cost), int(latency_ms), model_reported]
        if request_hash is not None:
            sets.append("request_hash=?")
            vals.append(request_hash)
        with transaction(self.conn) as c:
            c.execute(f"UPDATE ledger SET {', '.join(sets)} WHERE id=?", (*vals, int(row_id)))

    def add_row(self, *, run_id: str, build_id: str | None, stage: str, item_key: str, task: str, prompt_sha: str,
                request_hash: str, attempt: int, status: str, currency: str, est_cost: float = 0.0,
                cost: float = 0.0, provider: str = "", failover: bool = False, batch_id: str | None = None) -> int:
        """A row that never had a pending phase (cache_local hits, budget_stop)."""
        if status not in LEDGER_STATUSES or status == "pending":
            raise ValueError(f"bad ledger status {status!r}")
        with transaction(self.conn) as c:
            cur = c.execute(
                "INSERT INTO ledger(ts, run_id, build_id, stage, item_key, task, prompt_sha, request_hash, provider, "
                "failover, attempt, status, est_cost, cost, currency, batch_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (_now(), run_id, build_id, stage, item_key, task, prompt_sha, request_hash, provider,
                 1 if failover else 0, int(attempt), status, float(est_cost), float(cost), currency, batch_id))
            return int(cur.lastrowid)

    def ledger_rows(self, *, run_id: str | None = None, stage: str | None = None, status: str | None = None,
                    batch_id: str | None = None, limit: int | None = None) -> list[dict]:
        where, args = [], []
        for col, val in (("run_id", run_id), ("stage", stage), ("status", status), ("batch_id", batch_id)):
            if val is not None:
                where.append(f"{col}=?")
                args.append(val)
        sql = "SELECT * FROM ledger" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id"
        if limit is not None:
            sql += " LIMIT ?"
            args.append(int(limit))
        return _rows(self.conn.execute(sql, args))

    def pending_rows(self) -> list[dict]:
        return self.ledger_rows(status="pending")

    def spent(self, *, run_id: str | None = None, stage: str | None = None, since: str | None = None,
              rates: dict[str, float] | None = None) -> dict:
        """{cost: Σ cost of answered rows, pending_est: Σ est_cost of pending rows, rows: n, by_currency:
        {currency: {cost, pending_est}}} in the scope; `cost` / `pending_est` are converted with `rates`."""
        where, args = [], []
        for col, val in (("run_id", run_id), ("stage", stage)):
            if val is not None:
                where.append(f"{col}=?")
                args.append(val)
        if since is not None:
            where.append("ts >= ?")
            args.append(since)
        w = (" WHERE " + " AND ".join(where)) if where else ""
        marks = ",".join("?" * len(SPENT_STATUSES))
        groups = _rows(self.conn.execute(
            f"SELECT currency, COUNT(*) AS n, "
            f"COALESCE(SUM(CASE WHEN status IN ({marks}) THEN cost ELSE 0 END), 0) AS cost, "
            f"COALESCE(SUM(CASE WHEN status='pending' THEN est_cost ELSE 0 END), 0) AS pending_est "
            f"FROM ledger{w} GROUP BY currency", (*SPENT_STATUSES, *args)))
        out = {"cost": 0.0, "pending_est": 0.0, "rows": 0, "by_currency": {}}
        for g in groups:
            rate = float((rates or {}).get(g["currency"], 1.0))
            out["by_currency"][g["currency"]] = {"cost": float(g["cost"]), "pending_est": float(g["pending_est"])}
            out["cost"] += float(g["cost"]) * rate
            out["pending_est"] += float(g["pending_est"]) * rate
            out["rows"] += int(g["n"])
        return out

    def totals(self, *, run_id: str | None = None, stage: str | None = None) -> dict:
        """`atlas cost report`: per (provider, stage, task) counts by status, token sums and cost (in the row
        currency), the per-provider totals, the grand total, the local-cache hit rate (rows) and the prompt-cache
        read ratio (tokens). Costs are not converted here (`by_provider` carries each currency)."""
        where, args = [], []
        for col, val in (("run_id", run_id), ("stage", stage)):
            if val is not None:
                where.append(f"{col}=?")
                args.append(val)
        w = (" WHERE " + " AND ".join(where)) if where else ""
        groups = _rows(self.conn.execute(
            f"SELECT provider, stage, task, status, COUNT(*) AS n, SUM(in_tokens) AS in_tokens, "
            f"SUM(cache_write_tokens) AS cache_write_tokens, SUM(cache_read_tokens) AS cache_read_tokens, "
            f"SUM(out_tokens) AS out_tokens, SUM(cost) AS cost, SUM(est_cost) AS est_cost, MAX(currency) AS currency, "
            f"SUM(failover) AS failover FROM ledger{w} GROUP BY provider, stage, task, status "
            f"ORDER BY provider, stage, task, status", args))
        by: dict[tuple[str, str, str], dict] = {}
        by_provider: dict[str, dict] = {}
        total = {"rows": 0, "in_tokens": 0, "cache_write_tokens": 0, "cache_read_tokens": 0, "out_tokens": 0,
                 "cost": 0.0, "pending": 0, "pending_est": 0.0, "currency": None}
        for g in groups:
            d = by.setdefault((g["provider"], g["stage"], g["task"]), {
                "provider": g["provider"], "stage": g["stage"], "task": g["task"], "rows": 0, "by_status": {},
                "in_tokens": 0, "cache_write_tokens": 0, "cache_read_tokens": 0, "out_tokens": 0, "cost": 0.0,
                "failover": 0, "currency": g["currency"]})
            p = by_provider.setdefault(g["provider"], {"provider": g["provider"], "rows": 0, "in_tokens": 0,
                                                        "cache_write_tokens": 0, "cache_read_tokens": 0,
                                                        "out_tokens": 0, "cost": 0.0, "failover": 0,
                                                        "currency": g["currency"]})
            d["by_status"][g["status"]] = int(g["n"])
            for agg in (d, p, total):
                agg["rows"] += int(g["n"])
                agg["cost"] += float(g["cost"] or 0)
                for k in ("in_tokens", "cache_write_tokens", "cache_read_tokens", "out_tokens"):
                    agg[k] += int(g[k] or 0)
            d["failover"] += int(g["failover"] or 0)
            p["failover"] += int(g["failover"] or 0)
            total["currency"] = total["currency"] or g["currency"]
            if g["status"] == "pending":
                total["pending"] += int(g["n"])
                total["pending_est"] += float(g["est_cost"] or 0)
        rows = list(by.values())
        answered = sum(v for d in rows for s, v in d["by_status"].items() if s not in ("budget_stop", "pending"))
        hits = sum(d["by_status"].get("cache_local", 0) for d in rows)
        tin = total["in_tokens"] + total["cache_write_tokens"] + total["cache_read_tokens"]
        return {"rows": rows, "by_provider": list(by_provider.values()), "total": total,
                "cache_local_rate": (hits / answered) if answered else 0.0,
                "cache_read_ratio": (total["cache_read_tokens"] / tin) if tin else 0.0}

    def task_observations(self, task: str, prompt_sha: str, provider: str | None = None) -> dict:
        """What the ledger knows about answered calls of (task, prompt_sha[, provider]): {n, cache_ratio,
        out_median, cost_median, in_median}. n = 0 when nothing was observed (the estimator then uses defaults)."""
        sql = ("SELECT in_tokens, cache_write_tokens, cache_read_tokens, out_tokens, cost FROM ledger "
               "WHERE task=? AND prompt_sha=? AND status IN ('ok', 'cache_batch') AND "
               "(in_tokens + cache_write_tokens + cache_read_tokens) > 0")
        args: list = [task, prompt_sha]
        if provider is not None:
            sql += " AND provider=?"
            args.append(provider)
        rows = _rows(self.conn.execute(sql, args))
        if not rows:
            return {"n": 0, "cache_ratio": 0.0, "out_median": 0, "cost_median": 0.0, "in_median": 0}
        tin = sum(r["in_tokens"] + r["cache_write_tokens"] + r["cache_read_tokens"] for r in rows)
        tread = sum(r["cache_read_tokens"] for r in rows)
        return {"n": len(rows), "cache_ratio": (tread / tin) if tin else 0.0,
                "out_median": int(statistics.median(r["out_tokens"] for r in rows)),
                "cost_median": float(statistics.median(r["cost"] for r in rows)),
                "in_median": int(statistics.median(r["in_tokens"] + r["cache_write_tokens"] + r["cache_read_tokens"]
                                                   for r in rows))}

    # ---- batches --------------------------------------------------------------------------------------------------
    def add_batch(self, *, batch_id: str, provider: str, model: str, status: str, n_requests: int, run_id: str = "",
                  stage: str = "") -> None:
        with transaction(self.conn) as c:
            c.execute("INSERT OR REPLACE INTO batches(batch_id, provider, model, created, status, n_requests, "
                      "run_id, stage) VALUES (?,?,?,?,?,?,?,?)",
                      (batch_id, provider, model, _now(), status, int(n_requests), run_id, stage))

    def update_batch(self, batch_id: str, *, status: str, n_done: int | None = None, n_failed: int | None = None,
                     cost: float | None = None) -> None:
        sets, vals = ["status=?"], [status]
        for col, val in (("n_done", n_done), ("n_failed", n_failed), ("cost", cost)):
            if val is not None:
                sets.append(f"{col}=?")
                vals.append(val)
        with transaction(self.conn) as c:
            c.execute(f"UPDATE batches SET {', '.join(sets)} WHERE batch_id=?", (*vals, batch_id))

    def get_batch(self, batch_id: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
        return dict(r) if r is not None else None

    def list_batches(self) -> list[dict]:
        return _rows(self.conn.execute("SELECT * FROM batches ORDER BY created, batch_id"))

    # ---- balance probes (DeepSeek) ------------------------------------------------------------------------------
    def add_balance_probe(self, provider: str, balance: float, currency: str) -> None:
        with transaction(self.conn) as c:
            c.execute("INSERT INTO balance_probes(ts, provider, balance, currency) VALUES (?,?,?,?)",
                      (_now(), provider, float(balance), currency))

    def last_balance(self, provider: str) -> dict | None:
        r = self.conn.execute("SELECT ts, provider, balance, currency FROM balance_probes WHERE provider=? "
                              "ORDER BY id DESC LIMIT 1", (provider,)).fetchone()
        return dict(r) if r is not None else None

    # ---- unit costs -----------------------------------------------------------------------------------------------
    def unit_cost(self, task: str, prompt_sha: str, unit: str = "item") -> dict | None:
        r = self.conn.execute("SELECT * FROM unit_costs WHERE task=? AND prompt_sha=? AND unit=?",
                              (task, prompt_sha, unit)).fetchone()
        return dict(r) if r is not None else None

    def unit_costs(self) -> list[dict]:
        return _rows(self.conn.execute("SELECT * FROM unit_costs ORDER BY task, prompt_sha, unit"))

    def calibrate(self, unit: str = "item") -> list[dict]:
        """Refresh `unit_costs` from the ledger: the median cost of answered (ok / cache_batch) rows per
        (task, prompt_sha). Returns the rows written."""
        groups = _rows(self.conn.execute(
            "SELECT task, prompt_sha FROM ledger WHERE status IN ('ok', 'cache_batch') AND task <> '' "
            "GROUP BY task, prompt_sha ORDER BY task, prompt_sha"))
        out = []
        now = _now()
        with transaction(self.conn) as c:
            for g in groups:
                costs = [float(r[0]) for r in c.execute(
                    "SELECT cost FROM ledger WHERE task=? AND prompt_sha=? AND status IN ('ok', 'cache_batch')",
                    (g["task"], g["prompt_sha"]))]
                row = {"task": g["task"], "prompt_sha": g["prompt_sha"], "unit": unit,
                       "cost_median": float(statistics.median(costs)), "n": len(costs), "measured_at": now}
                c.execute("INSERT OR REPLACE INTO unit_costs(task, prompt_sha, unit, cost_median, n, measured_at) "
                          "VALUES (?,?,?,?,?,?)", (row["task"], row["prompt_sha"], unit, row["cost_median"],
                                                   row["n"], now))
                out.append(row)
        return out

    def add_unit_costs(self, rows: Iterable[dict]) -> None:
        """Explicit seeding (tests, imports): rows with task, prompt_sha, unit, cost_median, n."""
        now = _now()
        with transaction(self.conn) as c:
            c.executemany("INSERT OR REPLACE INTO unit_costs(task, prompt_sha, unit, cost_median, n, measured_at) "
                          "VALUES (?,?,?,?,?,?)",
                          [(r["task"], r["prompt_sha"], r.get("unit", "item"), float(r["cost_median"]),
                            int(r.get("n", 0)), r.get("measured_at") or now) for r in rows])
