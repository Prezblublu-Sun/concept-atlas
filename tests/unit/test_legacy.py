"""atlas.tools.legacy: the v3 → curated export on a synthetic terms_v3-shaped database (every file and key of
increment 2 §1, cross-group recovery from the spec / by domain, unmapped labels, unknown domains, --force,
confirmations) and the highlights import on synthetic highlights.sqlite + sentences.db files."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from atlas.config import init_project
from atlas.core.ids import extraction_id, sentence_id
from atlas.core.schemas import DocMeta, PageText, SentenceRow, Unit
from atlas.pipeline import gates
from atlas.store.curated import CURATED_FILES, load_curated, read_confirmations
from atlas.store.evidence import EvidenceStore
from atlas.store.user import UserStore
from atlas.tools import legacy

ITEM_COLS = ("id", "label", "zh", "type", "origin", "mode", "domains", "place", "forms", "gen_forms", "case_forms", "rx",
             "probe", "readable", "shared_with", "n_papers", "n_occ", "precision")


def _v3_db(path: Path, cross_anchors, extra_item_domain: str | None = None) -> None:
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE item (id TEXT PRIMARY KEY, label TEXT, zh TEXT, type TEXT, origin TEXT, mode TEXT, domains TEXT, place TEXT,
            forms TEXT, gen_forms TEXT, case_forms TEXT, rx TEXT, probe TEXT, readable TEXT, shared_with TEXT, n_papers INT, n_occ INT, precision REAL);
        CREATE TABLE entry (id INT PRIMARY KEY, domain TEXT, dom_order INT, section TEXT, sec_order INT, sub TEXT, sub_order INT,
            term TEXT, zh TEXT, type TEXT, variants TEXT, df INT, v1_df INT, share REAL, sec_lift REAL, dom_lift REAL, seed INT,
            cross_domain INT, note TEXT, example TEXT, ex_paper TEXT, item_id TEXT);
        CREATE TABLE category (domain TEXT, id TEXT, ord INT, name TEXT, desc TEXT);
        CREATE TABLE concept_category (domain TEXT, item_id TEXT, primary_cat TEXT, secondary_cat TEXT, reason TEXT, source TEXT);
        CREATE TABLE subdomain (id TEXT, ord INT, kind TEXT, domain TEXT, name TEXT, desc TEXT, anchors TEXT, papers INT, core INT,
            base INT, years TEXT, overlaps TEXT, reviewed INT, review_ok INT, review_bad INT, review_precision REAL, review_notes TEXT);
        CREATE TABLE paper (id TEXT PRIMARY KEY, title TEXT, authors TEXT, year INT, venue TEXT, doc_type TEXT, domains TEXT);
        CREATE TABLE i18n (zh TEXT, en TEXT, grp TEXT, "by" TEXT);
    """)
    items = [
        ("C_stem", "stem", "柄", "名词术语", "概念（已合并）", "词形", '["骨科"]', '["title"]', '["stem", "stems"]', '["stemses"]',
         "[]", None, "", "", "[]", 1, 1, None),
        ("C_finiteelement", "finite element (FE)", "有限元", "方法或模型名", "概念（已合并）", "词形", '["有限元"]', "[]",
         '["finite element"]', "[]", '["FE"]', None, "", "", '["stem"]', 1, 1, None),
        ("C_kriging", "Kriging", "克里金", "方法或模型名", "概念（已合并）", "词形", '["代理模型"]', "[]", '["kriging"]', "[]", "[]",
         None, "", "", "[]", 1, 1, None),
        ("C_stressstrainfield", "stress/strain field", "应力场", "量与指标", "概念（已合并）", "字面", '["有限元"]', "[]", "[]", "[]",
         "[]", r"(?<![^ ¶])(?:stress|strain) ?field(?:s|es)?(?![^ ¶])", "field", "stress field", "[]", 1, 1, None),
        ("C_phrase", "in the present study", "", "通用句式", "通用部分用语", "词形", json.dumps([extra_item_domain or "通用写作句式"]),
         "[]", '["in the present study"]', "[]", "[]", None, "", "", "[]", 1, 1, None),
    ]
    con.executemany(f"INSERT INTO item VALUES ({','.join('?' * len(ITEM_COLS))})", items)
    con.executemany("INSERT INTO entry(id, domain, dom_order, section, sec_order, sub, sub_order, term, zh, type, note, example, ex_paper, item_id) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", [
                        (1, "骨科", 0, "title", 1, "对象", 0, "stem", "柄", "名词术语", "n", "the stem", "P1", "C_stem"),
                        (2, "有限元", 1, "methods", 4, "方法", 0, "finite element", "有限元", "方法或模型名", "", "", "", "C_finiteelement"),
                        (3, "通用写作句式", 4, "introduction", 3, "句式", 0, "in the present study", "", "通用句式", "", "", "", "C_phrase")])
    con.executemany("INSERT INTO category VALUES (?,?,?,?,?)", [("骨科", "O1", 0, "对象", "植入物"), ("有限元", "F1", 0, "建模", None)])
    con.executemany("INSERT INTO concept_category VALUES (?,?,?,?,?,?)",
                    [("骨科", "C_stem", "O1", None, "r", "llm"), ("有限元", "C_finiteelement", "F1", "O1", "", "llm")])
    con.executemany("INSERT INTO subdomain(id, ord, kind, domain, name, desc, anchors, review_notes) VALUES (?,?,?,?,?,?,?,?)", [
        ("ORT-S", 0, "领域内", "骨科", "柄", "股骨柄", '["C_stem", "C_missing"]', "ok"),
        ("X-FS", 1, "交叉", "交叉", "有限元×柄", "", json.dumps(cross_anchors), None)])
    con.executemany("INSERT INTO paper(id, domains) VALUES (?,?)", [("P1", "骨科、有限元"), ("P2", ""), ("P3", "骨科、未知域")])
    con.executemany("INSERT INTO i18n VALUES (?,?,?,?)", [("柄", "stem", "framework", "x"), ("对象", "Objects", "framework", "x"),
                                                         ("植入物", "implants", "description", "x")])
    con.commit()
    con.close()


