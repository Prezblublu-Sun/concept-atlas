"""atlas.llm.tasks: prompt files and prompt_sha, deterministic rendering / request hashes, validation and caps,
the truncation / malformed / refusal policies, ledger rows per attempt, batch-mode queueing, the provider split
with failover tagging, the run settings snapshot (FakeChatClient only)."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from atlas.config import init_project
from atlas.llm.base import ChatResponse, FakeChatClient, request_hash
from atlas.llm.client import choose_provider
from atlas.llm.tasks import (RETRY_SUFFIX, SETTINGS_FILE, StructuredTask, apply_limits, load_prompt, make_runner,
                             merge_parts, parse_json, plan_items, read_settings, render_payload, run_task, task_for,
                             validate)
from atlas.store.llm import LlmStore

SCHEMA = {
    "type": "object",
    "properties": {"terms": {"type": "array", "items": {
        "type": "object",
        "properties": {"term": {"type": "string", "maxLength": 40}, "type": {"type": "string", "enum": ["名词术语", "缩写"]},
                       "sids": {"type": "array", "items": {"type": "string"}, "maxItems": 3}},
        "required": ["term", "sids"], "additionalProperties": False}}},
    "required": ["terms"], "additionalProperties": False,
}
PROMPT = "Extract terms.\nEvery item cites sids.\n---payload---\nSection: {section}\nSentences: {sentences}\nLiteral {{braces}}.\n"


def ok_json(*terms: str) -> str:
    return json.dumps({"terms": [{"term": t, "sids": ["s1"]} for t in terms]}, ensure_ascii=False)


@pytest.fixture
def prompt_file(tmp_path: Path) -> Path:
    p = tmp_path / "terms_extract.md"
    p.write_text(PROMPT, encoding="utf-8")
    return p


@pytest.fixture
def project(tmp_path: Path):
    return init_project(tmp_path / "proj", name="llmtest")


def make_task(prompt_file: Path, **kw) -> StructuredTask:
    base = dict(limits={"terms": 2, "terms[].sids": 3}, max_tokens=1000, splittable=False, unit_cost_default=0.0)
    base.update(kw)
    return StructuredTask("terms_extract", prompt_file, SCHEMA, **base)


def runner_for(project, fake: FakeChatClient, **kw):
    kw.setdefault("mode", "live")
    kw.setdefault("governed", False)
    return make_runner(project, "terms", client=fake, **kw)


# ---- prompts and rendering ---------------------------------------------------------------------------------------------
def test_prompt_file_sections_and_sha(prompt_file: Path):
    p = load_prompt(prompt_file)
    assert p.system == ("Extract terms.\nEvery item cites sids.",)              # CRLF of a Windows write normalised
    assert p.template.startswith("Section: {section}") and p.template.endswith("Literal {{braces}}.")
    assert p.sha == hashlib.sha256(prompt_file.read_bytes()).hexdigest()
    assert load_prompt(prompt_file) is p                                     # cached by (mtime, size)
    lf = prompt_file.with_name("lf.md")
    lf.write_bytes(PROMPT.encode("utf-8"))
    crlf = prompt_file.with_name("crlf.md")
    crlf.write_bytes(PROMPT.replace("\n", "\r\n").encode("utf-8"))
    assert load_prompt(lf).system == load_prompt(crlf).system and load_prompt(lf).template == load_prompt(crlf).template
    assert load_prompt(lf).sha != load_prompt(crlf).sha                      # the sha is over the bytes
    prompt_file.write_text(PROMPT + "\nmore rules\n", encoding="utf-8")
    assert load_prompt(prompt_file).sha != p.sha                             # a changed file is a new prompt
    bad = prompt_file.with_name("bad.md")
    bad.write_text("no separator", encoding="utf-8")
    with pytest.raises(ValueError, match="---payload---"):
        load_prompt(bad)
    t = task_for("terms_extract", SCHEMA, prompts_dir=prompt_file.parent, max_tokens=5)
    assert t.prompt_file == prompt_file and t.max_tokens == 5 and len(t.prompt_sha) == 64


def test_render_payload_canonical_json_and_missing_field(prompt_file: Path):
    task = make_task(prompt_file)
    out = task.render({"section": "methods", "sentences": [{"sid": "s2", "text": "b"}, {"sid": "s1", "text": "a"}]})
    assert 'Sentences: [{"sid":"s2","text":"b"},{"sid":"s1","text":"a"}]' in out and "Literal {braces}." in out
    with pytest.raises(KeyError, match="sentences"):
        render_payload(task.prompt().template, {"section": "x"})


def test_two_builds_of_the_same_item_give_the_same_request_hash(project, prompt_file: Path):
    task = make_task(prompt_file)
    runner = runner_for(project, FakeChatClient())
    a = {"section": "methods", "sentences": [{"sid": "s1", "text": "alpha"}, {"sid": "s2", "text": "beta"}]}
    b = {"sentences": [{"text": "alpha", "sid": "s1"}, {"text": "beta", "sid": "s2"}], "section": "methods"}
    ra, rb = runner.request(task, a, "chunk1"), runner.request(task, b, "chunk1")
    assert ra == rb and request_hash("fake", ra) == request_hash("fake", rb)
    assert ra.system == task.prompt().system and ra.output_schema == SCHEMA and ra.task == "terms_extract"
    rc = runner.request(task, {**a, "section": "results"}, "chunk1")
    assert request_hash("fake", rc) != request_hash("fake", ra)
    rd = runner.request(task, a, "chunk1", context=("scheme block",))
    assert rd.system == task.prompt().system + ("scheme block",) and request_hash("fake", rd) != request_hash("fake", ra)
    runner.close()


# ---- validation and caps ----------------------------------------------------------------------------------------------
def test_validate_type_required_enum_caps():
    assert validate(SCHEMA, {"terms": [{"term": "a", "sids": ["s1"]}]}) == []
    assert validate(SCHEMA, {"terms": [{"term": "a", "type": "缩写", "sids": []}]}) == []
    assert any("missing required 'terms'" in e for e in validate(SCHEMA, {}))
    assert any("expected object" in e for e in validate(SCHEMA, [1]))
    assert any("not in enum" in e for e in validate(SCHEMA, {"terms": [{"term": "a", "type": "動詞", "sids": []}]}))
    assert any("maxItems" in e for e in validate(SCHEMA, {"terms": [{"term": "a", "sids": ["1", "2", "3", "4"]}]}))
    assert any("maxLength" in e for e in validate(SCHEMA, {"terms": [{"term": "x" * 41, "sids": []}]}))
    assert any("unexpected key" in e for e in validate(SCHEMA, {"terms": [], "extra": 1}))
    assert any("expected string" in e for e in validate(SCHEMA, {"terms": [{"term": 3, "sids": []}]}))
    assert validate({"type": "integer"}, True) and not validate({"type": "integer"}, 3)
    assert validate({"type": "number"}, False) and not validate({"type": "number"}, 2.5)
    assert not validate({"type": ["string", "null"]}, None)
    assert validate(None, {"anything": 1}) == []


def test_apply_limits_paths():
    data = {"terms": [{"term": str(i), "sids": ["a", "b", "c", "d", "e"]} for i in range(5)], "other": [1, 2, 3]}
    out, truncated = apply_limits(data, {"terms": 2, "terms[].sids": 3, "other": 10, "missing": 1})
    assert [t["term"] for t in out["terms"]] == ["0", "1"] and all(len(t["sids"]) == 3 for t in out["terms"])
    assert out["other"] == [1, 2, 3]
    assert truncated == ["terms (5→2)", "terms[].sids (5→3)", "terms[].sids (5→3)"]
    with pytest.raises(ValueError):
        apply_limits({}, {"a[0]": 1})


def test_parse_json_and_merge_parts():
    assert parse_json('```json\n{"a": 1}\n```') == ({"a": 1}, None)
    assert parse_json('Here you go: {"a": [1, 2]} thanks') == ({"a": [1, 2]}, None)
    assert parse_json("nothing here")[1] == "no JSON object in the response"
    assert parse_json('{"a": }')[1].startswith("invalid JSON")
    assert parse_json("42")[1] == "JSON is a int, not an object"
    merged = merge_parts([{"terms": [1], "meta": {"a": 1}, "n": 1}, {"terms": [2, 3], "meta": {"b": 2}, "n": 2}])
    assert merged == {"terms": [1, 2, 3], "meta": {"a": 1, "b": 2}, "n": 1}


# ---- policies --------------------------------------------------------------------------------------------------------
def test_ok_run_caps_lists_and_writes_one_ok_row(project, prompt_file: Path):
    task = make_task(prompt_file)
    fake = FakeChatClient({"c1": ok_json("x", "y", "z")})
    runner = runner_for(project, fake)
    r = runner.run(task, {"section": "methods", "sentences": []}, "c1")
    assert r.ok and r.status == "ok" and r.attempts == 1 and r.cost > 0
    assert r.provider == "fake" and r.model_reported == "claude-opus-5-5" and not r.failover
    assert [t["term"] for t in r.data["terms"]] == ["x", "y"] and r.truncated_by_code == ["terms (3→2)"]
    rows = runner.store.ledger_rows()
    assert len(rows) == 1 and rows[0]["status"] == "ok" and rows[0]["item_key"] == "c1"
    assert rows[0]["task"] == "terms_extract" and rows[0]["prompt_sha"] == task.prompt_sha
    assert rows[0]["provider"] == "fake" and rows[0]["failover"] == 0 and rows[0]["model_reported"] == "claude-opus-5-5"
    assert rows[0]["run_id"] == runner.run_id and rows[0]["stage"] == "terms" and rows[0]["cost"] == pytest.approx(r.cost)
    call = runner.store.get_call(r.request_hash)
    assert call["status"] == "ok" and call["validated"] and call["response_text"] == ok_json("x", "y", "z")
    assert call["request"]["task"] == "terms_extract" and call["request"]["output_schema"] == SCHEMA
    runner.close()


def test_second_run_is_a_local_cache_hit_without_a_call(project, prompt_file: Path):
    task = make_task(prompt_file)
    fake = FakeChatClient({"c1": ok_json("x")})
    runner = runner_for(project, fake)
    first = runner.run(task, {"section": "m", "sentences": []}, "c1")
    second = run_task(runner, task, {"section": "m", "sentences": []}, "c1")
    assert second.status == "cache_local" and second.ok and second.data == first.data and second.cost == 0
    assert len(fake.requests) == 1
    statuses = [r["status"] for r in runner.store.ledger_rows()]
    assert statuses == ["ok", "cache_local"]
    hit = [r for r in runner.store.ledger_rows() if r["status"] == "cache_local"][0]
    assert hit["cost"] == 0 and hit["in_tokens"] == 0 and hit["request_hash"] == first.request_hash
    runner.close()


def test_truncation_splits_a_splittable_item(project, prompt_file: Path):
    task = make_task(prompt_file, splittable=True)
    fake = FakeChatClient({
        "k": lambda r: ChatResponse(text='{"terms": [', stop_reason="max_tokens"),
        "k/0": ok_json("p"), "k/1": ok_json("q"),
    })
    runner = runner_for(project, fake)
    payload = {"section": "m", "sentences": [{"sid": "s1"}, {"sid": "s2"}]}
    split = lambda p: [{**p, "sentences": p["sentences"][:1]}, {**p, "sentences": p["sentences"][1:]}]  # noqa: E731
    r = runner.run(task, payload, "k", split=split)
    assert r.status == "ok" and r.parts == 2 and r.attempts == 3
    assert [t["term"] for t in r.data["terms"]] == ["p", "q"]
    assert [q.key for q in fake.requests] == ["k", "k/0", "k/1"]
    assert [x["status"] for x in runner.store.ledger_rows()] == ["truncated", "ok", "ok"]
    assert r.cost == pytest.approx(sum(x["cost"] for x in runner.store.ledger_rows()))
    runner.close()


def test_truncation_retries_once_with_doubled_max_tokens(project, prompt_file: Path):
    task = make_task(prompt_file, max_tokens=1000)
    fake = FakeChatClient({"terms_extract": lambda r: (ok_json("a") if r.max_tokens == 2000 else
                                                      ChatResponse(text="{", stop_reason="max_tokens"))})
    runner = runner_for(project, fake)
    r = runner.run(task, {"section": "m", "sentences": []}, "k")
    assert r.status == "ok" and r.attempts == 2 and [q.max_tokens for q in fake.requests] == [1000, 2000]
    assert [x["status"] for x in runner.store.ledger_rows()] == ["truncated", "ok"]
    # both attempts truncated → failed:truncated after exactly two attempts (other payloads: the hash ignores keys)
    fake2 = FakeChatClient({"terms_extract": lambda r: ChatResponse(text="{", stop_reason="max_tokens")})
    runner2 = runner_for(project, fake2, store=runner.store)
    r2 = runner2.run(task, {"section": "m", "sentences": ["2"]}, "k2")
    assert r2.status == "failed:truncated" and r2.attempts == 2 and not r2.ok and "max_tokens" in r2.error
    assert [q.max_tokens for q in fake2.requests] == [1000, 2000]
    # a cap below ×2 limits the retry; a cap at the current value forbids it
    runner3 = runner_for(project, fake2, store=runner.store)
    runner3.max_tokens_cap = 1500
    r3 = runner3.run(task, {"section": "m", "sentences": ["3"]}, "k3")
    assert r3.status == "failed:truncated" and [q.max_tokens for q in fake2.requests[2:]] == [1000, 1500]
    runner3.max_tokens_cap = 1000
    r4 = runner3.run(task, {"section": "m", "sentences": ["4"]}, "k4")
    assert r4.status == "failed:truncated" and r4.attempts == 1
    runner.close()


def test_malformed_answer_gets_one_retry_with_a_different_hash(project, prompt_file: Path):
    task = make_task(prompt_file)
    fake = FakeChatClient({"terms_extract": lambda r: (ok_json("a") if r.user.endswith(RETRY_SUFFIX) else "not json")})
    runner = runner_for(project, fake)
    r = runner.run(task, {"section": "m", "sentences": []}, "k")
    assert r.status == "ok" and r.attempts == 2
    h1, h2 = (request_hash("fake", q) for q in fake.requests)
    assert h1 != h2 and fake.requests[1].user == fake.requests[0].user + RETRY_SUFFIX
    assert [x["status"] for x in runner.store.ledger_rows()] == ["malformed", "ok"]
    fake2 = FakeChatClient(default="still not json")
    runner2 = runner_for(project, fake2, store=runner.store)
    r2 = runner2.run(task, {"section": "m", "sentences": ["other"]}, "k2")
    assert r2.status == "failed:malformed" and r2.attempts == 2 and "no JSON object" in r2.error
    assert runner.store.get_call(r2.request_hash)["status"] == "malformed"
    assert runner.store.lookup_valid(r2.request_hash) is None
    runner.close()


def test_schema_violation_is_malformed_even_when_json_parses(project, prompt_file: Path):
    task = make_task(prompt_file)
    fake = FakeChatClient(default='{"terms": [{"term": "a"}]}')          # sids missing
    runner = runner_for(project, fake)
    r = runner.run(task, {"section": "m", "sentences": []}, "k")
    assert r.status == "failed:malformed" and "missing required 'sids'" in r.error and r.data == {"terms": [{"term": "a"}]}
    runner.close()


def test_refusal_is_recorded_and_never_retried(project, prompt_file: Path):
    task = make_task(prompt_file, splittable=True)
    fake = FakeChatClient({"k": lambda r: ChatResponse(text="", stop_reason="refusal")})
    runner = runner_for(project, fake)
    r = runner.run(task, {"section": "m", "sentences": []}, "k", split=lambda p: [p, p])
    assert r.status == "failed:refused" and r.attempts == 1 and len(fake.requests) == 1
    rows = runner.store.ledger_rows()
    assert [x["status"] for x in rows] == ["refused"] and rows[0]["in_tokens"] > 0
    assert runner.store.get_call(r.request_hash)["status"] == "refused"
    runner.close()


def test_transport_error_and_free_text_task(project, prompt_file: Path):
    task = make_task(prompt_file)
    fake = FakeChatClient({"k": lambda r: ChatResponse(text="", stop_reason="error", error="HTTP 500")})
    runner = runner_for(project, fake)
    r = runner.run(task, {"section": "m", "sentences": []}, "k")
    assert r.status == "failed:error" and r.error == "HTTP 500" and r.attempts == 1
    free = StructuredTask("summary", prompt_file, None, max_tokens=50)
    fake.responses["f"] = "A plain summary."
    r2 = runner.run(free, {"section": "m", "sentences": []}, "f")
    assert r2.status == "ok" and r2.data is None and r2.text == "A plain summary."
    assert fake.requests[-1].output_schema is None
    assert runner.run(free, {"section": "m", "sentences": []}, "f").status == "cache_local"
    runner.close()


def test_every_attempt_is_billed(project, prompt_file: Path):
    task = make_task(prompt_file)
    fake = FakeChatClient({"terms_extract": lambda r: (ok_json("a") if r.user.endswith(RETRY_SUFFIX) else "no")})
    runner = runner_for(project, fake)
    r = runner.run(task, {"section": "m", "sentences": []}, "k")
    rows = runner.store.ledger_rows()
    assert len(rows) == 2 and all(x["cost"] > 0 for x in rows) and r.cost == pytest.approx(sum(x["cost"] for x in rows))
    assert runner.backends["fake"].ledger.cost == pytest.approx(r.cost) and [x["attempt"] for x in rows] == [1, 2]
    runner.close()


def test_batch_mode_queues_misses_and_answers_cache_hits(project, prompt_file: Path):
    task = make_task(prompt_file)
    fake = FakeChatClient({"a": ok_json("a")})
    live = runner_for(project, fake, mode="live")
    live.run(task, {"section": "m", "sentences": []}, "a")
    batch = runner_for(project, fake, mode="batch", store=live.store)
    hit = batch.run(task, {"section": "m", "sentences": []}, "a")
    miss = batch.run(task, {"section": "m", "sentences": [{"sid": "s9"}]}, "b")
    assert hit.status == "cache_local" and hit.data["terms"][0]["term"] == "a"
    assert miss.status == "queued" and miss.data is None and not miss.ok
    assert [q.item_key for q in batch.queued] == ["b"] and batch.queued[0].estimate.cost_high > 0
    assert len(fake.requests) == 1                                           # nothing sent in batch mode
    live.close()


def test_make_runner_reads_the_configuration(project, prompt_file: Path):
    fake = FakeChatClient()
    runner = make_runner(project, "terms", client=fake)
    assert runner.mode == "batch" and runner.models == {"fake": "claude-opus-5-5"} and runner.primary == "fake"
    assert runner.split == {"fake": 1.0} and runner.backends["fake"].effort == "medium" and runner.currency == "USD"
    assert runner.max_tokens_cap == 65536 and runner.governor is not None and not runner.failover
    assert runner.governor.budgets == {"run": 80.0, "stage": 40.0, "daily": 150.0}
    assert runner.governor.rates == {"USD": 1.0, "RMB": pytest.approx(0.14)} and runner.rates == runner.governor.rates
    assert runner.run_id.startswith("terms_") and runner.startup_notes == []
    req = runner.request(make_task(prompt_file), {"section": "m", "sentences": []}, "k")
    assert req.model == "claude-opus-5-5" and req.effort == "medium"
    runner.close()
    other = make_runner(project, "scheme", client=fake, mode="live", budget=3, run_id="r1")
    assert other.mode == "live" and other.run_id == "r1" and other.governor.budgets["run"] == 3.0
    assert other.governor.budgets["stage"] == 5.0
    other.close()
    with pytest.raises(ValueError):
        make_runner(project, "terms", client=fake, mode="turbo")
    store = LlmStore.open(project.llm_db)
    assert store.ledger_rows() == []
    store.close()


# ---- provider split, failover, settings snapshot ----------------------------------------------------------------------
def two_fakes(responses: dict | None = None) -> dict[str, FakeChatClient]:
    """Two fakes posing as the anthropic and deepseek backends (the latter in RMB, without a batch API)."""
    a = FakeChatClient(responses)
    a.provider = "anthropic"
    d = FakeChatClient(responses)
    d.provider, d.currency, d.batch_api = "deepseek", "RMB", False
    return {"anthropic": a, "deepseek": d}


def test_items_are_split_deterministically_and_rows_name_their_provider(project, prompt_file: Path):
    task = make_task(prompt_file)
    clients = two_fakes({"terms_extract": ok_json("a")})
    runner = make_runner(project, "terms", clients=clients, mode="live", governed=False)
    assert runner.primary == "anthropic" and runner.split == {"anthropic": pytest.approx(0.9), "deepseek": pytest.approx(0.1)}
    assert runner.models == {"anthropic": "claude-opus-5-5", "deepseek": "deepseek-flash"} and runner.currency == "USD"
    assert runner.backends["deepseek"].effort is None and runner.backends["anthropic"].effort == "medium"
    keys = [f"chunk-{i:03d}" for i in range(60)]
    results = {k: runner.run(task, {"section": "m", "sentences": [k]}, k) for k in keys}
    expected = {k: choose_provider(k, runner.split) for k in keys}
    assert {k: r.provider for k, r in results.items()} == expected and len(set(expected.values())) == 2
    assert {q.key for q in clients["anthropic"].requests} == {k for k, p in expected.items() if p == "anthropic"}
    assert {q.key for q in clients["deepseek"].requests} == {k for k, p in expected.items() if p == "deepseek"}
    assert all(q.model == "deepseek-flash" and q.effort is None for q in clients["deepseek"].requests)
    assert all(q.model == "claude-opus-5-5" and q.effort == "medium" for q in clients["anthropic"].requests)
    rows = {r["item_key"]: r for r in runner.store.ledger_rows()}
    assert {k: r["provider"] for k, r in rows.items()} == expected
    ds_key = next(k for k, p in expected.items() if p == "deepseek")
    assert rows[ds_key]["currency"] == "RMB" and rows[ds_key]["failover"] == 0
    assert results[ds_key].cost == pytest.approx(rows[ds_key]["cost"] * 0.14)          # TaskResult.cost in USD
    again = make_runner(project, "terms", clients=two_fakes(), mode="live", governed=False, store=runner.store, run_id="r2")
    assert all(again.run(task, {"section": "m", "sentences": [k]}, k).status == "cache_local" for k in keys[:5])
    assert [r["provider"] for r in again.store.ledger_rows(run_id="r2")] == [expected[k] for k in keys[:5]]
    runner.close()


def test_failover_reruns_a_failed_item_on_the_other_backend_and_tags_the_rows(project, prompt_file: Path):
    task = make_task(prompt_file)
    clients = two_fakes({"terms_extract": ok_json("a")})
    runner = make_runner(project, "terms", clients=clients, mode="live", governed=False)
    ds_key = next(f"k{i}" for i in range(500) if choose_provider(f"k{i}", runner.split) == "deepseek")
    clients["deepseek"].responses[ds_key] = lambda r: ChatResponse(text="", stop_reason="error", error="HTTP 503")
    r = runner.run(task, {"section": "m", "sentences": []}, ds_key)
    assert r.status == "failed:error" and r.provider == "deepseek" and not r.failover    # llm.failover is off
    runner.failover = True
    r2 = runner.run(task, {"section": "m", "sentences": []}, ds_key)
    assert r2.status == "ok" and r2.provider == "anthropic" and r2.failover and r2.attempts == 2
    rows = runner.store.ledger_rows()
    assert [(x["provider"], x["status"], x["failover"]) for x in rows] == [
        ("deepseek", "error", 0), ("deepseek", "error", 0), ("anthropic", "ok", 1)]
    assert runner.backend_for(ds_key).provider == "deepseek"                   # the assignment itself is stable
    r3 = runner.run(task, {"section": "m", "sentences": []}, ds_key)           # deepseek fails again → the cached failover answer
    assert r3.status == "cache_local" and r3.provider == "anthropic" and r3.failover
    assert [(x["provider"], x["status"], x["failover"]) for x in runner.store.ledger_rows()[3:]] == [
        ("deepseek", "error", 0), ("anthropic", "cache_local", 1)]
    runner.close()


def test_batch_mode_calls_a_backend_without_batch_api_live(project, prompt_file: Path):
    task = make_task(prompt_file)
    clients = two_fakes({"terms_extract": ok_json("a")})
    runner = make_runner(project, "terms", clients=clients, mode="batch", governed=False)
    keys = [f"k{i}" for i in range(40)]
    results = {k: runner.run(task, {"section": "m", "sentences": [k]}, k) for k in keys}
    assert {r.status for r in results.values() if r.provider == "anthropic"} == {"queued"}
    assert {r.status for r in results.values() if r.provider == "deepseek"} == {"ok"}
    assert {q.provider for q in runner.queued} == {"anthropic"} and clients["anthropic"].requests == []
    assert len(clients["deepseek"].requests) == sum(1 for r in results.values() if r.provider == "deepseek") > 0
    runner.close()


def test_settings_snapshot_is_written_on_the_first_spend_not_by_a_plan(project, prompt_file: Path):
    task = make_task(prompt_file, max_tokens=777)
    other = StructuredTask("classify", prompt_file, SCHEMA, max_tokens=100)
    fake = FakeChatClient({"terms_extract": ok_json("a"), "classify": ok_json("b")})
    runner = runner_for(project, fake, run_id="terms_r1", tasks=[task])
    path = project.work_dir / "runs" / "terms_r1" / SETTINGS_FILE
    plan_items(runner, [(task, {"section": "m", "sentences": []}, "k")])
    assert not path.exists() and read_settings(project, "terms_r1") is None       # a dry run writes nothing
    runner.run(task, {"section": "m", "sentences": []}, "k")
    s = read_settings(project, "terms_r1")
    assert runner.settings_path() == path and path.is_file()
    assert s["run_id"] == "terms_r1" and s["stage"] == "terms" and s["mode"] == "live" and s["primary"] == "fake"
    assert s["split"] == {"fake": 1.0} and s["failover"] is False and s["currency"] == "USD" and s["max_tokens_cap"] == 65536
    assert s["providers"] == {"fake": {"model": "claude-opus-5-5", "effort": "medium", "currency": "USD", "batch_api": True,
                                       "price": {"input": 4.0, "output": 20.0, "cache_write": 5.0, "cache_read": 0.2,
                                                 "currency": "USD", "batch_multiplier": 0.5}}}
    assert s["tasks"] == {"terms_extract": {"prompt_file": prompt_file.name, "prompt_sha": task.prompt_sha, "max_tokens": 777,
                                            "splittable": False, "limits": {"terms": 2, "terms[].sids": 3},
                                            "unit_cost_default": 0.0, "schema": True}}
    assert len(s["pricing_sha"]) == 64 and s["fx"] == {"USD": 1.0, "RMB": pytest.approx(0.14)} and s["created"].endswith("+00:00")
    runner.run(other, {"section": "m", "sentences": []}, "c")                     # a new task rewrites the snapshot
    assert set(read_settings(project, "terms_r1")["tasks"]) == {"terms_extract", "classify"}
    batch = runner_for(project, fake, mode="batch", run_id="terms_r2", store=runner.store)
    batch.run(task, {"section": "m", "sentences": ["new"]}, "q")                 # queueing counts as the first spend
    assert read_settings(project, "terms_r2")["mode"] == "batch"
    runner.close()
