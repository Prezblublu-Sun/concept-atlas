"""Build orchestration (`atlas build …`): new → match → assign → finish → promote. Implemented by: increment 2.

- `new_build`: `builds/b<YYYY-MM-DD>_<seq3>/` with build.sqlite (status open), manifest.json, a snapshot of
  curated/, the pinned evidence and the scheme tables loaded from the snapshot.
- `promote`: writes `builds/current.txt` (temp file + os.replace) after `relink` (increment 3: returns {}).
- `build_all`: the whole chain in one go, with the wall time per stage.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from atlas.config import Project
from atlas.store.build import (BuildStore, current_build_id, list_build_dirs, new_build_id, read_manifest,
                               set_current_build, sha256_file)
from atlas.store.curated import read_curated

log = logging.getLogger(__name__)


class BuildError(Exception):
    pass


def build_dir_of(project: Project, build_id: str) -> Path:
    return project.builds_dir / build_id


def list_builds(project: Project) -> list[dict]:
    out = []
    cur = current_build_id(project.builds_dir)
    for d in list_build_dirs(project.builds_dir):
        m = read_manifest(d) or {"build_id": d.name, "status": "unknown"}
        m["current"] = d.name == cur
        m["folder"] = str(d)
        out.append(m)
    return out


def resolve_build(project: Project, build: str | Path | None = None, require_open: bool = False) -> Path:
    """The build folder for an id / path, else the newest build (open when `require_open`)."""
    if build:
        p = Path(build)
        if not p.is_absolute() or not p.is_dir():
            p = build_dir_of(project, str(build))
        if not (p / "build.sqlite").is_file():
            raise BuildError(f"no such build: {build}")
        if require_open and (read_manifest(p) or {}).get("status") != "open":
            raise BuildError(f"build {p.name} is not open (status {(read_manifest(p) or {}).get('status')})")
        return p
    dirs = list_build_dirs(project.builds_dir)
    for d in reversed(dirs):
        m = read_manifest(d) or {}
        if not require_open or m.get("status") == "open":
            return d
    raise BuildError("no open build; run `atlas build new` first" if require_open else "no build; run `atlas build new`")


def open_build_store(project: Project, build: str | Path | None = None, require_open: bool = False,
                     mode: str = "rw") -> BuildStore:
    d = resolve_build(project, build, require_open)
    if mode == "rw" and (read_manifest(d) or {}).get("status") == "finished":
        mode = "immutable"
    return BuildStore.open(d, mode, evidence_path=project.evidence_db)


def _doc_domains(project: Project) -> dict[str, list[str]]:
    data = read_curated(project.curated_dir, "doc_domains") or {}
    docs = data.get("docs") if isinstance(data, dict) else None
    return {str(k): list(v or []) for k, v in (docs or {}).items()}


def new_build(project: Project, note: str | None = None) -> dict:
    if not project.evidence_db.is_file():
        raise BuildError(f"no evidence store at {project.evidence_db}; run `atlas run ingest` first")
    build_id = new_build_id(project.builds_dir)
    t0 = time.perf_counter()
    store = BuildStore.create(project.builds_dir, build_id, project.name, project.curated_dir, note)
    try:
        pinned = store.pin_evidence(project.evidence_db, _doc_domains(project))
        scheme = store.load_scheme()
        ev_sha = None
        try:
            ev_sha = sha256_file(project.evidence_db) if project.evidence_db.stat().st_size < 512 * 1024 * 1024 else None
        except OSError:
            ev_sha = None
        m = store.update_manifest(docs=pinned["docs"], sentences=pinned["sentences"], units=pinned["units"],
                                  scheme=scheme, evidence_sha_at_pin=ev_sha,
                                  stages={"new": {"seconds": round(time.perf_counter() - t0, 2)}})
        store.meta_set("n_docs", str(pinned["docs"]))
        store.meta_set("n_sentences", str(pinned["sentences"]))
        store.meta_set("n_concepts", str(scheme["concepts"]))
    finally:
        store.close()
    log.info("build %s created: %s", build_id, m)
    return m


def build_status(project: Project, build: str | None = None) -> dict:
    d = resolve_build(project, build)
    m = read_manifest(d) or {"build_id": d.name}
    st = BuildStore.open(d, "immutable" if m.get("status") == "finished" else "ro")
    try:
        m["stage_items"] = st.stage_summary()
        m["counts"] = st.stats()
        m["checks"] = st.checks()
    finally:
        st.close()
    m["current"] = current_build_id(project.builds_dir) == d.name
    m["folder"] = str(d)
    return m


def relink(project: Project, old_build: str | None, new_build: str) -> dict:
    """Increment 3: re-resolve user refs (notes/collections) against the new build. TODO — returns {} for now."""
    return {}


def promote(project: Project, build: str | None = None) -> dict:
    d = resolve_build(project, build)
    m = read_manifest(d) or {}
    if m.get("status") != "finished":
        raise BuildError(f"build {d.name} is not finished (status {m.get('status')}); run `atlas run finish` first")
    old = current_build_id(project.builds_dir)
    links = relink(project, old, d.name)
    set_current_build(project.builds_dir, d.name)
    log.info("build %s promoted (was %s)", d.name, old)
    return {"build_id": d.name, "previous": old, "relink": links}


def build_all(project: Project, note: str | None = None, workers: int = 1) -> dict:
    """new → match → assign → finish → promote; returns {build_id, timings: {stage: seconds}, reports}."""
    from atlas.pipeline.runner import run_stage
    timings: dict[str, float] = {}
    reports: dict[str, dict] = {}
    t = time.perf_counter()
    m = new_build(project, note)
    timings["new"] = round(time.perf_counter() - t, 2)
    build_id = m["build_id"]
    for stage, opts in (("match", {"workers": workers}), ("assign", {}), ("finish", {})):
        t = time.perf_counter()
        rep = run_stage(project, stage, build=build_id, **opts)
        timings[stage] = round(time.perf_counter() - t, 2)
        reports[stage] = {"summary": rep.summary(), "warnings": rep.warnings[:50], "failed": rep.items_failed}
        if rep.items_failed:
            raise BuildError(f"stage {stage} reported {rep.items_failed} failures in build {build_id}; not promoted")
    t = time.perf_counter()
    promote(project, build_id)
    timings["promote"] = round(time.perf_counter() - t, 2)
    return {"build_id": build_id, "timings": timings, "reports": reports}