def _spec(path: Path, groups) -> Path:
    path.write_text(json.dumps({"_说明": "x", "within": {"骨科": [{"id": "ORT-S", "name": "柄", "desc": "", "anchors": ["stem"]}]},
                                "cross": [{"id": "X-FS", "name": "有限元×柄", "desc": "", "groups": groups}]}, ensure_ascii=False),
                    encoding="utf-8")
    return path


def test_export_writes_every_curated_file_with_the_contract_keys(tmp_path: Path):
    db = tmp_path / "terms_v3.db"
    _v3_db(db, [["C_finiteelement"], ["C_stem"]])
    proj = init_project(tmp_path / "proj")
    r = legacy.export_curated(db, proj)
    assert set(r["files"]) == set(CURATED_FILES) and r["spec"] is None
    assert r["concepts"] == 5 and r["term_entries"] == 3 and r["categories"] == 2 and r["concept_categories"] == 2
    assert r["subdomains"] == 2 and r["docs"] == 3 and r["docs_with_domain"] == 2 and r["i18n"] == 3 + 5   # + domain names
    cur = load_curated(proj.curated_dir)
    assert set(cur) == set(CURATED_FILES)
    doms = {d["id"]: d for d in cur["domains"]["domains"]}
    assert doms["DOM_ortho"] == {"id": "DOM_ortho", "zh": "骨科", "en": "Orthopaedics", "order": 0, "scope": True}
    assert doms["DOM_writing"]["scope"] is False and len(doms) == 5
    c = {x["id"]: x for x in cur["concepts"]["concepts"]}
    assert set(c["C_stem"]) == {"id", "label", "zh", "type", "mode", "forms", "gen_forms", "case_forms", "rx", "probe",
                                "readable", "domains", "place", "shared_with", "origin", "merged_into", "source"}
    assert c["C_stem"]["domains"] == ["DOM_ortho"] and c["C_stem"]["source"] == "legacy-v3" and c["C_stem"]["merged_into"] is None
    assert c["C_finiteelement"]["shared_with"] == ["C_stem"] and c["C_finiteelement"]["case_forms"] == ["FE"]
    assert c["C_stressstrainfield"]["mode"] == "literal" and c["C_stressstrainfield"]["probe"] == "field"
    assert c["C_phrase"]["mode"] == "forms" and c["C_phrase"]["domains"] == ["DOM_writing"]
    p = cur["patterns"]
    assert {"bad_next", "bad_prev", "short_caps_max_letters", "stem_caps_rule", "token_regex", "version"} <= set(p)
    assert p["short_caps_max_letters"] == 3 and p["version"] == "v2"
    tree = {d["id"]: d for d in cur["term_tree"]["domains"]}
    t = tree["DOM_ortho"]["sections"][0]
    assert t["section"] == "title" and t["subs"][0]["name"] == "对象"
    assert t["subs"][0]["terms"][0] == {"term": "stem", "zh": "柄", "type": "名词术语", "concept_id": "C_stem", "note": "n",
                                        "example": "the stem", "example_doc": "P1"}
    assert "DOM_writing" in tree
    cats = {k["id"]: k for k in cur["categories"]["categories"]}
    assert cats["O1"] == {"id": "O1", "domain": "DOM_ortho", "name_zh": "对象", "name_en": "Objects", "desc_zh": "植入物",
                          "desc_en": "implants", "order": 0}
    assert cats["F1"]["desc_zh"] == "" and cats["F1"]["name_en"] == ""
    rows = {x["concept_id"]: x for x in cur["concept_categories"]["rows"]}
    assert rows["C_finiteelement"]["primary"] == "F1" and rows["C_finiteelement"]["secondary"] == "O1"
    assert rows["C_stem"]["secondary"] is None and rows["C_stem"]["domain"] == "DOM_ortho" and rows["C_stem"]["source"] == "legacy-v3"
    subs = {s["id"]: s for s in cur["subdomains"]["subdomains"]}
    assert subs["ORT-S"]["kind"] == "within" and subs["ORT-S"]["domain"] == "DOM_ortho" and subs["ORT-S"]["anchors"] == ["C_stem", "C_missing"]
    assert subs["ORT-S"]["name_en"] == "stem" and subs["ORT-S"]["notes"] == "ok" and "groups" not in subs["ORT-S"]
    assert subs["X-FS"]["kind"] == "cross" and subs["X-FS"]["domain"] is None and subs["X-FS"]["groups"] == [["C_finiteelement"], ["C_stem"]]
    assert "groups_recovered" not in subs["X-FS"] and r["groups_recovered"] == []
    assert cur["doc_domains"]["docs"] == {"P1": ["DOM_ortho", "DOM_fe"], "P2": [], "P3": ["DOM_ortho"]}
    assert r["unknown_domains"] == ["未知域"] and r["unknown_domain_items"] == []
    assert any(w.startswith("sub-domain ORT-S: anchors not in concepts") for w in r["warnings"])
    strings = {s["zh"]: s for s in cur["i18n"]["strings"]}
    assert strings["柄"] == {"zh": "柄", "en": "stem", "grp": "framework"} and strings["骨科"]["en"] == "Orthopaedics"
    # confirmations: every file confirmed with its current sha
    conf = read_confirmations(proj.curated_dir)
    assert set(conf) == set(CURATED_FILES) and set(r["confirmed"]) == set(CURATED_FILES)
    assert all(row["state"] == "confirmed" for row in gates.status(proj))
    # refuses to overwrite without force; --force rewrites and re-confirms
    with pytest.raises(FileExistsError, match="--force"):
        legacy.export_curated(db, proj)
    (proj.curated_dir / "concepts.yaml").write_text("concepts: []\n", encoding="utf-8")
    assert [row["state"] for row in gates.status(proj) if row["name"] == "concepts"] == ["changed"]
    r2 = legacy.export_curated(db, proj, force=True)
    assert r2["concepts"] == 5 and all(row["state"] == "confirmed" for row in gates.status(proj))
    out = legacy.format_export_report(r)
    assert "concepts 5" in out and "unknown domain names: ['未知域']" in out and "sub-domain spec: (none)" in out


