"""Structured tasks (increment 4 §1.3): prompt files, payload rendering, local validation and the retry policy,
run through the ledger and the governor by `TaskRunner` — one backend per provider of the split.

Prompt file `atlas/llm/prompts/<task>.md` (owned by the stages): the stable system text, a line
`---payload---`, then the payload template rendered with `str.format` (`{field}`; a non-string field is
inserted as canonical JSON — sorted keys, no spaces — so a template can say `{sentences}`; literal braces are
written `{{` / `}}`). Line endings are normalised to `\\n` when the file is read, so a CRLF checkout renders the
same request. The stage's payload builder is pure (sorted keys, sorted sids, no timestamps, no build ids): two
builds of the same item give the same request hash. `prompt_sha` = sha256 of the prompt file bytes; it enters
every ledger row, call, draft and the run's settings snapshot.

Request = (prompt system block, + optional per-run `context` blocks such as a scheme, the last one carrying the
cache breakpoint) + rendered payload + the task schema (`output_config.format`) + the backend's model / effort.

Providers: `make_runner` builds one `Backend` (client, model, price, ledger) per provider of `llm.split` (plus
the primary); `choose_provider(sha1(item key), split)` assigns every item to a backend deterministically, so the
same item goes to the same provider in every run and the two providers' answers can be compared through the
ledger (`provider` on every row and every `TaskResult`). A split part (`key/0`, `key/1`) stays on its parent's
backend. `llm.failover: true` re-runs an item whose provider answered with a transport error on another backend;
such rows carry `failover = 1`. Costs in `TaskResult.cost` and in the governor are in the primary provider's
currency (`llm.fx` rates); ledger rows keep their own currency.

Policy by response (every attempt is a ledger row):
- `end_turn` + JSON that parses and passes `validate(schema)` → `ok` (lists over `limits` are cut and the paths
  recorded in `truncated_by_code`); a free-text task (schema None) is `ok` on `end_turn`;
- `max_tokens` → when the task is `splittable` and the caller passed `split(payload) -> parts`, each part runs as
  its own item (`key/0`, `key/1`, depth ≤ MAX_SPLIT_DEPTH) and the parts are merged (lists concatenated);
  otherwise one retry with `max_tokens × 2` (≤ `llm.max_tokens_cap`), else `failed:truncated`;
- invalid or schema-violating JSON → one retry with "Return only the JSON object." appended to the payload (a
  different hash), else `failed:malformed`;
- `refusal` → `failed:refused`, never retried; a transport error → `failed:error` (or the failover above).
Before any call the exact request hash is looked up in `calls` (`cache_local`, zero cost). In `--mode batch`
cache misses of a backend with a batch API are queued (`TaskResult.status == "queued"`) for
`atlas.llm.batch.submit_queued`; a backend without one (DeepSeek) is called live.

Settings snapshot: the first call or queued request of a run writes
`<project>/work/runs/<run_id>/llm_settings.json` (primary, split, failover, per-provider model / effort /
price / batch API, mode, max_tokens cap, the tasks seen with their prompt_sha, the price table's sha); it is
rewritten when a further task appears. A dry run plans only and writes nothing.

Public API for the stages: `StructuredTask`, `task_for`, `make_runner(project, stage, …)`,
`TaskRunner.run(task, payload, key, context=(), split=None) -> TaskResult` (= `run_task(runner, …)`),
`TaskRunner.plan_row` / `plan_items` for `--dry-run`, `BudgetExceeded` from `atlas.llm.governor`.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from atlas.config import Project
from atlas.llm.base import ChatClient, ChatRequest, ChatResponse
from atlas.llm.client import (choose_provider, fx_rates, llm_config, make_clients, models_of, primary_provider,
                              split_of)
from atlas.llm.governor import BudgetExceeded, Governor, Plan, PlanRow, governor_for, idle_notice
from atlas.llm.ledger import (Estimate, Ledger, Price, PriceTable, estimate, new_run_id, price_table,
                              provider_multiplier, report_unknown_pending)
from atlas.store.llm import LlmStore

log = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).with_name("prompts")
PAYLOAD_SEPARATOR = "---payload---"
RETRY_SUFFIX = "\n\nReturn only the JSON object."
MAX_SPLIT_DEPTH = 3
OBSERVATION_REFRESH = 20          # finished calls between two refreshes of a task's ledger observations
SETTINGS_FILE = "llm_settings.json"
_PATH_RE = re.compile(r"^[^.\[\]]+(\[\])?$")


# ---- prompts ----------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Prompt:
    system: tuple[str, ...]
    template: str
    sha: str
    path: Path


_PROMPT_CACHE: dict[Path, tuple[tuple[int, int], Prompt]] = {}


def load_prompt(path: str | Path) -> Prompt:
    """Parse a prompt file; cached by (mtime, size)."""
    p = Path(path)
    st = p.stat()
    stamp = (st.st_mtime_ns, st.st_size)
    hit = _PROMPT_CACHE.get(p)
    if hit is not None and hit[0] == stamp:
        return hit[1]
    raw = p.read_bytes()
    lines = raw.decode("utf-8").replace("\r\n", "\n").split("\n")
    try:
        cut = next(i for i, ln in enumerate(lines) if ln.strip() == PAYLOAD_SEPARATOR)
    except StopIteration:
        raise ValueError(f"prompt file {p} has no `{PAYLOAD_SEPARATOR}` line") from None
    system = "\n".join(lines[:cut]).strip()
    template = "\n".join(lines[cut + 1:]).strip("\n")
    if not system:
        raise ValueError(f"prompt file {p}: empty system section")
    prompt = Prompt(system=(system,), template=template, sha=hashlib.sha256(raw).hexdigest(), path=p)
    _PROMPT_CACHE[p] = (stamp, prompt)
    return prompt


def canonical_json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def render_payload(template: str, payload: Mapping) -> str:
    """`str.format` with every non-string field as canonical JSON; a missing field is a KeyError."""
    fields = {k: (v if isinstance(v, str) else canonical_json(v)) for k, v in payload.items()}
    try:
        return template.format_map(fields)
    except KeyError as e:
        raise KeyError(f"payload has no field {e.args[0]!r} required by the prompt template") from None


@dataclass(frozen=True)
class StructuredTask:
    name: str
    prompt_file: Path
    schema: dict | None                      # None = free text
    limits: Mapping[str, int] = field(default_factory=dict)   # list path → cap ("terms", "concepts[].forms")
    max_tokens: int = 4096
    splittable: bool = False
    unit_cost_default: float = 0.0           # per-item low estimate before the ledger has observations

    def prompt(self) -> Prompt:
        return load_prompt(self.prompt_file)

    @property
    def prompt_sha(self) -> str:
        return self.prompt().sha

    def render(self, payload: Mapping) -> str:
        return render_payload(self.prompt().template, payload)

    def settings(self) -> dict:
        return {"prompt_file": Path(self.prompt_file).name, "prompt_sha": self.prompt_sha, "max_tokens": self.max_tokens,
                "splittable": self.splittable, "limits": dict(self.limits), "unit_cost_default": self.unit_cost_default,
                "schema": self.schema is not None}


def task_for(name: str, schema: dict | None, prompts_dir: str | Path = PROMPTS_DIR, **kw) -> StructuredTask:
    """A task whose prompt is `<prompts_dir>/<name>.md`."""
    return StructuredTask(name=name, prompt_file=Path(prompts_dir) / f"{name}.md", schema=schema, **kw)


# ---- validation --------------------------------------------------------------------------------------------------------
_TYPES = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None)}


def _type_ok(t: str, v) -> bool:
    if t == "integer":
        return isinstance(v, int) and not isinstance(v, bool)
    if t == "number":
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    cls = _TYPES.get(t)
    return cls is not None and isinstance(v, cls)


def validate(schema: dict | None, data, path: str = "$") -> list[str]:
    """Pure-Python subset of JSON Schema: type / required / enum / properties / items / additionalProperties /
    maxItems / minItems / maxLength / minLength. Returns human-readable errors (empty = valid)."""
    if not schema:
        return []
    errs: list[str] = []
    t = schema.get("type")
    if t is not None:
        types = t if isinstance(t, list) else [t]
        if not any(_type_ok(x, data) for x in types):
            return [f"{path}: expected {'|'.join(types)}, got {type(data).__name__}"]
    if "enum" in schema and data not in schema["enum"]:
        errs.append(f"{path}: {data!r} not in enum {schema['enum']!r}")
    if isinstance(data, dict):
        for k in schema.get("required", ()):
            if k not in data:
                errs.append(f"{path}: missing required {k!r}")
        props = schema.get("properties") or {}
        for k, sub in props.items():
            if k in data:
                errs += validate(sub, data[k], f"{path}.{k}")
        if schema.get("additionalProperties") is False:
            for k in data:
                if k not in props:
                    errs.append(f"{path}: unexpected key {k!r}")
    elif isinstance(data, list):
        if "maxItems" in schema and len(data) > int(schema["maxItems"]):
            errs.append(f"{path}: {len(data)} items > maxItems {schema['maxItems']}")
        if "minItems" in schema and len(data) < int(schema["minItems"]):
            errs.append(f"{path}: {len(data)} items < minItems {schema['minItems']}")
        items = schema.get("items")
        if isinstance(items, dict):
            for i, v in enumerate(data):
                errs += validate(items, v, f"{path}[{i}]")
    elif isinstance(data, str):
        if "maxLength" in schema and len(data) > int(schema["maxLength"]):
            errs.append(f"{path}: length {len(data)} > maxLength {schema['maxLength']}")
        if "minLength" in schema and len(data) < int(schema["minLength"]):
            errs.append(f"{path}: length {len(data)} < minLength {schema['minLength']}")
    return errs


def parse_json(text: str) -> tuple[dict | list | None, str | None]:
    """(data, None) for a JSON object / array (surrounding prose tolerated), (None, error) otherwise."""
    s = (text or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n?|```$", "", s).strip()
    try:
        data = json.loads(s)
    except ValueError:
        start = min((i for i in (s.find("{"), s.find("[")) if i >= 0), default=-1)
        end = max(s.rfind("}"), s.rfind("]"))
        if start < 0 or end <= start:
            return None, "no JSON object in the response"
        try:
            data = json.loads(s[start:end + 1])
        except ValueError as e:
            return None, f"invalid JSON: {e}"
    if not isinstance(data, (dict, list)):
        return None, f"JSON is a {type(data).__name__}, not an object"
    return data, None


def apply_limits(data, limits: Mapping[str, int]) -> tuple[object, list[str]]:
    """Cut lists over their cap. Path grammar: `a.b` descends into dicts, `a[]` into every element of a list;
    the last segment names the list. Returns (data, ["terms (31→25)", …])."""
    truncated: list[str] = []
    for path, cap in (limits or {}).items():
        segs = path.split(".")
        for s in segs:
            if not _PATH_RE.match(s):
                raise ValueError(f"bad limit path {path!r}")
        _cut(data, segs, int(cap), path, truncated)
    return data, truncated


def _cut(node, segs: list[str], cap: int, path: str, out: list[str]) -> None:
    if not segs:
        return
    seg, rest = segs[0], segs[1:]
    name, each = (seg[:-2], True) if seg.endswith("[]") else (seg, False)
    if not isinstance(node, dict) or name not in node:
        return
    val = node[name]
    if not rest:
        if isinstance(val, list) and len(val) > cap:
            out.append(f"{path} ({len(val)}→{cap})")
            node[name] = val[:cap]
        return
    if each:
        if isinstance(val, list):
            for el in val:
                _cut(el, rest, cap, path, out)
    else:
        _cut(val, rest, cap, path, out)


def merge_parts(parts: Sequence[dict]) -> dict:
    """Merge the answers of a split item: lists concatenate, dicts merge shallowly, scalars keep the first."""
    out: dict = {}
    for part in parts:
        for k, v in (part or {}).items():
            if k not in out:
                out[k] = list(v) if isinstance(v, list) else (dict(v) if isinstance(v, dict) else v)
            elif isinstance(out[k], list) and isinstance(v, list):
                out[k].extend(v)
            elif isinstance(out[k], dict) and isinstance(v, dict):
                out[k].update(v)
    return out


def classify(resp: ChatResponse, schema: dict | None) -> tuple[str, dict | list | None, list[str]]:
    """(status ok|truncated|malformed|refused|error, parsed data, errors) for a response of a task."""
    if resp.stop_reason == "refusal":
        return "refused", None, []
    if resp.error or resp.stop_reason == "error":
        return "error", None, [resp.error or "error"]
    if resp.stop_reason == "max_tokens":
        return "truncated", None, []
    if schema is None:
        return "ok", None, []
    data, err = parse_json(resp.text)
    if err:
        return "malformed", None, [err]
    errs = validate(schema, data)
    if errs:
        return "malformed", data, errs
    return "ok", data, []


# ---- results, backends and the runner -------------------------------------------------------------------------------
@dataclass
class TaskResult:
    key: str
    status: str                              # ok | cache_local | queued | failed:truncated | failed:malformed |
                                             # failed:refused | failed:error
    data: dict | list | None = None
    text: str = ""
    request_hash: str = ""
    attempts: int = 0
    cost: float = 0.0                        # in the runner's (primary provider's) currency
    truncated_by_code: list[str] = field(default_factory=list)
    error: str | None = None
    parts: int = 1
    provider: str = ""                       # the backend that answered (or queued) the item
    model_reported: str | None = None
    failover: bool = False

    @property
    def ok(self) -> bool:
        return self.status in ("ok", "cache_local")


@dataclass
class Backend:
    provider: str
    client: ChatClient
    model: str
    effort: str | None
    price: Price
    ledger: Ledger
    batch_api: bool

    @property
    def currency(self) -> str:
        return self.client.currency

    def settings(self) -> dict:
        return {"model": self.model, "effort": self.effort, "currency": self.currency, "batch_api": self.batch_api,
                "price": asdict(self.price)}


@dataclass
class Queued:
    task: StructuredTask
    req: ChatRequest
    item_key: str
    estimate: Estimate
    provider: str


Splitter = Callable[[Mapping], Sequence[Mapping]]


class TaskRunner:
    def __init__(self, clients: Mapping[str, ChatClient], store: LlmStore, prices: PriceTable, *, run_id: str,
                 stage: str, models: Mapping[str, str], split: Mapping[str, float] | None = None,
                 primary: str | None = None, effort: str | None = None, build_id: str | None = None,
                 mode: str = "live", governor: Governor | None = None, max_tokens_cap: int = 65536,
                 failover: bool = False, rates: Mapping[str, float] | None = None, runs_dir: Path | None = None,
                 owns_store: bool = False) -> None:
        if mode not in ("live", "batch"):
            raise ValueError(f"mode must be live or batch, not {mode!r}")
        if not clients:
            raise ValueError("no provider clients")
        self.store = store
        self.prices = prices
        self.run_id = run_id
        self.stage = stage
        self.build_id = build_id
        self.mode = mode
        self.governor = governor
        self.max_tokens_cap = int(max_tokens_cap)
        self.failover = bool(failover)
        self.rates = dict(rates or {})
        self.runs_dir = runs_dir
        self.backends: dict[str, Backend] = {}
        for provider, client in clients.items():
            model = models.get(provider)
            if not model:
                raise ValueError(f"no model configured for provider {provider!r} (llm.models)")
            self.backends[provider] = Backend(
                provider=provider, client=client, model=model, effort=None if provider == "deepseek" else effort,
                price=prices.get(model), batch_api=bool(getattr(client, "batch_api", True)),
                ledger=Ledger(store, prices, provider=provider, currency=client.currency, run_id=run_id, stage=stage,
                              build_id=build_id))
        self.primary = primary or next(iter(self.backends))
        if self.primary not in self.backends:
            raise ValueError(f"primary provider {self.primary!r} has no client")
        shares = {p: float(w) for p, w in (split or {self.primary: 1.0}).items() if p in self.backends and float(w) > 0}
        self.split: dict[str, float] = shares or {self.primary: 1.0}
        self.queued: list[Queued] = []
        self.startup_notes: list[str] = []
        self.tasks: dict[str, StructuredTask] = {}
        self._owns_store = owns_store
        self._obs: dict[tuple[str, str, str], tuple[int, dict]] = {}
        self._finished = 0
        self._settings_written = False

    def close(self) -> None:
        if self._owns_store:
            self.store.close()

    # ---- providers --------------------------------------------------------------------------------------------------
    @property
    def currency(self) -> str:
        return self.backends[self.primary].currency

    @property
    def models(self) -> dict[str, str]:
        return {p: b.model for p, b in self.backends.items()}

    def backend_for(self, key: str) -> Backend:
        return self.backends[choose_provider(key, self.split)]

    def to_primary(self, amount: float, currency: str) -> float:
        return amount * float(self.rates.get(currency, 1.0))

    def _failover_backend(self, failed: Backend) -> Backend | None:
        others = [p for p in self.split if p != failed.provider] or [p for p in self.backends if p != failed.provider]
        return self.backends[others[0]] if others else None

    # ---- settings snapshot ----------------------------------------------------------------------------------------
    def settings(self) -> dict:
        prices = {p: asdict(b.price) for p, b in sorted(self.backends.items())}
        return {"run_id": self.run_id, "stage": self.stage, "build_id": self.build_id, "mode": self.mode,
                "created": datetime.fromtimestamp(time.time(), tz=timezone.utc).isoformat(timespec="seconds"),
                "primary": self.primary, "split": dict(self.split), "failover": self.failover,
                "currency": self.currency, "fx": dict(self.rates), "max_tokens_cap": self.max_tokens_cap,
                "providers": {p: b.settings() for p, b in sorted(self.backends.items())},
                "tasks": {name: t.settings() for name, t in sorted(self.tasks.items())},
                "pricing_sha": hashlib.sha256(canonical_json(prices).encode("utf-8")).hexdigest()}

    def settings_path(self) -> Path | None:
        return (self.runs_dir / self.run_id / SETTINGS_FILE) if self.runs_dir is not None else None

    def write_settings(self) -> Path | None:
        p = self.settings_path()
        if p is None:
            return None
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.settings(), ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
        self._settings_written = True
        return p

    def note_task(self, task: StructuredTask) -> None:
        if task.name not in self.tasks:
            self.tasks[task.name] = task
            if self._settings_written:
                self.write_settings()

    def _before_spend(self, task: StructuredTask) -> None:
        self.note_task(task)
        if not self._settings_written:
            self.write_settings()

    # ---- requests and estimates ---------------------------------------------------------------------------------
    def request(self, task: StructuredTask, payload: Mapping, key: str, context: Sequence[str] = (),
                max_tokens: int | None = None, suffix: str = "", backend: Backend | None = None) -> ChatRequest:
        b = backend or self.backend_for(key)
        prompt = task.prompt()
        return ChatRequest(model=b.model, system=prompt.system + tuple(context),
                           user=render_payload(prompt.template, payload) + suffix,
                           max_tokens=int(max_tokens or task.max_tokens), output_schema=task.schema,
                           effort=b.effort, task=task.name, key=key)

    def observations(self, task: StructuredTask, backend: Backend) -> dict:
        k = (task.name, task.prompt_sha, backend.provider)
        hit = self._obs.get(k)
        if hit is None or self._finished - hit[0] >= OBSERVATION_REFRESH:
            hit = (self._finished, backend.ledger.observations(task.name, task.prompt_sha))
            self._obs[k] = hit
        return hit[1]

    def estimate(self, task: StructuredTask, req: ChatRequest, backend: Backend) -> Estimate:
        """Live-price estimate in the backend's currency (batch pricing is applied by the plan / batch layer)."""
        return estimate(req, backend.price, self.observations(task, backend), task.unit_cost_default,
                        multiplier=provider_multiplier(backend.client))

    def plan_row(self, task: StructuredTask, payload: Mapping, key: str, context: Sequence[str] = ()) -> PlanRow:
        self.note_task(task)
        b = self.backend_for(key)
        req = self.request(task, payload, key, context, backend=b)
        cached = b.ledger.lookup(req) is not None
        est = self.estimate(task, req, b)
        return PlanRow(key=key, task=task.name, provider=b.provider, cached=cached, in_tokens=est.in_tokens,
                       out_low=est.out_low, out_high=est.out_high, cost_low=0.0 if cached else est.cost_low,
                       cost_high=0.0 if cached else est.cost_high, currency=b.currency,
                       batch_multiplier=b.price.batch_multiplier if b.batch_api else 1.0, observed=est.observed)

    def new_plan(self) -> Plan:
        plan = Plan(stage=self.stage, models=self.models, currency=self.currency, rates=dict(self.rates))
        plan.notes.extend(self.startup_notes)
        if len(self.split) > 1:
            plan.notes.append("provider split: " + ", ".join(f"{p} {w:.0%}" for p, w in sorted(self.split.items())))
        return plan

    # ---- running ----------------------------------------------------------------------------------------------------
    def run(self, task: StructuredTask, payload: Mapping, key: str, *, context: Sequence[str] = (),
            split: Splitter | None = None) -> TaskResult:
        self.note_task(task)
        return self._run(task, payload, key, tuple(context), split, 0, self.backend_for(key))

    def _finalize(self, task: StructuredTask, text: str) -> tuple[dict | list | None, list[str]]:
        if task.schema is None:
            return None, []
        data, err = parse_json(text)
        if err:
            return None, []
        data, truncated = apply_limits(data, task.limits)
        return data, truncated

    def _run(self, task: StructuredTask, payload: Mapping, key: str, context: tuple[str, ...], split: Splitter | None,
             depth: int, backend: Backend, max_tokens: int | None = None, suffix: str = "", attempts: int = 0,
             cost: float = 0.0, retried_malformed: bool = False, failover: bool = False) -> TaskResult:
        ledger = backend.ledger.for_failover() if failover else backend.ledger
        req = self.request(task, payload, key, context, max_tokens, suffix, backend)
        h = ledger.hash(req)
        cached = ledger.lookup(req)
        if cached is not None:
            attempts += 1
            ledger.cache_hit(req, key, task.prompt_sha, attempts)
            data, truncated = self._finalize(task, cached["response_text"])
            return TaskResult(key, "cache_local", data, cached["response_text"], h, attempts, cost, truncated,
                              provider=backend.provider, model_reported=(cached.get("usage") or {}).get("model_reported"),
                              failover=failover)
        if self.mode == "batch" and backend.batch_api:
            self._before_spend(task)
            self.queued.append(Queued(task, req, key, self.estimate(task, req, backend), backend.provider))
            return TaskResult(key, "queued", None, "", h, attempts, cost, provider=backend.provider, failover=failover)
        resp, status, data, errs, c = self._call(task, req, key, attempts + 1, backend, ledger)
        attempts += 1
        cost += c
        common = dict(provider=backend.provider, model_reported=resp.usage.model_reported, failover=failover)
        if status == "ok":
            data, truncated = apply_limits(data, task.limits) if task.schema is not None else (None, [])
            return TaskResult(key, "ok", data, resp.text, h, attempts, cost, truncated, **common)
        if status == "truncated":
            if task.splittable and split is not None and depth < MAX_SPLIT_DEPTH:
                parts = list(split(payload))
                if len(parts) >= 2:
                    results = [self._run(task, p, f"{key}/{i}", context, split, depth + 1, backend, failover=failover)
                               for i, p in enumerate(parts)]
                    return self._merged(key, h, results, attempts, cost, common)
            doubled = min(req.max_tokens * 2, self.max_tokens_cap)
            if max_tokens is None and doubled > req.max_tokens:
                log.info("%s: max_tokens reached at %d; retrying with %d", key, req.max_tokens, doubled)
                return self._run(task, payload, key, context, split, depth, backend, doubled, suffix, attempts, cost,
                                 retried_malformed, failover)
            return TaskResult(key, "failed:truncated", None, resp.text, h, attempts, cost,
                              error=f"max_tokens ({req.max_tokens}) reached", **common)
        if status == "malformed":
            if not retried_malformed:
                log.info("%s: malformed answer (%s); retrying once", key, "; ".join(errs)[:200])
                return self._run(task, payload, key, context, split, depth, backend, max_tokens, suffix + RETRY_SUFFIX,
                                 attempts, cost, True, failover)
            return TaskResult(key, "failed:malformed", data, resp.text, h, attempts, cost, error="; ".join(errs), **common)
        if status == "refused":
            return TaskResult(key, "failed:refused", None, resp.text, h, attempts, cost, error="refusal", **common)
        other = self._failover_backend(backend) if (self.failover and not failover) else None
        if other is not None:
            log.warning("%s: %s failed (%s); failing over to %s", key, backend.provider, "; ".join(errs)[:200], other.provider)
            return self._run(task, payload, key, context, split, depth, other, attempts=attempts, cost=cost, failover=True)
        return TaskResult(key, "failed:error", None, resp.text, h, attempts, cost, error="; ".join(errs) or "error", **common)

    def _merged(self, key: str, h: str, results: list[TaskResult], attempts: int, cost: float, common: dict) -> TaskResult:
        attempts += sum(r.attempts for r in results)
        cost += sum(r.cost for r in results)
        failed = [r for r in results if not r.ok]
        if failed:
            return TaskResult(key, failed[0].status, None, "", h, attempts, cost, error=failed[0].error,
                              parts=len(results), **common)
        data = merge_parts([r.data for r in results if isinstance(r.data, dict)])
        truncated = [t for r in results for t in r.truncated_by_code]
        return TaskResult(key, "ok", data, "", h, attempts, cost, truncated, parts=len(results), **common)

    def _call(self, task: StructuredTask, req: ChatRequest, key: str, attempt: int, backend: Backend, ledger: Ledger):
        est = self.estimate(task, req, backend)
        reservation = None
        if self.governor is not None:
            try:
                reservation = self.governor.reserve(self.to_primary(est.cost_high, backend.currency))
            except BudgetExceeded:
                ledger.budget_stop(req, key, task.prompt_sha, est.cost_high, attempt)
                raise
        self._before_spend(task)
        row = ledger.begin(req, key, task.prompt_sha, attempt, est.cost_high)
        try:
            resp = backend.client.complete(req)
        except BaseException as e:
            ledger.fail_pending(row, f"{type(e).__name__}: {e}")
            if self.governor is not None:
                self.governor.release(reservation)
            raise
        status, data, errs = classify(resp, task.schema)
        cost = ledger.finish(row, req, resp, status, validated=(status == "ok"), prompt_sha=task.prompt_sha,
                             multiplier=provider_multiplier(backend.client))
        self._finished += 1
        if self.governor is not None:
            self.governor.release(reservation)
        return resp, status, data, errs, self.to_primary(cost, backend.currency)


