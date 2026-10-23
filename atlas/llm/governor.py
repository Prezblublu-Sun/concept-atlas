"""Governor (increment 4 §1.5): budget scopes, reservations, `BudgetExceeded`, the dry-run plan.

Scopes, all in the primary provider's currency (rows of another currency convert through `rates` = `llm.fx`):
`run` (`--budget`, default `budgets.run`), `stage` (`budgets.stage.<name>`), `daily` (`budgets.daily.total`, the
project's spend since local midnight). A scope with budget None is unlimited. Submitting a call reserves its
HIGH estimate: it is allowed when, for every scope, `budget − spent − pending − reserved ≥ estimate`, where
`spent` is the ledger's real cost in the scope, `pending` the estimates of ledger rows still pending (batches not
collected, calls in flight elsewhere) and `reserved` the sum of this process's open reservations. Completion
releases the reservation (the real cost is in the ledger by then). `BudgetExceeded` names the scope and the
numbers; the stage stops cleanly — completed items stay valid.

The plan (`Plan`, `format_plan`) is what `--dry-run` prints: per item the key, its provider, whether the exact
request hash is already in `calls` (zero cost), the local token estimate and the low / high cost in the row's
currency; totals per provider and overall (converted) for live and batch pricing. `prefer_idle` applies only to
providers with peak pricing (DeepSeek): `idle_notice` is a warning, not a stop.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from atlas.config import Project
from atlas.llm.base import ChatClient
from atlas.llm.ledger import RETRY_OVERHEAD
from atlas.store.llm import LlmStore

log = logging.getLogger(__name__)

SCOPES = ("run", "stage", "daily")


class BudgetExceeded(Exception):
    def __init__(self, scope: str, budget: float, spent: float, pending: float, reserved: float, estimate: float,
                 currency: str) -> None:
        self.scope, self.budget, self.spent, self.pending = scope, budget, spent, pending
        self.reserved, self.estimate, self.currency = reserved, estimate, currency
        super().__init__(f"{scope} budget {budget:.2f} {currency} would be exceeded: spent {spent:.4f}, pending "
                         f"{pending:.4f}, reserved {reserved:.4f}, next call up to {estimate:.4f} {currency}")


@dataclass
class Reservation:
    amount: float
    released: bool = False


def local_midnight_utc() -> str:
    """ISO-8601 UTC timestamp of today's local midnight (the ledger's `ts` is UTC)."""
    today = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    return today.astimezone(timezone.utc).isoformat(timespec="seconds")


class Governor:
    def __init__(self, store: LlmStore, currency: str, *, run_id: str, stage: str, run_budget: float | None,
                 stage_budget: float | None, daily_budget: float | None, day_start: str | None = None,
                 rates: dict[str, float] | None = None) -> None:
        self.store = store
        self.currency = currency
        self.run_id = run_id
        self.stage = stage
        self.budgets: dict[str, float | None] = {"run": run_budget, "stage": stage_budget, "daily": daily_budget}
        self.day_start = day_start or local_midnight_utc()
        self.rates = dict(rates or {})
        self.reserved = 0.0

    def to_currency(self, amount: float, currency: str) -> float:
        """An amount of another provider's currency in the governor's currency (1:1 without a rate)."""
        return amount * float(self.rates.get(currency, 1.0))

    def spent(self, scope: str) -> dict:
        if scope == "run":
            return self.store.spent(run_id=self.run_id, rates=self.rates)
        if scope == "stage":
            return self.store.spent(stage=self.stage, rates=self.rates)
        if scope == "daily":
            return self.store.spent(since=self.day_start, rates=self.rates)
        raise KeyError(scope)

    def headroom(self) -> dict[str, float | None]:
        """{scope: budget − spent − pending − reserved} (None = unlimited)."""
        out: dict[str, float | None] = {}
        for scope in SCOPES:
            b = self.budgets[scope]
            if b is None:
                out[scope] = None
                continue
            s = self.spent(scope)
            out[scope] = float(b) - s["cost"] - s["pending_est"] - self.reserved
        return out

    def reserve(self, amount: float) -> Reservation:
        for scope in SCOPES:
            b = self.budgets[scope]
            if b is None:
                continue
            s = self.spent(scope)
            if float(b) - s["cost"] - s["pending_est"] - self.reserved < amount:
                raise BudgetExceeded(scope, float(b), s["cost"], s["pending_est"], self.reserved, amount, self.currency)
        self.reserved += amount
        return Reservation(amount)

    def release(self, res: Reservation | None) -> None:
        if res is None or res.released:
            return
        res.released = True
        self.reserved = max(0.0, self.reserved - res.amount)


def budgets_for(project: Project, stage: str, run_budget: float | None = None) -> dict[str, float | None]:
    """The three scope budgets from `budgets:` (a run budget given on the command line wins)."""
    def _num(v) -> float | None:
        return None if v is None else float(v)
    return {"run": _num(run_budget if run_budget is not None else project.get("budgets.run")),
            "stage": _num(project.get(f"budgets.stage.{stage}")),
            "daily": _num(project.get("budgets.daily.total"))}


def governor_for(project: Project, store: LlmStore, currency: str, *, run_id: str, stage: str,
                 budget: float | None = None, rates: dict[str, float] | None = None) -> Governor:
    b = budgets_for(project, stage, budget)
    return Governor(store, currency, run_id=run_id, stage=stage, run_budget=b["run"], stage_budget=b["stage"],
                    daily_budget=b["daily"], rates=rates)


def idle_notice(client: ChatClient, prefer_idle: bool) -> str | None:
    """A warning when `prefer_idle` is set and the provider is inside its peak-price window (DeepSeek only)."""
    is_peak = getattr(client, "is_peak", None)
    if prefer_idle and callable(is_peak) and is_peak():
        return (f"{client.provider}: peak hours (prices × {getattr(client, 'peak_multiplier', 2)}); "
                f"llm.prefer_idle is set — consider running outside UTC+8 weekdays 09–12 / 14–18")
    return None


# ---- dry-run plan ---------------------------------------------------------------------------------------------------
@dataclass
class PlanRow:
    key: str
    task: str
    provider: str
    cached: bool
    in_tokens: int
    out_low: int
    out_high: int
    cost_low: float          # live price in `currency`; 0 when cached
    cost_high: float
    currency: str = "USD"
    batch_multiplier: float = 0.5   # 1.0 for a provider without a batch API (its share runs live either way)
    observed: bool = False
    note: str = ""


@dataclass
class Plan:
    stage: str
    models: dict[str, str]                       # provider → model
    currency: str                                # the primary provider's (totals are converted into it)
    rates: dict[str, float] = field(default_factory=dict)
    rows: list[PlanRow] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def cached(self) -> int:
        return sum(1 for r in self.rows if r.cached)

    def _rate(self, currency: str) -> float:
        return float(self.rates.get(currency, 1.0))

    def totals(self, batch: bool = False, provider: str | None = None) -> dict:
        """Sums over the uncached rows (of one provider when given), costs converted into `currency`; `batch`
        applies each row's batch multiplier."""
        rows = [r for r in self.rows if provider is None or r.provider == provider]
        live = [r for r in rows if not r.cached]
        m = (lambda r: r.batch_multiplier) if batch else (lambda r: 1.0)
        return {"items": len(rows), "calls": len(live), "cached": len(rows) - len(live),
                "in_tokens": sum(r.in_tokens for r in live), "out_low": sum(r.out_low for r in live),
                "out_high": sum(r.out_high for r in live),
                "cost_low": sum(r.cost_low * m(r) * self._rate(r.currency) for r in live),
                "cost_high": sum(r.cost_high * m(r) * self._rate(r.currency) for r in live), "currency": self.currency}

    @property
    def providers(self) -> list[str]:
        return sorted({r.provider for r in self.rows})


