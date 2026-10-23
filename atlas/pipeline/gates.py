"""Curated-file gates: `atlas curate confirm <name>` / `atlas curate status` and the sha check that gated stages
run before they start (architecture §7). Implemented by: increment 2 (derived layer); increment 4 adds
`atlas curate merge <draft>` (work package S).

`confirmations.json` = {name: {sha, confirmed_at, from_draft_sha}}. A gated stage stops with
"`<name>` changed after confirmation — run `atlas curate confirm <name>`" when the file's sha differs from the
confirmed one (or was never confirmed).

Merge (`merge_draft`): the only writer of curated files besides `atlas legacy export-curated`. A draft of stage
`terms` goes into `term_tree.yaml` (domain = `--domain`, else the first counting-scope domain, else
`DOM_<project>`; section = the term's main section in the v3 vocabulary; sub-structure = the term type), `concepts`
into `concepts.yaml` + `patterns.yaml` (delta drafts also apply `merge_into` / `new_form` proposals as new forms;
`retire` proposals are reported, never applied), `scheme` into `domains.yaml` / `categories.yaml` /
`subdomains.yaml`, `classify` into `concept_categories.yaml` with `curated/overrides/categories.yaml` applied on
top. Entries are keyed by id (terms: domain + section + normalised term). Three-way rule per entry: not in the
curated file → **added** (`source: llm`, `added_in: <run>`); in both and the curated entry is the owner's
(`source` other than llm) or was edited since the run that added it (its base is that run's draft) → **kept**,
reported as a **conflict** when the draft differs; in both, unedited since its own run → **updated**; in the
curated file (source llm) but not in the draft → listed under **retire** (reported only: drafts are samples). No
duplicate forms across concepts is checked (a colliding draft concept is a conflict, not written).
"""
from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path

from atlas.config import Project
from atlas.core.ids import slug
from atlas.core.matcher import DEFAULT_PATTERNS, form_keys, norm_key
from atlas.store.curated import (CURATED_FILES, curated_path, file_sha, read_confirmations, read_curated, read_yaml,
                                 write_confirmations, write_yaml)


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


# ---- drafts → curated (increment 4) ----------------------------------------------------------------------------------

DRAFT_TARGETS: dict[str, tuple[str, ...]] = {"terms": ("term_tree",), "concepts": ("concepts", "patterns"),
                                              "scheme": ("domains", "categories", "subdomains"),
                                              "classify": ("concept_categories",)}
#: v3 section vocabulary of the term tree; other section labels map onto it
TREE_SECTIONS = ("title", "keywords", "abstract", "introduction", "methods", "results", "discussion")
_SECTION_MAP = {"conclusion": "discussion", "other": "introduction", "front": "introduction", "appendix": "methods",
                "acknowledgements": "introduction", "references": "introduction"}
OVERRIDES_DIR = "overrides"
CATEGORY_OVERRIDES = "categories.yaml"
_META_KEYS = ("source", "added_in", "status")


class MergeError(ValueError):
    pass


def _read_draft(path: str | Path) -> dict:
    from atlas.pipeline.llm_stage import read_draft
    return read_draft(path)


def _draft_entries(project: Project, stage: str, run_id: str | None, section: str) -> dict | None:
    """The entries of `section` in the draft of `run_id` (the three-way base), or None when it is not on disk."""
    from atlas.pipeline.llm_stage import drafts_dir
    if not run_id:
        return None
    folder = drafts_dir(project, stage)
    for name in (f"{run_id}.yaml", f"{run_id}.delta.yaml"):
        p = folder / name
        if p.is_file():
            try:
                return _read_draft(p).get(section) or []
            except Exception:
                return None
    return None


def _strip(entry: Mapping, drop: Sequence[str] = ()) -> dict:
    return {k: v for k, v in entry.items() if k not in _META_KEYS and k not in drop}


def _same(a: Mapping, b: Mapping, drop: Sequence[str] = ()) -> bool:
    return _strip(a, drop) == _strip(b, drop)