def test_cross_groups_recovered_from_the_spec_or_by_domain(tmp_path: Path):
    db = tmp_path / "terms_v3.db"
    _v3_db(db, ["C_finiteelement", "C_stem", "C_kriging"])            # flat anchor list, as before the v2 grouping
    spec = _spec(tmp_path / "subdomain_spec.json", [["finite element (FE)", "no such label"], ["stem", "Kriging"]])
    proj = init_project(tmp_path / "p1")
    r = legacy.export_curated(db, proj, spec=spec)
    s = {x["id"]: x for x in load_curated(proj.curated_dir)["subdomains"]["subdomains"]}["X-FS"]
    assert s["groups"] == [["C_finiteelement"], ["C_stem", "C_kriging"]] and s["groups_recovered"] is True
    assert r["groups_recovered"] == [{"sub": "X-FS", "from": "spec"}] and r["spec"] == str(spec)
    assert r["unknown_anchor_labels"] == [{"sub": "X-FS", "label": "no such label"}]
    assert "X-FS: no such label" in legacy.format_export_report(r) and "X-FS (from spec)" in legacy.format_export_report(r)
    # no spec: split by the domain of the anchor concepts (fe | ortho | surrogate → three groups, domain order)
    proj2 = init_project(tmp_path / "p2")
    r = legacy.export_curated(db, proj2)
    s = {x["id"]: x for x in load_curated(proj2.curated_dir)["subdomains"]["subdomains"]}["X-FS"]
    assert s["groups"] == [["C_finiteelement"], ["C_stem"], ["C_kriging"]] and s["groups_recovered"] is True
    assert r["groups_recovered"] == [{"sub": "X-FS", "from": "domain"}]
    # a missing explicit spec is reported, the default search finds `_work/subdomain_spec.json` next to the db
    proj3 = init_project(tmp_path / "p3")
    r = legacy.export_curated(db, proj3, spec=tmp_path / "nope.json")
    assert r["spec"] is None and any("spec not found" in w for w in r["warnings"])
    (tmp_path / "_work").mkdir()
    _spec(tmp_path / "_work" / "subdomain_spec.json", [["finite element (FE)"], ["stem", "Kriging"]])
    assert legacy.find_spec(db) == tmp_path / "_work" / "subdomain_spec.json"


