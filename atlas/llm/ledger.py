"""Ledger and cache (increment 4 §1.4): prices, cost arithmetic, estimates and the row lifecycle over
`atlas.store.llm.LlmStore`.

Rules:
- cost = (in × p_in + cache_write × p_write + cache_read × p_read + out × p_out) / 1e6, × `batch_multiplier` for
  batch rows, × the provider's peak multiplier (DeepSeek) — in the provider currency; the row is priced by the
  model that answered (`usage.model_reported`) when the price table knows it, else by the requested model;
- a `pending` ledger row with the high estimate is written BEFORE the call (`begin`) and finished afterwards
  (`finish`), which also writes the `calls` row; an exact-hash cache hit writes a `cache_local` row at zero cost;
  a refused budget writes `budget_stop`;
- estimates are local (`atlas.llm.base.estimate_tokens`, no network, so a dry run never calls a provider): the
  high estimate assumes every input token is uncached and the output reaches `max_tokens`; the low estimate uses
  the ledger-observed cache ratio and median output of the same (task, prompt_sha) when there are observations,
  else the share of the stable system prefix as the cache ratio (it is cached after the first request) and
  `max_tokens / 4` output — or the task's `unit_cost_default` per item when the author set one. Both carry
  `RETRY_OVERHEAD` (+6 %) and the low estimate never exceeds the high one;
- `new_run_id(stage)` = `<stage>_<UTC timestamp>`; stages pass it to every row of one run.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from atlas.config import Project
from atlas.llm.base import (ChatClient, ChatRequest, ChatResponse, Usage, canonical_request, estimate_tokens,
                            request_hash)
from atlas.store.llm import LlmStore

log = logging.getLogger(__name__)

RETRY_OVERHEAD = 0.06
LOW_OUTPUT_FRACTION = 4          # unobserved low estimate: max_tokens / 4 output tokens
MILLION = 1_000_000.0


@dataclass(frozen=True)
class Price:
    input: float
    output: float
    cache_write: float
    cache_read: float
    currency: str = "USD"
    batch_multiplier: float = 0.5

    @classmethod
    def from_config(cls, d: dict) -> "Price":
        return cls(input=float(d["input"]), output=float(d["output"]), cache_write=float(d.get("cache_write", d["input"])),
                   cache_read=float(d.get("cache_read", d["input"])), currency=str(d.get("currency") or "USD"),
                   batch_multiplier=float(d.get("batch_multiplier", 0.5)))


class PriceTable:
    """{model: Price} from `llm.pricing`; `price_for(requested, reported)` prefers the model that answered."""

    def __init__(self, prices: dict[str, Price]) -> None:
        self.prices = dict(prices)

    @classmethod
    def from_config(cls, pricing: dict | None) -> "PriceTable":
        out: dict[str, Price] = {}
        for model, d in (pricing or {}).items():
            if isinstance(d, dict) and "input" in d and "output" in d:
                out[str(model)] = Price.from_config(d)
        return cls(out)

    def get(self, model: str) -> Price:
        try:
            return self.prices[model]
        except KeyError:
            raise KeyError(f"no price for model {model!r} in llm.pricing (known: {sorted(self.prices)})") from None

    def price_for(self, requested: str, reported: str | None = None) -> Price:
        if reported and reported in self.prices:
            return self.prices[reported]
        return self.get(requested)


def price_table(project: Project) -> PriceTable:
    return PriceTable.from_config(project.get("llm.pricing") or {})


def cost_of(usage: Usage, price: Price, batch: bool = False, multiplier: float = 1.0) -> float:
    base = (usage.input * price.input + usage.cache_write * price.cache_write + usage.cache_read * price.cache_read
            + usage.output * price.output) / MILLION
    if batch:
        base *= price.batch_multiplier
    return base * float(multiplier)


def provider_multiplier(client: ChatClient) -> float:
    """The provider's current price multiplier (DeepSeek peak hours); 1.0 for providers without one."""
    fn = getattr(client, "price_multiplier", None)
    return float(fn()) if callable(fn) else 1.0