class _Merge:
    """Three-way merge of one list of entries (see the module docstring)."""

    def __init__(self, project: Project, stage: str, section: str, key_fn: Callable[[Mapping], object], run_id: str,
                 draft_only: Sequence[str] = (), compare_ignore: Sequence[str] = (),
                 base_convert: Callable[[Mapping], dict] | None = None):
        """`draft_only` keys are stripped from the written entries and ignored in comparisons; `compare_ignore`
        keys are written but ignored in comparisons; `base_convert` turns a raw draft row into the entry shape
        (the term tree stores a draft term differently from the draft)."""
        self.project, self.stage, self.section, self.key_fn, self.run_id = project, stage, section, key_fn, run_id
        self.draft_only = tuple(draft_only)
        self.ignore = tuple(draft_only) + tuple(compare_ignore)
        self.base_convert = base_convert
        self.added: list = []
        self.updated: list = []
        self.kept: list = []
        self.unchanged: list = []
        self.conflicts: list[dict] = []
        self.retire: list = []
        self._bases: dict[str, dict] = {}

    def _base(self, entry: Mapping):
        run = entry.get("added_in")
        if not run:
            return None
        if run not in self._bases:
            rows = _draft_entries(self.project, self.stage, str(run), self.section)
            if rows and self.base_convert:
                rows = [self.base_convert(r) for r in rows]
            self._bases[run] = {self.key_fn(r): r for r in rows} if rows else {}
        return self._bases[run].get(self.key_fn(entry))

    def clean(self, entry: Mapping) -> dict:
        out = {k: v for k, v in entry.items() if k not in self.draft_only and k != "status"}
        out["source"] = "llm"
        out["added_in"] = self.run_id
        return out

    def run(self, current: Sequence[Mapping], theirs: Sequence[Mapping],
            reject: Callable[[Mapping, dict], str | None] | None = None) -> list[dict]:
        cur = {self.key_fn(e): dict(e) for e in current}
        order = list(cur)
        seen: set = set()
        for t in theirs:
            k = self.key_fn(t)
            if k in seen:
                continue
            seen.add(k)
            new = self.clean(t)
            c = cur.get(k)
            if c is None:
                why = reject(new, cur) if reject else None
                if why:
                    self.conflicts.append({"key": k, "reason": why})
                    continue
                cur[k] = new
                order.append(k)
                self.added.append(k)
                continue
            if _same(c, new, self.ignore):
                self.unchanged.append(k)
                continue
            if str(c.get("source") or "owner") != "llm":
                self.kept.append(k)
                self.conflicts.append({"key": k, "reason": "owner entry differs from the draft (kept)"})
                continue
            base = self._base(c)
            if base is not None and _same(c, base, self.ignore):
                why = reject(new, {x: y for x, y in cur.items() if x != k}) if reject else None
                if why:
                    self.conflicts.append({"key": k, "reason": why})
                    continue
                cur[k] = new
                self.updated.append(k)
            else:
                self.kept.append(k)
                self.conflicts.append({"key": k, "reason": "entry edited since its run (kept)"})
        self.retire = [k for k in order if k not in seen and str(cur[k].get("source") or "") == "llm"]
        return [cur[k] for k in order]

    def report(self) -> dict:
        return {"added": [str(k) for k in self.added], "updated": [str(k) for k in self.updated],
                "kept": [str(k) for k in self.kept], "unchanged": len(self.unchanged),
                "conflicts": [{"key": str(c["key"]), "reason": c["reason"]} for c in self.conflicts],
                "retire": [str(k) for k in self.retire]}


def _write(project: Project, name: str, data: Mapping, report: dict, section_report: dict) -> None:
    sha = write_yaml(curated_path(project.curated_dir, name), data)
    report["files"][name] = dict(section_report, sha=sha, file=str(curated_path(project.curated_dir, name)))


def _default_domain(project: Project, domain: str | None) -> str:
    if domain:
        return domain
    doms = (read_curated(project.curated_dir, "domains") or {}).get("domains") or []
    for d in doms:
        if d.get("scope", True):
            return str(d["id"])
    return "DOM_" + (slug(project.name, 20).replace("-", "_") or "project")


def tree_section(section: str | None) -> str:
    s = str(section or "introduction")
    return s if s in TREE_SECTIONS else _SECTION_MAP.get(s, "introduction")


def term_entry(t: Mapping, dom: str, texts: Mapping[str, str] | None = None) -> dict | None:
    """A terms-draft term as a term-tree entry (flat: domain, section, sub + the v3 term keys)."""
    term = " ".join(str(t.get("term") or "").split())
    if not term:
        return None
    secs = t.get("sections") or {}
    main = max(secs, key=lambda s: (secs[s], s)) if secs else "introduction"
    ex_sid = (t.get("example_sids") or [None])[0]
    return {"domain": dom, "section": tree_section(main), "sub": str(t.get("type") or "名词术语"), "term": term,
            "zh": str(t.get("zh") or ""), "type": str(t.get("type") or ""), "concept_id": None,
            "note": str(t.get("gloss") or ""), "example": (texts or {}).get(ex_sid, "") if ex_sid else "",
            "example_doc": (t.get("docs") or [""])[0] if t.get("docs") else ""}


