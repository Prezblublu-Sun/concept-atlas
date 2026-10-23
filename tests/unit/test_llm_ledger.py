"""atlas.llm.ledger + atlas.store.llm: prices and cost arithmetic (cache tokens, batch multiplier), estimates,
the pending → ok row lifecycle, the exact-hash cache rule, unknown pending spend, scopes, unit costs."""
from __future__ import annotations

from pathlib import Path

import pytest

from atlas.config import init_project
from atlas.llm.base import ChatRequest, ChatResponse, FakeChatClient, Usage, request_hash
from atlas.llm.ledger import (RETRY_OVERHEAD, Ledger, Price, PriceTable, cost_of, estimate, new_run_id, price_table,
                              provider_multiplier, report_unknown_pending)
from atlas.store.db import user_version
from atlas.store.llm import LLM_SCHEMA_VERSION, LlmStore

OPUS = Price(input=4.0, output=20.0, cache_write=5.0, cache_read=0.20, currency="USD", batch_multiplier=0.5)


def req(**kw) -> ChatRequest:
    base = dict(model="claude-opus-5-5", system=("stable " * 50,), user="payload " * 20, max_tokens=1000,
                output_schema={"type": "object"}, effort="medium", task="terms_extract", key="k1")
    base.update(kw)
    return ChatRequest(**base)


@pytest.fixture
def store(tmp_path: Path):
    s = LlmStore.open(tmp_path / "llm" / "llm.sqlite")
    yield s
    s.close()


def ledger(store: LlmStore, **kw) -> Ledger:
    base = dict(provider="fake", currency="USD", run_id="run1", stage="terms", build_id=None)
    base.update(kw)
    return Ledger(store, PriceTable({"claude-opus-5-5": OPUS, "claude-haiku-5-5": Price(0.1, 0.5, 0.125, 0.01)}), **base)


def test_cost_arithmetic_with_cache_tokens_and_batch_multiplier():
    u = Usage(input=1000, cache_write=500, cache_read=2000, output=100)
    assert cost_of(u, OPUS) == pytest.approx((1000 * 4 + 500 * 5 + 2000 * 0.2 + 100 * 20) / 1e6)      # 0.0089
    assert cost_of(u, OPUS, batch=True) == pytest.approx(0.0089 * 0.5)
    assert cost_of(u, OPUS, multiplier=2.0) == pytest.approx(0.0178)
    assert cost_of(Usage(), OPUS) == 0.0


def test_price_table_from_config_and_reported_model(tmp_path: Path):
    project = init_project(tmp_path / "p")
    table = price_table(project)
    assert table.get("claude-opus-5-5") == OPUS
    assert table.get("claude-haiku-5-5").cache_write == 0.125 and table.get("deepseek-flash").currency == "RMB"
    assert table.price_for("claude-opus-5-5", "claude-haiku-5-5") == table.get("claude-haiku-5-5")   # a fallback answered
    assert table.price_for("claude-opus-5-5", "unknown-model") == OPUS
    with pytest.raises(KeyError, match="no price for model"):
        table.get("gpt-x")
    assert PriceTable.from_config({"m": {"input": 1, "output": 2}}).get("m") == Price(1, 2, 1, 1, "USD", 0.5)
    assert PriceTable.from_config({"junk": "x", "m": {"output": 1}}).prices == {}


