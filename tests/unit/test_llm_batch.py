"""atlas.llm.batch: submit / collect idempotence, de-duplication by hash, half-price cache_batch rows, failure
classification on collect, custom-id mapping, chunking, polling, the budget check before submit."""
from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from atlas.config import init_project
from atlas.llm import batch as B
from atlas.llm.base import BatchStatus, ChatRequest, ChatResponse, FakeChatClient, request_hash
from atlas.llm.client import custom_id_for
from atlas.llm.governor import BudgetExceeded
from atlas.llm.ledger import Price, estimate, price_table
from atlas.llm.tasks import Queued, StructuredTask, make_runner

SCHEMA = {"type": "object", "properties": {"terms": {"type": "array"}}, "required": ["terms"]}
OK = json.dumps({"terms": ["t"]})


@pytest.fixture
def project(tmp_path: Path):
    return init_project(tmp_path / "proj")


@pytest.fixture
def task(tmp_path: Path) -> StructuredTask:
    p = tmp_path / "t.md"
    p.write_text("rules\n---payload---\n{sentences}\n", encoding="utf-8")
    return StructuredTask("terms_extract", p, SCHEMA, max_tokens=500)


class CustomIdFake(FakeChatClient):
    """Keys results by custom id, as the Anthropic client does."""

    def submit_batch(self, reqs: Sequence[ChatRequest]) -> str:
        bid = super().submit_batch(reqs)
        self.batches[bid] = {custom_id_for(k): v for k, v in self.batches[bid].items()}
        return bid


class SlowFake(FakeChatClient):
    """Reports in_progress for the first `n_wait` status probes."""

    def __init__(self, *a, n_wait: int = 2, **kw) -> None:
        super().__init__(*a, **kw)
        self.n_wait = n_wait
        self.probes = 0

    def batch_status(self, batch_id: str) -> BatchStatus:
        self.probes += 1
        if self.probes <= self.n_wait:
            return BatchStatus(batch_id, "in_progress", n_requests=len(self.batches.get(batch_id, {})))
        return super().batch_status(batch_id)


def test_submit_and_collect_are_idempotent_and_half_price(project, task):
    fake = FakeChatClient({"terms_extract": OK})
    runner = make_runner(project, "terms", client=fake, mode="batch", governed=False)
    for key, text in (("a", "one"), ("b", "two"), ("b2", "two")):              # b and b2 share the request hash
        assert runner.run(task, {"sentences": [text]}, key).status == "queued"
    ids = B.submit_queued(runner)
    assert ids == ["fakebatch_0001"] and runner.queued == [] and B.submit_queued(runner) == []
    store = runner.store
    assert store.get_batch(ids[0])["n_requests"] == 2 and store.get_batch(ids[0])["stage"] == "terms"
    pend = store.ledger_rows(batch_id=ids[0])
    assert [r["status"] for r in pend] == ["pending"] * 3 and len(fake.requests) == 2
    assert store.get_call(pend[1]["request_hash"])["status"] == "pending"
    assert store.spent(stage="terms")["pending_est"] == pytest.approx(sum(r["est_cost"] for r in pend))
    r = B.collect(store, fake, ids[0], price_table(project))
    assert r["state"] == "ended" and r["pending_before"] == 3 and r["collected"] == 3 and r["failed"] == 0 and r["missing"] == 0
    rows = store.ledger_rows(batch_id=ids[0])
    assert [x["status"] for x in rows] == ["cache_batch"] * 3 and all(x["cost"] > 0 for x in rows)
    live_cost = (rows[0]["in_tokens"] * 4 + rows[0]["out_tokens"] * 20) / 1e6
    assert rows[0]["cost"] == pytest.approx(live_cost * 0.5)
    assert rows[1]["request_hash"] == rows[2]["request_hash"] and rows[1]["cost"] == rows[2]["cost"]
    b = store.get_batch(ids[0])
    assert b["status"] == "collected" and b["n_done"] == 3 and b["n_failed"] == 0 and b["cost"] == pytest.approx(sum(x["cost"] for x in rows))
    again = B.collect(store, fake, ids[0], price_table(project))
    assert again["collected"] == 0 and again["pending_before"] == 0 and store.get_batch(ids[0])["status"] == "collected"
    assert len(store.ledger_rows()) == 3
    # the next stage run finds every answer in calls → cache_local, no new request
    live = make_runner(project, "terms", client=fake, mode="live", governed=False, store=store)
    for key, text in (("a", "one"), ("b", "two"), ("b2", "two")):
        res = live.run(task, {"sentences": [text]}, key)
        assert res.status == "cache_local" and res.data == {"terms": ["t"]}
    assert len(fake.requests) == 2 and store.spent()["pending_est"] == 0.0
    with pytest.raises(KeyError):
        B.collect(store, fake, "nope", price_table(project))
    runner.close()


