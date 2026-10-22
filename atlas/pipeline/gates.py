"""Curated-file gates: `atlas curate confirm <name>` / `atlas curate status` and the sha check that gated stages
run before they start (architecture §7). Implemented by: increment 2 (derived layer).

`confirmations.json` = {name: {sha, confirmed_at, from_draft_sha}}. A gated stage stops with
"`<name>` changed after confirmation — run `atlas curate confirm <name>`" when the file's sha differs from the
confirmed one (or was never confirmed).
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

from atlas.config import Project
from atlas.store.curated import CURATED_FILES, curated_path, file_sha, read_confirmations, write_confirmations


class GateError(Exception):
    """A gated stage must not run: the message names the file and the command that fixes it."""


def _now() -> str:
    return datetime.fromtimestamp(time.time(), tz=timezone.utc).isoformat(timespec="seconds")


def confirm(project: Project, name: str, from_draft_sha: str | None = None) -> dict:
    """Record the current sha of curated/<name> as confirmed. KeyError for an unknown name, FileNotFoundError when
    the file does not exist."""
    p = curated_path(project.curated_dir, name)
    sha = file_sha(p)
    if sha is None:
        raise FileNotFoundError(f"curated file missing: {p}")
    conf = read_confirmations(project.curated_dir)
    conf[name] = {"sha": sha, "confirmed_at": _now(), "from_draft_sha": from_draft_sha}
    write_confirmations(project.curated_dir, conf)
    return conf[name]


def confirm_all(project: Project) -> dict:
    """Confirm every curated file that exists (used by the legacy export: the files are the user's own system)."""
    out = {}
    for name in CURATED_FILES:
        if curated_path(project.curated_dir, name).is_file():
            out[name] = confirm(project, name)
    return out


def status(project: Project | Path, confirmations: dict | None = None) -> list[dict]:
    """One row per curated file: {name, file, exists, sha, confirmed_sha, confirmed_at, state} with
    state ∈ confirmed | changed | unconfirmed | missing."""
    cdir = project.curated_dir if isinstance(project, Project) else Path(project)
    conf = confirmations if confirmations is not None else read_confirmations(cdir)
    rows = []
    for name, fn in CURATED_FILES.items():
        p = cdir / fn
        sha = file_sha(p)
        c = conf.get(name) or {}
        if sha is None:
            state = "missing"
        elif not c.get("sha"):
            state = "unconfirmed"
        elif c["sha"] == sha:
            state = "confirmed"
        else:
            state = "changed"
        rows.append({"name": name, "file": fn, "exists": sha is not None, "sha": sha, "confirmed_sha": c.get("sha"),
                     "confirmed_at": c.get("confirmed_at"), "state": state})
    return rows


def check_gate(project: Project | Path, names: tuple[str, ...] | list[str]) -> None:
    """Raise GateError unless every named curated file exists and matches its confirmation."""
    by_name = {r["name"]: r for r in status(project)}
    for name in names:
        r = by_name.get(name)
        if r is None:
            raise GateError(f"unknown curated file {name!r}")
        if r["state"] == "missing":
            raise GateError(f"`{name}` is missing from curated/ — run `atlas legacy export-curated …` or write it, "
                            f"then `atlas curate confirm {name}`")
        if r["state"] in ("changed", "unconfirmed"):
            raise GateError(f"`{name}` changed after confirmation — run `atlas curate confirm {name}`")