def _merge_terms(project: Project, draft: Mapping, run_id: str, report: dict, domain: str | None) -> None:
    from atlas.store.evidence import EvidenceStore
    dom = _default_domain(project, domain)
    terms = draft.get("terms") or []
    sids = [t["example_sids"][0] for t in terms if t.get("example_sids")]
    texts: dict[str, str] = {}
    if sids and project.evidence_db.is_file():
        store = EvidenceStore.open(project.evidence_db, "ro")
        try:
            texts = store.texts_of(sids)
        finally:
            store.close()
    theirs = [e for e in (term_entry(t, dom, texts) for t in terms) if e]
    data = read_curated(project.curated_dir, "term_tree") or {}
    current = []
    sub_order: dict[tuple, int] = {}
    for d in data.get("domains") or []:
        for sec in d.get("sections") or []:
            for sub in sec.get("subs") or []:
                sub_order[(d["id"], sec.get("section"), sub.get("name"))] = int(sub.get("order", 0) or 0)
                for e in sub.get("terms") or []:
                    current.append({"domain": d["id"], "section": sec.get("section"), "sub": sub.get("name"), **e})
    key_fn = lambda e: (str(e.get("domain")), str(e.get("section")), norm_key(str(e.get("term") or "")))  # noqa: E731
    # the base of an entry is its own run's draft term in the same flat shape (example texts are not compared, so
    # an edit of the example alone does not count as a user edit)
    m = _Merge(project, "terms", "terms", key_fn, run_id, compare_ignore=("example", "example_doc"),
               base_convert=lambda t: term_entry(t, dom) or {})
    merged = m.run(current, theirs)
    tree: dict[str, dict] = {}
    for e in merged:
        d = tree.setdefault(str(e["domain"]), {"id": str(e["domain"]), "sections": {}})
        sec = d["sections"].setdefault(str(e["section"]), {"section": str(e["section"]), "subs": {}})
        subname = str(e.get("sub") or "名词术语")
        if subname not in sec["subs"]:
            known = sub_order.get((str(e["domain"]), str(e["section"]), subname))
            nxt = max([v for (dd, ss, _), v in sub_order.items() if dd == str(e["domain"]) and ss == str(e["section"])] + [-1]) + 1
            if known is None:
                sub_order[(str(e["domain"]), str(e["section"]), subname)] = nxt
            sec["subs"][subname] = {"name": subname, "order": known if known is not None else nxt, "terms": []}
        entry = {k: v for k, v in e.items() if k not in ("domain", "section", "sub")}
        sec["subs"][subname]["terms"].append(entry)
    doms = []
    for d in tree.values():
        secs = []
        for s in sorted(d["sections"].values(), key=lambda s: (TREE_SECTIONS.index(s["section"]) if s["section"] in TREE_SECTIONS else 99, s["section"])):
            s["subs"] = sorted(s["subs"].values(), key=lambda x: (x["order"], x["name"]))
            secs.append(s)
        doms.append({"id": d["id"], "sections": secs})
    out = dict(data)
    out["domains"] = doms
    out.setdefault("source", "mixed")
    _write(project, "term_tree", out, report, m.report())
    report["notes"].append(f"terms placed under domain {dom}; sections by the term's main section; sub-structure = term type")


def _concept_keys(c: Mapping) -> set[str]:
    out: set[str] = set()
    for f in list(c.get("forms") or []) + list(c.get("gen_forms") or []):
        out.update(form_keys(str(f)))
    for f in c.get("case_forms") or []:
        k = norm_key(str(f))
        if k:
            out.add(k)
    return out


