"""atlas.llm.governor: reservations across the run / stage / daily scopes, BudgetExceeded (and the budget_stop
ledger row the runner writes), configuration, the dry-run plan text, the idle notice."""
from __future__ import annotations

from pathlib import Path

import pytest

from atlas.config import init_project
from atlas.llm.base import FakeChatClient
from atlas.llm.governor import (BudgetExceeded, Governor, Plan, PlanRow, budgets_for, format_plan, governor_for,
                                idle_notice)
from atlas.llm.tasks import StructuredTask, make_runner, plan_items
from atlas.store.llm import LlmStore

SCHEMA = {"type": "object", "properties": {"terms": {"type": "array"}}, "required": ["terms"]}


@pytest.fixture
def store(tmp_path: Path):
    s = LlmStore.open(tmp_path / "llm.sqlite")
    yield s
    s.close()


@pytest.fixture
def project(tmp_path: Path):
    return init_project(tmp_path / "proj")


@pytest.fixture
def task(tmp_path: Path) -> StructuredTask:
    p = tmp_path / "t.md"
    p.write_text("rules\n---payload---\n{sentences}\n", encoding="utf-8")
    return StructuredTask("terms_extract", p, SCHEMA, limits={"terms": 25}, max_tokens=2000)


def seed(store: LlmStore, **kw) -> None:
    base = dict(run_id="r1", build_id=None, stage="terms", item_key="k", task="t", prompt_sha="p", request_hash="h",
                attempt=1, currency="USD")
    base.update(kw)
    if base.pop("status") == "pending":
        base.pop("cost", None)
        store.add_pending(**base)
    else:
        store.add_row(status=kw["status"], **base)


def test_reserve_checks_every_scope_and_release_frees(store: LlmStore):
    g = Governor(store, "USD", run_id="r1", stage="terms", run_budget=1.0, stage_budget=0.5, daily_budget=None)
    assert g.headroom() == {"run": 1.0, "stage": 0.5, "daily": None}
    res = g.reserve(0.4)
    assert g.reserved == 0.4 and g.headroom()["stage"] == pytest.approx(0.1)
    with pytest.raises(BudgetExceeded) as e:
        g.reserve(0.2)
    assert e.value.scope == "stage" and e.value.budget == 0.5 and e.value.reserved == 0.4 and e.value.estimate == 0.2
    assert "stage budget 0.50 USD would be exceeded" in str(e.value) and "next call up to 0.2000 USD" in str(e.value)
    g.release(res)
    g.release(res)                                                             # idempotent
    assert g.reserved == 0.0
    g.reserve(0.5)
    with pytest.raises(BudgetExceeded) as e2:
        g.reserve(0.01)
    assert e2.value.scope == "stage"
    g2 = Governor(store, "USD", run_id="r1", stage="terms", run_budget=0.3, stage_budget=None, daily_budget=None)
    with pytest.raises(BudgetExceeded) as e3:
        g2.reserve(0.31)
    assert e3.value.scope == "run"


def test_spent_and_pending_rows_count_against_the_scopes(store: LlmStore):
    seed(store, status="ok", cost=0.3)                                        # run r1, stage terms
    seed(store, status="pending", est_cost=0.2, run_id="r2")                  # another run, same stage, pending
    seed(store, status="ok", cost=1.0, run_id="r3", stage="classify")
    seed(store, status="cache_local", cost=0.0)
    seed(store, status="budget_stop", est_cost=5.0)                           # never spend
    g = Governor(store, "USD", run_id="r1", stage="terms", run_budget=1.0, stage_budget=1.0, daily_budget=2.0,
                 day_start="2000-01-01T00:00:00+00:00")
    run, stage = g.spent("run"), g.spent("stage")
    assert (run["cost"], run["pending_est"], run["rows"]) == (pytest.approx(0.3), 0.0, 3)
    assert (stage["cost"], stage["pending_est"], stage["rows"]) == (pytest.approx(0.3), pytest.approx(0.2), 4)
    assert stage["by_currency"] == {"USD": {"cost": pytest.approx(0.3), "pending_est": pytest.approx(0.2)}}
    assert g.spent("daily")["cost"] == pytest.approx(1.3) and g.spent("daily")["pending_est"] == pytest.approx(0.2)
    h = g.headroom()
    assert h["run"] == pytest.approx(0.7) and h["stage"] == pytest.approx(0.5) and h["daily"] == pytest.approx(0.5)
    with pytest.raises(BudgetExceeded) as e:
        g.reserve(0.55)
    assert e.value.scope == "stage" and e.value.pending == pytest.approx(0.2)
    g.reserve(0.49)
    with pytest.raises(BudgetExceeded) as e2:
        g.reserve(0.02)
    assert e2.value.scope == "stage"
    future = Governor(store, "USD", run_id="r1", stage="terms", run_budget=None, stage_budget=None, daily_budget=0.1,
                      day_start="2999-01-01T00:00:00+00:00")
    assert future.headroom()["daily"] == pytest.approx(0.1)                   # nothing spent "today"
    with pytest.raises(KeyError):
        g.spent("week")


