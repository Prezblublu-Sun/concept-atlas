"""pipeline/llm_shim: prompt loading, rendering determinism, validation, caps, the stop-reason policies, cost
arithmetic and the plan (no network, FakeChatClient only)."""
from __future__ import annotations

import json

import pytest

from atlas.llm.base import ChatResponse, FakeChatClient, Usage, request_hash
from atlas.pipeline import llm_shim as sh


def test_prompt_files_have_two_sections_and_stable_sha():
    for name in ("terms_extract", "concepts_merge", "scheme_draft", "classify"):
        p = sh.load_prompt(name)
        assert p.system.strip() and p.template.strip() and len(p.sha) == 64
        assert sh.load_prompt(name).sha == p.sha
        assert "sid" in p.system.lower()                      # the evidence rule is in every prompt
    with pytest.raises(FileNotFoundError):
        sh.load_prompt("nope")


def test_render_is_pure_and_hash_is_stable_per_item():
    p = sh.load_prompt("terms_extract")
    payload = {"section": "methods", "n_sentences": 2, "sentences": "[S1a] a {brace} text\n[S1b] b"}
    r1 = sh.build_request(p, payload, task="terms_extract", key="k1", schema={"type": "object"}, max_tokens=100)
    r2 = sh.build_request(p, dict(reversed(list(payload.items()))), task="terms_extract", key="k2", schema={"type": "object"}, max_tokens=100)
    assert r1.user == r2.user and "{brace}" in r1.user
    assert request_hash("fake", r1) == request_hash("fake", r2)
    r3 = sh.build_request(p, payload, task="t", key="k", schema=None, max_tokens=100, system_extra=("CTX",))
    assert r3.system[-1] == "CTX" and r3.system[0] == p.system
    with pytest.raises(sh.PromptError):
        p.render({"section": "x"})


def test_validate_and_truncate():
    schema = {"type": "object", "required": ["terms"], "properties": {"terms": {"type": "array", "maxItems": 2, "items": {
        "type": "object", "required": ["term", "type"], "properties": {"term": {"type": "string", "maxLength": 5},
                                                                        "type": {"enum": ["a", "b"]},
                                                                        "n": {"type": ["integer", "null"]}}}}}}
    assert sh.validate(schema, {"terms": [{"term": "ok", "type": "a", "n": None}]}) == []
    errs = sh.validate(schema, {"terms": [{"term": "toolong", "type": "c"}, {"type": "a"}, {"term": "x", "type": "a", "n": True}]})
    assert any("maxItems" in e or "more than" in e for e in errs)
    assert any("not in enum" in e for e in errs) and any("required" in e for e in errs) and any("longer" in e for e in errs)
    assert any("expected" in e for e in errs)
    assert sh.validate(schema, []) == ["$: expected object, got list"]
    obj = {"terms": [{"sids": [1, 2, 3, 4, 5]}, {"sids": [1, 2]}, {"sids": []}]}
    assert sh.truncate(obj, {"terms.sids": 3, "terms": 2}) == {"terms.sids": 2, "terms": 1}
    assert obj == {"terms": [{"sids": [1, 2, 3]}, {"sids": [1, 2]}]}
    assert sh.parse_json('```json\n{"a": 1}\n```') == {"a": 1} and sh.parse_json('noise {"a": [1]} tail') == {"a": [1]}
    with pytest.raises(ValueError):
        sh.parse_json("")


