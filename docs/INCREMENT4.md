# Increment 4 + 6 — LLM layer and the curation pipeline (pipeline only)

Status: contract for implementation (2026-10-23). Architecture sections: §6 (stages 2 `terms`, 3 `concepts`, 4 `scheme`, 7 `classify`), §7 (gates), §9 (LLM layer), §10 (checks, blind review), §14 (config). Increments 1–2 are in `docs/INCREMENT1.md` / `docs/INCREMENT2.md`; their ids, schemas and routes stay as they are.

**Scope and the one hard rule.** This increment delivers code, tests and dry runs only. No real model call is made by any test or by any stage run in this increment: the user's API key is not configured yet, and the pipeline is to be *built*, not *run*. Every stage works end to end with `FakeChatClient`; the only command that may spend money is `atlas llm ping`, which the user runs by hand later. A guard makes this enforceable: when the environment variable `ATLAS_LLM_NO_NETWORK=1` is set (the test suite sets it in `tests/conftest.py`), `AnthropicClient` and `DeepSeekClient` raise `NetworkForbidden` before any HTTP call.

Provider: the **Anthropic Python SDK** (`anthropic` 2.1.0 is installed), default model `claude-opus-5-5`. The SDK documentation extracted for this session is at `C:\Users\sun\AppData\Local\Temp\claude\bundled-skills\2.1.308\8322e67932c3940826f391918b9f6bbc\claude-api\` — read `python/claude-api/README.md` (client, caching, thinking/effort, usage fields, errors), `python/claude-api/tool-use.md` § Structured Outputs (`output_config={"format": {"type": "json_schema", "schema": …}}`: the first text block is valid JSON for the schema), `python/claude-api/batches.md` (Message Batches: `client.messages.batches.create(requests=[Request(custom_id, params=MessageCreateParamsNonStreaming(...))])`, `retrieve`, `results`; 50 % price), and `shared/prompt-caching.md`. Facts that matter here: Claude Opus 5.5 — thinking is always on (omit the `thinking` parameter), depth is `output_config.effort` (`low|medium|high|xhigh|max`, default `medium`), `temperature`/`top_p` are rejected, no assistant prefill, no forced `tool_choice`; prices (USD per million tokens) input 4, output 20, cache write 5, cache read 0.20; Sonnet 5.5 2 / 10 / 2.5 / 0.20; Haiku 5.5 0.10 / 0.50 / 0.125 / 0.01; Message Batches halve every token price; `response.usage` carries `input_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`, `output_tokens`; `stop_reason` is `end_turn | max_tokens | refusal | …` and a refusal has `stop_details`. Never guess SDK method names: verify by `inspect.signature` on the installed client when a shape is not in those files.

The shared base module `atlas/llm/base.py` (dataclasses, the `ChatClient` protocol, `FakeChatClient`, `NetworkForbidden`) is written before the work packages start and is **frozen**: both packages code against it; additions go in new modules.

---

## 1. LLM layer (`atlas/llm/`, work package L)

### 1.1 Requests and responses (`base.py`, frozen)

```python
@dataclass(frozen=True)
class ChatRequest:
    model: str
    system: tuple[str, ...]          # stable blocks first; the LAST block gets cache_control (prefix caching)
    user: str                        # the rendered payload (one user turn)
    max_tokens: int
    output_schema: dict | None       # JSON schema → output_config.format (json_schema); None = free text
    effort: str | None = None        # low|medium|high|xhigh|max (anthropic only)
    task: str = ""                   # task name, for the ledger
    key: str = ""                    # caller's item key (custom_id in batches)

@dataclass
class ChatResponse:
    text: str                        # first text block (JSON when a schema was given)
    stop_reason: str                 # end_turn | max_tokens | refusal | error
    usage: Usage                     # input, cache_write, cache_read, output (tokens), plus model_reported
    latency_ms: int
    raw_id: str | None               # provider message id
    error: str | None = None