def test_budgets_from_configuration(project):
    assert budgets_for(project, "terms") == {"run": 80.0, "stage": 40.0, "daily": 150.0}
    assert budgets_for(project, "unknown_stage", run_budget=2.5) == {"run": 2.5, "stage": None, "daily": 150.0}
    store = LlmStore.open(project.llm_db)
    g = governor_for(project, store, "USD", run_id="x", stage="classify", budget=7)
    assert g.budgets == {"run": 7.0, "stage": 30.0, "daily": 150.0} and g.currency == "USD" and g.day_start.endswith("+00:00")
    store.close()


def test_runner_stops_cleanly_with_a_budget_stop_row(project, task):
    fake = FakeChatClient({"terms_extract": '{"terms": []}'})
    runner = make_runner(project, "terms", client=fake, mode="live", budget=0.05)
    ok = runner.run(task, {"sentences": ["a"]}, "k1")
    assert ok.status == "ok"
    runner.governor.budgets["run"] = ok.cost + 0.001                         # the next high estimate will not fit
    with pytest.raises(BudgetExceeded) as e:
        runner.run(task, {"sentences": ["b"]}, "k2")
    assert e.value.scope == "run" and e.value.spent == pytest.approx(ok.cost)
    rows = runner.store.ledger_rows()
    assert [r["status"] for r in rows] == ["ok", "budget_stop"] and rows[1]["item_key"] == "k2" and rows[1]["cost"] == 0
    assert rows[1]["est_cost"] == pytest.approx(e.value.estimate) and len(fake.requests) == 1
    assert runner.governor.reserved == 0.0
    assert runner.run(task, {"sentences": ["a"]}, "k1").status == "cache_local"   # completed items stay valid
    runner.close()


def test_dry_run_plan_text_and_totals(project, task):
    fake = FakeChatClient({"terms_extract": '{"terms": []}'})
    runner = make_runner(project, "terms", client=fake, mode="live", governed=False)
    runner.run(task, {"sentences": ["cached one"]}, "c")
    plan = plan_items(runner, [(task, {"sentences": ["cached one"]}, "c"),
                              (task, {"sentences": ["new one"]}, "n1"),
                              (task, {"sentences": ["new two"]}, "n2", ("scheme",))])
    assert isinstance(plan, Plan) and plan.stage == "terms" and plan.models == {"fake": "claude-opus-5-5"} and plan.cached == 1
    assert plan.rows[0].cached and plan.rows[0].cost_high == 0 and plan.rows[0].cost_low == 0
    assert all(isinstance(r, PlanRow) and r.provider == "fake" and r.currency == "USD" for r in plan.rows)
    assert not plan.rows[1].cached and plan.rows[1].batch_multiplier == 0.5
    assert plan.rows[2].in_tokens > plan.rows[1].in_tokens                    # the context block counts
    assert plan.rows[1].out_high == 2000 and plan.rows[1].out_low == 500 and plan.rows[1].cost_low <= plan.rows[1].cost_high
    live, batch = plan.totals(), plan.totals(batch=True)
    assert live["items"] == 3 and live["calls"] == 2 and live["cached"] == 1 and live["currency"] == "USD"
    assert batch["cost_high"] == pytest.approx(live["cost_high"] * 0.5) and batch["cost_low"] == pytest.approx(live["cost_low"] * 0.5)
    assert live["cost_high"] == pytest.approx(plan.rows[1].cost_high + plan.rows[2].cost_high)
    assert plan.totals(provider="fake") == live and plan.totals(provider="other")["items"] == 0 and plan.providers == ["fake"]
    text = format_plan(plan)
    assert text.startswith("[terms] dry run: 3 items, 1 cached (exact hash), models fake claude-opus-5-5")
    assert "key\ttask\tprovider\tcached\tin_tok\tout_low\tout_high\tcost_low\tcost_high\tcurrency" in text
    assert "c\tterms_extract\tfake\tyes\t" in text and "n1\tterms_extract\tfake\t\t" in text
    assert f"live: 2 calls, in {live['in_tokens']} tokens" in text and "batch: 2 calls" in text
    assert f"{live['cost_low']:.4f}–{live['cost_high']:.4f} USD" in text and "+6% retry overhead" in text
    assert "no provider call is made by a dry run" in text and "live fake:" not in text   # one provider: no per-provider lines
    assert len(fake.requests) == 1                                             # the plan called nothing
    short = format_plan(plan, limit=1)
    assert "… 2 more items" in short
    # ledger observations lower the low estimate once calls were observed for this prompt
    obs_runner = make_runner(project, "terms", client=fake, mode="live", governed=False, store=runner.store)
    row = obs_runner.plan_row(task, {"sentences": ["new three"]}, "n3")
    assert row.observed and row.out_low < 500 and row.cost_low < plan.rows[1].cost_low
    runner.close()


