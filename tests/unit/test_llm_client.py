"""atlas.llm.client: the no-network guard, AnthropicClient request construction against a fake SDK object
(system blocks with cache_control on the last block, output_config shape, no thinking / temperature / top_p, the
beta refusal fallback), response and error mapping, batch parameters, DeepSeekClient peak hours / body / parsing
through an injected opener, make_client from the configuration. No test reaches the network."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS

import anthropic
import httpx2
import pytest
import yaml

from atlas.config import init_project, load_project
from atlas.llm import client as C
from atlas.llm.base import NO_NETWORK_ENV, ChatRequest, FakeChatClient, NetworkForbidden
from atlas.llm.client import (FALLBACK_BETA, AnthropicClient, DeepSeekClient, LlmError, choose_provider, custom_id_for,
                              describe_secret, fx_rates, is_peak, key_env_of, make_client, make_clients,
                              message_to_response, models_of, resolve_secret, split_of)

URL = "https://api.anthropic.com/v1/messages"


class StubRegistry:
    """A `winreg` stand-in: {(hive attribute name, path): {name: value}}; OpenKey is a context manager."""
    HKEY_CURRENT_USER = "HKCU"
    HKEY_LOCAL_MACHINE = "HKLM"

    def __init__(self, values: dict[tuple[str, str], dict[str, str]]) -> None:
        self.values = values
        self.opened: list[tuple[str, str]] = []

    def OpenKey(self, hive, path):                                             # noqa: N802 - winreg API
        reg = self

        class _Key:
            def __enter__(self_inner):
                reg.opened.append((hive, path))
                if (hive, path) not in reg.values:
                    raise OSError("no such key")
                return (hive, path)

            def __exit__(self_inner, *a):
                return False
        return _Key()

    def QueryValueEx(self, key, name):                                         # noqa: N802
        try:
            return self.values[key][name], 1
        except KeyError:
            raise FileNotFoundError(name) from None


@pytest.fixture
def no_registry(monkeypatch):
    """Isolate the resolver from the real Windows registry of the machine running the suite."""
    monkeypatch.setattr(C, "winreg", None)


def req(**kw) -> ChatRequest:
    base = dict(model="claude-opus-5-5", system=("rules", "scheme"), user="payload", max_tokens=1000,
                output_schema={"type": "object", "properties": {"a": {"type": "integer"}}}, effort=None,
                task="terms_extract", key="chunk-1")
    base.update(kw)
    return ChatRequest(**base)


def msg(text: str = '{"a": 1}', stop: str = "end_turn", **kw) -> NS:
    content = [NS(type="thinking", thinking=""), NS(type="text", text=text)] if text is not None else []
    base = dict(id="msg_01", model="claude-opus-5-5", content=content, stop_reason=stop, stop_details=None,
                usage=NS(input_tokens=120, cache_creation_input_tokens=30, cache_read_input_tokens=400, output_tokens=12))
    base.update(kw)
    return NS(**base)


class FakeSDK:
    """Records every call; answers come from `script` (a list of return values or exceptions, consumed in order)."""

    def __init__(self, script: list | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.script = list(script or [msg()])
        self.messages = NS(create=lambda **kw: self._answer("messages.create", kw),
                           count_tokens=lambda **kw: self._record("count_tokens", kw, NS(input_tokens=42)),
                           batches=NS(create=lambda **kw: self._record("batches.create", kw, NS(id="msgbatch_1")),
                                      retrieve=lambda bid: self._record("batches.retrieve", {"id": bid}, self.batch),
                                      results=lambda bid: self._record("batches.results", {"id": bid}, iter(self.results))))
        self.beta = NS(messages=NS(create=lambda **kw: self._answer("beta.messages.create", kw)))
        self.batch = NS(processing_status="ended", request_counts=NS(processing=0, succeeded=2, errored=1, canceled=0, expired=0))
        self.results = [NS(custom_id="chunk-1", result=NS(type="succeeded", message=msg())),
                        NS(custom_id="k_bad", result=NS(type="errored", error=NS(type="error", error=NS(type="invalid_request", message="too long")))),
                        NS(custom_id="gone", result=NS(type="expired", error=None))]

    def _record(self, name: str, kw: dict, value):
        self.calls.append((name, kw))
        return value

    def _answer(self, name: str, kw: dict):
        self.calls.append((name, kw))
        out = self.script.pop(0) if self.script else msg()
        if isinstance(out, Exception):
            raise out
        return out


def status_error(cls, code: int, message: str = "boom"):
    return cls(message, response=httpx2.Response(code, request=httpx2.Request("POST", URL)), body=None)


@pytest.fixture
def online(monkeypatch):
    monkeypatch.delenv(NO_NETWORK_ENV, raising=False)


def client(sdk: FakeSDK, **kw) -> AnthropicClient:
    base = dict(model="claude-opus-5-5", effort="medium", retry_sleep=0.0)
    base.update(kw)
    return AnthropicClient(sdk=sdk, **base)


# ---- guard --------------------------------------------------------------------------------------------------------------
def test_guard_blocks_every_network_method_before_the_sdk_is_touched():
    sdk = FakeSDK()
    c = client(sdk)
    for call in (lambda: c.complete(req()), lambda: c.count_tokens(req()), lambda: c.submit_batch([req()]),
                 lambda: c.batch_status("b"), lambda: c.batch_results("b")):
        with pytest.raises(NetworkForbidden, match="ATLAS_LLM_NO_NETWORK"):
            call()
    assert sdk.calls == []
    ds = DeepSeekClient("deepseek-flash", opener=lambda *a: (200, b"{}"))
    with pytest.raises(NetworkForbidden):
        ds.complete(req())
    with pytest.raises(NetworkForbidden):
        ds.balance()


# ---- request construction -----------------------------------------------------------------------------------------
def test_request_construction_cache_control_output_config_and_beta_fallback(online):
    sdk = FakeSDK()
    c = client(sdk)
    r = c.complete(req())
    assert r.ok and r.text == '{"a": 1}'
    name, kw = sdk.calls[0]
    assert name == "beta.messages.create"
    assert kw["betas"] == [FALLBACK_BETA] and kw["fallbacks"] == "default"
    assert kw["model"] == "claude-opus-5-5" and kw["max_tokens"] == 1000
    assert kw["system"] == [{"type": "text", "text": "rules"},
                            {"type": "text", "text": "scheme", "cache_control": {"type": "ephemeral"}}]
    assert kw["messages"] == [{"role": "user", "content": "payload"}]
    assert kw["output_config"] == {"effort": "medium", "format": {"type": "json_schema", "schema": req().output_schema}}
    for forbidden in ("thinking", "temperature", "top_p", "tool_choice", "tools", "stream"):
        assert forbidden not in kw
    assert set(kw) == {"betas", "fallbacks", "model", "max_tokens", "system", "messages", "output_config"}
    # per-request effort overrides the client's; no schema → no format; no effort anywhere → no output_config
    p = c.build_params(req(effort="high", output_schema=None))
    assert p["output_config"] == {"effort": "high"}
    p2 = AnthropicClient("claude-opus-5-5", effort=None, sdk=sdk).build_params(req(output_schema=None))
    assert "output_config" not in p2 and "system" in p2
    p3 = c.build_params(req(system=()))
    assert "system" not in p3


def test_fallback_can_be_switched_off(online):
    sdk = FakeSDK()
    c = client(sdk, refusal_fallback=False)
    c.complete(req())
    name, kw = sdk.calls[0]
    assert name == "messages.create" and "fallbacks" not in kw and "betas" not in kw
    assert kw["system"][-1]["cache_control"] == {"type": "ephemeral"}


def test_response_mapping_and_refusal_pass_through(online):
    sdk = FakeSDK([msg(), msg(text=None, stop="refusal", stop_details=NS(category="cyber", explanation=None)),
                   msg(text="{", stop="max_tokens", model="claude-opus-4-8")])
    c = client(sdk)
    r = c.complete(req())
    assert r.usage.input == 120 and r.usage.cache_write == 30 and r.usage.cache_read == 400 and r.usage.output == 12
    assert r.usage.model_reported == "claude-opus-5-5" and r.raw_id == "msg_01" and r.stop_reason == "end_turn"
    assert r.latency_ms >= 0 and r.error is None
    refused = c.complete(req())
    assert refused.stop_reason == "refusal" and refused.text == "" and refused.error is None and not refused.ok
    cut = c.complete(req())
    assert cut.stop_reason == "max_tokens" and cut.text == "{" and cut.usage.model_reported == "claude-opus-4-8"
    assert message_to_response(NS(id=None, model=None, content=None, stop_reason=None, stop_details=None,
                                  usage=NS(input_tokens=None, output_tokens=None))).stop_reason == "end_turn"


def test_error_chain_retry_fatal_item(online):
    rate = status_error(anthropic.RateLimitError, 429)
    sdk = FakeSDK([rate, msg()])
    r = client(sdk).complete(req())
    assert r.ok and len(sdk.calls) == 2                                        # one outer retry after the SDK's own
    sdk2 = FakeSDK([status_error(anthropic.InternalServerError, 500), status_error(anthropic.OverloadedError, 529)])
    r2 = client(sdk2).complete(req())
    assert r2.stop_reason == "error" and r2.error.startswith("OverloadedError") and len(sdk2.calls) == 2
    sdk3 = FakeSDK([anthropic.APIConnectionError(request=httpx2.Request("POST", URL)), msg()])
    assert client(sdk3).complete(req()).ok
    with pytest.raises(LlmError, match="AuthenticationError"):
        client(FakeSDK([status_error(anthropic.AuthenticationError, 401, "bad key")])).complete(req())
    with pytest.raises(LlmError, match="NotFoundError"):
        client(FakeSDK([status_error(anthropic.NotFoundError, 404, "no such model")])).complete(req())
    sdk4 = FakeSDK([status_error(anthropic.BadRequestError, 400, "prompt is too long")])
    r4 = client(sdk4).complete(req())
    assert r4.stop_reason == "error" and "prompt is too long" in r4.error and len(sdk4.calls) == 1
    with pytest.raises(RuntimeError, match="programming error"):
        client(FakeSDK([RuntimeError("programming error")])).complete(req())


def test_count_tokens_and_lazy_sdk_key_handling(online, no_registry, monkeypatch):
    sdk = FakeSDK()
    assert client(sdk).count_tokens(req()) == 42
    name, kw = sdk.calls[0]
    assert name == "count_tokens" and set(kw) == {"model", "messages", "system", "output_config"}
    assert kw["output_config"] == {"format": {"type": "json_schema", "schema": req().output_schema}}
    assert kw["system"][-1]["cache_control"] == {"type": "ephemeral"}
    sdk.calls.clear()
    client(sdk).count_tokens(req(output_schema=None))
    assert "output_config" not in sdk.calls[0][1]
    monkeypatch.delenv("MY_KEY", raising=False)
    with pytest.raises(LlmError, match="MY_KEY"):
        AnthropicClient("claude-opus-5-5", api_key_env="MY_KEY").complete(req())
    # the resolved key is handed to the SDK constructor explicitly (never read by the SDK from ANTHROPIC_API_KEY)
    monkeypatch.setenv("MY_KEY", "sk-test-value-not-real")
    seen: dict = {}

    class _Anthropic:
        def __init__(self, **kw):
            seen.update(kw)
            self.messages = sdk.messages
            self.beta = sdk.beta
    monkeypatch.setattr(anthropic, "Anthropic", _Anthropic)
    c = AnthropicClient("claude-opus-5-5", api_key_env="MY_KEY", max_retries=3)
    assert c.complete(req()).ok and seen == {"api_key": "sk-test-value-not-real", "max_retries": 3}


# ---- secrets, split, factories ------------------------------------------------------------------------------------
def test_resolve_secret_order_env_then_user_then_system_registry(monkeypatch):
    user_env = ("HKCU", r"Environment")
    sys_env = ("HKLM", r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")
    reg = StubRegistry({user_env: {"RS_TEST_KEY": "from-user-registry"}, sys_env: {"RS_TEST_KEY": "from-system",
                                                                                   "RS_SYS_ONLY": "sys-value"}})
    monkeypatch.setattr(C, "winreg", reg)
    monkeypatch.setenv("RS_TEST_KEY", "from-process")
    assert resolve_secret("RS_TEST_KEY") == ("from-process", "env") and reg.opened == []
    monkeypatch.delenv("RS_TEST_KEY")
    assert resolve_secret("RS_TEST_KEY") == ("from-user-registry", "registry") and reg.opened == [user_env]
    assert resolve_secret("RS_SYS_ONLY") == ("sys-value", "registry")
    assert resolve_secret("RS_NOWHERE") == (None, None)
    monkeypatch.setenv("RS_TEST_KEY", "")
    assert resolve_secret("RS_TEST_KEY")[1] == "registry"                      # an empty variable does not count
    assert describe_secret("RS_SYS_ONLY") == "key: found (9 chars) via registry"
    monkeypatch.setenv("RS_TEST_KEY", "abc")
    assert describe_secret("RS_TEST_KEY") == "key: found (3 chars) via env"
    assert describe_secret("RS_NOWHERE") == "key: not found (RS_NOWHERE)"
    for text in (describe_secret("RS_SYS_ONLY"), describe_secret("RS_TEST_KEY")):
        assert "sys-value" not in text and "abc" not in text
    monkeypatch.setattr(C, "winreg", None)                                     # non-Windows: process environment only
    assert resolve_secret("RS_SYS_ONLY") == (None, None) and resolve_secret("RS_TEST_KEY") == ("abc", "env")


def test_choose_provider_is_deterministic_and_proportional():
    split = {"anthropic": 0.9, "deepseek": 0.1}
    keys = [f"S1{i:016x}" for i in range(20_000)]
    picks = [choose_provider(k, split) for k in keys]
    share = picks.count("deepseek") / len(picks)
    assert 0.085 < share < 0.115 and picks == [choose_provider(k, split) for k in keys]
    assert choose_provider("x", {"anthropic": 9, "deepseek": 1}) == choose_provider("x", split)   # shares normalise
    assert {choose_provider(k, {"anthropic": 1.0}) for k in keys[:100]} == {"anthropic"}
    assert {choose_provider(k, {"anthropic": 1.0, "deepseek": 0.0}) for k in keys[:100]} == {"anthropic"}
    assert choose_provider("", split) in split
    with pytest.raises(ValueError):
        choose_provider("x", {})
    thirds = {"a": 1, "b": 1, "c": 1}
    counts = {p: [choose_provider(k, thirds) for k in keys].count(p) / len(keys) for p in thirds}
    assert all(0.31 < v < 0.36 for v in counts.values())


def test_split_models_fx_and_key_env_from_configuration(tmp_path: Path):
    project = init_project(tmp_path / "p")
    cfg = project.get("llm")
    assert split_of(cfg) == {"anthropic": pytest.approx(0.9), "deepseek": pytest.approx(0.1)}
    assert split_of({"provider": "fake"}) == {"fake": 1.0} and split_of({"provider": "fake", "split": {"x": 0}}) == {"fake": 1.0}
    assert split_of({"provider": "a", "split": {"a": 3, "b": 1}}) == {"a": 0.75, "b": 0.25}
    assert models_of(cfg) == {"anthropic": "claude-opus-5-5", "deepseek": "deepseek-flash"}
    assert models_of({"provider": "deepseek", "model": "deepseek-x", "models": {"anthropic": "claude-haiku-5-5"}}) == {
        "anthropic": "claude-haiku-5-5", "deepseek": "deepseek-x"}
    assert fx_rates(cfg, "USD") == {"USD": 1.0, "RMB": pytest.approx(0.14)}
    assert fx_rates(cfg, "RMB") == {"USD": pytest.approx(1 / 0.14), "RMB": 1.0}
    assert fx_rates({}, "USD") == {"USD": 1.0}
    assert key_env_of(cfg, "anthropic") == "RS_ANTHROPIC_API_KEY" and key_env_of(cfg, "deepseek") == "RS_DEEPSEEK_API_KEY"
    assert key_env_of({"api_key_env": "A", "deepseek": {"api_key_env": "D"}}, "deepseek") == "D"
    assert key_env_of({}, "anthropic") == "RS_ANTHROPIC_API_KEY" and key_env_of({}, "deepseek") == "RS_DEEPSEEK_API_KEY"


def test_make_clients_builds_the_primary_and_every_provider_of_the_split(tmp_path: Path):
    project = init_project(tmp_path / "p")
    clients = make_clients(project)
    assert list(clients) == ["anthropic", "deepseek"]
    assert isinstance(clients["anthropic"], AnthropicClient) and clients["anthropic"].model == "claude-opus-5-5"
    assert isinstance(clients["deepseek"], DeepSeekClient) and clients["deepseek"].model == "deepseek-flash"
    assert clients["anthropic"].api_key_env == "RS_ANTHROPIC_API_KEY" and clients["deepseek"].api_key_env == "RS_DEEPSEEK_API_KEY"
    # project.yaml deep-merges into the defaults: a provider is dropped from the split by giving it share 0
    (project.root / "project.yaml").write_text(yaml.safe_dump({"llm": {"split": {"deepseek": 0}}}), encoding="utf-8")
    assert list(make_clients(load_project(project.root))) == ["anthropic"]
    assert split_of(load_project(project.root).get("llm")) == {"anthropic": 1.0}
    (project.root / "project.yaml").write_text(yaml.safe_dump({"llm": {"provider": "fake", "split": None}}), encoding="utf-8")
    only = make_clients(load_project(project.root))
    assert list(only) == ["fake"] and isinstance(only["fake"], FakeChatClient)


def test_batch_parameters_status_and_results(online):
    sdk = FakeSDK()
    c = client(sdk)
    bid = c.submit_batch([req(), req(key="chunk 7 / 第二章")])
    assert bid == "msgbatch_1"
    name, kw = sdk.calls[0]
    assert name == "batches.create" and [r["custom_id"] for r in kw["requests"]] == ["chunk-1", custom_id_for("chunk 7 / 第二章")]
    params = kw["requests"][0]["params"]
    assert params == c.build_params(req()) and "fallbacks" not in params and "betas" not in params
    assert params["system"][-1]["cache_control"] == {"type": "ephemeral"} and params["output_config"]["effort"] == "medium"
    with pytest.raises(ValueError, match="duplicate"):
        c.submit_batch([req(), req()])
    with pytest.raises(ValueError, match="empty"):
        c.submit_batch([])
    st = c.batch_status("msgbatch_1")
    assert (st.state, st.n_requests, st.n_done, st.n_failed) == ("ended", 3, 2, 1)
    res = c.batch_results("msgbatch_1")
    assert res["chunk-1"].ok and res["chunk-1"].text == '{"a": 1}'
    assert res["k_bad"].stop_reason == "error" and res["k_bad"].error == "errored: too long"
    assert res["gone"].stop_reason == "error" and res["gone"].error.startswith("expired")
    sdk.batch = None
    sdk.messages.batches.retrieve = lambda bid: (_ for _ in ()).throw(status_error(anthropic.NotFoundError, 404, "no batch"))
    with pytest.raises(LlmError, match="no batch"):
        c.batch_status("msgbatch_9")


def test_custom_id_for_rules():
    assert custom_id_for("S1abcdef0123456789") == "S1abcdef0123456789" and custom_id_for("C_slug-1") == "C_slug-1"
    assert custom_id_for("x" * 64) == "x" * 64 and custom_id_for("x" * 65).startswith("k_")
    assert custom_id_for("a.b") != "a.b" and custom_id_for("") .startswith("k_")
    assert custom_id_for("第二章") == custom_id_for("第二章") and custom_id_for("第二章") != custom_id_for("第三章")


# ---- DeepSeek ----------------------------------------------------------------------------------------------------------
def test_deepseek_peak_hours():
    assert is_peak(datetime(2026, 10, 19, 2, 0, tzinfo=timezone.utc))          # Monday 10:00 UTC+8
    assert is_peak(datetime(2026, 10, 19, 9, 59, tzinfo=timezone.utc))         # Monday 17:59
    assert not is_peak(datetime(2026, 10, 19, 5, 0, tzinfo=timezone.utc))      # Monday 13:00 (lunch)
    assert not is_peak(datetime(2026, 10, 19, 10, 0, tzinfo=timezone.utc))     # Monday 18:00
    assert not is_peak(datetime(2026, 10, 24, 2, 0, tzinfo=timezone.utc))      # Saturday
    assert not is_peak(datetime(2026, 10, 18, 2, 0, tzinfo=timezone.utc))      # Sunday
    assert is_peak(datetime(2026, 10, 19, 2, 0))                               # naive = UTC
    ds = DeepSeekClient("deepseek-flash", peak_multiplier=3.0)
    assert ds.price_multiplier(datetime(2026, 10, 19, 2, 0, tzinfo=timezone.utc)) == 3.0
    assert ds.price_multiplier(datetime(2026, 10, 24, 2, 0, tzinfo=timezone.utc)) == 1.0
    assert not DeepSeekClient("m", peak={"tz_offset_hours": 0, "weekdays": [5], "hours": [[0, 24]]}).is_peak(
        datetime(2026, 10, 19, 2, 0, tzinfo=timezone.utc))


def test_deepseek_body_parse_retries_and_balance(online, no_registry, monkeypatch):
    monkeypatch.setenv("ATLAS_TEST_DS_KEY", "test-key-not-real")
    answers = [(429, b"slow down"),
               (200, b'{"id": "ds1", "model": "deepseek-flash", "choices": [{"message": {"content": "{\\"a\\": 1}"}, "finish_reason": "stop"}],'
                     b' "usage": {"prompt_tokens": 100, "completion_tokens": 7, "prompt_cache_hit_tokens": 60, "prompt_cache_miss_tokens": 40}}')]
    seen: list[tuple[str, dict | None, dict]] = []

    def opener(url: str, data: bytes | None, headers: dict) -> tuple[int, bytes]:
        import json
        seen.append((url, json.loads(data) if data else None, headers))
        return answers.pop(0)
    ds = DeepSeekClient("deepseek-flash", base_url="https://api.deepseek.com/", api_key_env="ATLAS_TEST_DS_KEY",
                        extra_body={"thinking": {"type": "disabled"}}, opener=opener, retry_sleep=0.0)
    r = ds.complete(req(model="deepseek-flash"))
    assert r.ok and r.text == '{"a": 1}' and r.raw_id == "ds1" and r.usage.model_reported == "deepseek-flash"
    assert (r.usage.input, r.usage.cache_read, r.usage.output) == (40, 60, 7)
    assert len(seen) == 2 and seen[0][0] == "https://api.deepseek.com/chat/completions"
    body = seen[0][1]
    assert body["messages"] == [{"role": "system", "content": "rules\n\nscheme"}, {"role": "user", "content": "payload"}]
    assert body["response_format"] == {"type": "json_object"} and body["thinking"] == {"type": "disabled"}
    assert body["max_tokens"] == 1000 and body["stream"] is False and "temperature" not in body
    assert seen[0][2]["Authorization"].startswith("Bearer ") and "test-key" not in str(r)
    assert "response_format" not in ds.body(req(output_schema=None))
    assert DeepSeekClient._parse(b'{"choices": [{"message": {"content": "x"}, "finish_reason": "length"}], "usage": {"prompt_tokens": 5}}', 1).stop_reason == "max_tokens"
    assert DeepSeekClient._parse(b'{"choices": [{"message": {}, "finish_reason": "content_filter"}]}', 1).stop_reason == "refusal"
    assert DeepSeekClient._parse(b"garbage", 1).stop_reason == "error"
    answers[:] = [(500, b"err")] * 3
    assert ds.complete(req(model="deepseek-flash")).error.startswith("HTTP 500")
    answers[:] = [(401, b"bad key")]
    with pytest.raises(LlmError, match="401"):
        ds.complete(req(model="deepseek-flash"))
    answers[:] = [(400, b"bad request")]
    assert ds.complete(req(model="deepseek-flash")).error.startswith("HTTP 400")
    answers[:] = [(200, b'{"balance_infos": [{"currency": "CNY", "total_balance": "12.50"}]}')]
    assert ds.balance() == 12.5 and seen[-1][0].endswith("/user/balance") and seen[-1][1] is None
    answers[:] = [(500, b"")]
    assert ds.balance() is None
    assert ds.count_tokens(req()) is None and ds.batch_status("x").state == "unknown" and ds.batch_results("x") == {}
    with pytest.raises(NotImplementedError):
        ds.submit_batch([req()])
    assert ds.batch_api is False and AnthropicClient("m").batch_api is True
    monkeypatch.delenv("ATLAS_TEST_DS_KEY")
    with pytest.raises(LlmError, match="ATLAS_TEST_DS_KEY"):
        ds.complete(req())


# ---- factory -------------------------------------------------------------------------------------------------------------
def test_make_client_from_configuration(tmp_path: Path):
    project = init_project(tmp_path / "p")
    c = make_client(project)
    assert isinstance(c, AnthropicClient) and c.model == "claude-opus-5-5" and c.effort == "medium"
    assert c.max_retries == 2 and c.api_key_env == "RS_ANTHROPIC_API_KEY" and c.refusal_fallback and c.provider == "anthropic"
    assert isinstance(make_client(project, "fake"), FakeChatClient)
    assert make_client(project, "deepseek").model == "deepseek-flash"           # llm.models for a non-primary provider
    pf = project.root / "project.yaml"
    pf.write_text(yaml.safe_dump({"llm": {"provider": "deepseek", "model": "deepseek-reasoner", "refusal_fallback": False,
                                          "deepseek": {"extra_body": {"x": 1}, "peak_multiplier": 1.5}}}), encoding="utf-8")
    ds = make_client(load_project(project.root))
    assert isinstance(ds, DeepSeekClient) and ds.model == "deepseek-reasoner" and ds.extra_body == {"x": 1}
    assert ds.peak_multiplier == 1.5 and ds.api_key_env == "RS_DEEPSEEK_API_KEY" and ds.base_url == "https://api.deepseek.com"
    pf.write_text(yaml.safe_dump({"llm": {"provider": "anthropic", "model": "claude-haiku-5-5", "effort": "low",
                                          "api_key_env": "MY_KEY", "refusal_fallback": False, "max_retries": 5}}), encoding="utf-8")
    a = make_client(load_project(project.root))
    assert a.model == "claude-haiku-5-5" and a.effort == "low" and a.api_key_env == "MY_KEY" and not a.refusal_fallback and a.max_retries == 5
    with pytest.raises(ValueError, match="unknown llm.provider"):
        make_client(project, "openai")
