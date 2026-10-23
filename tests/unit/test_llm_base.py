"""atlas.llm.base: request hash, the fake client, the no-network guard helpers."""
from __future__ import annotations

from atlas.llm.base import (NO_NETWORK_ENV, ChatClient, ChatRequest, ChatResponse, FakeChatClient, Usage,
                            canonical_request, chunked, estimate_tokens, network_forbidden, request_hash)


def req(**kw) -> ChatRequest:
    base = dict(model="claude-opus-5-5", system=("stable prompt",), user="payload", max_tokens=1000,
                output_schema={"type": "object"}, effort="medium", task="terms_extract", key="chunk-1")
    base.update(kw)
    return ChatRequest(**base)


def test_request_hash_ignores_bookkeeping_fields_and_is_stable():
    a = request_hash("anthropic", req())
    assert a == request_hash("anthropic", req(task="other", key="other-key"))
    assert a != request_hash("anthropic", req(user="payload 2"))
    assert a != request_hash("anthropic", req(effort="high"))
    assert a != request_hash("deepseek", req())
    assert "task" not in canonical_request("anthropic", req()) and "key" not in canonical_request("anthropic", req())


def test_fake_client_answers_by_key_then_task_then_default():
    fake = FakeChatClient({"chunk-1": '{"terms": []}', "terms_extract": '{"terms": [1]}'}, default="{}")
    assert isinstance(fake, ChatClient)
    assert fake.complete(req()).text == '{"terms": []}'
    assert fake.complete(req(key="chunk-2")).text == '{"terms": [1]}'
    assert fake.complete(req(key="x", task="other")).text == "{}"
    assert len(fake.requests) == 3
    r = fake.complete(req())
    assert r.ok and r.usage.input > 0 and r.usage.output > 0 and r.usage.model_reported == "claude-opus-5-5"


def test_fake_client_callable_can_simulate_truncation_and_refusal():
    fake = FakeChatClient({
        "trunc": lambda r: ChatResponse(text='{"terms": [', stop_reason="max_tokens"),
        "refuse": lambda r: ChatResponse(text="", stop_reason="refusal"),
    })
    t = fake.complete(req(key="trunc"))
    assert t.stop_reason == "max_tokens" and not t.ok and t.usage.input > 0
    assert fake.complete(req(key="refuse")).stop_reason == "refusal"


def test_fake_batches_resolve_immediately_and_are_keyed_by_request_key():
    fake = FakeChatClient({"a": "A", "b": "B"})
    bid = fake.submit_batch([req(key="a"), req(key="b")])
    st = fake.batch_status(bid)
    assert st.state == "ended" and st.n_requests == 2 and st.n_done == 2 and st.n_failed == 0
    res = fake.batch_results(bid)
    assert {k: v.text for k, v in res.items()} == {"a": "A", "b": "B"}
    assert fake.batch_status("nope").state == "unknown"


def test_network_guard_env(monkeypatch):
    monkeypatch.delenv(NO_NETWORK_ENV, raising=False)
    assert not network_forbidden()
    monkeypatch.setenv(NO_NETWORK_ENV, "1")
    assert network_forbidden()


def test_estimate_tokens_and_chunked():
    assert estimate_tokens("") == 1
    assert estimate_tokens("a" * 400) == 101
    assert estimate_tokens("中" * 15) == 11
    assert chunked(range(5), 2) == [[0, 1], [2, 3], [4]]
    assert chunked([], 3) == []
    u = Usage(input=10, cache_write=5, cache_read=20, output=3)
    assert u.total_input == 35