def test_estimate_defaults_and_observations():
    r = req()
    e = estimate(r, OPUS)
    assert e.sys_tokens > 0 and e.in_tokens > e.sys_tokens and e.out_high == 1000 and e.out_low == 250
    assert not e.observed and e.cost_low < e.cost_high
    assert e.cost_high == pytest.approx((e.in_tokens * 4 + 1000 * 20) / 1e6 * (1 + RETRY_OVERHEAD))
    read = round(e.in_tokens * e.sys_tokens / e.in_tokens)
    assert e.cost_low == pytest.approx(((e.in_tokens - read) * 4 + read * 0.2 + 250 * 20) / 1e6 * (1 + RETRY_OVERHEAD))
    d = estimate(r, OPUS, unit_cost_default=0.002)
    assert d.cost_low == pytest.approx(0.002 * (1 + RETRY_OVERHEAD))
    big = estimate(r, OPUS, unit_cost_default=99)
    assert big.cost_low == big.cost_high                                       # never above the high estimate
    o = estimate(r, OPUS, observations={"n": 5, "cache_ratio": 0.5, "out_median": 120}, unit_cost_default=99)
    assert o.observed and o.out_low == 120
    half = round(o.in_tokens * 0.5)
    assert o.cost_low == pytest.approx(((o.in_tokens - half) * 4 + half * 0.2 + 120 * 20) / 1e6 * (1 + RETRY_OVERHEAD))
    b = estimate(r, OPUS, batch=True)
    assert b.cost_high == pytest.approx(e.cost_high * 0.5) and b.cost_low == pytest.approx(e.cost_low * 0.5)
    assert estimate(r, OPUS, multiplier=2.0).cost_high == pytest.approx(e.cost_high * 2)
    assert e.scaled(0.5).cost_high == pytest.approx(e.cost_high * 0.5)


def test_provider_multiplier_defaults_to_one():
    assert provider_multiplier(FakeChatClient()) == 1.0

    class Peak:
        def price_multiplier(self) -> float:
            return 2.0
    assert provider_multiplier(Peak()) == 2.0                                 # type: ignore[arg-type]


def test_store_schema_and_reopen(tmp_path: Path):
    p = tmp_path / "llm.sqlite"
    s = LlmStore.open(p)
    assert user_version(s.conn) == LLM_SCHEMA_VERSION
    names = {r[0] for r in s.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"calls", "ledger", "batches", "balance_probes", "unit_costs"} <= names
    s.close()
    s2 = LlmStore.open(p)
    assert s2.ledger_rows() == [] and s2.list_batches() == []
    s2.close()
    assert LlmStore.open(p, "ro").pending_rows() == []


def test_pending_row_then_ok_and_the_calls_row(store: LlmStore):
    led = ledger(store)
    r = req()
    row = led.begin(r, "k1", "sha-p", 1, est_cost=0.05)
    pend = store.ledger_rows()
    assert len(pend) == 1 and pend[0]["status"] == "pending" and pend[0]["est_cost"] == 0.05 and pend[0]["id"] == row
    assert pend[0]["request_hash"] == request_hash("fake", r) and pend[0]["batch_id"] is None
    assert store.get_call(pend[0]["request_hash"]) is None                    # a live call stores nothing until it ends
    assert report_unknown_pending(store).startswith("1 pending ledger row(s) = unknown spend up to 0.0500 USD")
    resp = ChatResponse(text='{"a": 1}', stop_reason="end_turn", latency_ms=321, raw_id="msg_1",
                        usage=Usage(input=1000, cache_write=500, cache_read=2000, output=100, model_reported="claude-opus-5-5"))
    cost = led.finish(row, r, resp, "ok", validated=True, prompt_sha="sha-p")
    assert cost == pytest.approx(0.0089) and led.cost == pytest.approx(0.0089)
    done = store.ledger_rows()[0]
    assert done["status"] == "ok" and done["in_tokens"] == 1000 and done["cache_write_tokens"] == 500
    assert done["cache_read_tokens"] == 2000 and done["out_tokens"] == 100 and done["cost"] == pytest.approx(0.0089)
    assert done["latency_ms"] == 321 and done["model_reported"] == "claude-opus-5-5" and done["currency"] == "USD"
    call = store.get_call(done["request_hash"])
    assert call["status"] == "ok" and call["validated"] and call["response_text"] == '{"a": 1}'
    assert call["usage"] == {"input": 1000, "cache_write": 500, "cache_read": 2000, "output": 100,
                             "model_reported": "claude-opus-5-5"}
    assert call["request"]["key"] == "k1" and call["request"]["provider"] == "fake" and call["prompt_sha"] == "sha-p"
    assert report_unknown_pending(store) is None
    # a fallback model that answered is priced at its own rate
    row2 = led.begin(r, "k2", "sha-p", 1, 0.01)
    resp2 = ChatResponse(text="{}", stop_reason="end_turn", usage=Usage(input=1000, output=100, model_reported="claude-haiku-5-5"))
    assert led.finish(row2, req(key="k2"), resp2, "ok", True, "sha-p") == pytest.approx((1000 * 0.1 + 100 * 0.5) / 1e6)