def run_task(runner: TaskRunner, task: StructuredTask, payload: Mapping, key: str, *, context: Sequence[str] = (),
             split: Splitter | None = None) -> TaskResult:
    """Module-level form of `TaskRunner.run` (the name the stages' shim used)."""
    return runner.run(task, payload, key, context=context, split=split)


def plan_items(runner: TaskRunner, items: Iterable[tuple]) -> Plan:
    """Dry-run plan for `(task, payload, key)` or `(task, payload, key, context)` tuples."""
    plan = runner.new_plan()
    for it in items:
        task, payload, key = it[0], it[1], it[2]
        context = it[3] if len(it) > 3 else ()
        plan.rows.append(runner.plan_row(task, payload, key, context))
    return plan


def make_runner(project: Project, stage: str, *, client: ChatClient | None = None,
                clients: Mapping[str, ChatClient] | None = None, run_id: str | None = None,
                build_id: str | None = None, mode: str | None = None, budget: float | None = None,
                store: LlmStore | None = None, governed: bool = True,
                tasks: Iterable[StructuredTask] = ()) -> TaskRunner:
    """Wire a runner from the project configuration. Clients: `client` = a single backend that takes every item
    (tests pass a `FakeChatClient`); `clients` = {provider: client} for the split; neither = `make_clients`.
    Also `llm.sqlite`, prices, the governor (budgets.run / stage / daily in the primary currency, `budget` =
    `--budget`), mode (`--mode`, default from `llm.batch_default`), `llm.failover`, `llm.fx`. `tasks` are
    registered for the settings snapshot up front. Leftover pending ledger rows are reported in
    `startup_notes` and logged. The runner closes the store it opened (`close()`)."""
    cfg = llm_config(project)
    if client is not None:
        clients = {client.provider: client}
        primary = client.provider
        split: dict[str, float] = {primary: 1.0}
    elif clients is not None:
        primary = primary_provider(cfg) if primary_provider(cfg) in clients else next(iter(clients))
        split = split_of(cfg)
    else:
        clients = make_clients(project)
        primary = primary_provider(cfg)
        split = split_of(cfg)
    models = models_of(cfg)
    for p in clients:
        models.setdefault(p, str(cfg.get("model") or ""))
    owns = store is None
    store = store or LlmStore.open(project.llm_db)
    run_id = run_id or new_run_id(stage)
    mode = mode or ("batch" if cfg.get("batch_default", True) else "live")
    currency = clients[primary].currency
    rates = fx_rates(cfg, currency)
    gov = governor_for(project, store, currency, run_id=run_id, stage=stage, budget=budget, rates=rates) if governed else None
    runner = TaskRunner(clients, store, price_table(project), run_id=run_id, stage=stage, models=models, split=split,
                        primary=primary, effort=cfg.get("effort") or None, build_id=build_id, mode=mode, governor=gov,
                        max_tokens_cap=int(cfg.get("max_tokens_cap", 65536)), failover=bool(cfg.get("failover", False)),
                        rates=rates, runs_dir=project.work_dir / "runs", owns_store=owns)
    for t in tasks:
        runner.note_task(t)
    notes = [report_unknown_pending(store)]
    notes += [idle_notice(b.client, bool(cfg.get("prefer_idle", False))) for b in runner.backends.values()]
    for note in notes:
        if note:
            log.warning("%s", note)
            runner.startup_notes.append(note)
    return runner


def read_settings(project: Project, run_id: str) -> dict | None:
    """The settings snapshot of a run (`atlas cost report --run`), None when the run wrote none."""
    p = project.work_dir / "runs" / run_id / SETTINGS_FILE
    if not p.is_file():
        return None
    return json.loads(p.read_text(encoding="utf-8"))
