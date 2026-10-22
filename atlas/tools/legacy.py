"""Legacy import: the previous viewer's `terms_v3.db` → `<project>/curated/*.yaml` (increment 2 §1), and its user
highlights → notes. Implemented by: increment 2 (derived layer).

`export_curated(terms_db, project, spec=None, force=False)` writes domains, concepts, patterns, term_tree,
categories, concept_categories, subdomains, doc_domains, i18n and confirmations.json (the files are the user's
own curated system, so the export confirms them) and returns a report dict. It refuses to overwrite an existing
`curated/` without `force`.

`import_highlights(highlights_db, sentences_db, project)`: v3 `hl(kind='sent')` rows become notes targeting
`S:<new sid>`; the v3 integer sid resolves through `sentences.db` (paper, text) to the new sid by
(doc_id, norm(text)) — exact first, then containment; unmatched rows are reported; statement highlights
(kind='stmt') are skipped with a count.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from pathlib import Path

from atlas.config import Project
from atlas.core.ids import norm
from atlas.core.matcher import DEFAULT_PATTERNS
from atlas.pipeline.gates import confirm_all
from atlas.store.curated import CURATED_FILES, curated_path, write_yaml
from atlas.store.db import connect
from atlas.store.evidence import EvidenceStore
from atlas.store.user import UserStore

log = logging.getLogger(__name__)

DOMAIN_MAP: dict[str, tuple[str, str, int, bool]] = {
    "骨科": ("DOM_ortho", "Orthopaedics", 0, True),
    "有限元": ("DOM_fe", "Finite element analysis", 1, True),
    "代理模型": ("DOM_surrogate", "Surrogate modelling", 2, True),
    "优化": ("DOM_opt", "Optimization", 3, True),
    "通用写作句式": ("DOM_writing", "Common writing phrases", 4, False),
    "通用": ("DOM_writing", "Common writing phrases", 4, False),
}
MODE_MAP = {"词形": "forms", "字面": "literal"}
KIND_MAP = {"领域内": "within", "交叉": "cross"}
SOURCE = "legacy-v3"


def _loads(s, default):
    if not s:
        return default
    try:
        return json.loads(s)
    except ValueError:
        return default


def _dom_id(zh: str | None, unknown: set[str]) -> str | None:
    if not zh:
        return None
    m = DOMAIN_MAP.get(zh.strip())
    if m is None:
        unknown.add(zh)
        return None
    return m[0]


def _split_domains(s: str | None) -> list[str]:
    return [d.strip() for d in re.split(r"[、,;；，]", s or "") if d.strip()]


def _ro(path: str | Path) -> sqlite3.Connection:
    return connect(path, "ro")


def export_curated(terms_db: str | Path, project: Project, spec: str | Path | None = None, force: bool = False) -> dict:
    cdir = project.curated_dir
    existing = [n for n in CURATED_FILES if curated_path(cdir, n).exists()]
    if existing and not force:
        raise FileExistsError(f"curated/ already has {', '.join(existing)} — use --force to overwrite")
    con = _ro(terms_db)
    unknown_domains: set[str] = set()
    report: dict = {"source": str(terms_db), "project": str(project.root), "files": {}, "unknown_domain_items": [],
                    "groups_recovered": [], "unknown_anchor_labels": [], "warnings": []}
    try:
        i18n_rows = [dict(r) for r in con.execute("SELECT zh, en, grp FROM i18n ORDER BY grp, zh")]
        en_of = {r["zh"]: r["en"] for r in i18n_rows if r["en"]}

        # ---- domains
        doms = [{"id": v[0], "zh": k, "en": v[1], "order": v[2], "scope": v[3]}
                for k, v in DOMAIN_MAP.items() if k != "通用"]
        report["files"]["domains"] = write_yaml(curated_path(cdir, "domains"), {"domains": doms, "source": SOURCE})

        # ---- concepts
        items = [dict(r) for r in con.execute("SELECT * FROM item ORDER BY id")]
        label2id = {it["label"]: it["id"] for it in items}
        concepts = []
        for it in items:
            unk: set[str] = set()
            dom_ids = [d for d in (_dom_id(z, unk) for z in _loads(it["domains"], [])) if d]
            if unk:
                report["unknown_domain_items"].append({"id": it["id"], "domains": sorted(unk)})
                unknown_domains |= unk
            concepts.append({
                "id": it["id"], "label": it["label"], "zh": it["zh"] or "", "type": it["type"] or "",
                "mode": MODE_MAP.get(it["mode"], "forms"),
                "forms": _loads(it["forms"], []), "gen_forms": _loads(it["gen_forms"], []),
                "case_forms": _loads(it["case_forms"], []), "rx": it["rx"], "probe": it["probe"] or "",
                "readable": it["readable"] or "", "domains": dom_ids, "place": _loads(it["place"], []),
                "shared_with": [label2id.get(x, x) for x in _loads(it["shared_with"], [])],
                "origin": it["origin"] or "", "merged_into": None, "source": SOURCE,
            })
        report["files"]["concepts"] = write_yaml(curated_path(cdir, "concepts"), {"concepts": concepts, "source": SOURCE})
        report["concepts"] = len(concepts)

        # ---- patterns
        report["files"]["patterns"] = write_yaml(curated_path(cdir, "patterns"), dict(DEFAULT_PATTERNS))

        # ---- term tree
        tree: dict[str, dict] = {}
        n_entries = 0
        for e in con.execute("SELECT * FROM entry ORDER BY dom_order, sec_order, sub_order, id"):
            e = dict(e)
            unk: set[str] = set()
            dom = _dom_id(e["domain"], unk) or "DOM_writing"
            unknown_domains |= unk
            d = tree.setdefault(dom, {"id": dom, "sections": {}})
            sec = d["sections"].setdefault(e["section"], {"section": e["section"], "subs": {}})
            sub = sec["subs"].setdefault(e["sub"], {"name": e["sub"], "order": int(e["sub_order"] or 0), "terms": []})
            sub["terms"].append({"term": e["term"], "zh": e["zh"] or "", "type": e["type"] or "",
                                 "concept_id": e["item_id"], "note": e["note"] or "", "example": e["example"] or "",
                                 "example_doc": e["ex_paper"] or ""})
            n_entries += 1
        tdoms = []
        for dom in sorted(tree.values(), key=lambda d: next((v[2] for v in DOMAIN_MAP.values() if v[0] == d["id"]), 99)):
            secs = []
            for sec in dom["sections"].values():
                sec["subs"] = sorted(sec["subs"].values(), key=lambda s: (s["order"], s["name"]))
                secs.append(sec)
            tdoms.append({"id": dom["id"], "sections": secs})
        report["files"]["term_tree"] = write_yaml(curated_path(cdir, "term_tree"), {"domains": tdoms, "source": SOURCE})
        report["term_entries"] = n_entries

        # ---- categories
        cats = []
        for c in con.execute("SELECT * FROM category ORDER BY domain, ord"):
            c = dict(c)
            unk: set[str] = set()
            cats.append({"id": c["id"], "domain": _dom_id(c["domain"], unk), "name_zh": c["name"],
                         "name_en": en_of.get(c["name"], ""), "desc_zh": c["desc"] or "",
                         "desc_en": en_of.get(c["desc"] or "", ""), "order": int(c["ord"] or 0)})
            unknown_domains |= unk
        report["files"]["categories"] = write_yaml(curated_path(cdir, "categories"), {"categories": cats, "source": SOURCE})
        report["categories"] = len(cats)

        rows = []
        for r in con.execute("SELECT * FROM concept_category ORDER BY domain, item_id"):
            r = dict(r)
            unk: set[str] = set()
            rows.append({"concept_id": r["item_id"], "domain": _dom_id(r["domain"], unk), "primary": r["primary_cat"],
                         "secondary": r["secondary_cat"] or None, "reason": r["reason"] or "",
                         "legacy_source": r["source"] or "", "source": SOURCE})
            unknown_domains |= unk
        report["files"]["concept_categories"] = write_yaml(curated_path(cdir, "concept_categories"), {"rows": rows, "source": SOURCE})
        report["concept_categories"] = len(rows)

        # ---- sub-domains (cross groups from the v3 db when already grouped, else from the spec, else by domain)
        spec_groups: dict[str, list[list[str]]] = {}
        if spec and Path(spec).is_file():
            sp = json.loads(Path(spec).read_text(encoding="utf-8"))
            for s in sp.get("cross", []) or []:
                groups = []
                for g in s.get("groups", []) or []:
                    ids = []
                    for lab in g:
                        cid = label2id.get(lab)
                        if cid is None:
                            report["unknown_anchor_labels"].append(lab)
                        else:
                            ids.append(cid)
                    groups.append(ids)
                spec_groups[s["id"]] = groups
        concept_doms = {c["id"]: c["domains"] for c in concepts}
        subs = []
        for s in con.execute("SELECT * FROM subdomain ORDER BY ord"):
            s = dict(s)
            kind = KIND_MAP.get(s["kind"], "within")
            unk: set[str] = set()
            anchors = _loads(s["anchors"], [])
            row = {"id": s["id"], "kind": kind, "domain": _dom_id(s["domain"], unk) if kind == "within" else None,
                   "name_zh": s["name"], "name_en": en_of.get(s["name"], ""), "desc_zh": s["desc"] or "",
                   "desc_en": en_of.get(s["desc"] or "", ""), "order": int(s["ord"] or 0),
                   "notes": s.get("review_notes") or ""}
            if kind == "within":
                row["anchors"] = [a for a in anchors if isinstance(a, str)]
            else:
                if anchors and all(isinstance(g, list) for g in anchors):
                    row["groups"] = anchors
                elif s["id"] in spec_groups:
                    row["groups"] = spec_groups[s["id"]]
                    row["groups_recovered"] = True
                    report["groups_recovered"].append(s["id"])
                else:
                    by_dom: dict[str, list[str]] = {}
                    for a in anchors:
                        for d in concept_doms.get(a, ["?"]) or ["?"]:
                            by_dom.setdefault(d, []).append(a)
                    row["groups"] = [sorted(set(v)) for _, v in sorted(by_dom.items())]
                    row["groups_recovered"] = True
                    report["groups_recovered"].append(s["id"])
            flat = row.get("anchors") or [a for g in row.get("groups", []) for a in g]
            missing = [a for a in flat if a not in concept_doms]
            if missing:
                report["warnings"].append(f"sub-domain {s['id']}: anchors not in concepts: {missing}")
            subs.append(row)
        report["files"]["subdomains"] = write_yaml(curated_path(cdir, "subdomains"), {"subdomains": subs, "source": SOURCE})
        report["subdomains"] = len(subs)

        # ---- doc domains
        docs: dict[str, list[str]] = {}
        n_assigned = 0
        for p in con.execute("SELECT id, domains FROM paper ORDER BY id"):
            unk: set[str] = set()
            ids = [d for d in (_dom_id(z, unk) for z in _split_domains(p["domains"])) if d]
            unknown_domains |= unk
            docs[p["id"]] = ids
            if ids:
                n_assigned += 1
        report["files"]["doc_domains"] = write_yaml(curated_path(cdir, "doc_domains"), {"docs": docs, "source": SOURCE})
        report["docs"] = len(docs)
        report["docs_with_domain"] = n_assigned

        # ---- i18n
        strings = [{"zh": r["zh"], "en": r["en"] or "", "grp": r["grp"] or ""} for r in i18n_rows]
        for k, v in DOMAIN_MAP.items():
            if k not in en_of and k != "通用":
                strings.append({"zh": k, "en": v[1], "grp": "ui"})
        report["files"]["i18n"] = write_yaml(curated_path(cdir, "i18n"), {"strings": strings, "source": SOURCE})
        report["i18n"] = len(strings)
    finally:
        con.close()
    report["unknown_domains"] = sorted(unknown_domains)
    report["confirmed"] = sorted(confirm_all(project))
    return report


def format_export_report(r: dict) -> str:
    lines = [f"curated files written to {r['project']}\\curated from {r['source']}",
             f"  concepts {r.get('concepts')}, term entries {r.get('term_entries')}, categories {r.get('categories')}, "
             f"concept categories {r.get('concept_categories')}, sub-domains {r.get('subdomains')}, "
             f"documents {r.get('docs')} ({r.get('docs_with_domain')} with a domain), i18n strings {r.get('i18n')}",
             f"  confirmed: {', '.join(r.get('confirmed', []))}"]
    if r.get("unknown_domain_items"):
        lines.append(f"  items with an unknown domain: {len(r['unknown_domain_items'])} "
                     f"(e.g. {r['unknown_domain_items'][:3]})")
    if r.get("unknown_domains"):
        lines.append(f"  unknown domain names: {r['unknown_domains']}")
    if r.get("groups_recovered"):
        lines.append(f"  cross sub-domains whose groups were recovered: {r['groups_recovered']}")
    if r.get("unknown_anchor_labels"):
        lines.append(f"  spec anchor labels not found: {sorted(set(r['unknown_anchor_labels']))}")
    for w in r.get("warnings", []):
        lines.append(f"  warning: {w}")
    return "\n".join(lines)


# ---- highlights ------------------------------------------------------------------------------------------------

def _resolve_sid(store: EvidenceStore, cache: dict[str, list[tuple[str, str]]], doc_id: str, text: str) -> tuple[str | None, str]:
    rows = cache.get(doc_id)
    if rows is None:
        rows = [(sid, norm(t)) for sid, t in store.sentence_texts_of_doc(doc_id)]
        cache[doc_id] = rows
    key = norm(text)
    if not key:
        return None, "empty"
    for sid, k in rows:
        if k == key:
            return sid, "exact"
    best: tuple[int, str] | None = None
    for sid, k in rows:
        if len(k) >= 20 and (key in k or k in key):
            score = min(len(k), len(key))
            if best is None or score > best[0]:
                best = (score, sid)
    if best:
        return best[1], "contains"
    return None, "unmatched"


def import_highlights(highlights_db: str | Path, sentences_db: str | Path, project: Project) -> dict:
    hl = _ro(highlights_db)
    sdb = _ro(sentences_db)
    ev = EvidenceStore.open(project.evidence_db, "ro")
    us = UserStore.open(project.user_db, "rw")
    report = {"imported": 0, "unmatched": [], "skipped_statements": 0, "methods": {}}
    cache: dict[str, list[tuple[str, str]]] = {}
    try:
        for r in hl.execute("SELECT * FROM hl ORDER BY id"):
            r = dict(r)
            if r.get("kind") != "sent":
                report["skipped_statements"] += 1
                continue
            s = sdb.execute("SELECT paper, page, text FROM sent WHERE sid=?", (r.get("sid"),)).fetchone()
            if s is None:
                report["unmatched"].append({"id": r["id"], "reason": "sid not in sentences.db"})
                continue
            new_sid, how = _resolve_sid(ev, cache, s["paper"], s["text"])
            report["methods"][how] = report["methods"].get(how, 0) + 1
            if new_sid is None:
                report["unmatched"].append({"id": r["id"], "reason": how, "paper": s["paper"], "quote": (r.get("quote") or "")[:80]})
                continue
            color = r.get("color") or "yellow"
            us.create_note(title="Highlight", body_md=r.get("note") or "", tags=["highlight", color],
                           targets=[{"ref": f"S:{new_sid}", "snapshot_text": r.get("quote") or s["text"],
                                     "snapshot_json": {"ref": f"S:{new_sid}", "kind": "S", "doc_id": s["paper"],
                                                       "page": s["page"], "sid": new_sid, "text": s["text"],
                                                       "legacy": {"hl_id": r["id"], "sid": r.get("sid"),
                                                                  "start": r.get("start"), "end": r.get("end")}},
                                     "snapshot_build": None}])
            report["imported"] += 1
    finally:
        us.close()
        ev.close()
        sdb.close()
        hl.close()
    return report
