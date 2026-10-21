"""Project configuration.

A project is a folder outside this repository:

    <root>/project.yaml          overrides of atlas/defaults.yaml (deep-merged)
    <root>/manifest.csv          optional: doc_id,title,year,authors,doi,venue,doc_type,file
    <root>/pdf/                  the PDFs (or any folder named by paths.pdf_dir)
    <root>/evidence/evidence.sqlite
    <root>/user/user.sqlite
    <root>/llm/llm.sqlite
    <root>/builds/               one write-once folder per build (later increments)
    <root>/curated/              owner-confirmed YAML (later increments)
    <root>/work/                 drafts, logs, scratch
    <root>/vectors/

Secrets never live in project.yaml; the LLM key is read from the environment variable named by llm.api_key_env.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

DEFAULTS_PATH = Path(__file__).with_name("defaults.yaml")
PROJECT_FILE = "project.yaml"


def load_defaults() -> dict:
    with open(DEFAULTS_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def deep_merge(base: dict, override: dict) -> dict:
    """Return a new dict: `override` merged into `base`; nested dicts merge, everything else replaces."""
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


@dataclass
class Project:
    root: Path
    config: dict

    # ---- identity
    @property
    def name(self) -> str:
        return str(self.config.get("name") or self.root.name)

    # ---- paths (all under root unless configured absolute)
    def _p(self, rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else self.root / p

    @property
    def pdf_dir(self) -> Path:
        return self._p(self.get("paths.pdf_dir", "pdf"))

    @property
    def manifest_path(self) -> Path:
        return self.root / "manifest.csv"

    @property
    def evidence_db(self) -> Path:
        return self.root / "evidence" / "evidence.sqlite"

    @property
    def user_db(self) -> Path:
        return self.root / "user" / "user.sqlite"

    @property
    def llm_db(self) -> Path:
        return self.root / "llm" / "llm.sqlite"

    @property
    def builds_dir(self) -> Path:
        return self.root / "builds"

    @property
    def curated_dir(self) -> Path:
        return self.root / "curated"

    @property
    def work_dir(self) -> Path:
        return self.root / "work"

    @property
    def logs_dir(self) -> Path:
        return self.root / "work" / "logs"

    @property
    def vectors_dir(self) -> Path:
        return self.root / "vectors"

    @property
    def exports_dir(self) -> Path:
        return self.root / "user" / "exports"

    def ensure_dirs(self) -> None:
        for d in (self.evidence_db.parent, self.user_db.parent, self.llm_db.parent, self.builds_dir, self.curated_dir,
                  self.work_dir, self.logs_dir, self.vectors_dir, self.exports_dir):
            d.mkdir(parents=True, exist_ok=True)

    # ---- config access
    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self.config
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur


def load_project(root: str | Path) -> Project:
    root = Path(root).resolve()
    pf = root / PROJECT_FILE
    if not pf.exists():
        raise FileNotFoundError(f"not a concept-atlas project (no {PROJECT_FILE}): {root}")
    with open(pf, encoding="utf-8") as f:
        override = yaml.safe_load(f) or {}
    proj = Project(root=root, config=deep_merge(load_defaults(), override))
    proj.ensure_dirs()
    return proj


def init_project(root: str | Path, name: str | None = None, doc_type: str = "paper", pdf_dir: str = "pdf",
                 force: bool = False) -> Project:
    """Create <root>/project.yaml (only the keys that differ from defaults) and the standard folders."""
    root = Path(root).resolve()
    pf = root / PROJECT_FILE
    if pf.exists() and not force:
        raise FileExistsError(f"{pf} already exists (use force=True to overwrite)")
    root.mkdir(parents=True, exist_ok=True)
    override = {"name": name or root.name, "doc_type": doc_type, "paths": {"pdf_dir": pdf_dir}}
    with open(pf, "w", encoding="utf-8") as f:
        f.write("# concept-atlas project configuration; see atlas/defaults.yaml for every key\n")
        yaml.safe_dump(override, f, allow_unicode=True, sort_keys=False)
    return load_project(root)
