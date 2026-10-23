"""Message Batches (increment 4 §1.6): submit the requests a `TaskRunner` queued in `--mode batch`, poll, collect.

Submit: the queued requests of one stage run become one batch per provider and chunk (the API caps a batch at
100,000 requests / 256 MB; `BATCH_MAX_BYTES` keeps a margin). Requests with the same hash are sent once. Before
a chunk is submitted the governor reserves the sum of its high estimates at batch prices (in the primary
currency); after the provider accepted it the reservation is released because every request now has a
`pending` ledger row with `batch_id` (and a `pending` `calls` row holding the request), which the governor
counts as pending spend until collected. `custom_id` = `custom_id_for(item key)` (the key itself whenever it
conforms to the API's `[A-Za-z0-9_-]{1,64}`). Only backends with a batch API queue requests (the runner calls
DeepSeek's share live).

Collect (`atlas llm batches collect <id>`, or `--wait` on a stage run): when the provider reports `ended`, every
pending ledger row of the batch is matched to its result (by custom id, else by another row's result with the
same request hash), classified exactly like a live answer (`atlas.llm.tasks.classify` against the schema stored
with the call), priced at `batch_multiplier` and finished — `cache_batch` for a valid answer, else truncated /
malformed / refused / error; the `calls` row gets the response. Idempotent: rows already finished are skipped, a
second collect reports 0 new rows. A stage run that follows finds every answered request in `calls` and
proceeds on `cache_local` hits.
"""
from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Sequence

from atlas.llm.base import ChatClient, ChatResponse
from atlas.llm.client import custom_id_for
from atlas.llm.ledger import PriceTable, cost_of, provider_multiplier, usage_dict
from atlas.llm.tasks import Queued, TaskRunner, classify
from atlas.store.llm import LlmStore

log = logging.getLogger(__name__)

BATCH_MAX_REQUESTS = 100_000
BATCH_MAX_BYTES = 240 * 1024 * 1024


def _request_bytes(q: Queued) -> int:
    return len(json.dumps({"system": q.req.system, "user": q.req.user, "schema": q.req.output_schema},
                          ensure_ascii=False).encode("utf-8")) + 200


def chunk_queued(queued: Sequence[Queued], max_requests: int = BATCH_MAX_REQUESTS,
                 max_bytes: int = BATCH_MAX_BYTES) -> list[list[Queued]]:
    chunks: list[list[Queued]] = []
    cur: list[Queued] = []
    size = 0
    for q in queued:
        b = _request_bytes(q)
        if cur and (len(cur) >= max_requests or size + b > max_bytes):
            chunks.append(cur)
            cur, size = [], 0
        cur.append(q)
        size += b
    if cur:
        chunks.append(cur)
    return chunks


def submit_queued(runner: TaskRunner) -> list[str]:
    """Submit `runner.queued` as Message Batches (one per provider and chunk); returns the batch ids. Raises
    BudgetExceeded before anything is sent when a chunk does not fit the budgets; `runner.queued` is cleared on
    success (items of a chunk that was submitted are removed even when a later chunk is refused)."""
    ids: list[str] = []
    for provider in sorted({q.provider for q in runner.queued}):
        backend = runner.backends[provider]
        ledger, client, mult = backend.ledger, backend.client, backend.price.batch_multiplier
        mine = [q for q in runner.queued if q.provider == provider]
        for chunk in chunk_queued(mine):
            total_high = runner.to_primary(sum(q.estimate.cost_high for q in chunk) * mult, backend.currency)
            reservation = runner.governor.reserve(total_high) if runner.governor is not None else None
            unique: dict[str, Queued] = {}
            for q in chunk:
                unique.setdefault(ledger.hash(q.req), q)
            try:
                batch_id = client.submit_batch([q.req for q in unique.values()])
            finally:
                if runner.governor is not None:
                    runner.governor.release(reservation)
            runner.store.add_batch(batch_id=batch_id, provider=provider, model=backend.model, status="in_progress",
                                   n_requests=len(unique), run_id=runner.run_id, stage=runner.stage)
            for q in chunk:
                ledger.begin(q.req, q.item_key, q.task.prompt_sha, 1, q.estimate.cost_high * mult, batch_id=batch_id)
            sent = set(map(id, chunk))
            runner.queued[:] = [q for q in runner.queued if id(q) not in sent]
            log.info("batch %s (%s) submitted: %d requests (%d queued items)", batch_id, provider, len(unique), len(chunk))
            ids.append(batch_id)
    return ids


