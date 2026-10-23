"""Thin local task runner for the curation stages — a stand-in for work package L's `atlas/llm/tasks.py` /
`ledger.py` / `governor.py` / `batch.py` (increment 4 §1.3–1.6), written against the frozen `atlas/llm/base.py`
only, so the stages run end to end with `FakeChatClient` before the LLM layer lands.

What it does: read a prompt file (stable system text, a `---payload---` line, a `str.format` payload template),
build a `ChatRequest`, call `client.complete` (or `submit_batch` / `batch_results`), parse the JSON answer,
validate it locally against the task's JSON schema (type / required / enum / maxItems / minItems / maxLength,
pure Python), truncate lists to the task's caps (`truncated_by_code`), apply the stop-reason policy of §1.3 in
its simplest form (`max_tokens` → one retry with doubled max_tokens, invalid JSON → one "return only the JSON
object" retry, `refusal` → failed, never retried), and bill every attempt with the local pricing table.

What it does not do: no ledger rows, no llm.sqlite, no cache lookups, no daily budgets, no DeepSeek peak logic.
`plan_task` gives the dry-run estimate from `estimate_tokens` (low: system prefix cached after the first call and a
median-sized answer; high: every token at the miss price and `max_tokens` of output, plus 6 % retry overhead).

Integration note: the stages call this module through exactly one method, `LlmStage._call_task` in
`atlas/pipeline/llm_stage.py` (live) and `LlmStage._run_batch` (batch); swapping in L's task runner means
replacing the bodies of those two methods (and `plan_task` → the governor's plan). The result dict shape documented
in `TaskResult` is what the stages consume.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from atlas.llm.base import ChatClient, ChatRequest, ChatResponse, Usage, estimate_tokens, request_hash

log = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "llm" / "prompts"
PAYLOAD_SEP = "---payload---"
DEFAULT_MODEL = "claude-opus-5-5"
MODEL_MAX_TOKENS = 32_000
RETRY_OVERHEAD = 1.06
POLL_SECONDS = 2.0
#: USD per million tokens (contract §0); DeepSeek in RMB from the previous defaults; `batch_multiplier` halves
#: every token price for Message Batches
PRICING: dict[str, dict] = {
    "claude-opus-5-5": {"input": 4.0, "output": 20.0, "cache_write": 5.0, "cache_read": 0.20, "currency": "USD",
                        "batch_multiplier": 0.5},
    "claude-sonnet-5-5": {"input": 2.0, "output": 10.0, "cache_write": 2.5, "cache_read": 0.20, "currency": "USD",
                          "batch_multiplier": 0.5},
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50, "cache_write": 0.125, "cache_read": 0.01, "currency": "USD",
                         "batch_multiplier": 0.5},
    "deepseek-flash": {"input": 1.0, "output": 4.0, "cache_write": 1.0, "cache_read": 0.02, "currency": "RMB",
                       "batch_multiplier": 1.0},
}
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.S)


class PromptError(ValueError):
    pass


# ---- prompts ---------------------------------------------------------------------------------------------------

class Prompt:
    """A loaded prompt file: `system` (stable text), `template` (payload section), `sha` (sha256 of the file bytes)."""

    def __init__(self, path: Path, system: str, template: str, sha: str):
        self.path, self.system, self.template, self.sha = path, system, template, sha

    def render(self, payload: Mapping[str, Any]) -> str:
        """The payload template with `str.format`; non-string values are canonical JSON (sorted keys)."""
        fields = {k: (v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, sort_keys=True))
                  for k, v in payload.items()}
        try:
            return self.template.format(**fields)
        except (KeyError, IndexError, ValueError) as e:
            raise PromptError(f"{self.path.name}: payload template needs {e} (payload keys: {sorted(payload)})") from e


def prompt_path(name_or_path: str | Path) -> Path:
    p = Path(name_or_path)
    if p.suffix == ".md" and p.is_file():
        return p
    cand = PROMPTS_DIR / (p.name if p.suffix == ".md" else f"{p.name}.md")
    if not cand.is_file():
        raise FileNotFoundError(f"prompt file not found: {name_or_path} (looked in {PROMPTS_DIR})")
    return cand


def load_prompt(name_or_path: str | Path) -> Prompt:
    p = prompt_path(name_or_path)
    raw = p.read_bytes()
    text = raw.decode("utf-8")
    if PAYLOAD_SEP not in text:
        raise PromptError(f"{p.name}: no '{PAYLOAD_SEP}' line")
    system, template = text.split(PAYLOAD_SEP, 1)
    return Prompt(p, system.strip() + "\n", template.strip("\r\n") + "\n", hashlib.sha256(raw).hexdigest())


# ---- validation and caps ---------------------------------------------------------------------------------------

_TYPES = {"object": dict, "array": list, "string": str, "integer": int, "number": (int, float), "boolean": bool,
          "null": type(None)}


def _type_ok(value: Any, t: str) -> bool:
    py = _TYPES.get(t)
    if py is None:
        return True
    if t in ("integer", "number") and isinstance(value, bool):
        return False
    return isinstance(value, py)


def validate(schema: Mapping | None, obj: Any, path: str = "$") -> list[str]:
    """Error strings ('' when valid) for the subset of JSON schema the tasks use: type (one or a list), required,
    properties, items, enum, maxItems, minItems, maxLength. Unknown keywords are ignored."""
    errors: list[str] = []
    if not schema:
        return errors
    t = schema.get("type")
    if t:
        types = t if isinstance(t, list) else [t]
        if not any(_type_ok(obj, x) for x in types):
            return [f"{path}: expected {t}, got {type(obj).__name__}"]
    if "enum" in schema and obj not in schema["enum"]:
        errors.append(f"{path}: {obj!r} not in enum")
    if isinstance(obj, str) and "maxLength" in schema and len(obj) > int(schema["maxLength"]):
        errors.append(f"{path}: longer than {schema['maxLength']}")
    if isinstance(obj, dict):
        for k in schema.get("required", []) or []:
            if k not in obj:
                errors.append(f"{path}.{k}: required")
        for k, sub in (schema.get("properties") or {}).items():
            if k in obj:
                errors += validate(sub, obj[k], f"{path}.{k}")
    if isinstance(obj, list):
        if "maxItems" in schema and len(obj) > int(schema["maxItems"]):
            errors.append(f"{path}: more than {schema['maxItems']} items")
        if "minItems" in schema and len(obj) < int(schema["minItems"]):
            errors.append(f"{path}: fewer than {schema['minItems']} items")
        items = schema.get("items")
        if items:
            for i, v in enumerate(obj):
                errors += validate(items, v, f"{path}[{i}]")
    return errors


def truncate(obj: Any, caps: Mapping[str, int] | None) -> dict[str, int]:
    """Cut lists to the caps in place. `caps` maps dotted paths to a maximum length; a path segment crossing a
    list applies to every element ('terms.sids': every term's sids). Returns {path: items dropped}."""
    dropped: dict[str, int] = {}
    for path, cap in (caps or {}).items():
        _truncate_path(obj, path.split("."), int(cap), path, dropped)
    return dropped


def _truncate_path(node: Any, parts: Sequence[str], cap: int, label: str, dropped: dict[str, int]) -> None:
    if isinstance(node, list) and (len(parts) > 0):
        for el in node:
            _truncate_path(el, parts, cap, label, dropped)
        return
    if not isinstance(node, dict) or not parts:
        return
    head, rest = parts[0], parts[1:]
    if head not in node:
        return
    if not rest:
        v = node[head]
        if isinstance(v, list) and len(v) > cap:
            dropped[label] = dropped.get(label, 0) + len(v) - cap
            del v[cap:]
        return
    _truncate_path(node[head], rest, cap, label, dropped)


def parse_json(text: str) -> Any:
    """The JSON object in a model answer: as is, inside a code fence, or the first `{ … }` block."""
    s = (text or "").strip()
    if not s:
        raise ValueError("empty answer")
    m = _FENCE_RE.match(s)
    if m:
        s = m.group(1)
    try:
        return json.loads(s)
    except ValueError:
        a, b = s.find("{"), s.rfind("}")
        if a >= 0 and b > a:
            return json.loads(s[a:b + 1])
        raise


# ---- pricing ---------------------------------------------------------------------------------------------------

def pricing_for(model: str, pricing: Mapping | None = None) -> dict:
    """The price row of a model: the project's `llm.pricing.<model>` when it has the per-million fields, else the
    built-in table, else the default model's row (a plan is always printable)."""
    row = (pricing or {}).get(model) if isinstance(pricing, Mapping) else None
    if isinstance(row, Mapping) and "input" in row and "output" in row:
        out = dict(PRICING.get(model, PRICING[DEFAULT_MODEL]))
        out.update(row)
        return out
    return dict(PRICING.get(model) or PRICING[DEFAULT_MODEL])


def cost_of(usage: Usage, model: str, pricing: Mapping | None = None, batch: bool = False) -> float:
    p = pricing_for(model, pricing)
    c = (usage.input * p["input"] + usage.cache_write * p["cache_write"] + usage.cache_read * p["cache_read"]
         + usage.output * p["output"]) / 1_000_000
    if batch:
        c *= float(p.get("batch_multiplier", 0.5))
    return round(c, 6)


# ---- requests and results ----------------------------------------------------------------------------------------

def build_request(prompt: Prompt, payload: Mapping[str, Any], *, task: str, key: str, schema: Mapping | None,
                  max_tokens: int, model: str = DEFAULT_MODEL, effort: str | None = None,
                  system_extra: Sequence[str] = ()) -> ChatRequest:
    """The stable prompt first, any per-run context blocks after it (the last block gets the cache breakpoint),
    the rendered payload as the user turn."""
    return ChatRequest(model=model, system=(prompt.system, *[str(x) for x in system_extra if x]),
                       user=prompt.render(payload), max_tokens=int(max_tokens),
                       output_schema=dict(schema) if schema else None, effort=effort, task=task, key=key)


class TaskResult(dict):
    """{key, status: ok | invalid | truncated | malformed | refused | error | budget_stop, data, errors,
    truncated_by_code, attempts, usage: {input, cache_write, cache_read, output}, cost, currency, request_hash,
    prompt_sha, latency_ms, raw_id}"""

    @property
    def ok(self) -> bool:
        return self.get("status") == "ok"


def _usage_dict(u: Usage) -> dict:
    return {"input": u.input, "cache_write": u.cache_write, "cache_read": u.cache_read, "output": u.output}


def finish_response(resp: ChatResponse, req: ChatRequest, prompt: Prompt, schema: Mapping | None,
                    caps: Mapping[str, int] | None, provider: str, model: str, pricing: Mapping | None,
                    batch: bool = False, attempts: int = 1, prior_usage: Usage | None = None) -> TaskResult:
    """Parse + validate + truncate one final response into a TaskResult (no retries here)."""
    usage = Usage(input=resp.usage.input, cache_write=resp.usage.cache_write, cache_read=resp.usage.cache_read,
                  output=resp.usage.output, model_reported=resp.usage.model_reported)
    if prior_usage:
        usage.input += prior_usage.input
        usage.cache_write += prior_usage.cache_write
        usage.cache_read += prior_usage.cache_read
        usage.output += prior_usage.output
    res = TaskResult(key=req.key, status="ok", data=None, errors=[], truncated_by_code={}, attempts=attempts,
                     usage=_usage_dict(usage), cost=cost_of(usage, model, pricing, batch),
                     currency=pricing_for(model, pricing)["currency"], request_hash=request_hash(provider, req),
                     prompt_sha=prompt.sha, latency_ms=resp.latency_ms, raw_id=resp.raw_id)
    if resp.error or resp.stop_reason == "error":
        res["status"], res["errors"] = "error", [resp.error or "provider error"]
        return res
    if resp.stop_reason == "refusal":
        res["status"], res["errors"] = "refused", ["the model refused"]
        return res
    if resp.stop_reason == "max_tokens":
        res["status"], res["errors"] = "truncated", ["answer hit max_tokens"]
        return res
    try:
        data = parse_json(resp.text)
    except ValueError as e:
        res["status"], res["errors"] = "malformed", [f"invalid JSON: {e}"]
        return res
    # caps first (an over-long list is trimmed, not failed), then the local schema check
    res["truncated_by_code"] = truncate(data, caps)
    errors = validate(schema, data)
    if errors:
        res["status"], res["errors"], res["data"] = "invalid", errors[:20], data
        return res
    res["data"] = data
    return res


def run_task(client: ChatClient, task_name: str, prompt_file: str | Path, schema: Mapping | None,
             payload: Mapping[str, Any], *, key: str, max_tokens: int, caps: Mapping[str, int] | None = None,
             model: str = DEFAULT_MODEL, effort: str | None = None, system_extra: Sequence[str] = (),
             pricing: Mapping | None = None) -> TaskResult:
    """One structured call with the §1.3 policy (simplified): max_tokens → one retry with max_tokens × 2 (≤ the
    model cap); invalid / malformed JSON → one retry asking for the JSON object only; refusal → failed, no retry."""
    prompt = load_prompt(prompt_file)
    req = build_request(prompt, payload, task=task_name, key=key, schema=schema, max_tokens=max_tokens, model=model,
                        effort=effort, system_extra=system_extra)
    provider = getattr(client, "provider", "fake")
    resp = client.complete(req)
    attempts = 1
    prior: Usage | None = None
    if resp.stop_reason == "max_tokens" and not resp.error and max_tokens * 2 <= MODEL_MAX_TOKENS:
        prior = resp.usage
        req = replace(req, max_tokens=int(max_tokens) * 2)
        resp = client.complete(req)
        attempts += 1
    res = finish_response(resp, req, prompt, schema, caps, provider, model, pricing, attempts=attempts, prior_usage=prior)
    if res["status"] in ("malformed", "invalid"):
        prior = Usage(**{k: v for k, v in res["usage"].items()})
        req2 = replace(req, user=req.user + "\n\nReturn only the JSON object that matches the schema, nothing else.")
        resp2 = client.complete(req2)
        res2 = finish_response(resp2, req2, prompt, schema, caps, provider, model, pricing, attempts=attempts + 1,
                               prior_usage=prior)
        if res2["status"] == "ok":
            return res2
        res["attempts"], res["usage"], res["cost"] = res2["attempts"], res2["usage"], res2["cost"]
        res["errors"] = res["errors"] + [f"retry: {e}" for e in res2["errors"]]
    return res


def run_batch(client: ChatClient, reqs: Sequence[ChatRequest], *, wait: bool, poll_seconds: float = POLL_SECONDS,
              max_wait_seconds: float | None = None) -> tuple[str, dict[str, ChatResponse] | None]:
    """Submit one batch; with `wait` poll until it ended (or `max_wait_seconds`). Returns (batch_id, results or
    None when still running)."""
    batch_id = client.submit_batch(list(reqs))
    return batch_id, collect_batch(client, batch_id, wait=wait, poll_seconds=poll_seconds, max_wait_seconds=max_wait_seconds)


def collect_batch(client: ChatClient, batch_id: str, *, wait: bool, poll_seconds: float = POLL_SECONDS,
                  max_wait_seconds: float | None = None) -> dict[str, ChatResponse] | None:
    t0 = time.time()
    while True:
        st = client.batch_status(batch_id)
        if st.state == "ended":
            return client.batch_results(batch_id)
        if st.state == "unknown":
            raise KeyError(f"unknown batch {batch_id}")
        if not wait or (max_wait_seconds is not None and time.time() - t0 > max_wait_seconds):
            return None
        time.sleep(poll_seconds)


# ---- planning ----------------------------------------------------------------------------------------------------

def plan_task(prompt_file: str | Path, payloads: Sequence[tuple[str, Mapping[str, Any]]], *, max_tokens: int,
              out_low: int, model: str = DEFAULT_MODEL, pricing: Mapping | None = None,
              system_extra: Sequence[str] = (), known_hits: int = 0) -> dict:
    """Token / cost estimate without any client: {model, currency, items, known_cache_hits, tokens: {system,
    input_low, input_high, output_low, output_high}, cost_low, cost_high, cost_batch_low, cost_batch_high, per_item}.
    Low assumes the system prefix is written to the cache once and read afterwards and a median answer of
    `out_low` tokens; high assumes every token at the miss price and `max_tokens` of output, × RETRY_OVERHEAD."""
    prompt = load_prompt(prompt_file)
    p = pricing_for(model, pricing)
    sys_tokens = estimate_tokens(prompt.system + "\n".join(str(x) for x in system_extra))
    users = [estimate_tokens(prompt.render(pl)) for _, pl in payloads]
    n = len(users)
    user_tokens = sum(users)
    in_low = user_tokens + (sys_tokens if n else 0) * n
    in_high = user_tokens + sys_tokens * n
    out_lo, out_hi = int(out_low) * n, int(max_tokens) * n
    cost_low = ((user_tokens * p["input"] + (sys_tokens * p["cache_write"] if n else 0)
                 + sys_tokens * max(0, n - 1) * p["cache_read"] + out_lo * p["output"]) / 1_000_000)
    cost_high = ((in_high * p["input"] + out_hi * p["output"]) / 1_000_000) * RETRY_OVERHEAD
    bm = float(p.get("batch_multiplier", 0.5))
    per_item_high = ((sys_tokens + (user_tokens / n if n else 0)) * p["input"] + max_tokens * p["output"]) / 1_000_000 * RETRY_OVERHEAD
    return {"model": model, "currency": p["currency"], "items": n, "known_cache_hits": int(known_hits),
            "prompt_sha": prompt.sha,
            "tokens": {"system": sys_tokens, "user": user_tokens, "input_low": in_low, "input_high": in_high,
                       "output_low": out_lo, "output_high": out_hi},
            "cost_low": round(cost_low, 4), "cost_high": round(cost_high, 4),
            "cost_batch_low": round(cost_low * bm, 4), "cost_batch_high": round(cost_high * bm, 4),
            "per_item_high": round(per_item_high, 6), "max_tokens": int(max_tokens), "out_low": int(out_low)}
