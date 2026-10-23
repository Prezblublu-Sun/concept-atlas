"""Shared machinery of the LLM curation stages `terms`, `concepts`, `scheme`, `classify` (increment 4 §2.2).

A stage subclasses `LlmStage` and implements `prepare` (read its inputs; pure data in a context dict), `items`
(the work units: key, pure payload, bookkeeping meta, optional per-run context blocks), `schema`, and `finish`
(aggregate the task results into the draft body). The base class does the rest, in this order:

  prepare → items → fingerprints (stage version, prompt sha, item content sha) → task runner (`atlas.llm.tasks`:
  one backend per provider of `llm.split`, the ledger, the governor) → plan (`plan_items`: per item its provider,
  known cache hits, tokens, low / high cost; totals live and batch, per provider) → `--dry-run` stops here, without
  a call and without a file (the ledger is opened read-only, or in memory when the project has none yet) → accept
  gate (a full run of `terms` / `classify` needs a passing blind review for the same task + prompt sha, `atlas qa
  report`) → `--budget` trims the item list to what the high estimates allow (the governor also holds the run,
  stage and daily budgets while calling) → `runner.run` per item (`--mode live`) or queue + `submit_queued` +
  collect (`--mode batch`, `--wait`) → results → finish → draft `work/drafts/<stage>/<run_id>.yaml` +
  `<run_id>.report.md`.

The ledger's run id of a stage run is `<stage>_<run_id>` (`terms_r2026-10-23_001`): every ledger row of the run,
the settings snapshot `work/runs/<ledger run id>/llm_settings.json` and `atlas cost report --run <id>` use it; the
draft keeps it as `ledger_run_id`. Per-item usage and cost in the draft come from the ledger rows of the run (every
attempt, batch rows at batch prices, cache hits at zero), converted into the primary provider's currency; every
item row names the provider that answered it.

Batch mode: cache misses are queued and submitted as Message Batches (one per provider and chunk); when a batch is
still running the stage writes `<run_id>.pending.yaml` (keys, fingerprints, batch ids) and returns; the next run
with the same items (`--wait` polls) collects the batches into the ledger and re-runs the items, which are then
local cache hits (`cache_local`) — the rerun-collects-cache_local pattern. A provider without a batch API (DeepSeek)
answers its share live inside the same run.

Guard: with ATLAS_LLM_NO_NETWORK set, a non-dry run of a stage whose provider is not `fake` raises
NetworkForbidden before a runner (and so before any provider client) exists; a dry run still works. A missing API
key of any provider of the split is refused before the first call.

Stages never write `curated/` (only `atlas curate merge|confirm` do); a dry run writes nothing at all.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

from atlas.config import Project, deep_merge
from atlas.core.ids import canonical_json
from atlas.llm.base import ChatClient, ChatResponse, FakeChatClient, NetworkForbidden, estimate_tokens, network_forbidden
from atlas.llm.batch import collect, submit_queued, wait_for_batch
from atlas.llm.client import llm_config, models_of, primary_provider, resolve_secret
from atlas.llm.governor import BudgetExceeded, Plan
from atlas.llm.ledger import RETRY_OVERHEAD, price_table
from atlas.llm.tasks import PROMPTS_DIR, StructuredTask, TaskResult, TaskRunner, load_prompt, make_runner, plan_items
from atlas.llm.tasks import classify as classify_response
from atlas.pipeline.runner import Report, StageError
from atlas.store.db import MEMORY
from atlas.store.llm import LlmStore

log = logging.getLogger(__name__)

DRAFTS_DIR = ("work", "drafts")
RUN_ID_RE = re.compile(r"^r(\d{4}-\d{2}-\d{2})_(\d{3})$")
PENDING_SUFFIX = ".pending.yaml"
REPORT_SUFFIX = ".report.md"
DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_POLL_SECONDS = 30.0
#: ledger statuses of a batch row that mean the request was answered but failed
FAILED_LEDGER_STATUSES = ("truncated", "malformed", "refused", "error")
#: draft item statuses that are not failures
NOT_FAILED = ("ok", "cache_local", "pending", "skipped")


def _now() -> str:
    return datetime.fromtimestamp(time.time(), tz=timezone.utc).isoformat(timespec="seconds")


def clean_text(s: str | None) -> str:
    return " ".join((s or "").split())


@dataclass
class Item:
    """One work unit of a stage: `payload` is the pure input of the payload template (sorted keys, sorted sids, no
    timestamps or build ids), `meta` what `finish` needs (never part of the request), `context` per-run system
    blocks appended after the prompt's stable text (the scheme for `classify`; the last block carries the cache
    breakpoint)."""
    key: str
    payload: dict
    meta: dict = field(default_factory=dict)
    context: tuple[str, ...] = ()

    @property
    def content_sha(self) -> str:
        return hashlib.sha256(canonical_json(self.payload).encode("utf-8")).hexdigest()


def fingerprint(version: str, prompt_sha: str, content_sha: str) -> str:
    return f"{version}:{prompt_sha[:12]}:{content_sha[:16]}"


def ledger_run_id(stage: str, run_id: str) -> str:
    """The ledger / settings-snapshot id of a stage run: `terms_r2026-10-23_001`."""
    return f"{stage}_{run_id}"


# ---- drafts ------------------------------------------------------------------------------------------------------

def drafts_dir(project: Project, stage: str) -> Path:
    return project.root.joinpath(*DRAFTS_DIR, stage)


def run_id_of(path: str | Path) -> str:
    """'r2026-10-23_001' from any draft file name (…/r2026-10-23_001.delta.yaml)."""
    return Path(path).name.split(".", 1)[0]


def new_run_id(folder: Path, today: str | None = None) -> str:
    day = today or datetime.now().strftime("%Y-%m-%d")
    seqs: list[int] = []
    if folder.is_dir():
        for p in folder.iterdir():
            m = RUN_ID_RE.match(run_id_of(p))
            if m and m.group(1) == day:
                seqs.append(int(m.group(2)))
    return f"r{day}_{max(seqs, default=0) + 1:03d}"


def list_drafts(project: Project, stage: str) -> list[Path]:
    """Draft files of a stage, oldest first (`<run>.yaml` and `<run>.delta.yaml`; pending / report files left out)."""
    d = drafts_dir(project, stage)
    if not d.is_dir():
        return []
    out = [p for p in d.iterdir() if p.is_file() and p.suffix == ".yaml" and not p.name.endswith(PENDING_SUFFIX)
           and RUN_ID_RE.match(run_id_of(p))]
    return sorted(out, key=lambda p: (run_id_of(p), p.name))


def newest_draft(project: Project, stage: str) -> Path | None:
    ds = list_drafts(project, stage)
    return ds[-1] if ds else None


def read_draft(path: str | Path) -> dict:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"not a draft file: {path}")
    return data


def write_draft(path: str | Path, data: Mapping) -> str:
    """Insertion-ordered YAML (the head keys first), UTF-8, atomic; returns the file's sha256."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        yaml.safe_dump(dict(data), f, allow_unicode=True, sort_keys=False, default_flow_style=False, width=120)
    os.replace(tmp, p)
    return hashlib.sha256(p.read_bytes()).hexdigest()