def test_collect_classifies_failures_like_live_answers(project, task):
    fake = FakeChatClient({
        "ok": OK, "bad": "not json", "short": '{"terms": "x"}',
        "cut": lambda r: ChatResponse(text="{", stop_reason="max_tokens"),
        "no": lambda r: ChatResponse(text="", stop_reason="refusal"),
        "err": lambda r: ChatResponse(text="", stop_reason="error", error="expired: batch expired"),
    })
    runner = make_runner(project, "terms", client=fake, mode="batch", governed=False)
    for key in ("ok", "bad", "short", "cut", "no", "err"):
        runner.run(task, {"sentences": [key]}, key)
    bid = B.submit_queued(runner)[0]
    r = B.collect(runner.store, fake, bid, price_table(project))
    assert r["collected"] == 6 and r["failed"] == 5
    by = {x["item_key"]: x for x in runner.store.ledger_rows(batch_id=bid)}
    assert by["ok"]["status"] == "cache_batch" and by["bad"]["status"] == "malformed" and by["short"]["status"] == "malformed"
    assert by["cut"]["status"] == "truncated" and by["no"]["status"] == "refused" and by["err"]["status"] == "error"
    calls = {k: runner.store.get_call(v["request_hash"]) for k, v in by.items()}
    assert calls["ok"]["validated"] and calls["ok"]["status"] == "ok"
    assert not calls["bad"]["validated"] and calls["bad"]["status"] == "malformed" and calls["no"]["status"] == "refused"
    assert runner.store.lookup_valid(by["bad"]["request_hash"]) is None
    assert runner.store.get_batch(bid)["n_failed"] == 5 and runner.store.get_batch(bid)["n_done"] == 1
    runner.close()


def test_collect_before_the_batch_ended_writes_nothing(project, task):
    fake = SlowFake({"terms_extract": OK}, n_wait=3)
    runner = make_runner(project, "terms", client=fake, mode="batch", governed=False)
    runner.run(task, {"sentences": ["x"]}, "k")
    bid = B.submit_queued(runner)[0]
    r = B.collect(runner.store, fake, bid, price_table(project))
    assert r["state"] == "in_progress" and r["collected"] == 0 and r["pending_before"] == 1
    assert runner.store.ledger_rows(batch_id=bid)[0]["status"] == "pending"
    assert runner.store.get_batch(bid)["status"] == "in_progress"
    st = B.status_report(runner.store, fake, bid)
    assert st["ledger"] == {"rows": 1, "pending": 1, "cache_batch": 0, "failed": 0, "cost": 0.0}
    assert st["provider_state"]["state"] == "in_progress" and st["status"] == "in_progress" and st["provider"] == "fake"
    slept: list[float] = []
    done = B.wait_for_batch(runner.store, fake, bid, price_table(project), poll_seconds=7, sleep=slept.append)
    assert done["state"] == "ended" and done["collected"] == 1 and slept == [7] and fake.probes == 4
    assert B.status_report(runner.store, fake, bid)["status"] == "collected"
    runner.close()


def test_wait_for_batch_times_out(project, task):
    fake = SlowFake({"terms_extract": OK}, n_wait=99)
    runner = make_runner(project, "terms", client=fake, mode="batch", governed=False)
    runner.run(task, {"sentences": ["x"]}, "k")
    bid = B.submit_queued(runner)[0]
    r = B.wait_for_batch(runner.store, fake, bid, price_table(project), poll_seconds=0, timeout_seconds=0, sleep=lambda s: None)
    assert r["state"] == "in_progress" and r["collected"] == 0
    runner.close()


def test_custom_id_mapping_for_nonconforming_keys(project, task):
    fake = CustomIdFake({"terms_extract": OK})
    runner = make_runner(project, "terms", client=fake, mode="batch", governed=False)
    keys = ["C_micromotion", "chunk 7 / 第二章", "x" * 70]
    for k in keys:
        runner.run(task, {"sentences": [k]}, k)
    bid = B.submit_queued(runner)[0]
    assert set(fake.batches[bid]) == {custom_id_for(k) for k in keys}
    assert custom_id_for("C_micromotion") == "C_micromotion"
    assert custom_id_for("chunk 7 / 第二章").startswith("k_") and len(custom_id_for("x" * 70)) == 42
    assert custom_id_for("a b") != custom_id_for("a  b") and custom_id_for("") == custom_id_for("")
    r = B.collect(runner.store, fake, bid, price_table(project))
    assert r["collected"] == 3 and r["missing"] == 0
    assert all(x["status"] == "cache_batch" for x in runner.store.ledger_rows(batch_id=bid))
    runner.close()


