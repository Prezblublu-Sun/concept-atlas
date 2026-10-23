"""Providers (increment 4 §1.2): `AnthropicClient` (the Anthropic Python SDK), `DeepSeekClient` (urllib, kept so
a project can switch or split providers), the API-key resolver, the provider split and the factories
`make_client` / `make_clients` from the `llm` block of the configuration.

Rules shared by both providers:
- the guard: every method that would reach the network calls `_require_network()` first, which raises
  `NetworkForbidden` when ATLAS_LLM_NO_NETWORK is set — before the SDK object or urllib is touched, so the test
  suite (tests/conftest.py sets the variable) can never spend money;
- the key is resolved by `resolve_secret(name)` from the variable named by `api_key_env` (default
  `RS_ANTHROPIC_API_KEY`, deliberately not `ANTHROPIC_API_KEY`, which Claude Code itself would pick up): the
  process environment first, then on Windows the user environment in the registry (`HKCU\\Environment`, because
  a desktop app's host process keeps a stale environment until it is fully restarted), then the system
  environment (`HKLM\\SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Environment`); elsewhere only the process
  environment. The value is passed explicitly to the SDK and is never printed, logged or stored — callers may
  only say "found (N chars) via env|registry";
- a provider never retries a refusal and never changes a request's content: the task layer owns the policy.

Provider split (user policy 2026-10-23: Claude API first, about 9:1 Claude:DeepSeek, settings kept so the two
can be compared): `llm.split: {anthropic: 0.9, deepseek: 0.1}`; `choose_provider(key, split)` maps the sha1 of an
item key onto the cumulative shares, so the same item goes to the same provider in every run (no failover unless
`llm.failover: true`). `make_clients(project)` builds one client per provider of the split (plus the primary).

AnthropicClient — request construction (verified against anthropic 2.1.0 by `inspect.signature`):
- `messages.create(model, max_tokens, system=[{type: text, text, cache_control: {type: ephemeral}} on the LAST
  block only], messages=[{role: user, content}], output_config={effort, format})`. `format` =
  `{type: json_schema, schema}` only when the request has a schema. Never `thinking`, `temperature` or `top_p`:
  Claude Opus 5.5 thinks always (depth = `output_config.effort`) and rejects sampling parameters.
- Refusal fallback: `client.beta.messages.create` accepts `fallbacks` (`Literal["default"] | Iterable[...]`)
  together with `output_config` (checked on the installed SDK: both are parameters of the beta `create`), so live
  calls go through the beta endpoint with `betas=["server-side-fallback-2026-07-01"]` and
  `fallbacks="default"` when `refusal_fallback` is on (default; `llm.refusal_fallback: false` switches back to
  `messages.create`). The Batches API rejects `fallbacks`, so batch requests never carry it. A response whose
  final `stop_reason` is `refusal` (the whole chain declined) is returned unchanged; the task layer records
  `failed:refused` and never retries. The model that actually answered is kept in `usage.model_reported`, and
  the ledger prices by it when it is in the price table (a fallback model bills at its own rates).
- Errors (the SDK's typed chain): `RateLimitError`, `APIConnectionError` (incl. timeouts) and `APIStatusError`
  with status >= 500 (overloaded, internal) are retryable — the SDK retries them itself (`max_retries`) and this
  client makes one more outer attempt; `AuthenticationError`, `PermissionDeniedError` and `NotFoundError` are
  fatal for the whole run (`LlmError`, nothing would succeed); every other API error (`BadRequestError` for a
  single oversized request, 413, 422 …) is returned as a `stop_reason="error"` response so the stage can move on
  and the ledger keeps the attempt.
- `count_tokens` through `messages.count_tokens` (same system / messages / output format as the call).
- Batches: `messages.batches.create(requests=[{custom_id, params}])` where `params` is the same dict as a
  `messages.create` call (no `fallbacks`). The API requires `custom_id` to match `[A-Za-z0-9_-]{1,64}`, so
  `custom_id_for(key)` keeps a conforming key verbatim and otherwise uses `k_<sha256 prefix>`; `batch_results`
  is keyed by that custom id (identical to the request key whenever the key conforms — the usual case: chunk
  shas, concept ids) and `atlas.llm.batch` maps the rows back through the same function.

DeepSeekClient — urllib POST `{base_url}/chat/completions` (JSON mode when a schema is given, `extra_body` merged
into the body), `finish_reason` stop | length | content_filter → end_turn | max_tokens | refusal, usage from
`prompt_cache_miss_tokens` / `prompt_cache_hit_tokens`; no batch API (`batch_api = False`; `submit_batch` raises
NotImplementedError, the runner calls its share live); peak hours UTC+8 weekdays 09–12 / 14–18 (`is_peak`,
`price_multiplier`) for `prefer_idle` and the cost; a balance probe for `balance_probes`. Currency RMB.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta, timezone

from atlas.config import Project
from atlas.llm.base import (BatchStatus, ChatClient, ChatRequest, ChatResponse, FakeChatClient, NetworkForbidden,
                            Usage, network_forbidden, now_ms)

try:
    import winreg
except ImportError:                                  # not Windows
    winreg = None

log = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"
CUSTOM_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
PROVIDERS = ("anthropic", "deepseek", "fake")
DEFAULT_KEY_ENV = {"anthropic": "RS_ANTHROPIC_API_KEY", "deepseek": "RS_DEEPSEEK_API_KEY"}
DEEPSEEK_PEAK = {"tz_offset_hours": 8, "weekdays": [0, 1, 2, 3, 4], "hours": [[9, 12], [14, 18]]}
_REGISTRY_ENVS = (("HKEY_CURRENT_USER", r"Environment"),
                  ("HKEY_LOCAL_MACHINE", r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"))


class LlmError(RuntimeError):
    """A provider failure that no retry can fix for this run (bad key, unknown model, missing batch)."""


def _require_network() -> None:
    if network_forbidden():
        raise NetworkForbidden("ATLAS_LLM_NO_NETWORK is set: no provider call is allowed in this process")


# ---- secrets ---------------------------------------------------------------------------------------------------------
def _registry_value(hive_name: str, path: str, name: str) -> str | None:
    if winreg is None:
        return None
    try:
        with winreg.OpenKey(getattr(winreg, hive_name), path) as key:
            value, _kind = winreg.QueryValueEx(key, name)
    except OSError:
        return None
    return str(value) if value else None


def resolve_secret(name: str) -> tuple[str | None, str | None]:
    """(value, source) for an environment variable: the process environment (`env`), else the Windows user and
    system environments in the registry (`registry`). The value must not be printed, logged or written."""
    value = os.environ.get(name)
    if value:
        return value, "env"
    for hive, path in _REGISTRY_ENVS:
        value = _registry_value(hive, path, name)
        if value:
            return value, "registry"
    return None, None


def describe_secret(name: str) -> str:
    """The only sentence about a key that may be printed: its presence, length and source."""
    value, source = resolve_secret(name)
    if value is None:
        return f"key: not found ({name})"
    return f"key: found ({len(value)} chars) via {source}"


def _require_secret(name: str) -> str:
    value, _source = resolve_secret(name)
    if not value:
        raise LlmError(f"no API key: {name} is not set (process environment or Windows user/system environment)")
    return value


# ---- the provider split -------------------------------------------------------------------------------------------
def choose_provider(key: str, split: dict[str, float]) -> str:
    """The provider of an item: sha1(key) scaled to [0, 1) against the cumulative shares of `split` (providers
    in sorted order, shares normalised). Deterministic: the same key gives the same provider in every run."""
    shares = {p: float(w) for p, w in (split or {}).items() if float(w) > 0}
    if not shares:
        raise ValueError("empty provider split")
    if len(shares) == 1:
        return next(iter(shares))
    total = sum(shares.values())
    x = int(hashlib.sha1((key or "").encode("utf-8")).hexdigest()[:12], 16) / float(1 << 48)
    acc = 0.0
    names = sorted(shares)
    for p in names:
        acc += shares[p] / total
        if x < acc:
            return p
    return names[-1]


def custom_id_for(key: str) -> str:
    """The Message Batches `custom_id` of a request key: the key itself when it conforms, else a sha-derived id."""
    if CUSTOM_ID_RE.match(key or ""):
        return key
    return "k_" + hashlib.sha256((key or "").encode("utf-8")).hexdigest()[:40]


def _text_of(content) -> str:
    for block in content or ():
        if getattr(block, "type", None) == "text":
            return block.text or ""
    return ""


def message_to_response(msg, latency_ms: int = 0) -> ChatResponse:
    """SDK `Message` / `BetaMessage` → ChatResponse (first text block, stop reason, the four usage counters,
    the served model, the message id). Works on any object with the same attributes (tests)."""
    u = msg.usage
    usage = Usage(input=int(getattr(u, "input_tokens", 0) or 0),
                  cache_write=int(getattr(u, "cache_creation_input_tokens", 0) or 0),
                  cache_read=int(getattr(u, "cache_read_input_tokens", 0) or 0),
                  output=int(getattr(u, "output_tokens", 0) or 0),
                  model_reported=getattr(msg, "model", None))
    stop = getattr(msg, "stop_reason", None) or "end_turn"
    if stop == "refusal":
        details = getattr(msg, "stop_details", None)
        log.warning("refusal from %s (category %s)", getattr(msg, "model", "?"),
                    getattr(details, "category", None) if details is not None else None)
    return ChatResponse(text=_text_of(getattr(msg, "content", None)), stop_reason=stop, usage=usage,
                        latency_ms=int(latency_ms), raw_id=getattr(msg, "id", None))


class AnthropicClient:
    provider = "anthropic"
    currency = "USD"
    batch_api = True

    def __init__(self, model: str, effort: str | None = "medium", max_retries: int = 2,
                 api_key_env: str = DEFAULT_KEY_ENV["anthropic"], refusal_fallback: bool = True, sdk=None,
                 retry_sleep: float = 2.0) -> None:
        self.model = model
        self.effort = effort
        self.max_retries = int(max_retries)
        self.api_key_env = api_key_env
        self.refusal_fallback = bool(refusal_fallback)
        self.retry_sleep = float(retry_sleep)
        self._sdk = sdk                          # injected in tests; built lazily otherwise

    # ---- SDK access -----------------------------------------------------------------------------------------------
    def _client(self):
        if self._sdk is None:
            import anthropic
            self._sdk = anthropic.Anthropic(api_key=_require_secret(self.api_key_env), max_retries=self.max_retries)
        return self._sdk

    @staticmethod
    def _error_kind(e: Exception) -> str:
        """retry | fatal | item | raise — the SDK's typed chain, most specific first."""
        import anthropic
        if isinstance(e, (anthropic.RateLimitError, anthropic.APIConnectionError)):
            return "retry"
        if isinstance(e, (anthropic.AuthenticationError, anthropic.PermissionDeniedError, anthropic.NotFoundError)):
            return "fatal"
        if isinstance(e, anthropic.APIStatusError):
            return "retry" if int(getattr(e, "status_code", 0) or 0) >= 500 else "item"
        if isinstance(e, anthropic.APIError):
            return "item"
        return "raise"

    # ---- request construction -----------------------------------------------------------------------------------
    def build_params(self, req: ChatRequest) -> dict:
        """The `messages.create` keyword dict for a request (also the `params` of a batch request)."""
        params: dict = {"model": req.model, "max_tokens": int(req.max_tokens),
                        "messages": [{"role": "user", "content": req.user}]}
        if req.system:
            system = [{"type": "text", "text": s} for s in req.system]
            system[-1]["cache_control"] = {"type": "ephemeral"}
            params["system"] = system
        output_config: dict = {}
        effort = req.effort or self.effort
        if effort:
            output_config["effort"] = effort
        if req.output_schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": req.output_schema}
        if output_config:
            params["output_config"] = output_config
        return params

    # ---- calls ------------------------------------------------------------------------------------------------------
    def complete(self, req: ChatRequest) -> ChatResponse:
        _require_network()
        sdk = self._client()
        params = self.build_params(req)
        t0 = now_ms()
        for attempt in (1, 2):
            try:
                if self.refusal_fallback:
                    msg = sdk.beta.messages.create(betas=[FALLBACK_BETA], fallbacks="default", **params)
                else:
                    msg = sdk.messages.create(**params)
                return message_to_response(msg, now_ms() - t0)
            except Exception as e:                       # noqa: BLE001 - classified below
                kind = self._error_kind(e)
                if kind == "raise":
                    raise
                if kind == "fatal":
                    raise LlmError(f"{type(e).__name__}: {getattr(e, 'message', e)}") from e
                if kind == "retry" and attempt == 1:
                    log.warning("%s on %s (attempt %d); retrying once", type(e).__name__, req.key or req.task, attempt)
                    if self.retry_sleep:
                        time.sleep(self.retry_sleep)
                    continue
                return ChatResponse(text="", stop_reason="error", latency_ms=now_ms() - t0,
                                    error=f"{type(e).__name__}: {getattr(e, 'message', e)}")
        raise AssertionError("unreachable")

    def count_tokens(self, req: ChatRequest) -> int | None:
        _require_network()
        params = self.build_params(req)
        kw = {k: params[k] for k in ("model", "messages", "system") if k in params}
        if "format" in params.get("output_config", {}):
            kw["output_config"] = {"format": params["output_config"]["format"]}
        return int(self._client().messages.count_tokens(**kw).input_tokens)

    # ---- batches ---------------------------------------------------------------------------------------------------
    def submit_batch(self, reqs: Sequence[ChatRequest]) -> str:
        _require_network()
        requests, seen = [], set()
        for r in reqs:
            cid = custom_id_for(r.key)
            if cid in seen:
                raise ValueError(f"duplicate batch request key {r.key!r}")
            seen.add(cid)
            requests.append({"custom_id": cid, "params": self.build_params(r)})
        if not requests:
            raise ValueError("empty batch")
        batch = self._client().messages.batches.create(requests=requests)
        return str(batch.id)

    def batch_status(self, batch_id: str) -> BatchStatus:
        _require_network()
        try:
            b = self._client().messages.batches.retrieve(batch_id)
        except Exception as e:                           # noqa: BLE001
            if self._error_kind(e) == "fatal":
                raise LlmError(f"batch {batch_id}: {type(e).__name__}: {getattr(e, 'message', e)}") from e
            raise
        rc = b.request_counts
        n_failed = int(rc.errored) + int(rc.canceled) + int(rc.expired)
        return BatchStatus(batch_id, str(b.processing_status), n_requests=int(rc.processing) + int(rc.succeeded) + n_failed,
                           n_done=int(rc.succeeded), n_failed=n_failed)

    def batch_results(self, batch_id: str) -> dict[str, ChatResponse]:
        """{custom_id: ChatResponse}; errored / canceled / expired results become `stop_reason="error"`."""
        _require_network()
        out: dict[str, ChatResponse] = {}
        for item in self._client().messages.batches.results(batch_id):
            res = item.result
            if res.type == "succeeded":
                out[item.custom_id] = message_to_response(res.message)
            else:
                err = getattr(res, "error", None)
                detail = getattr(getattr(err, "error", None), "message", None) or getattr(err, "type", None) or res.type
                out[item.custom_id] = ChatResponse(text="", stop_reason="error", error=f"{res.type}: {detail}")
        return out