def test_spec_mismatch_is_reported_and_the_v3_groups_kept(tmp_path: Path):
    db = tmp_path / "terms_v3.db"
    _v3_db(db, [["C_finiteelement"], ["C_stem"]], extra_item_domain="奇怪领域")
    spec = _spec(tmp_path / "spec.json", [["finite element (FE)"], ["stem", "Kriging"]])
    proj = init_project(tmp_path / "proj")
    r = legacy.export_curated(db, proj, spec=spec)
    s = {x["id"]: x for x in load_curated(proj.curated_dir)["subdomains"]["subdomains"]}["X-FS"]
    assert s["groups"] == [["C_finiteelement"], ["C_stem"]] and "groups_recovered" not in s
    assert r["spec_mismatch"] == [{"sub": "X-FS", "v3": [["C_finiteelement"], ["C_stem"]], "spec": [["C_finiteelement"], ["C_stem", "C_kriging"]]}]
    assert "groups of X-FS differ" in legacy.format_export_report(r)
    # an item whose domain is not in the map is listed (and its domains list stays empty)
    assert r["unknown_domain_items"] == [{"id": "C_phrase", "domains": ["奇怪领域"]}] and "奇怪领域" in r["unknown_domains"]
    assert {c["id"]: c for c in load_curated(proj.curated_dir)["concepts"]["concepts"]}["C_phrase"]["domains"] == []


# ---- highlights --------------------------------------------------------------------------------------------------

SHA = "b" * 64
SENTS = {"P1": ["Micromotion of the stem was measured.", "The collar reduced the micromotion at the calcar."],
         "P2": ["Finite element analysis of the stem."]}


def _evidence(path: Path) -> None:
    ev = EvidenceStore.open(path)
    for doc_id, sents in SENTS.items():
        ev.add_document(doc_id, "paper", "manifest", path.parent / f"{doc_id}.pdf", f"{doc_id}.pdf", SHA, DocMeta(title=doc_id))
        ext = extraction_id(doc_id, SHA, "t")
        text = " ".join(sents)
        rows, pos = [], 0
        for i, s in enumerate(sents):
            cs = text.index(s, pos)
            rows.append(SentenceRow(sid=sentence_id(doc_id, s), doc_id=doc_id, text=s, ext_id=ext, seq=i, unit_id=doc_id,
                                    page_no=1, cstart=cs, cend=cs + len(s), section="results", is_body=True))
            pos = cs + len(s)
        ev.add_extraction(ext_id=ext, doc_id=doc_id, pdf_sha256=SHA, extractor_version="t", doc_type="paper",
                          pages=[PageText(1, text)], masks=[], units=[Unit(doc_id, doc_id, "document", 0, None, 1, 1)],
                          sentences=rows, text_ratio=0.9, drop_ratio=0.0)
    ev.close()


