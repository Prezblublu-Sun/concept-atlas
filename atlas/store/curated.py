"""Curated files: the owner-confirmed YAML under `<project>/curated/` (increment 2 §1) and their confirmation
record. Stages never write here; only `atlas curate …` and `atlas legacy export-curated` do (tools/ + gates).

Implemented by: increment 2 (derived layer).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml

CURATED_FILES: dict[str, str] = {
    "domains": "domains.yaml",
    "concepts": "concepts.yaml",
    "patterns": "patterns.yaml",
    "term_tree": "term_tree.yaml",
    "categories": "categories.yaml",
    "concept_categories": "concept_categories.yaml",
    "subdomains": "subdomains.yaml",
    "doc_domains": "doc_domains.yaml",
    "i18n": "i18n.yaml",
}
CONFIRMATIONS_FILE = "confirmations.json"
UNITS_OVERRIDE_FILE = "units_override.yaml"


def curated_path(curated_dir: str | Path, name: str) -> Path:
    if name not in CURATED_FILES:
        raise KeyError(f"unknown curated file {name!r}; known: {sorted(CURATED_FILES)}")
    return Path(curated_dir) / CURATED_FILES[name]


def file_sha(path: str | Path) -> str | None:
    p = Path(path)
    if not p.is_file():
        return None
    return hashlib.sha256(p.read_bytes()).hexdigest()


def read_yaml(path: str | Path):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def write_yaml(path: str | Path, data) -> str:
    """Write with sorted keys, UTF-8, block style; returns the file's sha256."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=True, default_flow_style=False, width=120)
    return file_sha(p) or ""


def read_curated(curated_dir: str | Path, name: str):
    """The parsed file or None when it does not exist."""
    p = curated_path(curated_dir, name)
    return read_yaml(p) if p.is_file() else None


def load_curated(curated_dir: str | Path) -> dict[str, object]:
    """{name: data} for every curated file that exists."""
    out: dict[str, object] = {}
    for name in CURATED_FILES:
        data = read_curated(curated_dir, name)
        if data is not None:
            out[name] = data
    return out


def curated_shas(curated_dir: str | Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for name in CURATED_FILES:
        sha = file_sha(curated_path(curated_dir, name))
        if sha:
            out[name] = sha
    return out


def read_confirmations(curated_dir: str | Path) -> dict:
    p = Path(curated_dir) / CONFIRMATIONS_FILE
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def write_confirmations(curated_dir: str | Path, data: dict) -> None:
    p = Path(curated_dir) / CONFIRMATIONS_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(p)


def read_units_override(curated_dir: str | Path) -> dict:
    p = Path(curated_dir) / UNITS_OVERRIDE_FILE
    if not p.is_file():
        return {}
    data = read_yaml(p) or {}
    return data if isinstance(data, dict) else {}