def test_batch_rows_are_priced_at_half_and_marked_cache_batch(store: LlmStore):
    led = ledger(store)
    r = req()
    row = led.begin(r, "k1", "sha", 1, 0.02, batch_id="msgbatch_1")
    pend = store.ledger_rows()[0]
    assert pend["batch_id"] == "msgbatch_1"
    held = store.get_call(pend["request_hash"])
    assert held["status"] == "pending" and held["request"]["user"] == r.user and not held["validated"]
    resp = ChatResponse(text="{}", stop_reason="end_turn", usage=Usage(input=1000, output=100))
    cost = led.finish(row, r, resp, "ok", True, "sha", batch=True)
    assert cost == pytest.approx((1000 * 4 + 100 * 20) / 1e6 * 0.5)
    assert store.ledger_rows()[0]["status"] == "cache_batch"
    assert store.get_call(pend["request_hash"])["status"] == "ok"
    row_t = led.begin(r, "k1", "sha", 2, 0.02, batch_id="msgbatch_1")
    led.finish(row_t, r, ChatResponse(text="", stop_reason="max_tokens"), "truncated", False, "sha", batch=True)
    assert store.ledger_rows()[1]["status"] == "truncated"


def test_lookup_hits_only_ok_and_validated(store: LlmStore):
    led = ledger(store)
    r = req()
    h = led.hash(r)
    row = led.begin(r, "k", "sha", 1, 0.01)
    led.finish(row, r, ChatResponse(text="junk", stop_reason="end_turn"), "malformed", validated=False, prompt_sha="sha")
    assert led.lookup(r) is None and store.get_call(h)["status"] == "malformed"
    row2 = led.begin(r, "k", "sha", 2, 0.01)
    led.finish(row2, r, ChatResponse(text="{}", stop_reason="end_turn"), "ok", validated=True, prompt_sha="sha")
    assert led.lookup(r)["response_text"] == "{}"
    # a later worse result never overwrites the valid one (batch collected after a live answer)
    assert not store.put_call(request_hash=h, provider="fake", model="m", task="t", prompt_sha="s", request={},
                              status="error")
    assert led.lookup(r)["response_text"] == "{}"
    assert store.put_call(request_hash=h, provider="fake", model="m", task="t", prompt_sha="s", request={},
                          status="error", keep_valid=False)
    assert led.lookup(r) is None
    with pytest.raises(ValueError):
        store.put_call(request_hash="x", provider="fake", model="m", task="t", prompt_sha="s", request={}, status="weird")


def test_cache_hit_budget_stop_and_failed_pending_rows(store: LlmStore):
    led = ledger(store, build_id="b2026-10-23_001")
    r = req()
    led.cache_hit(r, "k", "sha", attempt=1)
    led.budget_stop(r, "k2", "sha", est_cost=0.7, attempt=1)
    row = led.begin(r, "k3", "sha", 1, 0.1)
    led.fail_pending(row, "NetworkForbidden: guard")
    led.for_failover().cache_hit(r, "k4", "sha")
    rows = store.ledger_rows()
    assert [x["status"] for x in rows] == ["cache_local", "budget_stop", "error", "cache_local"]
    assert rows[0]["cost"] == 0 and rows[0]["build_id"] == "b2026-10-23_001" and rows[1]["est_cost"] == 0.7
    assert rows[2]["cost"] == 0 and rows[2]["in_tokens"] == 0 and store.pending_rows() == []
    assert [x["failover"] for x in rows] == [0, 0, 0, 1] and all(x["provider"] == "fake" for x in rows)
    assert not led.failover and led.for_failover().failover and led.for_failover().provider == "fake"
    with pytest.raises(ValueError):
        store.finish_row(row, status="pending")
    with pytest.raises(ValueError):
        store.add_row(run_id="r", build_id=None, stage="s", item_key="k", task="t", prompt_sha="p", request_hash="h",
                      attempt=1, status="pending", currency="USD")