```

`ChatClient` protocol: `complete(req) -> ChatResponse`, `count_tokens(req) -> int | None`, `submit_batch(reqs) -> str`, `batch_status(batch_id) -> BatchStatus`, `batch_results(batch_id) -> dict[str, ChatResponse]` (keyed by `req.key`), `provider: str`, `currency: str`. `FakeChatClient(responses: dict[str, str | callable] | None, default=…)` answers by `req.key` or by `req.task`, records every request in `.requests`, and can simulate `max_tokens`, malformed JSON, `refusal` and errors; its batches resolve immediately. `request_hash(provider, req)` = sha256 of the canonical JSON of (provider, model, system, user, max_tokens, output_schema, effort).

### 1.2 Providers (`client.py`)

- `AnthropicClient(model, effort, max_retries=2)`: `anthropic.Anthropic()` zero-arg (key from `ANTHROPIC_API_KEY`); `messages.create(model, max_tokens, system=[{type: text, text, cache_control: {type: ephemeral}} on the last block], messages=[{role: user, content}], output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}} )` — `format` only when a schema is given; never send `thinking`, `temperature`, `top_p`; `count_tokens` through `messages.count_tokens`. Enable the refusal fallback (`fallbacks="default"` with beta `server-side-fallback-2026-07-01`, via `client.beta.messages.create`) **only if** the installed SDK accepts it together with `output_config` (check the signature; otherwise leave it out and document why); map `stop_reason == "refusal"` to the response unchanged (the task layer records `failed:refused`, never retries). Typed error chain per the README: `RateLimitError` / `APIStatusError >= 500` / `APIConnectionError` are retryable at the transport level (the SDK's own retries + one outer attempt), `BadRequestError`/`AuthenticationError`/`NotFoundError` are not.
- `DeepSeekClient`: urllib POST `{base_url}/chat/completions` with JSON mode, the legacy `extra_body` pass-through and the peak-hour logic (UTC+8 weekdays 09–12 / 14–18); currency RMB. Kept so the project config can switch providers; no new features.
- `make_client(project) -> ChatClient` from the `llm` block of `project.yaml`/`defaults.yaml`: `provider: anthropic|deepseek|fake`, `model`, `effort`, `api_key_env`, `max_tokens`, `batch_default`, `pricing` (per model: `input, output, cache_write, cache_read` per million, `currency`, `batch_multiplier: 0.5`).
- **Key resolution.** The user keeps the Anthropic key in the Windows *user* environment variable `RS_ANTHROPIC_API_KEY` — deliberately not `ANTHROPIC_API_KEY`, which Claude Code itself would pick up — so `llm.api_key_env` defaults to `RS_ANTHROPIC_API_KEY` and the value is passed explicitly (`anthropic.Anthropic(api_key=…)`). Resolution order: process environment → Windows registry user environment (`HKCU\Environment`, via `winreg`; the desktop app's host process keeps a stale environment until it is fully restarted) → system environment; non-Windows: process environment only. The key is never printed, logged or written; `atlas llm ping` reports only "key: found (N chars) via env|registry".
- **Provider split** (user policy of 2026-10-23: Claude API first, about 9:1 Claude:DeepSeek, settings kept so the two can be compared): `llm.split: {anthropic: 0.9, deepseek: 0.1}` (absent → 1.0 for the configured provider); `choose_provider(key, split)` assigns each item deterministically by sha1 of its key; no failover unless `llm.failover: true` (failed-over rows tagged); every ledger row and task result carries `provider` and `model_reported`; `atlas cost report` breaks totals down by provider; `make_clients(project) -> dict[provider, ChatClient]`.
- **Run settings snapshot.** Each stage run writes `work/runs/<run_id>/llm_settings.json` (split, models, effort, max_tokens, prompt_sha per task, pricing version, mode) and the ledger keeps `run_id`; `atlas cost report --run <id>` prints that header.

### 1.3 Tasks (`tasks.py`, prompts in `atlas/llm/prompts/<task>.md`)

`StructuredTask(name, prompt_file, schema, limits: dict[list_path, cap], max_tokens, splittable: bool, unit_cost_default)`. A prompt file has two sections separated by a `---payload---` line: the stable system text (bilingual instructions, the v3 term-type vocabulary where relevant, the evidence rule "every output item cites sids from the input; nothing without a sentence") and a payload template rendered with `str.format` from a pure payload builder (sorted keys, sorted sids, no timestamps, no build ids — a unit test asserts that two builds produce the same `request_hash` for the same item). `prompt_sha = sha256(prompt file bytes)` enters every ledger row, fingerprint and draft. Validation: the API guarantees schema-valid JSON when `output_config.format` is used; the task layer still validates locally (`validate(schema)`: type / required / enum / maxItems / maxLength, pure Python, no jsonschema dependency), truncates lists to `limits` and records `truncated_by_code`. Policy by `stop_reason`: `max_tokens` → split the item in two when `splittable` else one retry with `max_tokens × 2` (≤ the model cap) else `failed:truncated`; invalid JSON → one "return only the JSON object" retry (different hash) else `failed:malformed`; `refusal` → `failed:refused`, no retry; every attempt is billed into the ledger.

### 1.4 Ledger and cache (`ledger.py`, `store/llm.py`, `store/sql/llm.sql`)

`llm.sqlite` (per project): `calls(request_hash PK, provider, model, task, prompt_sha, request_json, response_text, stop_reason, usage_json, cost, currency, status, validated, created)` — lookups hit only `status='ok' AND validated=1`; `ledger(id, ts, run_id, build_id, stage, item_key, task, prompt_sha, request_hash, attempt, status pending|ok|cache_local|cache_batch|truncated|malformed|refused|error|budget_stop, in_tokens, cache_write_tokens, cache_read_tokens, out_tokens, est_cost, cost, currency, latency_ms, model_reported, batch_id)` — a row is inserted as `pending` with the estimate **before** the HTTP call and updated after; `batches(batch_id, provider, model, created, status, n_requests, n_done, n_failed, cost)`; `balance_probes` (DeepSeek only); `unit_costs(task, prompt_sha, unit, cost_median, n, measured_at)`. Cost = `in × p_in + cache_write × p_write + cache_read × p_read + out × p_out` (× `batch_multiplier` for batch rows), in the provider currency. Startup reports leftover `pending` rows as unknown spend. Only `store/db.py` connects.

### 1.5 Governor (`governor.py`)

Scopes: run (`--budget`), stage default (`budgets.stage.<name>`), project daily (`budgets.daily.total`), in the provider currency. Submit reserves the high estimate (`budget − reserved − spent ≥ estimate`), releases on completion; `BudgetExceeded` stops the stage cleanly (completed items stay valid, the plan for the rest is printed). `--dry-run` prints the plan — items, known cache hits (exact hash lookup), low/high token and cost estimates (low: ledger-observed cache ratio and median output; high: cache misses and `max_tokens`; +6 % retry overhead) — and exits without any call. `prefer_idle` applies only to providers with peak pricing (DeepSeek).

### 1.6 Batches (`batch.py`)

Stages run in `--mode live|batch` (default from `llm.batch_default`, true). Batch mode: requests of one stage run are submitted as one Message Batch (`custom_id` = item key; cap 100,000 / 256 MB → chunk), the ledger gets `pending` rows with `batch_id`, `atlas llm batches status|collect <batch_id>` polls and writes results (`cache_batch` rows with halved prices); `atlas run <stage> --mode batch` submits and returns immediately with the batch id, `--wait` polls until done. Collecting is idempotent.

### 1.7 CLI

`atlas llm ping` (the only spending command: one 20-token request, prints model, usage, cost, request id; refuses under `ATLAS_LLM_NO_NETWORK`), `atlas llm batches list|status|collect`, `atlas cost report [--run id] [--stage s]` (ledger totals, cache hit rate, unknown pending), `atlas cost calibrate` (refresh `unit_costs` from the ledger).

### 1.8 Tests (`tests/unit/test_llm_*.py`)

With `FakeChatClient`: request hash determinism; prompt rendering and `prompt_sha`; validation and caps; the truncation / malformed / refusal policies; ledger pending→ok and cost arithmetic incl. cache tokens and the batch multiplier; governor reservations and `BudgetExceeded`; batch submit/collect idempotence; dry-run plan output; `AnthropicClient` request construction with a monkeypatched SDK client object (no network): system blocks with `cache_control` on the last block, `output_config` shape, no `thinking`/`temperature`; the `NetworkForbidden` guard.

---

## 2. Curation stages (work package S)

### 2.1 Sampling (`core/sample.py`, pure)

`stratified_sample(rows, strata=("unit_kind|section", "venue", "year_bin"), n, per_doc_cap, seed)` over body sentences (`is_body`, not `references`/`appendix`), deterministic (seeded, sorted input), returns sids with doc/unit/section; `--pilot N` restricts to N documents chosen deterministically (by `document_meta.extra.proximity_tier` when present, else by doc id). Chunks of ≤ 40 sentences of one section type, chunk key = sha of its sids.

### 2.2 Stages

| stage | version | items | task → output | draft | gate |
|---|---|---|---|---|---|
| `terms` | `terms1` | sample chunks | `terms_extract`: sentences with sids → `{terms: [{term, zh, type ∈ TERM_TYPES, gloss ≤ 20 words, sids ≤ 3}]}`, cap 25/chunk | `work/drafts/terms/<run>.yaml`: term → {votes, types, sections, example sids, docs} + report | accept gate (blind review ≥ `qa.accept_threshold`) |
| `concepts` | `concepts1` | candidate groups | rules first (`core/matcher.norm_key`, plural/hyphen/case variants, shared sids → groups), then `concepts_merge` on each group → `{concepts: [{id C_slug, label, zh, type, forms[], case_forms[], merged_terms[], note}]}`; with confirmed concepts present → delta proposals `add | merge_into | new_form | retire` | `work/drafts/concepts/<run>.yaml` + `patterns` draft, or `<run>.delta.yaml` | edit gate (`concepts.yaml`, `patterns.yaml`; no duplicate forms across concepts — checked) |
| `scheme` | `scheme1` | one item (titles + abstracts sample + top-N concepts by document frequency) | `scheme_draft` → `{domains, categories, subdomains with anchors (concept ids)}`; anchors must resolve | `work/drafts/scheme/<run>.yaml` → `domains/categories/subdomains.yaml` | edit gate ×3 |
| `classify` | `classify1` | concepts | entry + ≤ 10 deterministic example sentences + scheme → `{domain, primary, secondary, reason}` | `work/drafts/classify/<run>.yaml` | accept gate; `curated/overrides/categories.yaml` patches |

`TERM_TYPES` = the v3 vocabulary (名词术语, 量与指标, 方法或模型名, 句式短语, 通用句式, 软件、设备或材料品牌, 期刊名, 缩写) so the two systems stay comparable. Every stage: `items()` with fingerprints `(stage version, prompt_sha, item content sha)`, `run_item` builds the payload and calls the task through the ledger/governor, `finish` aggregates and writes the draft and `report.md`; `--dry-run` prints the plan; `--pilot N`; `--mode live|batch`; `--limit`. Stages never write `curated/` (the existing static + runtime test is extended to the new stages).

### 2.3 Gates and review

`pipeline/gates.py`: `atlas curate merge <draft>` — three-way merge into the curated file (add / conflict / retire report, `source: owner|llm`, `added_in: <run>`; user edits preserved), `atlas curate confirm <name>`, `atlas curate status` (draft newer than confirmation → notice). `verify/qa.py`: `atlas qa sample --stage terms|classify --n 50 --seed s` → `evalset/<stage>_<run>.xlsx` (openpyxl: key, item, evidence sentences cleaned, judgement column empty) + jsonl keyed by `(task, prompt_sha, scheme sha, run, item)`; `atlas qa record <xlsx>` → `evalset/judgments/*.jsonl`; `atlas qa report` → accuracy per stage/stratum; the accept gate of `terms`/`classify` reads it and refuses `--full` without a passing pilot report for the same `(task, prompt_sha)`.

### 2.4 Runner

`STAGES = ["ingest", "terms", "concepts", "scheme", "match", "assign", "classify", "finish"]`; `requires` chain (`concepts` needs a `terms` draft merged or confirmed concepts; `scheme` needs confirmed concepts; `match` needs confirmed concepts + patterns; `classify` needs a build with `assign` done + confirmed scheme); gated stages check `confirmations.json` shas with the existing message. LLM stages take `--dry-run`, `--pilot`, `--mode`, `--budget`, `--wait`.

### 2.5 Tests

`tests/test_curation_pipeline.py`: fixture corpus → `FakeChatClient` with canned, schema-valid answers → `run terms --pilot 3` → `curate merge` → `run concepts` → `curate merge` + `confirm` → `run scheme` → confirm ×3 → `build new` → `match` → `assign` → `classify` → `qa sample/record/report` → accept → `finish`; asserts draft and curated shapes, fingerprints, gate messages, dry-run plans (tokens and cost columns), the no-network guard, and that `curated/` was written only by `curate` commands. Unit tests for sampling (determinism, strata, caps), the merge (three-way cases), qa sheet round trip.

---

## 3. Ownership and acceptance

| WP | owns | must not touch |
|---|---|---|
| **L** | `atlas/llm/{client,tasks,ledger,governor,batch}.py`, `atlas/store/llm.py`, `atlas/store/sql/llm.sql`, `atlas/defaults.yaml` (llm + budgets + pricing), `atlas/cli.py` verbs `llm`, `cost`, `tests/unit/test_llm_*.py`, `tests/conftest.py` (sets `ATLAS_LLM_NO_NETWORK=1`) | stages, gates, qa, prompts of the stages |
| **S** | `atlas/core/sample.py`, `atlas/pipeline/stages/{terms,concepts,scheme,classify}.py`, `atlas/pipeline/gates.py` (merge), `atlas/pipeline/runner.py` (STAGES, options), `atlas/verify/qa.py`, `atlas/llm/prompts/{terms_extract,concepts_merge,scheme_draft,classify}.md`, `atlas/cli.py` verbs `run <new stages>`, `curate merge`, `qa`, `tests/test_curation_pipeline.py`, `tests/unit/test_sample.py`, `test_gates_merge.py`, `test_qa.py` | `atlas/llm/*.py` other than prompts (code against `base.py`; use `FakeChatClient` and a thin local `run_task(client, task, payload)` shim until integration swaps in L's `tasks.py`) |

Integration merges L then S, replaces the shim with the real task runner, runs the suite, then the dry runs: `atlas run terms H:/projects/atlas-projects/ucl --dry-run --pilot 20` and `--dry-run` without `--pilot` (prints sample composition by stratum, chunk count, token and cost estimates for `claude-opus-5-5` live and batch — expected pilot ≤ $5, full ≤ $35), and `atlas cost report` on the empty ledger. Acceptance: suite green; no test or dry run performs a network call (guard active); documented commands in README; `docs/INCREMENT4.md` gains a Status section with the dry-run numbers; nothing under `H:\UCL_theses` touched; the femstem and UCL projects unchanged except `llm/llm.sqlite` created empty by the dry run.
