"""`atlas llm …` and `atlas cost …`: ping refuses under the guard (and works at zero cost with the fake provider
when the guard is lifted), reports on an empty and a filled ledger, batches list / status / collect."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from atlas import cli
from atlas.config import init_project
from atlas.llm import batch as B
from atlas.llm.base import NO_NETWORK_ENV, FakeChatClient
from atlas.llm.tasks import StructuredTask, make_runner
from atlas.store.llm import LlmStore

SCHEMA = {"type": "object", "properties": {"terms": {"type": "array"}}, "required": ["terms"]}


@pytest.fixture
def project(tmp_path: Path):
    p = init_project(tmp_path / "proj", name="cli")
    return p


def set_provider(project, provider: str) -> None:
    pf = project.root / "project.yaml"
    pf.write_text(yaml.safe_dump({"name": "cli", "llm": {"provider": provider}}), encoding="utf-8")


def test_ping_refuses_under_the_guard_and_writes_nothing(project, capsys):
    assert cli.main(["llm", "ping", str(project.root)]) == cli.EXIT_USAGE
    err = capsys.readouterr().err
    assert "ATLAS_LLM_NO_NETWORK" in err and "refuses" in err
    store = LlmStore.open(project.llm_db)
    assert store.ledger_rows() == []
    store.close()


def test_ping_with_the_fake_provider_when_the_guard_is_lifted(project, capsys, monkeypatch):
    monkeypatch.delenv(NO_NETWORK_ENV, raising=False)
    set_provider(project, "fake")
    assert cli.main(["llm", "ping", str(project.root), "--effort", "low"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert out.startswith("key: not needed (fake)\n")
    assert "provider fake  model claude-opus-5-5" in out and "stop_reason end_turn" in out
    assert "usage: input" in out and "cost 0." in out and "request id fake-1" in out
    assert "provider fake: no network call was made" in out
    store = LlmStore.open(project.llm_db)
    rows = store.ledger_rows()
    assert len(rows) == 1 and rows[0]["stage"] == "ping" and rows[0]["task"] == "ping" and rows[0]["status"] == "ok"
    assert rows[0]["run_id"].startswith("ping_") and rows[0]["cost"] > 0
    store.close()


def test_cost_report_on_the_empty_ledger(project, capsys):
    assert cli.main(["cost", "report", str(project.root)]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert out.startswith("ledger: 0 rows, cost 0.0000 -") and "unknown spend: none (no pending rows)" in out
    assert "cache: local hit rate 0.0% (rows), prompt cache read ratio 0.0% (tokens)" in out
    assert cli.main(["cost", "calibrate", str(project.root)]) == cli.EXIT_OK
    assert "0 unit costs refreshed" in capsys.readouterr().out
    assert cli.main(["llm", "batches", "list", str(project.root)]) == cli.EXIT_OK
    assert capsys.readouterr().out.strip().endswith("0 batches")
    assert (project.llm_db).is_file()


def test_reports_after_a_fake_run_and_batch_commands(project, tmp_path: Path, capsys):
    set_provider(project, "fake")
    prompt = tmp_path / "t.md"
    prompt.write_text("rules\n---payload---\n{sentences}\n", encoding="utf-8")
    task = StructuredTask("terms_extract", prompt, SCHEMA, max_tokens=300)
    fake = FakeChatClient({"terms_extract": '{"terms": ["x"]}'})
    runner = make_runner(project, "terms", client=fake, mode="live", run_id="terms_r1")
    runner.run(task, {"sentences": ["a"]}, "a")
    runner.run(task, {"sentences": ["a"]}, "a")
    batch = make_runner(project, "terms", client=fake, mode="batch", store=runner.store, run_id="terms_r2")
    batch.run(task, {"sentences": ["b"]}, "b")
    bid = B.submit_queued(batch)[0]
    runner.close()

    assert cli.main(["cost", "report", str(project.root)]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert out.startswith("ledger: 3 rows, cost ") and "USD" in out
    assert "  fake: 3 rows, in " in out and ", cost 0." in out                 # per-provider line
    assert "fake\tterms\tterms_extract\t3\t1\t1\t0\t0\t1\t" in out            # provider stage task rows ok cache_local cache_batch failed pending
    assert "cache: local hit rate 50.0% (rows)" in out
    assert "unknown spend: 1 pending ledger row(s) = unknown spend up to " in out and bid in out
    assert cli.main(["cost", "report", str(project.root), "--run", "terms_r1", "--stage", "terms"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert out.startswith("run terms_r1: stage terms, mode live, created ")  # the settings snapshot header
    assert "  providers: fake claude-opus-5-5 effort medium; split: fake 100%; failover False; pricing " in out
    assert f"  tasks (prompt_sha): terms_extract {task.prompt_sha[:12]}" in out
    assert "ledger (run terms_r1 stage terms): 2 rows" in out
    assert cli.main(["cost", "report", str(project.root), "--run", "terms_r9"]) == cli.EXIT_OK
    assert capsys.readouterr().out.startswith("run terms_r9: no settings snapshot")
    assert cli.main(["cost", "calibrate", str(project.root)]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "terms_extract\t" in out and "1 unit costs refreshed" in out

    assert cli.main(["llm", "batches", "list", str(project.root)]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert f"notice: 1 pending ledger row(s)" in out and f"{bid}\tfake\tin_progress\t" in out and "1 batches" in out
    # the CLI builds its own FakeChatClient for the batch's provider; it does not know the batch → `unknown`
    assert cli.main(["llm", "batches", "status", str(project.root), bid]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert '"batch_id": "fakebatch_0001"' in out and '"pending": 1' in out and '"state": "unknown"' in out
    assert cli.main(["llm", "batches", "collect", str(project.root), bid]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert f"batch {bid} (fake): state unknown, pending before 1, collected 0" in out and "not ended yet" in out
    assert cli.main(["llm", "batches", "collect", str(project.root), "msgbatch_missing"]) == cli.EXIT_USAGE
    assert "not in this project's ledger" in capsys.readouterr().err


def test_parser_knows_the_new_verbs():
    ap = cli.build_parser()
    a = ap.parse_args(["llm", "batches", "collect", "root", "b1", "--wait", "--poll", "5"])
    assert (a.cmd, a.llm_cmd, a.batches_cmd, a.batch_id, a.wait, a.poll) == ("llm", "batches", "collect", "b1", True, 5.0)
    a = ap.parse_args(["cost", "report", "root", "--run", "r", "--stage", "s"])
    assert (a.cmd, a.cost_cmd, a.run, a.stage) == ("cost", "report", "r", "s")
    assert ap.parse_args(["llm", "ping", "root"]).effort == "low"
    with pytest.raises(SystemExit):
        ap.parse_args(["llm", "ping", "root", "--effort", "ultra"])