# ---- DeepSeek -----------------------------------------------------------------------------------------------------
Opener = Callable[[str, bytes | None, dict], tuple[int, bytes]]


def _urllib_open(url: str, data: bytes | None, headers: dict, timeout: float = 120.0) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=data, headers=headers, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as r:
            return int(r.status), r.read()
    except urllib.error.HTTPError as e:
        return int(e.code), e.read()


def is_peak(dt: datetime | None = None, peak: dict | None = None) -> bool:
    """DeepSeek peak pricing window: weekdays 09–12 and 14–18 in UTC+8 (configurable)."""
    cfg = peak or DEEPSEEK_PEAK
    now = dt or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    local = now.astimezone(timezone(timedelta(hours=float(cfg.get("tz_offset_hours", 8)))))
    if local.weekday() not in set(cfg.get("weekdays", [0, 1, 2, 3, 4])):
        return False
    return any(int(a) <= local.hour < int(b) for a, b in cfg.get("hours", [[9, 12], [14, 18]]))


class DeepSeekClient:
    provider = "deepseek"
    currency = "RMB"
    batch_api = False

    def __init__(self, model: str, base_url: str = "https://api.deepseek.com",
                 api_key_env: str = DEFAULT_KEY_ENV["deepseek"], extra_body: dict | None = None,
                 peak: dict | None = None, peak_multiplier: float = 2.0, balance_endpoint: str = "/user/balance",
                 max_retries: int = 2, retry_sleep: float = 2.0, opener: Opener | None = None) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key_env = api_key_env
        self.extra_body = dict(extra_body or {})
        self.peak = dict(peak or DEEPSEEK_PEAK)
        self.peak_multiplier = float(peak_multiplier)
        self.balance_endpoint = balance_endpoint
        self.max_retries = int(max_retries)
        self.retry_sleep = float(retry_sleep)
        self._open: Opener = opener or _urllib_open

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {_require_secret(self.api_key_env)}", "Content-Type": "application/json"}

    def is_peak(self, dt: datetime | None = None) -> bool:
        return is_peak(dt, self.peak)

    def price_multiplier(self, dt: datetime | None = None) -> float:
        return self.peak_multiplier if self.is_peak(dt) else 1.0

    def body(self, req: ChatRequest) -> dict:
        messages = []
        if req.system:
            messages.append({"role": "system", "content": "\n\n".join(req.system)})
        messages.append({"role": "user", "content": req.user})
        b: dict = {"model": req.model, "messages": messages, "max_tokens": int(req.max_tokens), "stream": False}
        if req.output_schema is not None:
            b["response_format"] = {"type": "json_object"}
        b.update(self.extra_body)
        return b

    def complete(self, req: ChatRequest) -> ChatResponse:
        _require_network()
        headers = self._headers()
        data = json.dumps(self.body(req), ensure_ascii=False).encode("utf-8")
        t0 = now_ms()
        last = ""
        for attempt in range(self.max_retries + 1):
            try:
                status, raw = self._open(self.base_url + "/chat/completions", data, headers)
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last = f"{type(e).__name__}: {e}"
                status, raw = 0, b""
            if status == 200:
                return self._parse(raw, now_ms() - t0)
            if status in (401, 403):
                raise LlmError(f"DeepSeek {status}: {raw[:200]!r}")
            last = last if status == 0 else f"HTTP {status}: {raw[:200]!r}"
            if status in (0, 429) or status >= 500:
                if attempt < self.max_retries and self.retry_sleep:
                    time.sleep(self.retry_sleep)
                continue
            break
        return ChatResponse(text="", stop_reason="error", latency_ms=now_ms() - t0, error=last)

    @staticmethod
    def _parse(raw: bytes, latency_ms: int) -> ChatResponse:
        try:
            d = json.loads(raw.decode("utf-8"))
            choice = d["choices"][0]
        except (ValueError, KeyError, IndexError, TypeError) as e:
            return ChatResponse(text="", stop_reason="error", latency_ms=latency_ms, error=f"bad response: {e}")
        u = d.get("usage") or {}
        hit = int(u.get("prompt_cache_hit_tokens") or 0)
        miss = u.get("prompt_cache_miss_tokens")
        miss = int(miss) if miss is not None else max(0, int(u.get("prompt_tokens") or 0) - hit)
        usage = Usage(input=miss, cache_read=hit, output=int(u.get("completion_tokens") or 0), model_reported=d.get("model"))
        finish = choice.get("finish_reason") or "stop"
        stop = {"stop": "end_turn", "length": "max_tokens", "content_filter": "refusal"}.get(finish, "error")
        return ChatResponse(text=(choice.get("message") or {}).get("content") or "", stop_reason=stop, usage=usage,
                            latency_ms=latency_ms, raw_id=d.get("id"),
                            error=None if stop != "error" else f"finish_reason {finish}")

    def count_tokens(self, req: ChatRequest) -> int | None:
        return None                                      # no counting endpoint: the local estimate is used

    def submit_batch(self, reqs: Sequence[ChatRequest]) -> str:
        raise NotImplementedError("DeepSeek has no batch API; its share of a stage runs live")

    def batch_status(self, batch_id: str) -> BatchStatus:
        return BatchStatus(batch_id, "unknown")

    def batch_results(self, batch_id: str) -> dict[str, ChatResponse]:
        return {}

    def balance(self) -> float | None:
        """Account balance in RMB (the first balance_infos entry), None when the endpoint fails."""
        _require_network()
        status, raw = self._open(self.base_url + self.balance_endpoint, None, self._headers())
        if status != 200:
            return None
        try:
            infos = json.loads(raw.decode("utf-8")).get("balance_infos") or []
            return float(infos[0]["total_balance"])
        except (ValueError, KeyError, IndexError, TypeError):
            return None


