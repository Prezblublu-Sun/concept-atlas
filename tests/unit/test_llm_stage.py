"""pipeline/llm_stage against the real task layer: the four stages' prompt files and tasks (two sections, stable
prompt sha, the evidence rule, pure rendering), item fingerprints, the draft-status mapping, the model override,
the budget trim over plan rows, the plan text (no network, FakeChatClient only)."""
from __future__ import annotations

import pytest

from atlas.config import init_project
from atlas.llm.base import request_hash
from atlas.llm.tasks import TaskResult, load_prompt
from atlas.pipeline.llm_stage import (DEFAULT_MODEL, Item, LlmStage, draft_status, fingerprint, format_plan,
                                      ledger_run_id, model_for, project_with_model)
from atlas.pipeline.stages.classify import ClassifyStage
from atlas.pipeline.stages.concepts import ConceptsStage
from atlas.pipeline.stages.scheme import SchemeStage
from atlas.pipeline.stages.terms import TermsStage

STAGES = (TermsStage, ConceptsStage, SchemeStage, ClassifyStage)
CTX = {"dom_ids": ["DOM_a"], "cat_ids": ["A1"]}        # what ClassifyStage.schema reads; the others ignore ctx


@pytest.fixture
def project(tmp_path):
    return init_project(tmp_path / "proj", name="stage")


def test_prompt_files_have_two_sections_and_stable_sha():
    for cls in STAGES:
        st = cls()
        p = load_prompt(st.prompt_path)
        assert p.system[0].strip() and p.template.strip() and len(p.sha) == 64 and st.prompt_sha == p.sha
        assert "sid" in p.system[0].lower()                      # the evidence rule is in every prompt
        task = st.structured_task(CTX)
        assert task.name == st.task and task.prompt_sha == p.sha and task.max_tokens == st.max_tokens
        assert task.limits == st.caps and not task.splittable and task.schema["type"] == "object"
        for path in task.limits:                                 # the task layer's path grammar, not dotted paths
            assert all(seg.endswith("[]") for seg in path.split(".")[:-1]), path


def test_render_is_pure_and_hash_is_stable_per_item():
    task = TermsStage().structured_task(CTX)
    payload = {"section": "methods", "n_sentences": 2, "sentences": "[S1a] a {brace} text\n[S1b] b"}
    a = task.render(payload)
    b = task.render(dict(reversed(list(payload.items()))))
    assert a == b and "{brace}" in a and "Section type: methods" in a
    assert Item("k1", payload).content_sha == Item("k2", dict(reversed(list(payload.items())))).content_sha
    fp = fingerprint("terms1", task.prompt_sha, Item("k", payload).content_sha)
    assert fp == f"terms1:{task.prompt_sha[:12]}:{Item('k', payload).content_sha[:16]}"
    with pytest.raises(KeyError, match="sentences"):
        task.render({"section": "x", "n_sentences": 0})
    assert ledger_run_id("terms", "r2026-10-23_001") == "terms_r2026-10-23_001"


def test_draft_status_maps_the_task_layer_vocabulary():
    assert draft_status(TaskResult("k", "ok", {"terms": []})) == "ok"
    assert draft_status(TaskResult("k", "cache_local", {"terms": []})) == "cache_local"
    assert draft_status(TaskResult("k", "failed:malformed", None, error="no JSON object")) == "malformed"
    assert draft_status(TaskResult("k", "failed:malformed", {}, error="missing required 'terms'")) == "invalid"
    assert draft_status(TaskResult("k", "failed:invalid", {"domain": "x"}, error="primary A1 is not a category")) == "invalid"
    assert draft_status(TaskResult("k", "failed:truncated")) == "truncated"
    assert draft_status(TaskResult("k", "failed:refused", error="refusal")) == "refused"
    assert draft_status(TaskResult("k", "failed:error", error="HTTP 500")) == "error"
    assert draft_status(TaskResult("k", "queued")) == "queued"
    assert TaskResult("k", "cache_local").ok and not TaskResult("k", "failed:invalid").ok


def test_model_override_needs_a_price_row(project):
    assert model_for(project) == DEFAULT_MODEL == "claude-opus-5-5"
    assert model_for(project, "claude-haiku-5-5") == "claude-haiku-5-5"
    assert model_for(project, "no-such-model") == "claude-opus-5-5"
    cheap = project_with_model(project, "claude-haiku-5-5")
    assert cheap.get("llm.model") == "claude-haiku-5-5" and cheap.root == project.root and project.get("llm.model") == "claude-opus-5-5"
    assert project_with_model(project, "claude-opus-5-5") is project
    from atlas.llm.client import llm_config, models_of
    assert models_of(llm_config(cheap)) == {"anthropic": "claude-haiku-5-5", "deepseek": "deepseek-flash"}