def test_plan_with_two_providers_converts_currencies_and_lists_each(project, task):
    a = FakeChatClient({"terms_extract": '{"terms": []}'})
    a.provider = "anthropic"
    d = FakeChatClient({"terms_extract": '{"terms": []}'})
    d.provider, d.currency, d.batch_api = "deepseek", "RMB", False
    runner = make_runner(project, "terms", clients={"anthropic": a, "deepseek": d}, mode="batch", governed=False)
    plan = plan_items(runner, [(task, {"sentences": [f"s{i}"]}, f"k{i}") for i in range(60)])
    assert plan.providers == ["anthropic", "deepseek"] and plan.currency == "USD" and plan.rates["RMB"] == pytest.approx(0.14)
    ds = [r for r in plan.rows if r.provider == "deepseek"]
    assert ds and all(r.currency == "RMB" and r.batch_multiplier == 1.0 for r in ds)
    assert sum(1 for r in plan.rows if r.provider == "anthropic") > len(ds)
    t_ds, t_a = plan.totals(provider="deepseek"), plan.totals(provider="anthropic")
    assert t_ds["cost_high"] == pytest.approx(sum(r.cost_high for r in ds) * 0.14)
    assert plan.totals()["cost_high"] == pytest.approx(t_ds["cost_high"] + t_a["cost_high"])
    assert plan.totals(batch=True, provider="deepseek")["cost_high"] == pytest.approx(t_ds["cost_high"])   # no batch API
    assert plan.totals(batch=True, provider="anthropic")["cost_high"] == pytest.approx(t_a["cost_high"] * 0.5)
    text = format_plan(plan, limit=5)
    assert "models anthropic claude-opus-5-5, deepseek deepseek-flash" in text
    assert "live anthropic:" in text and "live deepseek:" in text and "batch deepseek:" in text
    assert "note: provider split: anthropic 90%, deepseek 10%" in text
    assert a.requests == [] and d.requests == []
    runner.close()


def test_governor_converts_other_currencies_through_rates(store: LlmStore):
    seed(store, status="ok", cost=10.0, currency="RMB", provider="deepseek")
    seed(store, status="ok", cost=1.0, currency="USD", provider="anthropic")
    g = Governor(store, "USD", run_id="r1", stage="terms", run_budget=3.0, stage_budget=None, daily_budget=None,
                 rates={"USD": 1.0, "RMB": 0.14})
    s = g.spent("run")
    assert s["cost"] == pytest.approx(2.4) and s["by_currency"] == {"RMB": {"cost": 10.0, "pending_est": 0.0},
                                                                     "USD": {"cost": 1.0, "pending_est": 0.0}}
    assert g.headroom()["run"] == pytest.approx(0.6) and g.to_currency(10.0, "RMB") == pytest.approx(1.4)
    assert Governor(store, "USD", run_id="r1", stage="terms", run_budget=3.0, stage_budget=None,
                    daily_budget=None).spent("run")["cost"] == pytest.approx(11.0)        # no rates: 1:1


def test_idle_notice_only_for_peak_priced_providers():
    assert idle_notice(FakeChatClient(), True) is None

    class Peak:
        provider = "deepseek"
        currency = "RMB"
        peak_multiplier = 2

        def is_peak(self) -> bool:
            return True

    class Idle(Peak):
        def is_peak(self) -> bool:
            return False
    assert "peak hours (prices × 2)" in idle_notice(Peak(), True)             # type: ignore[arg-type]
    assert idle_notice(Peak(), False) is None and idle_notice(Idle(), True) is None   # type: ignore[arg-type]