# ---- configuration and factories ------------------------------------------------------------------------------------
def llm_config(project: Project) -> dict:
    cfg = project.get("llm") or {}
    if not isinstance(cfg, dict):
        raise ValueError("llm: must be a mapping")
    return cfg


def primary_provider(cfg: dict) -> str:
    return str(cfg.get("provider") or "anthropic")


def split_of(cfg: dict) -> dict[str, float]:
    """`llm.split` normalised to shares that sum to 1 (providers with share 0 dropped); absent or empty →
    {primary: 1.0}."""
    raw = cfg.get("split") or {}
    shares = {str(p): float(w) for p, w in raw.items() if float(w) > 0} if isinstance(raw, dict) else {}
    if not shares:
        return {primary_provider(cfg): 1.0}
    total = sum(shares.values())
    return {p: w / total for p, w in shares.items()}


def models_of(cfg: dict) -> dict[str, str]:
    """{provider: model}: `llm.models` with `llm.model` as the primary provider's model."""
    models = {str(p): str(m) for p, m in (cfg.get("models") or {}).items() if m}
    if cfg.get("model"):
        models[primary_provider(cfg)] = str(cfg["model"])
    return models


def fx_rates(cfg: dict, currency: str) -> dict[str, float]:
    """{currency: value of one unit in `currency`} from `llm.fx` (values of one unit in USD)."""
    fx = {str(c): float(v) for c, v in (cfg.get("fx") or {"USD": 1.0}).items()}
    base = fx.get(currency, 1.0)
    return {c: v / base for c, v in fx.items()}