# ---- project-level LLM settings -----------------------------------------------------------------------------------

def model_for(project: Project, override: str | None = None) -> str:
    """The primary provider's model of a run: `--model` when the price table (`llm.pricing`) knows it, else the
    configured model (`llm.models.<primary>` / `llm.model`) when it has a price row, else the contract's default."""
    cfg = llm_config(project)
    known = price_table(project).prices
    configured = models_of(cfg).get(primary_provider(cfg)) or str(cfg.get("model") or "")
    if override and str(override) in known:
        return str(override)
    return configured if configured in known else DEFAULT_MODEL


def project_with_model(project: Project, model: str) -> Project:
    """The project with `llm.model` (the primary provider's model) set to `model` for one run; the project itself
    when that is already the configured model."""
    cfg = llm_config(project)
    current = models_of(cfg).get(primary_provider(cfg)) or str(cfg.get("model") or "")
    if not model or model == current:
        return project
    return Project(root=project.root, config=deep_merge(project.config, {"llm": {"model": model}}))


def draft_status(r: TaskResult) -> str:
    """The task layer's status in the draft's vocabulary: ok | cache_local | invalid (JSON parsed, schema or stage
    check failed) | malformed (no JSON) | truncated | refused | error | queued | budget_stop."""
    s = r.status[7:] if r.status.startswith("failed:") else r.status
    if s == "malformed" and r.data is not None:
        return "invalid"
    return s


# ---- the base stage ------------------------------------------------------------------------------------------------