def _merge_concepts(project: Project, draft: Mapping, run_id: str, report: dict) -> None:
    data = read_curated(project.curated_dir, "concepts") or {}
    current = [dict(c) for c in (data.get("concepts") or [])]
    m = _Merge(project, "concepts", "concepts", lambda c: str(c.get("id")), run_id,
               draft_only=("merged_terms", "example_sids", "note"))

    def reject(new: Mapping, others: Mapping) -> str | None:
        owners: dict[str, str] = {}
        for cid, c in others.items():
            if c.get("merged_into"):
                continue
            for k in _concept_keys(c):
                owners.setdefault(k, str(cid))
        clash = sorted({owners[k] for k in _concept_keys(new) if k in owners})
        return f"forms collide with {', '.join(clash)}" if clash else None

    merged = m.run(current, draft.get("concepts") or [], reject)
    by_id = {str(c["id"]): c for c in merged}
    proposals: list[dict] = []
    applied: list[dict] = []
    owners: dict[str, str] = {}
    for c in merged:
        if not c.get("merged_into"):
            for k in _concept_keys(c):
                owners.setdefault(k, str(c["id"]))
    for d in draft.get("deltas") or []:
        action = d.get("action")
        if action in ("merge_into", "new_form"):
            cid, form = str(d.get("concept_id") or ""), " ".join(str(d.get("form") or d.get("term") or "").split())
            c = by_id.get(cid)
            if c is None or not form:
                proposals.append(dict(d, result="rejected: unknown concept or empty form"))
                continue
            ks = form_keys(form)
            clash = sorted({owners[k] for k in ks if k in owners and owners[k] != cid})
            if clash:
                proposals.append(dict(d, result=f"conflict: form belongs to {', '.join(clash)}"))
                continue
            if any(k in _concept_keys(c) for k in ks):
                proposals.append(dict(d, result="unchanged: already a form"))
                continue
            c.setdefault("forms", []).append(form)
            for k in ks:
                owners[k] = cid
            applied.append({"action": "new_form", "concept_id": cid, "form": form})
        else:
            proposals.append(dict(d, result="proposal only (apply by hand)"))
    # the invariant: no duplicate forms across unmerged concepts
    seen: dict[str, str] = {}
    dups: list[str] = []
    for c in merged:
        if c.get("merged_into"):
            continue
        for k in _concept_keys(c):
            if k in seen and seen[k] != str(c["id"]):
                dups.append(f"{k!r}: {seen[k]} / {c['id']}")
            seen.setdefault(k, str(c["id"]))
    if dups:
        raise MergeError("duplicate forms across concepts after merge: " + "; ".join(dups[:10]))
    out = dict(data)
    out["concepts"] = sorted(merged, key=lambda c: str(c["id"]))
    out.setdefault("source", "mixed")
    rep = m.report()
    rep["deltas_applied"] = applied
    rep["deltas_proposals"] = proposals
    _write(project, "concepts", out, report, rep)
    pat_path = curated_path(project.curated_dir, "patterns")
    pats = draft.get("patterns") or dict(DEFAULT_PATTERNS)
    if not pat_path.is_file():
        _write(project, "patterns", dict(pats), report, {"added": list(pats), "updated": [], "kept": [], "unchanged": 0,
                                                          "conflicts": [], "retire": []})
    else:
        cur = read_yaml(pat_path) or {}
        same = cur == pats
        report["files"]["patterns"] = {"added": [], "updated": [], "kept": [] if same else sorted(cur), "unchanged": len(pats) if same else 0,
                                       "conflicts": [] if same else [{"key": "patterns", "reason": "existing patterns.yaml kept"}],
                                       "retire": [], "sha": file_sha(pat_path), "file": str(pat_path)}


def _merge_scheme(project: Project, draft: Mapping, run_id: str, report: dict) -> None:
    for name, key, draft_only in (("domains", "domains", ()), ("categories", "categories", ()),
                                  ("subdomains", "subdomains", ("evidence_docs",))):
        data = read_curated(project.curated_dir, name) or {}
        current = [dict(c) for c in (data.get(key) or [])]
        m = _Merge(project, "scheme", key, lambda c: str(c.get("id")), run_id, draft_only=draft_only)
        merged = m.run(current, draft.get(key) or [])
        out = dict(data)
        out[key] = merged
        out.setdefault("source", "mixed")
        _write(project, name, out, report, m.report())
    known = {str(c["id"]) for c in (read_curated(project.curated_dir, "concepts") or {}).get("concepts") or []}
    subs = (read_curated(project.curated_dir, "subdomains") or {}).get("subdomains") or []
    bad = sorted({a for s in subs for a in (s.get("anchors") or []) + [x for g in (s.get("groups") or []) for x in g] if a not in known})
    if bad:
        report["notes"].append(f"anchors not in concepts.yaml: {', '.join(bad[:20])}")


def read_category_overrides(project: Project) -> dict[str, dict]:
    """`curated/overrides/categories.yaml`: {concept_id: {domain, primary, secondary, reason}} or a `rows` list."""
    p = project.curated_dir / OVERRIDES_DIR / CATEGORY_OVERRIDES
    if not p.is_file():
        return {}
    data = read_yaml(p) or {}
    if isinstance(data, dict) and isinstance(data.get("rows"), list):
        return {str(r["concept_id"]): dict(r) for r in data["rows"] if isinstance(r, dict) and r.get("concept_id")}
    if isinstance(data, dict):
        return {str(k): dict(v) for k, v in data.items() if isinstance(v, dict)}
    return {}