def key_env_of(cfg: dict, provider: str) -> str:
    if provider == "deepseek":
        return str((cfg.get("deepseek") or {}).get("api_key_env") or DEFAULT_KEY_ENV["deepseek"])
    return str(cfg.get("api_key_env") or DEFAULT_KEY_ENV["anthropic"])


def make_client(project: Project, provider: str | None = None) -> ChatClient:
    """A ChatClient for one provider of the project's `llm` block (the primary when `provider` is None)."""
    cfg = llm_config(project)
    provider = provider or primary_provider(cfg)
    model = models_of(cfg).get(provider) or str(cfg.get("model") or "")
    if provider == "fake":
        return FakeChatClient()
    if provider == "anthropic":
        return AnthropicClient(model=model, effort=cfg.get("effort") or None,
                               max_retries=int(cfg.get("max_retries", 2)), api_key_env=key_env_of(cfg, provider),
                               refusal_fallback=bool(cfg.get("refusal_fallback", True)))
    if provider == "deepseek":
        ds = cfg.get("deepseek") or {}
        return DeepSeekClient(model=model, base_url=str(ds.get("base_url") or "https://api.deepseek.com"),
                              api_key_env=key_env_of(cfg, provider), extra_body=ds.get("extra_body") or {},
                              peak=ds.get("peak"), peak_multiplier=float(ds.get("peak_multiplier", 2.0)),
                              balance_endpoint=str(ds.get("balance_endpoint") or "/user/balance"),
                              max_retries=int(cfg.get("max_retries", 2)))
    raise ValueError(f"unknown llm.provider {provider!r}; expected one of {PROVIDERS}")


def make_clients(project: Project) -> dict[str, ChatClient]:
    """{provider: ChatClient} for the primary provider and every provider of the split (no network at
    construction; a missing key is noticed at the first call)."""
    cfg = llm_config(project)
    providers = [primary_provider(cfg)] + [p for p in split_of(cfg) if p != primary_provider(cfg)]
    return {p: make_client(project, p) for p in providers}