def test_fit_budget_walks_the_plan_rows_in_item_order():
    items = [Item(k, {"x": k}) for k in ("a", "b", "c", "d")]
    plan = {"rows": [{"key": "a", "cost_high": 0.04, "cost_batch_high": 0.02}, {"key": "b", "cost_high": 0.0, "cost_batch_high": 0.0},
                     {"key": "c", "cost_high": 0.05, "cost_batch_high": 0.025}, {"key": "d", "cost_high": 0.05, "cost_batch_high": 0.025}]}
    kept, stop = LlmStage._fit_budget(items, plan, 0.06, "live")
    assert [i.key for i in kept] == ["a", "b"] and stop == ["c", "d"]          # a cached item (0) is free
    kept, stop = LlmStage._fit_budget(items, plan, 0.06, "batch")
    assert [i.key for i in kept] == ["a", "b", "c"] and stop == ["d"]
    kept, stop = LlmStage._fit_budget(items, plan, 0.01, "live")
    assert kept == [] and stop == ["a", "b", "c", "d"]
    assert LlmStage._fit_budget(items, plan, 1.0, "live") == (items, [])


def test_format_plan_lists_sample_costs_and_the_split():
    plan = {"stage": "terms", "task": "terms_extract", "prompt_sha": "ab" * 32, "model": "claude-opus-5-5",
            "models": {"anthropic": "claude-opus-5-5", "deepseek": "deepseek-flash"}, "pilot": 20, "items": 12, "calls": 10,
            "known_cache_hits": 2, "currency": "USD", "max_tokens": 2000,
            "sample": {"documents": 20, "sentences": 480, "chunks": 12, "composition": {"section": {"methods": 300, "results": 180}}},
            "tokens": {"system": 900, "input_low": 20000, "input_high": 20000, "output_low": 5000, "output_high": 20000},
            "cost_low": 0.3, "cost_high": 1.2, "cost_batch_low": 0.15, "cost_batch_high": 0.6, "observed": 0,
            "providers": {"anthropic": {"model": "claude-opus-5-5", "share": 0.9, "items": 11, "cached": 2, "in_tokens": 18000,
                                        "cost_low": 0.29, "cost_high": 1.1, "cost_batch_low": 0.145, "cost_batch_high": 0.55,
                                        "native_currency": "USD", "batch_api": True},
                          "deepseek": {"model": "deepseek-flash", "share": 0.1, "items": 1, "cached": 0, "in_tokens": 2000,
                                       "cost_low": 0.01, "cost_high": 0.1, "cost_batch_low": 0.01, "cost_batch_high": 0.1,
                                       "native_currency": "RMB", "batch_api": False}},
            "notes": ["provider split: anthropic 90%, deepseek 10%"]}
    txt = format_plan(plan)
    assert txt.startswith("[terms] dry run — 12 items, task terms_extract @ prompt abababababab, model claude-opus-5-5, pilot 20 documents")
    assert "(anthropic claude-opus-5-5, deepseek deepseek-flash)" in txt and "  by section: methods 300, results 180" in txt
    assert "known cache hits: 2 (calls to make: 10)" in txt and "cost live:  USD 0.3000 (low) – 1.2000 (high)" in txt
    assert "cost batch: USD 0.1500 (low) – 0.6000 (high)" in txt
    assert "anthropic (claude-opus-5-5, share 90%): 11 items, 2 cached, in 18000 tokens; live USD 0.2900–1.1000, batch USD 0.1450–0.5500" in txt
    assert "deepseek (deepseek-flash, share 10%): 1 items" in txt and "(billed in RMB); no batch API: this share runs live" in txt
    assert "+6% retry overhead" in txt and "note: provider split" in txt
    one = dict(plan, models={"fake": "claude-opus-5-5"}, providers={"fake": dict(plan["providers"]["anthropic"], native_currency="USD")})
    assert "share" not in format_plan(one) and "(fake" not in format_plan(one)


def test_request_of_a_stage_item_through_the_runner(project):
    from atlas.llm.base import FakeChatClient
    from atlas.llm.tasks import make_runner
    st = ClassifyStage()
    task = st.structured_task(CTX)
    runner = make_runner(project, "classify", client=FakeChatClient(), mode="live", governed=False)
    try:
        payload = {"concept_id": "C_x", "label": "x", "zh": "-", "type": "-", "forms": "x", "n_examples": 1, "examples": "[S1] x."}
        req = runner.request(task, payload, "C_x", context=("Classification scheme / 分类体系:\nDomain DOM_a",))
        assert req.system[0].startswith(load_prompt(st.prompt_path).system[0][:40]) and req.system[1].startswith("Classification scheme")
        assert req.output_schema["properties"]["domain"]["enum"] == ["DOM_a"] and req.max_tokens == 400 and req.task == "classify"
        assert request_hash("fake", req) != request_hash("fake", runner.request(task, payload, "C_x"))   # the context is part of the hash
    finally:
        runner.close()