def test_spent_scopes_and_totals(store: LlmStore):
    a, b = ledger(store, run_id="r1", stage="terms"), ledger(store, run_id="r2", stage="classify")
    r = req()
    row = a.begin(r, "k1", "sha", 1, 0.5)
    a.finish(row, r, ChatResponse(text="{}", stop_reason="end_turn", usage=Usage(input=1_000_000, cache_read=1_000_000, output=0)),
             "ok", True, "sha")                                                 # 4 + 0.2
    a.cache_hit(r, "k1", "sha")
    b.begin(req(key="k2"), "k2", "sha", 1, 0.25)                               # stays pending
    row3 = b.begin(req(key="k3"), "k3", "sha", 1, 0.1)
    b.finish(row3, req(key="k3"), ChatResponse(text="", stop_reason="refusal", usage=Usage(input=500_000, output=0)), "refused", False, "sha")  # 2
    assert store.spent(run_id="r1") == {"cost": pytest.approx(4.2), "pending_est": 0.0, "rows": 2,
                                        "by_currency": {"USD": {"cost": pytest.approx(4.2), "pending_est": 0.0}}}
    assert store.spent(stage="classify") == {"cost": pytest.approx(2.0), "pending_est": 0.25, "rows": 2,
                                             "by_currency": {"USD": {"cost": pytest.approx(2.0), "pending_est": 0.25}}}
    assert store.spent()["cost"] == pytest.approx(6.2) and store.spent()["pending_est"] == 0.25 and store.spent()["rows"] == 4
    assert store.spent(since="2999-01-01T00:00:00+00:00") == {"cost": 0.0, "pending_est": 0.0, "rows": 0, "by_currency": {}}
    t = store.totals()
    assert t["total"]["rows"] == 4 and t["total"]["cost"] == pytest.approx(6.2) and t["total"]["pending"] == 1
    assert t["total"]["pending_est"] == 0.25 and t["total"]["currency"] == "USD"
    assert t["total"]["in_tokens"] == 1_500_000 and t["total"]["cache_read_tokens"] == 1_000_000
    by = {(x["stage"], x["task"]): x for x in t["rows"]}
    assert by[("terms", "terms_extract")]["by_status"] == {"ok": 1, "cache_local": 1}
    assert by[("classify", "terms_extract")]["by_status"] == {"pending": 1, "refused": 1}
    assert all(x["provider"] == "fake" and x["currency"] == "USD" and x["failover"] == 0 for x in t["rows"])
    assert t["by_provider"] == [{"provider": "fake", "rows": 4, "in_tokens": 1_500_000, "cache_write_tokens": 0,
                                 "cache_read_tokens": 1_000_000, "out_tokens": 0, "cost": pytest.approx(6.2),
                                 "failover": 0, "currency": "USD"}]
    assert t["cache_local_rate"] == pytest.approx(1 / 3)                       # the pending row is not answered yet
    assert t["cache_read_ratio"] == pytest.approx(1_000_000 / 2_500_000)
    only = store.totals(run_id="r2", stage="classify")
    assert only["total"]["rows"] == 2 and only["cache_local_rate"] == 0.0
    assert store.totals(run_id="none")["total"] == {"rows": 0, "in_tokens": 0, "cache_write_tokens": 0, "cache_read_tokens": 0,
                                                    "out_tokens": 0, "cost": 0.0, "pending": 0, "pending_est": 0.0, "currency": None}
    assert store.totals(run_id="none")["by_provider"] == []
    # currency conversion of the scopes and per-provider observations
    ds = ledger(store, provider="deepseek", currency="RMB", run_id="r1", stage="terms", failover=True)
    row = ds.begin(req(model="claude-haiku-5-5", key="d1"), "d1", "sha", 1, 0.1)
    ds.finish(row, req(model="claude-haiku-5-5", key="d1"), ChatResponse(text="{}", stop_reason="end_turn",
                                                                           usage=Usage(input=1_000_000, output=0)), "ok", True, "sha")   # 0.1 RMB
    conv = store.spent(run_id="r1", rates={"USD": 1.0, "RMB": 0.5})
    assert conv["cost"] == pytest.approx(4.2 + 0.05) and conv["by_currency"]["RMB"]["cost"] == pytest.approx(0.1)
    assert store.spent(run_id="r1")["cost"] == pytest.approx(4.3)                 # no rates: 1:1
    drow = store.ledger_rows(run_id="r1")[-1]
    assert drow["provider"] == "deepseek" and drow["failover"] == 1 and drow["currency"] == "RMB"
    assert store.task_observations("terms_extract", "sha", "deepseek")["n"] == 1
    assert store.task_observations("terms_extract", "sha", "fake")["n"] == 1 and store.task_observations("terms_extract", "sha")["n"] == 2
    assert [p["provider"] for p in store.totals()["by_provider"]] == ["deepseek", "fake"]
    assert store.totals()["by_provider"][0]["failover"] == 1


