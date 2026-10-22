"""Stage runner. Increment 1 knew one stage (`ingest`); increment 2 adds the build stages `match`, `assign`,
`finish` that operate on an open build (`builds/b<date>_<seq>/`).

A stage object has `name, version, llm, requires, gate` and `run(project, build, **opts) -> Report`; `ingest`
ignores `build`. `run_stage` resolves the build (an explicit id, else the newest open build; error when none),
checks `requires` (the required stages' '*' item must be `done` in that build) and the gate (`curate status`
sha check: stop with "`<name>` changed after confirmation — run `atlas curate confirm <name>`").
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from atlas.config import Project

log = logging.getLogger(__name__)


@dataclass
class Report:
    stage: str
    items_total: int = 0
    items_done: int = 0
    items_skipped: int = 0
    items_failed: int = 0
    warnings: list[str] = field(default_factory=list)
    details: dict = field(default_factory=dict)

    def summary(self) -> str:
        return (f"[{self.stage}] done {self.items_done}/{self.items_total}, skipped {self.items_skipped}, "
                f"failed {self.items_failed}, warnings {len(self.warnings)}")


class Stage(Protocol):
    name: str
    version: str
    llm: bool
    requires: tuple[str, ...]
    gate: tuple[str, ...]

    def run(self, project: Project, build=None, **opts) -> Report: ...


STAGES: list[str] = ["ingest", "match", "assign", "finish"]
# later: terms, concepts, scheme, classify, relations, overview, summaries, translate
BUILD_STAGES: tuple[str, ...] = ("match", "assign", "finish")


def get_stage(name: str) -> Stage:
    if name == "ingest":
        from .stages.ingest import IngestStage
        return IngestStage()
    if name == "match":
        from .stages.match import MatchStage
        return MatchStage()
    if name == "assign":
        from .stages.assign import AssignStage
        return AssignStage()
    if name == "finish":
        from .stages.finish import FinishStage
        return FinishStage()
    raise KeyError(f"unknown stage {name!r}; known: {STAGES}")


class StageError(Exception):
    """The stage cannot run (no open build, requirements not met)."""


def _check_requires(stage: Stage, build) -> None:
    for req in getattr(stage, "requires", ()) or ():
        st = build.stage_status(req)
        if st != "done":
            raise StageError(f"stage `{stage.name}` requires `{req}` (status: {st or 'not run'}) in build {build.build_id}")


def run_stage(project: Project, name: str, build: str | Path | None = None, **opts) -> Report:
    """Run one stage. For build stages `build` is a build id or folder (default: the newest open build)."""
    stage = get_stage(name)
    if name == "ingest":
        return stage.run(project, None, **opts)
    from atlas.pipeline.build import open_build_store
    from atlas.pipeline.gates import check_gate
    gate = getattr(stage, "gate", ()) or ()
    if gate:
        check_gate(project, tuple(gate))
    store = open_build_store(project, build, require_open=True)
    try:
        _check_requires(stage, store)
        return stage.run(project, store, **opts)
    finally:
        store.close()