def test_missing_results_stay_pending(project, task):
    fake = FakeChatClient({"terms_extract": OK})
    runner = make_runner(project, "terms", client=fake, mode="batch", governed=False)
    runner.run(task, {"sentences": ["a"]}, "a")
    runner.run(task, {"sentences": ["b"]}, "b")
    bid = B.submit_queued(runner)[0]
    del fake.batches[bid]["b"]
    r = B.collect(runner.store, fake, bid, price_table(project))
    assert r["collected"] == 1 and r["missing"] == 1
    assert {x["item_key"]: x["status"] for x in runner.store.ledger_rows(batch_id=bid)} == {"a": "cache_batch", "b": "pending"}
    assert runner.store.get_batch(bid)["status"] == "ended"
    runner.close()


def test_chunking_by_count_and_bytes(task):
    price = Price(4, 20, 5, 0.2)

    def q(i: int, size: int = 10) -> Queued:
        req = ChatRequest(model="m", system=("s",), user="u" * size, max_tokens=10, task="t", key=f"k{i}")
        return Queued(task, req, f"k{i}", estimate(req, price), "fake")
    items = [q(i) for i in range(5)]
    assert [len(c) for c in B.chunk_queued(items, max_requests=2)] == [2, 2, 1]
    assert [len(c) for c in B.chunk_queued(items)] == [5]
    big = [q(i, 1000) for i in range(3)]
    assert [len(c) for c in B.chunk_queued(big, max_bytes=1500)] == [1, 1, 1]
    assert B.chunk_queued([]) == []
    assert B.BATCH_MAX_REQUESTS == 100_000 and B.BATCH_MAX_BYTES < 256 * 1024 * 1024


def test_submit_respects_the_budget_before_sending(project, task):
    fake = FakeChatClient({"terms_extract": OK})
    runner = make_runner(project, "terms", client=fake, mode="batch", budget=0.0001)
    runner.run(task, {"sentences": ["a"]}, "a")
    with pytest.raises(BudgetExceeded) as e:
        B.submit_queued(runner)
    assert e.value.scope == "run" and fake.batches == {} and runner.store.ledger_rows() == [] and len(runner.queued) == 1
    assert runner.governor.reserved == 0.0
    runner.governor.budgets["run"] = 10.0
    ids = B.submit_queued(runner)
    assert len(ids) == 1 and runner.governor.reserved == 0.0 and runner.queued == []
    assert runner.store.spent(run_id=runner.run_id)["pending_est"] == pytest.approx(runner.store.ledger_rows()[0]["est_cost"])
    runner.close()


def test_two_providers_submit_one_batch_each_for_their_share(project, task):
    a = FakeChatClient({"terms_extract": OK})
    a.provider = "anthropic"
    d = FakeChatClient({"terms_extract": OK})
    d.provider = "deepseek"                                # a fake with a batch API, to see two batches
    d._n = 100                                             # distinct fake batch ids per instance
    runner = make_runner(project, "terms", clients={"anthropic": a, "deepseek": d}, mode="batch", governed=False)
    for i in range(40):
        assert runner.run(task, {"sentences": [str(i)]}, f"k{i}").status == "queued"
    ids = B.submit_queued(runner)
    assert len(ids) == 2 and runner.queued == []
    by_provider = {runner.store.get_batch(b)["provider"]: b for b in ids}
    assert set(by_provider) == {"anthropic", "deepseek"}
    assert runner.store.get_batch(by_provider["deepseek"])["model"] == "deepseek-flash"
    rows = runner.store.ledger_rows()
    assert {r["batch_id"] for r in rows} == set(ids) and all(r["provider"] == runner.store.get_batch(r["batch_id"])["provider"] for r in rows)
    for p, b in by_provider.items():
        r = B.collect(runner.store, {"anthropic": a, "deepseek": d}[p], b, price_table(project))
        assert r["provider"] == p and r["missing"] == 0 and r["collected"] == sum(1 for x in rows if x["batch_id"] == b)
    assert {x["status"] for x in runner.store.ledger_rows()} == {"cache_batch"}
    runner.close()


def test_two_runs_share_requests_through_the_hash(project, task):
    fake = FakeChatClient({"terms_extract": OK})
    first = make_runner(project, "terms", client=fake, mode="batch", governed=False)
    first.run(task, {"sentences": ["same"]}, "k")
    bid = B.submit_queued(first)[0]
    B.collect(first.store, fake, bid, price_table(project))
    second = make_runner(project, "terms", client=fake, mode="batch", governed=False, store=first.store, run_id="r2")
    res = second.run(task, {"sentences": ["same"]}, "other-key")
    assert res.status == "cache_local" and second.queued == []
    h = request_hash("fake", first.request(task, {"sentences": ["same"]}, "k"))
    assert [x["request_hash"] for x in first.store.ledger_rows()] == [h, h]
    first.close()