def collect(store: LlmStore, client: ChatClient, batch_id: str, prices: PriceTable) -> dict:
    """{state, collected, failed, missing, pending_before, cost}. Nothing is written unless the provider reports
    the batch `ended`; a batch unknown to the ledger is a KeyError. `client` is the batch's provider's client."""
    meta = store.get_batch(batch_id)
    if meta is None:
        raise KeyError(f"batch {batch_id} is not in this project's ledger")
    pending = store.ledger_rows(batch_id=batch_id, status="pending")
    status = client.batch_status(batch_id)
    out = {"batch_id": batch_id, "provider": meta["provider"], "state": status.state, "pending_before": len(pending),
           "collected": 0, "failed": 0, "missing": 0, "cost": 0.0, "currency": client.currency}
    if status.state != "ended":
        store.update_batch(batch_id, status=status.state, n_done=status.n_done, n_failed=status.n_failed)
        return out
    if not pending:
        store.update_batch(batch_id, status="collected")
        return out
    results = client.batch_results(batch_id)
    by_hash: dict[str, ChatResponse] = {}
    multiplier = provider_multiplier(client)
    for row in pending:
        resp = results.get(custom_id_for(row["item_key"]))
        if resp is None:
            resp = results.get(row["item_key"]) or by_hash.get(row["request_hash"])
        if resp is None:
            out["missing"] += 1
            continue
        by_hash[row["request_hash"]] = resp
        call = store.get_call(row["request_hash"]) or {}
        request = call.get("request") or {}
        schema = request.get("output_schema")
        st, _data, _errs = classify(resp, schema)
        price = prices.price_for(request.get("model") or meta["model"], resp.usage.model_reported)
        cost = cost_of(resp.usage, price, batch=True, multiplier=multiplier)
        store.finish_row(row["id"], status="cache_batch" if st == "ok" else st, in_tokens=resp.usage.input,
                         cache_write_tokens=resp.usage.cache_write, cache_read_tokens=resp.usage.cache_read,
                         out_tokens=resp.usage.output, cost=cost, latency_ms=resp.latency_ms,
                         model_reported=resp.usage.model_reported)
        store.put_call(request_hash=row["request_hash"], provider=meta["provider"], model=request.get("model") or meta["model"],
                       task=row["task"], prompt_sha=row["prompt_sha"], request=request, response_text=resp.text,
                       stop_reason=resp.stop_reason, usage=usage_dict(resp.usage), cost=cost, currency=price.currency,
                       status=st, validated=(st == "ok"))
        out["collected"] += 1
        out["cost"] += cost
        if st != "ok":
            out["failed"] += 1
    done = store.ledger_rows(batch_id=batch_id)
    n_failed = sum(1 for r in done if r["status"] in ("truncated", "malformed", "refused", "error"))
    n_done = sum(1 for r in done if r["status"] == "cache_batch")
    still = sum(1 for r in done if r["status"] == "pending")
    store.update_batch(batch_id, status="collected" if not still else "ended", n_done=n_done, n_failed=n_failed,
                       cost=sum(float(r["cost"]) for r in done))
    log.info("batch %s: %d collected, %d failed, %d missing", batch_id, out["collected"], out["failed"], out["missing"])
    return out


def status_report(store: LlmStore, client: ChatClient, batch_id: str, probe: bool = True) -> dict:
    """Ledger view of a batch (+ the provider's live counts when `probe`)."""
    meta = store.get_batch(batch_id)
    if meta is None:
        raise KeyError(f"batch {batch_id} is not in this project's ledger")
    rows = store.ledger_rows(batch_id=batch_id)
    out = dict(meta)
    out["ledger"] = {"rows": len(rows), "pending": sum(1 for r in rows if r["status"] == "pending"),
                     "cache_batch": sum(1 for r in rows if r["status"] == "cache_batch"),
                     "failed": sum(1 for r in rows if r["status"] in ("truncated", "malformed", "refused", "error")),
                     "cost": sum(float(r["cost"]) for r in rows)}
    if probe and meta["status"] != "collected":
        s = client.batch_status(batch_id)
        out["provider_state"] = {"state": s.state, "n_requests": s.n_requests, "n_done": s.n_done, "n_failed": s.n_failed}
        store.update_batch(batch_id, status=s.state, n_done=s.n_done, n_failed=s.n_failed)
        out["status"] = s.state
    return out


def wait_for_batch(store: LlmStore, client: ChatClient, batch_id: str, prices: PriceTable, *, poll_seconds: float = 30.0,
                   timeout_seconds: float | None = None, sleep: Callable[[float], None] = time.sleep) -> dict:
    """Poll until the batch ended (or the timeout passed), then collect. Returns the collect result (state
    `in_progress` when the timeout hit first)."""
    t0 = time.monotonic()
    while True:
        r = collect(store, client, batch_id, prices)
        if r["state"] == "ended" or r["state"] == "unknown":
            return r
        if timeout_seconds is not None and time.monotonic() - t0 >= timeout_seconds:
            return r
        sleep(poll_seconds)
