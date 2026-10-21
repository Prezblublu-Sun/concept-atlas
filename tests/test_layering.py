"""Dependency rule, enforced by parsing imports:

    interfaces(cli, http, mcp) → service → {query, user, pipeline, verify} → {store, ingest, llm} → {core, doctypes}

`core` and `doctypes` import only the standard library (numpy allowed in core). Nothing imports a stage module
except pipeline.runner / other stages. No module outside `store` calls sqlite3.connect.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "atlas"

LAYER_OF = {
    "core": 0, "doctypes": 0,
    "store": 1, "ingest": 1, "llm": 1,
    "query": 2, "user": 2, "pipeline": 2, "verify": 2,
    "service": 3,
    "cli": 4, "http": 4, "mcp": 4,
    "config": 1, "tools": 4,
}
STDLIB_OK_IN_CORE = {"numpy"}


def _layer(mod: str) -> int | None:
    top = mod.split(".")[0]
    return LAYER_OF.get(top)


def _module_name(path: Path) -> str:
    rel = path.relative_to(ROOT).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) or "atlas"


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.level:  # relative import → resolve against the package
                pkg = _module_name(path).split(".")
                if path.name != "__init__.py":
                    pkg = pkg[:-1]
                base = pkg[: len(pkg) - (node.level - 1)] if node.level > 1 else pkg
                out.append(".".join(base + [node.module]))
            else:
                out.append(node.module)
    return out


def test_dependency_direction():
    problems = []
    for py in ROOT.rglob("*.py"):
        mod = _module_name(py)
        my = _layer(mod)
        if my is None:
            continue
        for imp in _imports(py):
            if imp.startswith("atlas."):
                target = imp[len("atlas."):]
                tl = _layer(target)
                if tl is not None and tl > my:
                    problems.append(f"{mod} (layer {my}) imports {target} (layer {tl})")
            elif _layer(mod) == 0:
                top = imp.split(".")[0]
                if top not in STDLIB_OK_IN_CORE and not _is_stdlib(top):
                    problems.append(f"{mod} imports third-party {top}")
    assert not problems, "\n".join(problems)


def _is_stdlib(top: str) -> bool:
    import sys
    return top in sys.stdlib_module_names


def test_only_store_connects():
    offenders = []
    for py in ROOT.rglob("*.py"):
        if py.relative_to(ROOT).parts[0] == "store" and py.name == "db.py":
            continue
        if re.search(r"sqlite3\.connect\(", py.read_text(encoding="utf-8")):
            offenders.append(str(py.relative_to(ROOT)))
    assert not offenders, f"sqlite3.connect outside store/db.py: {offenders}"