def test_import_highlights(tmp_path: Path):
    proj = init_project(tmp_path / "proj")
    _evidence(proj.evidence_db)
    sdb = tmp_path / "sentences.db"
    con = sqlite3.connect(sdb)
    con.execute("CREATE TABLE sent (sid INTEGER PRIMARY KEY, paper TEXT, page INT, idx INT, section TEXT, text TEXT)")
    con.executemany("INSERT INTO sent VALUES (?,?,?,?,?,?)", [
        (1, "P1", 1, 1, "results", "Micromotion of the stem was measured."),          # exact
        (2, "P1", 1, 2, "results", "The collar reduced the micro- motion at the calcar."),   # differs → unmatched
        (3, "P2", 1, 1, "methods", "Finite element analysis of the stem"),             # edge punctuation only → exact after norm
        (4, "P9", 1, 1, "methods", "Unknown document.")])
    con.commit()
    con.close()
    hdb = tmp_path / "highlights.sqlite"
    con = sqlite3.connect(hdb)
    con.execute("CREATE TABLE hl (id INTEGER PRIMARY KEY, key TEXT, kind TEXT, item_id TEXT, lang TEXT, sid INT, paper TEXT, "
                "start INT, end INT, quote TEXT, color TEXT, note TEXT, created REAL, updated REAL)")
    con.executemany("INSERT INTO hl VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", [
        (1, "k1", "sent", "C_micromotion", "en", 1, "P1", 0, 11, "Micromotion", "yellow", "key sentence", 1.0, 1.0),
        (2, "k2", "sent", "C_micromotion", "en", 2, "P1", 4, 10, "collar", "green", "", 1.0, 1.0),
        (3, "k3", "sent", "C_stem", "en", 3, "P2", 0, 6, "Finite", None, "fe", 1.0, 1.0),
        (4, "k4", "sent", "C_x", "en", 4, "P9", 0, 3, "Unk", "red", "", 1.0, 1.0),
        (5, "k5", "sent", "C_x", "en", 99, "P1", 0, 3, "gone", "red", "", 1.0, 1.0),
        (6, "k6", "stmt", "C_femur", "zh", None, "", 0, 13, "股骨是人体最长的骨", "yellow", "", 1.0, 1.0)])
    con.commit()
    con.close()
    r = legacy.import_highlights(hdb, sdb, proj)
    assert r["imported"] == 2 and r["skipped_statements"] == 1 and r["methods"] == {"exact": 2, "unmatched": 2}
    assert {u["id"]: u["reason"] for u in r["unmatched"]} == {2: "unmatched", 4: "unmatched", 5: "sid not in sentences.db"}
    assert {u["id"]: u.get("paper") for u in r["unmatched"]}[4] == "P9"
    us = UserStore.open(proj.user_db, "ro")
    try:
        sid1 = sentence_id("P1", SENTS["P1"][0])
        notes = us.notes_for_ref(f"S:{sid1}")
        assert len(notes) == 1 and notes[0]["title"] == "Highlight" and notes[0]["body_md"] == "key sentence"
        assert sorted(notes[0]["tags"]) == ["highlight", "yellow"]
        tg = notes[0]["targets"][0]
        assert tg["ref"] == f"S:{sid1}" and tg["snapshot_text"] == "Micromotion"
        assert tg["snapshot_json"]["legacy"] == {"hl_id": 1, "sid": 1, "start": 0, "end": 11}
        sid3 = sentence_id("P2", SENTS["P2"][0])
        n3 = us.notes_for_ref(f"S:{sid3}")
        assert len(n3) == 1 and sorted(n3[0]["tags"]) == ["highlight", "yellow"]        # colour defaults to yellow
        assert us.list_notes()["total"] == 2
    finally:
        us.close()