class LlmStage:
    name = ""
    version = ""
    task = ""
    prompt_file = ""
    llm = True
    requires: tuple[str, ...] = ()
    gate: tuple[str, ...] = ()
    needs_build = False
    accept_gate = False          # a full run (no --pilot) needs a passing blind review (verify/qa.py)
    max_tokens = 2000
    caps: dict[str, int] = {}    # list caps in the task layer's path grammar: `terms`, `terms[].sids`
    splittable = False
    unit_cost_default = 0.0

    # ---- to implement per stage
    def prepare(self, project: Project, build, **opts) -> dict:
        raise NotImplementedError

    def items(self, ctx: dict) -> list[Item]:
        raise NotImplementedError

    def schema(self, ctx: dict) -> dict:
        raise NotImplementedError

    def finish(self, ctx: dict, items: Sequence[Item], results: Mapping[str, TaskResult], run: dict) -> dict:
        """The stage-specific body of the draft (keys after the head). May record stage items in a build."""
        raise NotImplementedError

    def draft_name(self, ctx: dict, run_id: str) -> str:
        return f"{run_id}.yaml"

    def report_lines(self, ctx: dict, draft: Mapping) -> list[str]:
        return []

    # ---- the task
    @property
    def prompt_path(self) -> Path:
        return PROMPTS_DIR / f"{self.prompt_file}.md"

    @property
    def prompt_sha(self) -> str:
        return load_prompt(self.prompt_path).sha

    def structured_task(self, ctx: dict) -> StructuredTask:
        return StructuredTask(name=self.task, prompt_file=self.prompt_path, schema=self.schema(ctx), limits=dict(self.caps),
                              max_tokens=int(self.max_tokens), splittable=bool(self.splittable),
                              unit_cost_default=float(self.unit_cost_default))

    # ---- run
    def run(self, project: Project, build=None, *, dry_run: bool = False, pilot: int | None = None,
            mode: str | None = None, budget: float | None = None, wait: bool = False, limit: int | None = None,
            seed: int | None = None, client: ChatClient | None = None, model: str | None = None, **opts) -> Report:
        report = Report(stage=self.name)
        t0 = time.perf_counter()
        prompt_sha = self.prompt_sha
        mode = str(mode or ("batch" if project.get("llm.batch_default", True) else "live")).lower()
        if mode not in ("live", "batch"):
            raise StageError(f"unknown mode {mode!r} (live | batch)")
        ctx = self.prepare(project, build, pilot=pilot, seed=seed, **opts)
        ctx.setdefault("pilot", pilot)
        items = self.items(ctx)
        if limit is not None and int(limit) >= 0:
            items = items[:int(limit)]
        fps = {it.key: fingerprint(self.version, prompt_sha, it.content_sha) for it in items}
        report.items_total = len(items) + int(ctx.get("n_skipped") or 0)
        report.items_skipped = int(ctx.get("n_skipped") or 0)
        report.details["fingerprints"] = fps
        report.details["prompt_sha"] = prompt_sha
        for w in ctx.get("warnings") or []:
            report.warnings.append(w)
        task = self.structured_task(ctx)
        model = model_for(project, model)
        proj = project_with_model(project, model)
        folder = drafts_dir(project, self.name)
        pending = self._pending_for(folder, fps) if (mode == "batch" and not dry_run) else None
        run_id = str(pending["run_id"]) if pending else new_run_id(folder)
        lrid = ledger_run_id(self.name, run_id)
        build_id = getattr(build, "build_id", None)
        runner, store_to_close = self._runner(proj, lrid, build_id, mode, budget, client, dry_run=dry_run, task=task)
        try:
            plan = self._plan_dict(runner, plan_items(runner, [(task, it.payload, it.key, it.context) for it in items]),
                                   task, items, ctx, model)
            plan["pilot"] = pilot
            report.details["plan"] = plan
            if dry_run:
                report.details["dry_run"] = True
                log.info("%s: dry run, %d items, %s %.4f–%.4f", self.name, len(items), plan["currency"], plan["cost_low"],
                         plan["cost_high"])
                return report
            for note in runner.startup_notes:
                report.warnings.append(note)
            if self.accept_gate and pilot is None and items:
                self._check_accept(project, prompt_sha)
            budget_stop: list[str] = []
            if budget is not None and items:
                items, budget_stop = self._fit_budget(items, plan, float(budget), mode)
                if budget_stop:
                    report.warnings.append(f"budget {budget} {plan['currency']}: {len(budget_stop)} of "
                                           f"{len(items) + len(budget_stop)} items left out (high estimate)")
            results: dict[str, TaskResult] = {}
            batch_ids: list[str] = []
            if items:
                runner.write_settings()
                if mode == "live":
                    results = self._run_live(runner, task, items, budget_stop, report)
                else:
                    poll = float(project.get("llm.batch_poll_seconds") or DEFAULT_POLL_SECONDS)
                    batch_ids, got = self._run_batch(runner, task, items, wait=wait, pending=pending, poll_seconds=poll)
                    if got is None:
                        self._write_pending(folder, run_id, lrid, batch_ids, items, fps)
                        report.details.update({"batch_id": batch_ids[0] if batch_ids else None, "batch_ids": batch_ids,
                                               "run_id": run_id, "ledger_run_id": lrid, "pending": True})
                        report.warnings.append(f"batch {', '.join(batch_ids)} submitted ({len(items)} items); run again "
                                               "with --wait (or --mode batch --wait) to collect it")
                        return report
                    results = got
                    p = folder / f"{run_id}{PENDING_SUFFIX}"
                    if p.is_file():
                        p.unlink()
            usage = self._usage_from_ledger(runner, lrid, [it.key for it in items])
            settings = runner.settings_path() if runner.settings_path() and runner.settings_path().is_file() else None
            run = {"run_id": run_id, "ledger_run_id": lrid, "prompt_sha": prompt_sha, "model": model, "models": dict(runner.models),
                   "provider": runner.primary, "split": dict(runner.split), "mode": mode,
                   "batch_id": batch_ids[0] if batch_ids else None, "batch_ids": batch_ids, "fingerprints": fps,
                   "currency": runner.currency, "settings": str(settings) if settings else None}
            body = self.finish(ctx, items, results, run)
            head = self._head(ctx, items, results, run, pilot, seed, budget_stop, usage)
            draft = {**head, **body}
            path = folder / self.draft_name(ctx, run_id)
            sha = write_draft(path, draft)
            rpath = folder / f"{run_id}{REPORT_SUFFIX}"
            rpath.write_text(self._report_md(ctx, draft, path, sha), encoding="utf-8")
            for it in items:
                r = results.get(it.key)
                if r is None:
                    continue
                if r.ok:
                    report.items_done += 1
                else:
                    report.items_failed += 1
                    report.warnings.append(f"{it.key}: {draft_status(r)}: {r.error or ''}".rstrip(": "))
            report.details.update({"run_id": run_id, "ledger_run_id": lrid, "draft": str(path), "draft_sha": sha,
                                   "report": str(rpath), "cost": head["cost"], "currency": head["currency"],
                                   "usage": head["usage"], "providers": head["providers"], "settings": run["settings"],
                                   "seconds": round(time.perf_counter() - t0, 2), "batch_id": run["batch_id"],
                                   "batch_ids": batch_ids})
            log.info(report.summary())
            return report
        finally:
            runner.close()
            if store_to_close is not None:
                store_to_close.close()

    # ---- the runner
    def _runner(self, project: Project, run_id: str, build_id: str | None, mode: str, budget: float | None,
                client: ChatClient | None, *, dry_run: bool, task: StructuredTask) -> tuple[TaskRunner, LlmStore | None]:
        """The task runner of this run. Provider `fake` gets a FakeChatClient that answers `{}` (tests pass their own
        client); a real provider is refused under ATLAS_LLM_NO_NETWORK before any client exists; a dry run opens
        the ledger read-only (in memory when the project has none) so that it creates nothing. Returns the runner
        and the store the stage must close itself (None when the runner owns it)."""
        provider = primary_provider(llm_config(project))
        if client is None and provider == "fake":
            client = FakeChatClient(default="{}")
        if client is None and not dry_run and network_forbidden():
            raise NetworkForbidden(f"ATLAS_LLM_NO_NETWORK is set: provider {provider!r} would call the network "
                                   "(use --dry-run, or llm.provider: fake)")
        store: LlmStore | None = None
        if dry_run:
            store = LlmStore.open(project.llm_db, "ro") if project.llm_db.is_file() else LlmStore.open(MEMORY)
        runner = make_runner(project, self.name, client=client, run_id=run_id, build_id=build_id, mode=mode,
                             budget=budget, store=store, tasks=[task])
        if not dry_run:
            self._check_keys(runner)
        return runner, store

    @staticmethod
    def _check_keys(runner: TaskRunner) -> None:
        """Every real provider of the split needs its key before the first call (no half-run on a missing key)."""
        for p, b in sorted(runner.backends.items()):
            env = getattr(b.client, "api_key_env", None)
            if env and not resolve_secret(str(env))[0]:
                raise StageError(f"no API key for provider {p!r}: set the environment variable {env} "
                                 f"(or leave {p!r} out of llm.split)")

    # ---- plan
    def _plan_dict(self, runner: TaskRunner, plan: Plan, task: StructuredTask, items: Sequence[Item], ctx: dict,
                   model: str) -> dict:
        """The governor's plan as the dict the CLI prints and the tests read: totals live / batch in the primary
        currency, per-provider totals, per-item rows, notes."""
        def rate(cur: str) -> float:
            return float(plan.rates.get(cur, 1.0))
        rows = []
        for r in plan.rows:
            f = rate(r.currency)
            rows.append({"key": r.key, "provider": r.provider, "cached": r.cached, "in_tokens": r.in_tokens,
                         "out_low": r.out_low, "out_high": r.out_high, "cost_low": r.cost_low * f, "cost_high": r.cost_high * f,
                         "cost_batch_low": r.cost_low * r.batch_multiplier * f,
                         "cost_batch_high": r.cost_high * r.batch_multiplier * f, "observed": r.observed})
        live, batch = plan.totals(False), plan.totals(True)
        providers: dict[str, dict] = {}
        for p in sorted(set(runner.split) | set(plan.providers)):       # every provider of the split, even with no item
            tl, tb = plan.totals(False, p), plan.totals(True, p)
            b = runner.backends[p]
            providers[p] = {"model": plan.models.get(p), "share": float(runner.split.get(p, 0.0)), "items": tl["items"],
                            "calls": tl["calls"], "cached": tl["cached"], "in_tokens": tl["in_tokens"], "out_low": tl["out_low"],
                            "out_high": tl["out_high"], "cost_low": tl["cost_low"], "cost_high": tl["cost_high"],
                            "cost_batch_low": tb["cost_low"], "cost_batch_high": tb["cost_high"],
                            "native_currency": b.currency, "batch_api": bool(b.batch_api)}
        sys_tokens = estimate_tokens("\n".join(task.prompt().system + tuple(items[0].context))) if items else 0
        return {"stage": self.name, "version": self.version, "task": self.task, "sample": dict(ctx.get("sample_summary") or {}),
                "model": model, "models": dict(plan.models), "split": dict(runner.split), "currency": plan.currency,
                "items": len(rows), "calls": live["calls"], "known_cache_hits": plan.cached, "prompt_sha": task.prompt_sha,
                "max_tokens": task.max_tokens,
                "tokens": {"system": sys_tokens, "input_low": live["in_tokens"], "input_high": live["in_tokens"],
                           "output_low": live["out_low"], "output_high": live["out_high"]},
                "cost_low": live["cost_low"], "cost_high": live["cost_high"],
                "cost_batch_low": batch["cost_low"], "cost_batch_high": batch["cost_high"],
                "per_item_high": (live["cost_high"] / live["calls"]) if live["calls"] else 0.0,
                "observed": sum(1 for r in plan.rows if r.observed and not r.cached),
                "providers": providers, "rows": rows, "notes": list(plan.notes)}

    @staticmethod
    def _fit_budget(items: list[Item], plan: Mapping, budget: float, mode: str) -> tuple[list[Item], list[str]]:
        """`--budget`: keep items in order while the cumulative high estimate (batch prices in batch mode; cached
        items are free) stays within the budget; the rest become `budget_stop`."""
        by_key = {r["key"]: r for r in plan.get("rows") or []}
        field_name = "cost_batch_high" if mode == "batch" else "cost_high"
        kept: list[Item] = []
        total = 0.0
        for i, it in enumerate(items):
            c = float((by_key.get(it.key) or {}).get(field_name) or 0.0)
            if total + c > budget:
                return kept, [x.key for x in items[i:]]
            total += c
            kept.append(it)
        return kept, []

    # ---- live
    def _run_live(self, runner: TaskRunner, task: StructuredTask, items: Sequence[Item], budget_stop: list[str],
                  report: Report) -> dict[str, TaskResult]:
        """One `runner.run` per item; a budget scope that would be exceeded stops the run cleanly (the items done
        stay valid, the rest are `budget_stop`)."""
        results: dict[str, TaskResult] = {}
        for i, it in enumerate(items):
            try:
                results[it.key] = runner.run(task, it.payload, it.key, context=it.context)
            except BudgetExceeded as e:
                budget_stop.extend(x.key for x in items[i:])
                report.warnings.append(f"{e.scope} budget stop after {i} of {len(items)} items: {e}")
                log.warning("%s: %s", self.name, e)
                break
        return results

    # ---- batch
    def _run_batch(self, runner: TaskRunner, task: StructuredTask, items: Sequence[Item], *, wait: bool,
                   pending: Mapping | None, poll_seconds: float) -> tuple[list[str], dict[str, TaskResult] | None]:
        """Queue (cache misses) → submit (`submit_queued`) → collect (`wait_for_batch` with `--wait`, else one
        probe) → when every batch of the run has ended, re-run the items: answered ones are `cache_local` hits,
        failed ones are read from the ledger rows of the batch. Returns (batch ids, results or None while running)."""
        store, prices = runner.store, runner.prices
        first: dict[str, TaskResult] = {}
        if pending:
            batch_ids = [str(b) for b in (pending.get("batch_ids") or [pending.get("batch_id")]) if b]
        else:
            for it in items:                                                  # queued | cache_local | live (no batch API)
                first[it.key] = runner.run(task, it.payload, it.key, context=it.context)
            batch_ids = submit_queued(runner)                                 # BudgetExceeded: nothing was sent
        for bid in batch_ids:
            meta = store.get_batch(bid)
            if meta is None:
                raise StageError(f"batch {bid} of the pending run is not in this project's ledger; delete the "
                                 f"pending file to resubmit")
            backend = runner.backends.get(meta["provider"])
            if backend is None:
                raise StageError(f"batch {bid} belongs to provider {meta['provider']!r}, which is not configured now")
            if wait:
                r = wait_for_batch(store, backend.client, bid, prices, poll_seconds=poll_seconds)
            else:
                r = collect(store, backend.client, bid, prices)
            if r["state"] == "unknown":
                raise StageError(f"batch {bid} is unknown to provider {meta['provider']}; delete the pending file to resubmit")
            if r["state"] != "ended":
                log.info("batch %s (%s): %s, %d pending", bid, meta["provider"], r["state"], r["pending_before"])
                return batch_ids, None
            log.info("batch %s (%s): collected %d, failed %d, missing %d", bid, meta["provider"], r["collected"], r["failed"], r["missing"])
        failed = self._failed_from_batches(runner, task, batch_ids)
        results: dict[str, TaskResult] = {}
        for it in items:
            r0 = first.get(it.key)
            if r0 is not None and r0.status != "queued":                      # answered before the batch: keep it
                results[it.key] = r0
                continue
            if it.key in failed:
                results[it.key] = failed[it.key]
                continue
            res = runner.run(task, it.payload, it.key, context=it.context)
            if res.status == "queued":                                        # no answer and no failure row: give up on it
                runner.queued[:] = [q for q in runner.queued if q.item_key != it.key]
                res = TaskResult(it.key, "failed:error", request_hash=res.request_hash, attempts=res.attempts,
                                 error="no result in the batch", provider=res.provider)
            results[it.key] = res
        return batch_ids, results

    @staticmethod
    def _failed_from_batches(runner: TaskRunner, task: StructuredTask, batch_ids: Sequence[str]) -> dict[str, TaskResult]:
        """Items whose batch request failed (truncated / malformed / refused / error) or stayed pending after the
        batch ended, as task results classified like a live answer (the call row keeps the response)."""
        store = runner.store
        out: dict[str, TaskResult] = {}
        for bid in batch_ids:
            for row in store.ledger_rows(batch_id=bid):
                st = row["status"]
                if st not in FAILED_LEDGER_STATUSES and st != "pending":
                    continue
                call = store.get_call(row["request_hash"]) or {}
                if st == "pending":
                    status, data, error = "error", None, f"no result in batch {bid}"
                else:
                    resp = ChatResponse(text=call.get("response_text") or "", stop_reason=call.get("stop_reason") or st,
                                        error=(f"batch {bid}: request failed" if st == "error" else None))
                    status, data, errs = classify_response(resp, task.schema)
                    if status == "ok":                                        # the row says otherwise: trust the row
                        status, data = st, None
                    error = "; ".join(errs) or ({"truncated": f"max_tokens reached in batch {bid}", "refused": "refusal"}.get(st) or st)
                out[row["item_key"]] = TaskResult(row["item_key"], f"failed:{status}", data, call.get("response_text") or "",
                                                  row["request_hash"], 1, 0.0, error=error, provider=row["provider"],
                                                  model_reported=row["model_reported"], failover=bool(row["failover"]))
        return out

    @staticmethod
    def _pending_for(folder: Path, fps: Mapping[str, str]) -> dict | None:
        """A pending batch of exactly these items (same keys and fingerprints), if one was submitted earlier."""
        if not folder.is_dir():
            return None
        for p in sorted(folder.glob(f"*{PENDING_SUFFIX}")):
            try:
                d = read_draft(p)
            except Exception:
                continue
            if {x["key"]: x["fingerprint"] for x in d.get("items") or []} == dict(fps):
                return d
        return None

    @staticmethod
    def _write_pending(folder: Path, run_id: str, lrid: str, batch_ids: Sequence[str], items: Sequence[Item],
                       fps: Mapping[str, str]) -> Path:
        p = folder / f"{run_id}{PENDING_SUFFIX}"
        write_draft(p, {"run_id": run_id, "ledger_run_id": lrid, "batch_id": batch_ids[0] if batch_ids else None,
                        "batch_ids": list(batch_ids), "created": _now(),
                        "items": [{"key": it.key, "fingerprint": fps[it.key]} for it in items]})
        return p

    # ---- draft head and report
    @staticmethod
    def _usage_from_ledger(runner: TaskRunner, lrid: str, keys: Sequence[str]) -> dict[str, dict]:
        """{item key: tokens, cost (primary currency), rows} summed over the ledger rows of this run — every
        attempt, batch rows at batch prices, cache hits at zero; split parts (`key/0`) count for their parent."""
        known = set(keys)
        out: dict[str, dict] = {}
        for row in runner.store.ledger_rows(run_id=lrid):
            key = str(row["item_key"])
            if key not in known and "/" in key and key.split("/", 1)[0] in known:
                key = key.split("/", 1)[0]
            u = out.setdefault(key, {"input": 0, "cache_write": 0, "cache_read": 0, "output": 0, "cost": 0.0, "rows": 0})
            u["input"] += int(row["in_tokens"] or 0)
            u["cache_write"] += int(row["cache_write_tokens"] or 0)
            u["cache_read"] += int(row["cache_read_tokens"] or 0)
            u["output"] += int(row["out_tokens"] or 0)
            u["cost"] += runner.to_primary(float(row["cost"] or 0.0), str(row["currency"] or runner.currency))
            u["rows"] += 1
        return out

    def _check_accept(self, project: Project, prompt_sha: str) -> None:
        from atlas.verify.qa import accept_status
        st = accept_status(project, self.task, prompt_sha)
        if not st["passed"]:
            raise StageError(
                f"`{self.name}` full run refused: no passing blind review for task {self.task} @ prompt "
                f"{prompt_sha[:12]} ({st['reason']}); run `atlas run {self.name} <root> --pilot N`, then "
                f"`atlas qa sample <root> --stage {self.name}`, fill the judgement column, "
                f"`atlas qa record <root> <xlsx>` and `atlas qa report <root>`")

    def _head(self, ctx: dict, items: Sequence[Item], results: Mapping[str, TaskResult], run: dict,
              pilot: int | None, seed: int | None, budget_stop: Sequence[str], usage: Mapping[str, Mapping]) -> dict:
        total = {"input": 0, "cache_write": 0, "cache_read": 0, "output": 0}
        cost = 0.0
        by_provider: dict[str, dict] = {}
        rows = []
        for it in items:
            r = results.get(it.key)
            row: dict = {"key": it.key, "fingerprint": run["fingerprints"][it.key],
                         "status": draft_status(r) if r else ("budget_stop" if it.key in budget_stop else "pending")}
            for k in ("section", "n_sentences", "n_groups", "concept_id"):
                if k in it.meta:
                    row[k] = it.meta[k]
            if r:
                u = usage.get(it.key) or {}
                row.update({"provider": r.provider, "attempts": r.attempts, "cost": round(float(u.get("cost") or 0.0), 6),
                            "usage": {k: int(u.get(k) or 0) for k in total}, "request_hash": r.request_hash})
                if r.model_reported:
                    row["model_reported"] = r.model_reported
                if r.failover:
                    row["failover"] = True
                if r.error:
                    row["errors"] = [r.error]
                if r.truncated_by_code:
                    row["truncated_by_code"] = list(r.truncated_by_code)
                for k in total:
                    total[k] += row["usage"][k]
                cost += row["cost"]
                p = by_provider.setdefault(r.provider, {"items": 0, "cost": 0.0})
                p["items"] += 1
                p["cost"] = round(p["cost"] + row["cost"], 6)
            rows.append(row)
        return {"stage": self.name, "version": self.version, "run_id": run["run_id"], "ledger_run_id": run["ledger_run_id"],
                "task": self.task, "prompt_sha": run["prompt_sha"], "model": run["model"], "models": run["models"],
                "provider": run["provider"], "split": run["split"], "mode": run["mode"], "batch_id": run.get("batch_id"),
                "batch_ids": list(run.get("batch_ids") or []), "created": _now(), "pilot": pilot, "seed": seed,
                "n_items": len(items), "n_ok": sum(1 for r in results.values() if r.ok),
                "n_failed": sum(1 for r in results.values() if not r.ok), "budget_stop": list(budget_stop),
                "usage": total, "cost": round(cost, 6), "currency": run["currency"], "providers": by_provider,
                "settings": run.get("settings"), "items": rows}

    def _report_md(self, ctx: dict, draft: Mapping, path: Path, sha: str) -> str:
        providers = ", ".join(f"{p} {v['items']} items {v['cost']} {draft.get('currency') or ''}"
                              for p, v in sorted((draft.get("providers") or {}).items())) or "-"
        split = ", ".join(f"{p} {w:.0%}" for p, w in sorted((draft.get("split") or {}).items()))
        lines = [f"# {self.name} run {draft['run_id']}", "",
                 f"draft: {path.name} (sha256 {sha[:12]})  ", f"stage version: {self.version}  task: {self.task}  ",
                 f"prompt sha: {draft['prompt_sha'][:12]}  model: {draft['model']}  mode: {draft['mode']}  ",
                 f"ledger run id: {draft.get('ledger_run_id')}  settings: {draft.get('settings') or '-'}  ",
                 f"providers: {providers} (split {split})  ",
                 f"pilot: {draft.get('pilot') if draft.get('pilot') is not None else '-'}  seed: {draft.get('seed') if draft.get('seed') is not None else '-'}",
                 "", f"items: {draft['n_items']}  ok: {draft['n_ok']}  failed: {draft['n_failed']}  "
                 f"budget stop: {len(draft.get('budget_stop') or [])}",
                 f"tokens: input {draft['usage']['input']}, cache write {draft['usage']['cache_write']}, "
                 f"cache read {draft['usage']['cache_read']}, output {draft['usage']['output']}  ",
                 f"cost: {draft['cost']} {draft.get('currency') or ''}", ""]
        extra = self.report_lines(ctx, draft)
        if extra:
            lines += extra + [""]
        failed = [r for r in draft["items"] if r["status"] not in NOT_FAILED]
        if failed:
            lines += ["## Failed items", ""] + [f"- {r['key']}: {r['status']} — {'; '.join(r.get('errors') or [])}" for r in failed[:100]] + [""]
        return "\n".join(lines)


