"""Shared machinery of the LLM curation stages `terms`, `concepts`, `scheme`, `classify` (increment 4 §2.2).

A stage subclasses `LlmStage` and implements `prepare` (read its inputs; pure data in a context dict), `items`
(the work units: key, pure payload, bookkeeping meta, optional per-run system blocks), `schema`, and `finish`
(aggregate the task results into the draft body). The base class does the rest, in this order:

  prepare → items → fingerprints (stage version, prompt sha, item content sha) → plan (tokens, low/high cost)
  → `--dry-run` stops here, before any client exists → accept gate (a full run of `terms` / `classify` needs a
  passing blind review for the same task + prompt sha, `atlas qa report`) → `--budget` trims the item list to what
  the high estimate allows → client (`--mode live|batch`, `--wait`) → results → finish → draft
  `work/drafts/<stage>/<run_id>.yaml` + `<run_id>.report.md`.

Stages never write `curated/` (only `atlas curate merge|confirm` do); a dry run writes nothing at all.

The LLM layer is reached through exactly two methods — `_call_task` (live) and `_run_batch` (batch) — both
delegating to `atlas/pipeline/llm_shim.py`; the integrator replaces those two bodies with work package L's task
runner / batch module and `plan` with the governor's plan. `default_client` prefers L's `make_client` when it
exists and otherwise knows only the fake provider.
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

from atlas.config import Project
from atlas.core.ids import canonical_json
from atlas.llm.base import ChatClient, FakeChatClient, NetworkForbidden, network_forbidden
from atlas.pipeline import llm_shim as shim
from atlas.pipeline.runner import Report, StageError

log = logging.getLogger(__name__)

DRAFTS_DIR = ("work", "drafts")
RUN_ID_RE = re.compile(r"^r(\d{4}-\d{2}-\d{2})_(\d{3})$")
PENDING_SUFFIX = ".pending.yaml"
REPORT_SUFFIX = ".report.md"


def _now() -> str:
    return datetime.fromtimestamp(time.time(), tz=timezone.utc).isoformat(timespec="seconds")


def clean_text(s: str | None) -> str:
    return " ".join((s or "").split())


@dataclass
class Item:
    """One work unit of a stage: `payload` is the pure input of the payload template (sorted keys, sorted sids, no
    timestamps or build ids), `meta` what `finish` needs (never part of the request), `system_extra` per-run
    context blocks appended to the system prompt (the scheme for `classify`)."""
    key: str
    payload: dict
    meta: dict = field(default_factory=dict)
    system_extra: tuple[str, ...] = ()

    @property
    def content_sha(self) -> str:
        return hashlib.sha256(canonical_json(self.payload).encode("utf-8")).hexdigest()


def fingerprint(version: str, prompt_sha: str, content_sha: str) -> str:
    return f"{version}:{prompt_sha[:12]}:{content_sha[:16]}"


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
    """The model of a run: `--model`, else `llm.model` when it has a price row (built-in or `llm.pricing`), else
    the default model of the contract."""
    m = str(override or project.get("llm.model") or "")
    return m if m in shim.PRICING or isinstance((project.get("llm.pricing") or {}).get(m), Mapping) else shim.DEFAULT_MODEL


def pricing_for(project: Project) -> dict | None:
    p = project.get("llm.pricing")
    return p if isinstance(p, Mapping) else None


def effort_for(project: Project) -> str | None:
    e = project.get("llm.effort")
    return str(e) if e else None


def default_client(project: Project) -> ChatClient:
    """The project's client: the fake provider needs nothing; a real provider goes through work package L's
    `make_client` when that module exists, and is refused outright under ATLAS_LLM_NO_NETWORK."""
    provider = str(project.get("llm.provider") or "fake").lower()
    if provider == "fake":
        return FakeChatClient(default="{}")
    if network_forbidden():
        raise NetworkForbidden(f"ATLAS_LLM_NO_NETWORK is set: provider {provider!r} would call the network "
                               "(use --dry-run, or llm.provider: fake)")
    try:
        from atlas.llm.client import make_client  # type: ignore[import-not-found]  # work package L
    except ImportError as e:
        raise StageError(f"no LLM client for provider {provider!r} in this checkout (atlas.llm.client missing); "
                         "use --dry-run or set llm.provider: fake") from e
    return make_client(project)


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
    out_low = 500                # the output tokens the low estimate assumes per item
    caps: dict[str, int] = {}

    # ---- to implement per stage
    def prepare(self, project: Project, build, **opts) -> dict:
        raise NotImplementedError

    def items(self, ctx: dict) -> list[Item]:
        raise NotImplementedError

    def schema(self, ctx: dict) -> dict:
        raise NotImplementedError

    def finish(self, ctx: dict, items: Sequence[Item], results: Mapping[str, shim.TaskResult], run: dict) -> dict:
        """The stage-specific body of the draft (keys after the head). May record stage items in a build."""
        raise NotImplementedError

    def draft_name(self, ctx: dict, run_id: str) -> str:
        return f"{run_id}.yaml"

    def report_lines(self, ctx: dict, draft: Mapping) -> list[str]:
        return []

    # ---- the one place that reaches the LLM layer (live)
    def _call_task(self, client: ChatClient, ctx: dict, item: Item, *, model: str, effort: str | None,
                   pricing: Mapping | None) -> shim.TaskResult:
        return shim.run_task(client, self.task, self.prompt_file, self.schema(ctx), item.payload, key=item.key,
                             max_tokens=self.max_tokens, caps=self.caps, model=model, effort=effort,
                             system_extra=item.system_extra, pricing=pricing)

    # ---- the one place that reaches the LLM layer (batch)
    def _run_batch(self, client: ChatClient, ctx: dict, items: Sequence[Item], *, model: str, effort: str | None,
                   pricing: Mapping | None, wait: bool, pending: dict | None) -> tuple[str, dict[str, shim.TaskResult] | None]:
        prompt = shim.load_prompt(self.prompt_file)
        schema = self.schema(ctx)
        reqs = {it.key: shim.build_request(prompt, it.payload, task=self.task, key=it.key, schema=schema,
                                           max_tokens=self.max_tokens, model=model, effort=effort,
                                           system_extra=it.system_extra) for it in items}
        if pending:
            batch_id = str(pending["batch_id"])
            responses = shim.collect_batch(client, batch_id, wait=wait)
        else:
            batch_id, responses = shim.run_batch(client, list(reqs.values()), wait=wait)
        if responses is None:
            return batch_id, None
        provider = getattr(client, "provider", "fake")
        out: dict[str, shim.TaskResult] = {}
        for key, req in reqs.items():
            resp = responses.get(key)
            if resp is None:
                out[key] = shim.TaskResult(key=key, status="error", data=None, errors=["no result in batch"],
                                           truncated_by_code={}, attempts=1,
                                           usage={"input": 0, "cache_write": 0, "cache_read": 0, "output": 0}, cost=0.0,
                                           currency=shim.pricing_for(model, pricing)["currency"], request_hash="",
                                           prompt_sha=prompt.sha, latency_ms=0, raw_id=None)
                continue
            out[key] = shim.finish_response(resp, req, prompt, schema, self.caps, provider, model, pricing, batch=True)
        return batch_id, out

    # ---- plan
    def plan(self, project: Project, ctx: dict, items: Sequence[Item]) -> dict:
        model = model_for(project, ctx.get("model"))
        plan = shim.plan_task(self.prompt_file, [(it.key, it.payload) for it in items], max_tokens=self.max_tokens,
                              out_low=self.out_low, model=model, pricing=pricing_for(project),
                              system_extra=items[0].system_extra if items else ())
        plan.update({"stage": self.name, "version": self.version, "task": self.task,
                     "sample": dict(ctx.get("sample_summary") or {})})
        return plan

    # ---- run
    def run(self, project: Project, build=None, *, dry_run: bool = False, pilot: int | None = None,
            mode: str | None = None, budget: float | None = None, wait: bool = False, limit: int | None = None,
            seed: int | None = None, client: ChatClient | None = None, model: str | None = None, **opts) -> Report:
        report = Report(stage=self.name)
        t0 = time.perf_counter()
        prompt = shim.load_prompt(self.prompt_file)
        ctx = self.prepare(project, build, pilot=pilot, seed=seed, **opts)
        ctx.setdefault("pilot", pilot)
        ctx["model"] = model
        items = self.items(ctx)
        if limit is not None and int(limit) >= 0:
            items = items[:int(limit)]
        fps = {it.key: fingerprint(self.version, prompt.sha, it.content_sha) for it in items}
        report.items_total = len(items) + int(ctx.get("n_skipped") or 0)
        report.items_skipped = int(ctx.get("n_skipped") or 0)
        plan = self.plan(project, ctx, items)
        plan["pilot"] = pilot
        report.details["plan"] = plan
        report.details["fingerprints"] = fps
        report.details["prompt_sha"] = prompt.sha
        for w in ctx.get("warnings") or []:
            report.warnings.append(w)
        if dry_run:
            report.details["dry_run"] = True
            log.info("%s: dry run, %d items, %s %s–%s", self.name, len(items), plan["currency"], plan["cost_low"], plan["cost_high"])
            return report
        if self.accept_gate and pilot is None and items:
            self._check_accept(project, prompt.sha)
        mode = str(mode or ("batch" if project.get("llm.batch_default", True) else "live")).lower()
        if mode not in ("live", "batch"):
            raise StageError(f"unknown mode {mode!r} (live | batch)")
        budget_stop: list[str] = []
        if budget is not None and items:
            items, budget_stop = self._fit_budget(items, plan, float(budget), mode)
            if budget_stop:
                report.warnings.append(f"budget {budget} {plan['currency']}: {len(budget_stop)} of "
                                       f"{len(items) + len(budget_stop)} items left out (high estimate)")
        folder = drafts_dir(project, self.name)
        pending = self._pending_for(folder, fps) if mode == "batch" else None
        run_id = str(pending["run_id"]) if pending else new_run_id(folder)
        model, effort, pricing = model_for(project, model), effort_for(project), pricing_for(project)
        results: dict[str, shim.TaskResult] = {}
        batch_id: str | None = None
        if items:
            client = client or default_client(project)
            if mode == "live":
                results = self._run_live(client, ctx, items, plan, budget, model, effort, pricing, budget_stop)
            else:
                batch_id, got = self._run_batch(client, ctx, items, model=model, effort=effort, pricing=pricing,
                                                wait=wait, pending=pending)
                if got is None:
                    self._write_pending(folder, run_id, batch_id, items, fps)
                    report.details.update({"batch_id": batch_id, "run_id": run_id, "pending": True})
                    report.warnings.append(f"batch {batch_id} submitted ({len(items)} items); run again with --wait "
                                           "(or --mode batch --wait) to collect it")
                    return report
                results = got
                p = folder / f"{run_id}{PENDING_SUFFIX}"
                if p.is_file():
                    p.unlink()
        run = {"run_id": run_id, "prompt_sha": prompt.sha, "model": model, "mode": mode, "batch_id": batch_id,
               "fingerprints": fps, "provider": getattr(client, "provider", None) if items else None}
        body = self.finish(ctx, items, results, run)
        head = self._head(ctx, items, results, run, pilot, seed, budget_stop)
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
                report.warnings.append(f"{it.key}: {r['status']}: {'; '.join(r['errors'][:3])}")
        report.details.update({"run_id": run_id, "draft": str(path), "draft_sha": sha, "report": str(rpath),
                               "cost": head["cost"], "currency": head["currency"], "usage": head["usage"],
                               "seconds": round(time.perf_counter() - t0, 2), "batch_id": batch_id})
        log.info(report.summary())
        return report

    # ---- helpers
    def _check_accept(self, project: Project, prompt_sha: str) -> None:
        from atlas.verify.qa import accept_status
        st = accept_status(project, self.task, prompt_sha)
        if not st["passed"]:
            raise StageError(
                f"`{self.name}` full run refused: no passing blind review for task {self.task} @ prompt "
                f"{prompt_sha[:12]} ({st['reason']}); run `atlas run {self.name} <root> --pilot N`, then "
                f"`atlas qa sample <root> --stage {self.name}`, fill the judgement column, "
                f"`atlas qa record <root> <xlsx>` and `atlas qa report <root>`")

    @staticmethod
    def _fit_budget(items: list[Item], plan: dict, budget: float, mode: str) -> tuple[list[Item], list[str]]:
        per = float(plan.get("per_item_high") or 0.0)
        if mode == "batch":
            per *= float(shim.pricing_for(plan["model"]).get("batch_multiplier", 0.5))
        if per <= 0:
            return items, []
        n = int(budget // per)
        return items[:n], [it.key for it in items[n:]]

    def _run_live(self, client: ChatClient, ctx: dict, items: Sequence[Item], plan: dict, budget: float | None,
                  model: str, effort: str | None, pricing: Mapping | None, budget_stop: list[str]) -> dict[str, shim.TaskResult]:
        results: dict[str, shim.TaskResult] = {}
        spent = 0.0
        per = float(plan.get("per_item_high") or 0.0)
        for i, it in enumerate(items):
            if budget is not None and spent + per > budget and results:
                budget_stop.extend(x.key for x in items[i:])
                log.warning("%s: budget %.4f reached after %d items (spent %.4f)", self.name, budget, i, spent)
                break
            res = self._call_task(client, ctx, it, model=model, effort=effort, pricing=pricing)
            results[it.key] = res
            spent += float(res.get("cost") or 0.0)
        return results

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
    def _write_pending(folder: Path, run_id: str, batch_id: str, items: Sequence[Item], fps: Mapping[str, str]) -> Path:
        p = folder / f"{run_id}{PENDING_SUFFIX}"
        write_draft(p, {"run_id": run_id, "batch_id": batch_id, "created": _now(),
                        "items": [{"key": it.key, "fingerprint": fps[it.key]} for it in items]})
        return p

    def _head(self, ctx: dict, items: Sequence[Item], results: Mapping[str, shim.TaskResult], run: dict,
              pilot: int | None, seed: int | None, budget_stop: Sequence[str]) -> dict:
        usage = {"input": 0, "cache_write": 0, "cache_read": 0, "output": 0}
        cost = 0.0
        currency = None
        rows = []
        for it in items:
            r = results.get(it.key)
            row = {"key": it.key, "fingerprint": run["fingerprints"][it.key],
                   "status": r["status"] if r else ("budget_stop" if it.key in budget_stop else "pending")}
            for k in ("section", "n_sentences", "n_groups", "concept_id"):
                if k in it.meta:
                    row[k] = it.meta[k]
            if r:
                row.update({"attempts": r["attempts"], "cost": r["cost"], "usage": dict(r["usage"]),
                            "request_hash": r["request_hash"]})
                if r["errors"]:
                    row["errors"] = list(r["errors"])[:5]
                if r["truncated_by_code"]:
                    row["truncated_by_code"] = dict(r["truncated_by_code"])
                for k in usage:
                    usage[k] += int(r["usage"].get(k) or 0)
                cost += float(r["cost"] or 0.0)
                currency = r["currency"]
            rows.append(row)
        return {"stage": self.name, "version": self.version, "run_id": run["run_id"], "task": self.task,
                "prompt_sha": run["prompt_sha"], "model": run["model"], "provider": run.get("provider"),
                "mode": run["mode"], "batch_id": run.get("batch_id"), "created": _now(), "pilot": pilot, "seed": seed,
                "n_items": len(items), "n_ok": sum(1 for r in results.values() if r.ok),
                "n_failed": sum(1 for r in results.values() if not r.ok), "budget_stop": list(budget_stop),
                "usage": usage, "cost": round(cost, 6), "currency": currency, "items": rows}

    def _report_md(self, ctx: dict, draft: Mapping, path: Path, sha: str) -> str:
        lines = [f"# {self.name} run {draft['run_id']}", "",
                 f"draft: {path.name} (sha256 {sha[:12]})  ", f"stage version: {self.version}  task: {self.task}  ",
                 f"prompt sha: {draft['prompt_sha'][:12]}  model: {draft['model']}  mode: {draft['mode']}  ",
                 f"pilot: {draft.get('pilot') if draft.get('pilot') is not None else '-'}  seed: {draft.get('seed') if draft.get('seed') is not None else '-'}",
                 "", f"items: {draft['n_items']}  ok: {draft['n_ok']}  failed: {draft['n_failed']}  "
                 f"budget stop: {len(draft.get('budget_stop') or [])}",
                 f"tokens: input {draft['usage']['input']}, cache write {draft['usage']['cache_write']}, "
                 f"cache read {draft['usage']['cache_read']}, output {draft['usage']['output']}  ",
                 f"cost: {draft['cost']} {draft.get('currency') or ''}", ""]
        extra = self.report_lines(ctx, draft)
        if extra:
            lines += extra + [""]
        failed = [r for r in draft["items"] if r["status"] not in ("ok", "pending")]
        if failed:
            lines += ["## Failed items", ""] + [f"- {r['key']}: {r['status']} — {'; '.join(r.get('errors') or [])}" for r in failed[:100]] + [""]
        return "\n".join(lines)


def format_plan(plan: Mapping) -> str:
    """The dry-run plan as printed by the CLI: items, sample composition, tokens, low / high cost, live and batch."""
    cur = plan.get("currency") or ""
    lines = [f"[{plan.get('stage')}] dry run — {plan.get('items', 0)} items, task {plan.get('task')} @ prompt "
             f"{str(plan.get('prompt_sha') or '')[:12]}, model {plan.get('model')}"
             + (f", pilot {plan['pilot']} documents" if plan.get("pilot") else "")]
    sample = plan.get("sample") or {}
    for k in ("documents", "pool_sentences", "sentences", "chunks", "groups", "candidates", "concepts", "skipped"):
        if k in sample:
            lines.append(f"  {k}: {sample[k]}")
    for dim, comp in (sample.get("composition") or {}).items():
        top = ", ".join(f"{v} {n}" for v, n in list(comp.items())[:8])
        more = f" (+{len(comp) - 8} more)" if len(comp) > 8 else ""
        lines.append(f"  by {dim}: {top}{more}")
    t = plan.get("tokens") or {}
    lines.append(f"  known cache hits: {plan.get('known_cache_hits', 0)}")
    lines.append(f"  tokens: system {t.get('system')} (per item), input {t.get('input_low')}–{t.get('input_high')}, "
                 f"output {t.get('output_low')}–{t.get('output_high')} (max_tokens {plan.get('max_tokens')})")
    lines.append(f"  cost live:  {cur} {plan.get('cost_low'):.4f} (low) – {plan.get('cost_high'):.4f} (high)")
    lines.append(f"  cost batch: {cur} {plan.get('cost_batch_low'):.4f} (low) – {plan.get('cost_batch_high'):.4f} (high)")
    return "\n".join(lines)