def test_run_task_policies_with_fake_client():
    schema = {"type": "object", "required": ["terms"], "properties": {"terms": {"type": "array", "maxItems": 1}}}
    calls = {"n": 0}

    def truncated_then_ok(req):
        calls["n"] += 1
        if req.max_tokens < 200:
            return ChatResponse(text='{"terms": [', stop_reason="max_tokens")
        return '{"terms": [1, 2, 3]}'

    def malformed_then_ok(req):
        return '{"terms": [1]}' if "Return only the JSON object" in req.user else "not json at all"

    fake = FakeChatClient({"t": truncated_then_ok, "m": malformed_then_ok,
                           "r": lambda r: ChatResponse(text="", stop_reason="refusal"),
                           "e": lambda r: ChatResponse(text="", stop_reason="error", error="boom"),
                           "bad": "{\"nope\": 1}"})
    pl = {"section": "s", "n_sentences": 0, "sentences": ""}
    res = sh.run_task(fake, "x", "terms_extract", schema, pl, key="t", max_tokens=100, caps={"terms": 1})
    assert res.ok and res["data"] == {"terms": [1]} and res["attempts"] == 2 and res["truncated_by_code"] == {"terms": 2}
    assert res["usage"]["input"] > 0 and res["cost"] > 0 and res["currency"] == "USD" and len(res["request_hash"]) == 64
    assert fake.requests[-1].max_tokens == 200
    res = sh.run_task(fake, "x", "terms_extract", schema, pl, key="m", max_tokens=100)
    assert res.ok and res["attempts"] == 2
    res = sh.run_task(fake, "x", "terms_extract", schema, pl, key="r", max_tokens=100)
    assert res["status"] == "refused" and res["attempts"] == 1
    res = sh.run_task(fake, "x", "terms_extract", schema, pl, key="e", max_tokens=100)
    assert res["status"] == "error" and res["errors"] == ["boom"]
    res = sh.run_task(fake, "x", "terms_extract", schema, pl, key="bad", max_tokens=100)
    assert res["status"] == "invalid" and res["attempts"] == 2 and any("required" in e for e in res["errors"])
    # a persistently truncated answer
    fake2 = FakeChatClient({"t": lambda r: ChatResponse(text="{", stop_reason="max_tokens")})
    res = sh.run_task(fake2, "x", "terms_extract", schema, pl, key="t", max_tokens=100)
    assert res["status"] == "truncated" and res["attempts"] == 2


def test_cost_and_plan():
    u = Usage(input=1_000_000, cache_write=0, cache_read=0, output=100_000)
    assert sh.cost_of(u, "claude-opus-5-5") == pytest.approx(4 + 2)
    assert sh.cost_of(u, "claude-opus-5-5", batch=True) == pytest.approx(3)
    u2 = Usage(input=0, cache_write=1_000_000, cache_read=1_000_000, output=0)
    assert sh.cost_of(u2, "claude-haiku-5-5") == pytest.approx(0.125 + 0.01)
    custom = {"my-model": {"input": 1, "output": 1, "cache_write": 1, "cache_read": 1, "currency": "RMB"}}
    assert sh.pricing_for("my-model", custom)["currency"] == "RMB" and sh.cost_of(u, "my-model", custom) == pytest.approx(1.1)
    assert sh.pricing_for("unknown")["currency"] == "USD"
    payloads = [(f"k{i}", {"section": "s", "n_sentences": 1, "sentences": "[S1] " + "word " * 100}) for i in range(10)]
    plan = sh.plan_task("terms_extract", payloads, max_tokens=2000, out_low=500)
    assert plan["items"] == 10 and plan["cost_low"] < plan["cost_high"] and plan["cost_batch_high"] == pytest.approx(plan["cost_high"] / 2, rel=1e-3)
    assert plan["tokens"]["output_high"] == 20000 and plan["tokens"]["output_low"] == 5000 and plan["tokens"]["system"] > 100
    assert plan["per_item_high"] * 10 == pytest.approx(plan["cost_high"], rel=1e-3)
    empty = sh.plan_task("terms_extract", [], max_tokens=2000, out_low=500)
    assert empty["items"] == 0 and empty["cost_high"] == 0


def test_batch_round_trip_with_fake_client():
    fake = FakeChatClient(default=json.dumps({"terms": []}))
    p = sh.load_prompt("terms_extract")
    reqs = [sh.build_request(p, {"section": "s", "n_sentences": 0, "sentences": ""}, task="t", key=f"k{i}", schema=None, max_tokens=10) for i in range(3)]
    bid, results = sh.run_batch(fake, reqs, wait=False)
    assert bid.startswith("fakebatch") and set(results) == {"k0", "k1", "k2"}
    assert sh.collect_batch(fake, bid, wait=False) is not None
    with pytest.raises(KeyError):
        sh.collect_batch(fake, "nope", wait=False)