def format_plan(plan: Mapping) -> str:
    """The dry-run plan as printed by the CLI: items, sample composition, known cache hits, tokens, low / high cost
    live and batch, the per-provider shares of the split, notes."""
    cur = plan.get("currency") or ""
    models = plan.get("models") or {}
    head = (f"[{plan.get('stage')}] dry run — {plan.get('items', 0)} items, task {plan.get('task')} @ prompt "
            f"{str(plan.get('prompt_sha') or '')[:12]}, model {plan.get('model')}"
            + (f", pilot {plan['pilot']} documents" if plan.get("pilot") else ""))
    if len(models) > 1:
        head += " (" + ", ".join(f"{p} {m}" for p, m in sorted(models.items())) + ")"
    lines = [head]
    sample = plan.get("sample") or {}
    for k in ("documents", "pool_sentences", "sentences", "chunks", "groups", "candidates", "concepts", "skipped"):
        if k in sample:
            lines.append(f"  {k}: {sample[k]}")
    for dim, comp in (sample.get("composition") or {}).items():
        top = ", ".join(f"{v} {n}" for v, n in list(comp.items())[:8])
        more = f" (+{len(comp) - 8} more)" if len(comp) > 8 else ""
        lines.append(f"  by {dim}: {top}{more}")
    t = plan.get("tokens") or {}
    lines.append(f"  known cache hits: {plan.get('known_cache_hits', 0)}"
                 + (f" (calls to make: {plan['calls']})" if "calls" in plan else ""))
    lines.append(f"  tokens: system {t.get('system')} (per item), input {t.get('input_low')}–{t.get('input_high')}, "
                 f"output {t.get('output_low')}–{t.get('output_high')} (max_tokens {plan.get('max_tokens')})")
    lines.append(f"  cost live:  {cur} {float(plan.get('cost_low') or 0):.4f} (low) – {float(plan.get('cost_high') or 0):.4f} (high)")
    lines.append(f"  cost batch: {cur} {float(plan.get('cost_batch_low') or 0):.4f} (low) – {float(plan.get('cost_batch_high') or 0):.4f} (high)")
    providers = plan.get("providers") or {}
    if len(providers) > 1 or any(v.get("native_currency") not in (None, cur) for v in providers.values()):
        for p, v in sorted(providers.items()):
            line = (f"  {p} ({v.get('model')}, share {float(v.get('share') or 0):.0%}): {v.get('items')} items, "
                    f"{v.get('cached')} cached, in {v.get('in_tokens')} tokens; live {cur} {float(v.get('cost_low') or 0):.4f}–"
                    f"{float(v.get('cost_high') or 0):.4f}, batch {cur} {float(v.get('cost_batch_low') or 0):.4f}–"
                    f"{float(v.get('cost_batch_high') or 0):.4f}")
            if v.get("native_currency") and v["native_currency"] != cur:
                line += f" (billed in {v['native_currency']})"
            if not v.get("batch_api", True):
                line += "; no batch API: this share runs live"
            lines.append(line)
    lines.append(f"  estimates: +{RETRY_OVERHEAD:.0%} retry overhead; low from ledger observations for {plan.get('observed', 0)} "
                 f"items, defaults for the rest; no provider call is made by a dry run")
    for n in plan.get("notes") or []:
        lines.append(f"  note: {n}")
    return "\n".join(lines)