@dataclass
class Estimate:
    in_tokens: int
    sys_tokens: int
    out_low: int
    out_high: int
    cost_low: float
    cost_high: float
    observed: bool = False

    def scaled(self, factor: float) -> "Estimate":
        return Estimate(self.in_tokens, self.sys_tokens, self.out_low, self.out_high, self.cost_low * factor,
                        self.cost_high * factor, self.observed)


def estimate(req: ChatRequest, price: Price, observations: dict | None = None, unit_cost_default: float = 0.0,
             batch: bool = False, multiplier: float = 1.0) -> Estimate:
    sys_tokens = estimate_tokens("\n".join(req.system)) if req.system else 0
    in_tokens = sys_tokens + estimate_tokens(req.user)
    mult = (price.batch_multiplier if batch else 1.0) * float(multiplier) * (1.0 + RETRY_OVERHEAD)
    out_high = int(req.max_tokens)
    cost_high = cost_of(Usage(input=in_tokens, output=out_high), price) * mult
    obs = observations or {}
    observed = int(obs.get("n", 0)) > 0
    if observed:
        ratio = min(1.0, max(0.0, float(obs.get("cache_ratio", 0.0))))
        out_low = int(obs.get("out_median", 0))
    else:
        ratio = (sys_tokens / in_tokens) if in_tokens else 0.0
        out_low = max(1, out_high // LOW_OUTPUT_FRACTION)
    read = int(round(in_tokens * ratio))
    cost_low = cost_of(Usage(input=in_tokens - read, cache_read=read, output=out_low), price) * mult
    if not observed and unit_cost_default > 0:
        cost_low = float(unit_cost_default) * mult
    return Estimate(in_tokens=in_tokens, sys_tokens=sys_tokens, out_low=out_low, out_high=out_high,
                    cost_low=min(cost_low, cost_high), cost_high=cost_high, observed=observed)


def new_run_id(stage: str) -> str:
    return f"{stage}_{datetime.fromtimestamp(time.time(), tz=timezone.utc).strftime('%Y%m%dT%H%M%S')}"


class Ledger:
    """Row lifecycle for one provider of one stage run: `lookup` → (`cache_hit` | `begin` → `finish`) |
    `budget_stop`. `failover` marks the rows of an item re-run here after its assigned provider failed."""

    def __init__(self, store: LlmStore, prices: PriceTable, *, provider: str, currency: str, run_id: str,
                 stage: str, build_id: str | None = None, failover: bool = False) -> None:
        self.store = store
        self.prices = prices
        self.provider = provider
        self.currency = currency
        self.run_id = run_id
        self.stage = stage
        self.build_id = build_id
        self.failover = failover
        self.cost = 0.0                      # actual spend of this run through this ledger (this process)

    def for_failover(self) -> "Ledger":
        """The same ledger with `failover` set (rows of re-run items are tagged)."""
        return Ledger(self.store, self.prices, provider=self.provider, currency=self.currency, run_id=self.run_id,
                      stage=self.stage, build_id=self.build_id, failover=True)

    def hash(self, req: ChatRequest) -> str:
        return request_hash(self.provider, req)

    def lookup(self, req: ChatRequest) -> dict | None:
        return self.store.lookup_valid(self.hash(req))

    def observations(self, task: str, prompt_sha: str) -> dict:
        return self.store.task_observations(task, prompt_sha, self.provider)

    def cache_hit(self, req: ChatRequest, item_key: str, prompt_sha: str, attempt: int = 1) -> int:
        return self.store.add_row(run_id=self.run_id, build_id=self.build_id, stage=self.stage, item_key=item_key,
                                  task=req.task, prompt_sha=prompt_sha, request_hash=self.hash(req), attempt=attempt,
                                  status="cache_local", currency=self.currency, provider=self.provider,
                                  failover=self.failover)

    def budget_stop(self, req: ChatRequest, item_key: str, prompt_sha: str, est_cost: float, attempt: int = 1) -> int:
        return self.store.add_row(run_id=self.run_id, build_id=self.build_id, stage=self.stage, item_key=item_key,
                                  task=req.task, prompt_sha=prompt_sha, request_hash=self.hash(req), attempt=attempt,
                                  status="budget_stop", currency=self.currency, est_cost=est_cost,
                                  provider=self.provider, failover=self.failover)

    def begin(self, req: ChatRequest, item_key: str, prompt_sha: str, attempt: int, est_cost: float,
              batch_id: str | None = None) -> int:
        """The pending row (and, for batch requests, the pending `calls` row that keeps the request)."""
        h = self.hash(req)
        row = self.store.add_pending(run_id=self.run_id, build_id=self.build_id, stage=self.stage, item_key=item_key,
                                     task=req.task, prompt_sha=prompt_sha, request_hash=h, attempt=attempt,
                                     est_cost=est_cost, currency=self.currency, provider=self.provider,
                                     failover=self.failover, batch_id=batch_id)
        if batch_id is not None:
            self.store.put_call(request_hash=h, provider=self.provider, model=req.model, task=req.task,
                                prompt_sha=prompt_sha, request=self.request_record(req), status="pending",
                                currency=self.currency)
        return row

    def request_record(self, req: ChatRequest) -> dict:
        d = canonical_request(self.provider, req)
        d["task"] = req.task
        d["key"] = req.key
        return d

    def finish(self, row_id: int, req: ChatRequest, resp: ChatResponse, status: str, validated: bool,
               prompt_sha: str, batch: bool = False, multiplier: float = 1.0) -> float:
        """Price the response, update the ledger row (`ok` → `cache_batch` for batch rows) and write the call."""
        price = self.prices.price_for(req.model, resp.usage.model_reported)
        cost = cost_of(resp.usage, price, batch=batch, multiplier=multiplier)
        ledger_status = "cache_batch" if (batch and status == "ok") else status
        self.store.finish_row(row_id, status=ledger_status, in_tokens=resp.usage.input,
                              cache_write_tokens=resp.usage.cache_write, cache_read_tokens=resp.usage.cache_read,
                              out_tokens=resp.usage.output, cost=cost, latency_ms=resp.latency_ms,
                              model_reported=resp.usage.model_reported)
        self.store.put_call(request_hash=self.hash(req), provider=self.provider, model=req.model, task=req.task,
                            prompt_sha=prompt_sha, request=self.request_record(req), response_text=resp.text,
                            stop_reason=resp.stop_reason, usage=usage_dict(resp.usage), cost=cost,
                            currency=price.currency, status=status, validated=validated)
        self.cost += cost
        return cost

    def fail_pending(self, row_id: int, error: str) -> None:
        """A pending row whose call raised (no response): closed as `error` so it is not unknown spend."""
        log.warning("call failed without a response: %s", error)
        self.store.finish_row(row_id, status="error")

    def unknown_pending(self) -> list[dict]:
        return self.store.pending_rows()


def usage_dict(u: Usage) -> dict:
    return {"input": u.input, "cache_write": u.cache_write, "cache_read": u.cache_read, "output": u.output,
            "model_reported": u.model_reported}


def report_unknown_pending(store: LlmStore) -> str | None:
    """Startup notice: leftover `pending` rows are spend the ledger could not confirm."""
    rows = store.pending_rows()
    if not rows:
        return None
    est = sum(float(r["est_cost"]) for r in rows)
    batches = sorted({r["batch_id"] for r in rows if r["batch_id"]})
    note = f"{len(rows)} pending ledger row(s) = unknown spend up to {est:.4f} {rows[0]['currency']}"
    if batches:
        note += f"; batches not collected: {', '.join(batches)}"
    return note