def format_plan(plan: Plan, limit: int = 30) -> str:
    """The dry-run text: a header, up to `limit` item rows, totals per provider and overall for live and batch
    pricing, notes."""
    cur = plan.currency
    models = ", ".join(f"{p} {m}" for p, m in sorted(plan.models.items()))
    lines = [f"[{plan.stage}] dry run: {len(plan.rows)} items, {plan.cached} cached (exact hash), models {models}",
             "key\ttask\tprovider\tcached\tin_tok\tout_low\tout_high\tcost_low\tcost_high\tcurrency"]
    for r in plan.rows[:limit]:
        lines.append(f"{r.key}\t{r.task}\t{r.provider}\t{'yes' if r.cached else ''}\t{r.in_tokens}\t{r.out_low}\t"
                     f"{r.out_high}\t{r.cost_low:.4f}\t{r.cost_high:.4f}\t{r.currency}" + (f"\t{r.note}" if r.note else ""))
    if len(plan.rows) > limit:
        lines.append(f"… {len(plan.rows) - limit} more items")
    for label, batch in (("live", False), ("batch", True)):
        for p in plan.providers if len(plan.providers) > 1 else []:
            t = plan.totals(batch, p)
            lines.append(f"{label} {p}: {t['calls']} calls, in {t['in_tokens']} tokens, out {t['out_low']}–{t['out_high']} "
                         f"tokens, cost {t['cost_low']:.4f}–{t['cost_high']:.4f} {cur}")
        t = plan.totals(batch)
        lines.append(f"{label}: {t['calls']} calls, in {t['in_tokens']} tokens, out {t['out_low']}–{t['out_high']} "
                     f"tokens, cost {t['cost_low']:.4f}–{t['cost_high']:.4f} {cur}")
    observed = sum(1 for r in plan.rows if r.observed and not r.cached)
    lines.append(f"estimates: +{RETRY_OVERHEAD:.0%} retry overhead; low from ledger observations for {observed} items, "
                 f"defaults for the rest; no provider call is made by a dry run")
    lines += [f"note: {n}" for n in plan.notes]
    return "\n".join(lines)
