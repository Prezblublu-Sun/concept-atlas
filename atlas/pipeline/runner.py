"""Stage runner. Increment 1 knows one stage (`ingest`); the Stage protocol is fixed now so later stages fit.

Implemented by: increment 1, agent "ingest" (minimal: STAGES table, `run_stage`, a Report dataclass).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from atlas.config import Project


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

    def run(self, project: Project, **opts) -> Report: ...


STAGES: list[str] = ["ingest"]   # later: terms, concepts, scheme, match, assign, classify, relations, overview, summaries, translate, finish


def get_stage(name: str) -> Stage:
    if name == "ingest":
        from .stages.ingest import IngestStage
        return IngestStage()
    raise KeyError(f"unknown stage {name!r}; known: {STAGES}")


def run_stage(project: Project, name: str, **opts) -> Report:
    return get_stage(name).run(project, **opts)