def test_observations_and_unit_cost_calibration(store: LlmStore):
    led = ledger(store)
    r = req()
    for i, (inp, read, out) in enumerate([(1000, 0, 100), (200, 800, 300), (500, 500, 200)]):
        row = led.begin(req(key=f"k{i}"), f"k{i}", "sha", 1, 0.01)
        led.finish(row, req(key=f"k{i}"), ChatResponse(text="{}", stop_reason="end_turn",
                                                       usage=Usage(input=inp, cache_read=read, output=out)), "ok", True, "sha")
    led.cache_hit(r, "k9", "sha")                                              # zero-token rows are not observations
    obs = store.task_observations("terms_extract", "sha")
    assert obs["n"] == 3 and obs["cache_ratio"] == pytest.approx(1300 / 3000) and obs["out_median"] == 200
    assert obs["in_median"] == 1000 and obs["cost_median"] > 0
    assert store.task_observations("terms_extract", "other")["n"] == 0
    assert store.unit_cost("terms_extract", "sha") is None
    rows = store.calibrate()
    assert len(rows) == 1 and rows[0]["n"] == 3 and rows[0]["unit"] == "item"
    assert rows[0]["cost_median"] == pytest.approx(obs["cost_median"])
    assert store.unit_cost("terms_extract", "sha")["n"] == 3 and len(store.unit_costs()) == 1
    store.add_unit_costs([{"task": "classify", "prompt_sha": "x", "cost_median": 0.002, "n": 10}])
    assert store.unit_cost("classify", "x")["cost_median"] == 0.002


def test_batches_and_balance_probes(store: LlmStore):
    store.add_batch(batch_id="msgbatch_1", provider="anthropic", model="claude-opus-5-5", status="in_progress",
                    n_requests=3, run_id="r1", stage="terms")
    assert store.get_batch("msgbatch_1")["n_requests"] == 3 and store.get_batch("nope") is None
    store.update_batch("msgbatch_1", status="ended", n_done=2, n_failed=1, cost=0.5)
    b = store.list_batches()[0]
    assert (b["status"], b["n_done"], b["n_failed"], b["cost"], b["stage"]) == ("ended", 2, 1, 0.5, "terms")
    assert store.last_balance("deepseek") is None
    store.add_balance_probe("deepseek", 12.5, "RMB")
    assert store.last_balance("deepseek")["balance"] == 12.5


def test_new_run_id_shape():
    rid = new_run_id("terms")
    assert rid.startswith("terms_") and len(rid) == len("terms_") + 15 and "T" in rid