def _merge_classify(project: Project, draft: Mapping, run_id: str, report: dict) -> None:
    data = read_curated(project.curated_dir, "concept_categories") or {}
    current = [dict(r) for r in (data.get("rows") or [])]
    m = _Merge(project, "classify", "rows", lambda r: str(r.get("concept_id")), run_id, draft_only=("evidence_sids",))
    theirs = [r for r in (draft.get("rows") or []) if r.get("status") in (None, "ok", "skipped") and r.get("domain") and r.get("primary")]
    merged = m.run(current, theirs)
    overrides = read_category_overrides(project)
    applied = []
    by_id = {str(r["concept_id"]): r for r in merged}
    for cid, o in sorted(overrides.items()):
        row = by_id.get(cid)
        if row is None:
            row = {"concept_id": cid, "domain": None, "primary": None, "secondary": None, "reason": ""}
            merged.append(row)
            by_id[cid] = row
        for k in ("domain", "primary", "secondary", "reason"):
            if k in o:
                row[k] = o[k]
        row["source"] = "owner"
        applied.append(cid)
    out = dict(data)
    out["rows"] = sorted(merged, key=lambda r: str(r["concept_id"]))
    out.setdefault("source", "mixed")
    rep = m.report()
    rep["overrides_applied"] = applied
    _write(project, "concept_categories", out, report, rep)


def merge_draft(project: Project, draft: str | Path, domain: str | None = None) -> dict:
    """`atlas curate merge <draft>`: three-way merge of a stage draft into its curated file(s); returns the report
    {draft, stage, run_id, files: {name: {added, updated, kept, unchanged, conflicts, retire, sha, file}}, notes}."""
    p = Path(draft)
    if not p.is_file():
        raise FileNotFoundError(f"draft not found: {p}")
    data = _read_draft(p)
    stage = str(data.get("stage") or "")
    if stage not in DRAFT_TARGETS:
        raise MergeError(f"not a stage draft (stage {stage!r}): {p}")
    run_id = str(data.get("run_id") or p.stem.split(".")[0])
    report: dict = {"draft": str(p), "draft_sha": file_sha(p), "stage": stage, "run_id": run_id, "files": {}, "notes": []}
    if stage == "terms":
        _merge_terms(project, data, run_id, report, domain)
    elif stage == "concepts":
        _merge_concepts(project, data, run_id, report)
    elif stage == "scheme":
        _merge_scheme(project, data, run_id, report)
    else:
        _merge_classify(project, data, run_id, report)
    return report


def format_merge_report(r: Mapping) -> str:
    lines = [f"merged {r['stage']} draft {Path(r['draft']).name} (run {r['run_id']})"]
    for name, f in r["files"].items():
        lines.append(f"  {name}: added {len(f['added'])}, updated {len(f['updated'])}, kept {len(f['kept'])}, "
                     f"unchanged {f['unchanged']}, conflicts {len(f['conflicts'])}, retire candidates {len(f['retire'])}"
                     f" → sha {str(f.get('sha') or '')[:12]}")
        for c in f["conflicts"][:20]:
            lines.append(f"    conflict {c['key']}: {c['reason']}")
        for k in f["retire"][:20]:
            lines.append(f"    retire? {k} (in the curated file, not in this draft)")
        for d in f.get("deltas_applied") or []:
            lines.append(f"    applied {d['action']} {d['form']} → {d['concept_id']}")
        for d in (f.get("deltas_proposals") or [])[:20]:
            lines.append(f"    proposal {d.get('action')} {d.get('term') or d.get('concept_id')} → {d.get('concept_id') or d.get('target')}: {d.get('result')}")
        if f.get("overrides_applied"):
            lines.append(f"    overrides applied: {', '.join(f['overrides_applied'][:20])}")
    for n in r.get("notes") or []:
        lines.append(f"  note: {n}")
    lines.append("  then: `atlas curate confirm <name>` for each file above")
    return "\n".join(lines)


def draft_notices(project: Project) -> list[str]:
    """`curate status` notices: a stage draft newer than the confirmation of its target file(s)."""
    from atlas.pipeline.llm_stage import list_drafts, read_draft
    conf = read_confirmations(project.curated_dir)
    out: list[str] = []
    for stage, targets in DRAFT_TARGETS.items():
        ds = list_drafts(project, stage)
        if not ds:
            continue
        p = ds[-1]
        try:
            created = str(read_draft(p).get("created") or "")
        except Exception:
            continue
        for name in targets:
            c = conf.get(name) or {}
            at = str(c.get("confirmed_at") or "")
            if not at or (created and created > at):
                out.append(f"{stage} draft {p.name} is newer than the confirmation of `{name}` "
                           f"({'never confirmed' if not at else 'confirmed ' + at}) — `atlas curate merge {p}` then confirm")
    return out
