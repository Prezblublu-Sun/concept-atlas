"""HTTP adapter + AtlasService end-to-end: a hand-built evidence/user DB (2 documents × 2 pages, a few sentences)
plus one real PDF generated with PyMuPDF, served by atlas.http.server on a free port; every route exercised with
urllib / http.client."""
from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import fitz  # PyMuPDF
import pytest

from atlas.config import init_project
from atlas.core.ids import extraction_id, sentence_id
from atlas.core.schemas import DocMeta, LineMask, PageText, SentenceRow, Unit
from atlas.http import server as http_server
from atlas.ingest import pdf as pdf_io
from atlas.service.api import AtlasService
from atlas.store.evidence import EvidenceStore
from atlas.store.user import UserStore

SHA1 = "1" * 64
SHA2 = "2" * 64

P1_PAGES = {
    1: "Stem loading study\nThe femoral stem transfers load to the calcar. Bone remodelling is governed by strain.",
    2: "Stress shielding reduces periprosthetic bone density. Shorter stems load the metaphysis more.\n"
       "[1] Smith J. A reference entry. J Biomech. 2019;1:1-2.",
}
P1_SENTS = {
    1: [("The femoral stem transfers load to the calcar.", "introduction", True, ("mech",)),
        ("Bone remodelling is governed by strain.", "introduction", True, ("mech",))],
    2: [("Stress shielding reduces periprosthetic bone density.", "results", True, ("result",)),
        ("Shorter stems load the metaphysis more.", "results", True, ("cmp",)),
        ("[1] Smith J. A reference entry. J Biomech. 2019;1:1-2.", "references", False, ())],
}
P2_PAGES = {
    1: "Porous coatings\nA porous coating increases osseointegration. The threshold was 150 μm.",
    2: "Micromotion above the threshold leads to fibrous tissue. The femoral stem design matters.",
}
P2_SENTS = {
    1: [("A porous coating increases osseointegration.", "abstract", True, ("mech",)),
        ("The threshold was 150 μm.", "abstract", True, ("quant",))],
    2: [("Micromotion above the threshold leads to fibrous tissue.", "discussion", True, ("mech",)),
        ("The femoral stem design matters.", "discussion", True, ())],
}


def _rows(doc_id: str, ext_id: str, pages: dict[int, str], sents: dict) -> list[SentenceRow]:
    out, seq = [], 0
    for page_no, text in pages.items():
        for s, section, body, tags in sents[page_no]:
            cs = text.index(s)
            out.append(SentenceRow(sid=sentence_id(doc_id, s), doc_id=doc_id, text=s, ext_id=ext_id, seq=seq,
                                   unit_id=doc_id, page_no=page_no, cstart=cs, cend=cs + len(s), section=section,
                                   is_body=body, tags=tags, flags=()))
            seq += 1
    return out


def _write_pdf(path: Path, pages: dict[int, str]) -> None:
    doc = fitz.open()
    for _, text in sorted(pages.items()):
        page = doc.new_page(width=595, height=842)
        y = 72
        for line in text.split("\n"):
            page.insert_text((60, y), line, fontsize=9, fontname="helv")
            y += 16
    doc.save(str(path))
    doc.close()


def _fallback_render(path, page_no, zoom=1.6, highlight=None, spans=None):
    """Used only while atlas.ingest.pdf.render_page is still the ingest agent's stub."""
    doc = fitz.open(str(path))
    pg = doc[int(page_no) - 1]
    rects = pg.search_for(highlight) if highlight else []
    if spans and not rects:
        text = pg.get_text("text").replace("\r", "").replace("\x00", "")
        for a, b in spans:
            rects += pg.search_for(text[a:b].strip())
    for r in rects:
        pg.add_highlight_annot(r)
    pix = pg.get_pixmap(matrix=fitz.Matrix(zoom, zoom), annots=True)
    info = {"page": int(page_no), "pages": doc.page_count, "hits": len(rects),
            "hit_y": (min(r.y0 for r in rects) / pg.rect.height) if rects else None}
    png = pix.tobytes("png")
    doc.close()
    return png, info


@pytest.fixture(scope="module")
def served(tmp_path_factory):
    root = tmp_path_factory.mktemp("atlas proj 测试")
    project = init_project(root / "proj", name="HTTP test project")
    project.pdf_dir.mkdir(parents=True, exist_ok=True)
    _write_pdf(project.pdf_dir / "p001.pdf", P1_PAGES)

    ev = EvidenceStore.open(project.evidence_db)
    ev.add_document("P001", "paper", "manifest", project.pdf_dir / "p001.pdf", "p001.pdf", SHA1,
                    DocMeta(title="Stem loading study", year=2021, authors="Smith J; Li W", doi="10.1000/stem.1",
                            venue="J Biomech", keywords="stem; calcar"))
    ev.add_document("P002", "paper", "manifest", project.pdf_dir / "missing.pdf", "missing.pdf", SHA2,
                    DocMeta(title="Porous coatings and micromotion", year=2019, authors="Wang Q", venue="Bone"))
    for doc_id, sha, pages, sents in (("P001", SHA1, P1_PAGES, P1_SENTS), ("P002", SHA2, P2_PAGES, P2_SENTS)):
        ext = extraction_id(doc_id, sha, "test")
        pt = [PageText(n, t, 595.0, 842.0) for n, t in sorted(pages.items())]
        units = [Unit(doc_id, doc_id, "document", 0, None, 1, 2)]
        if doc_id == "P002":    # thesis-shaped chapter units (what docs/{id} and resolve U: carry for theses)
            units += [Unit("P002/porous-coatings", "P002", "chapter", 1, "Porous coatings", 1, 1, 0.9, ("toc_matched",), "P002", "intro"),
                      Unit("P002/micromotion", "P002", "chapter", 2, "Micromotion", 2, 2, 0.6, ("no_toc",), "P002", "results")]
        ev.add_extraction(ext_id=ext, doc_id=doc_id, pdf_sha256=sha, extractor_version="test", doc_type="paper",
                          pages=pt, masks=[LineMask(1, 0, pages[1].index("\n"), "header_footer")], units=units,
                          sentences=_rows(doc_id, ext, pages, sents), text_ratio=0.9, drop_ratio=0.05)
    ev.close()
    UserStore.open(project.user_db).close()

    mp = pytest.MonkeyPatch()
    try:
        pdf_io.render_page(str(project.pdf_dir / "p001.pdf"), 1)
    except NotImplementedError:
        mp.setattr(pdf_io, "render_page", _fallback_render)

    service = AtlasService(project)
    srv, thread = http_server.run_in_thread(service, "127.0.0.1", 0)
    base = "http://127.0.0.1:%d" % srv.server_address[1]
    yield {"base": base, "project": project, "service": service, "server": srv}
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=5)
    service.shutdown()
    mp.undo()


def _req(base: str, method: str, path: str, body=None, raw: bool = False):
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            payload = r.read()
            status, hdrs = r.status, dict(r.headers)
    except urllib.error.HTTPError as e:
        payload = e.read()
        status, hdrs = e.code, dict(e.headers)
    if raw:
        return status, hdrs, payload
    try:
        return status, hdrs, json.loads(payload.decode("utf-8"))
    except ValueError:
        return status, hdrs, payload


@pytest.fixture
def api(served):
    base = served["base"]

    def call(method, path, body=None, raw=False):
        return _req(base, method, "/api/v1/" + path.lstrip("/"), body, raw)
    call.base = base
    call.page = lambda path: _req(base, "GET", path, raw=True)
    return call


def _sid(doc_id: str, text: str) -> str:
    return sentence_id(doc_id, text)


# ---- meta -------------------------------------------------------------------------------------------------
def test_info_and_headers(api):
    st, hd, body = api("GET", "info")
    assert st == 200
    assert body["project"] == "HTTP test project"
    assert body["build"] == "evidence"
    assert body["stats"]["documents"] == 2 and body["stats"]["sentences"] == 9
    assert body["version"]
    assert hd["X-Atlas-Build"] == "evidence"
    assert hd["Cache-Control"] == "no-cache"
    assert hd["Content-Type"].startswith("application/json")


def test_i18n(api):
    st, _, body = api("GET", "i18n")
    assert st == 200
    assert body["ui"]["nav.docs"] == {"zh": "文献", "en": "Documents"}
    assert body["sections"]["abstract"] == {"zh": "摘要", "en": "Abstract"}
    assert body["tags"]["quant"]["en"] == "Quantitative" and body["tags"]["quant"]["zh"]
    assert body["kinds"]["S"]["en"] == "Sentence"


# ---- documents and reading --------------------------------------------------------------------------------
def test_docs_list_and_filter(api):
    st, _, body = api("GET", "docs")
    assert st == 200 and body["total"] == 2
    ids = [r["doc_id"] for r in body["rows"]]
    assert ids == ["P001", "P002"]  # year desc
    assert all("notes_count" in r for r in body["rows"])
    st, _, body = api("GET", "docs?q=porous")
    assert st == 200 and [r["doc_id"] for r in body["rows"]] == ["P002"]
    st, _, body = api("GET", "docs?q=P00&limit=1&offset=1")
    assert st == 200 and body["total"] == 2 and len(body["rows"]) == 1
    st, _, body = api("GET", "docs?limit=abc")
    assert st == 400 and "limit" in body["error"]


def test_get_doc(api):
    st, _, d = api("GET", "docs/P001")
    assert st == 200
    assert d["title"] == "Stem loading study" and d["doi"] == "10.1000/stem.1"
    assert [p["page_no"] for p in d["pages"]] == [1, 2]
    assert [p["n_sentences"] for p in d["pages"]] == [2, 3]
    assert d["notes_count"] == 0 and d["has_pdf"] is True
    assert [u["kind"] for u in d["units"]] == ["document"]
    st, _, d = api("GET", "docs/P002")
    assert st == 200 and d["has_pdf"] is False
    # chapter units carry what the reader's chapter list shows: heading, pages, unit_kind, confidence, reasons
    assert [(u["unit_id"], u["kind"], u["ordinal"]) for u in d["units"]] == \
        [("P002", "document", 0), ("P002/porous-coatings", "chapter", 1), ("P002/micromotion", "chapter", 2)]
    ch = d["units"][1]
    assert ch["heading"] == "Porous coatings" and ch["page_from"] == 1 and ch["page_to"] == 1 and ch["unit_kind"] == "intro"
    assert ch["confidence"] == 0.9 and ch["reasons"] == ["toc_matched"] and ch["parent_unit"] == "P002"
    assert d["units"][2]["confidence"] == 0.6 and d["units"][2]["unit_kind"] == "results"
    st, _, body = api("GET", "docs/NOPE")
    assert st == 404 and "NOPE" in body["error"]


def test_page_json(api):
    st, _, p = api("GET", "docs/P001/pages/2")
    assert st == 200
    assert p["page_no"] == 2 and p["n_pages"] == 2 and p["title"] == "Stem loading study"
    assert p["text"] == P1_PAGES[2]
    sents = p["sentences"]
    assert len(sents) == 3
    assert sents[0]["text"] == "Stress shielding reduces periprosthetic bone density."
    assert sents[0]["tags"] == ["result"] and sents[0]["is_body"] is True and sents[0]["notes"] == 0
    assert sents[2]["section"] == "references" and sents[2]["is_body"] is False
    assert api("GET", "docs/P001/pages/9")[0] == 404
    assert api("GET", "docs/P001/pages/abc")[0] == 400
    assert api("GET", "docs/NOPE/pages/1")[0] == 404


def test_page_png(api):
    st, hd, png = api("GET", "docs/P001/pages/1.png?hl=femoral%20stem", raw=True)
    assert st == 200, png[:200]
    assert hd["Content-Type"] == "image/png" and png[:8] == b"\x89PNG\r\n\x1a\n"
    assert hd["Cache-Control"] == "max-age=600"
    assert hd["X-Atlas-Build"] == "evidence"
    assert hd["X-Page"] == "1" and hd["X-Pages"] == "2"
    assert int(hd["X-Hits"]) >= 1
    assert 0.0 < float(hd["X-Hit-Y"]) < 1.0
    assert "X-Hit-Y" in hd["Access-Control-Expose-Headers"]
    # no highlight → no hits, empty X-Hit-Y
    st, hd, _ = api("GET", "docs/P001/pages/2.png", raw=True)
    assert st == 200 and hd["X-Hits"] == "0" and hd["X-Hit-Y"] == ""
    # spans + zoom
    s = P1_PAGES[1].index("Bone remodelling")
    st, hd, small = api("GET", f"docs/P001/pages/1.png?spans={s}-{s + 39}&zoom=0.5", raw=True)
    assert st == 200 and len(small) < len(png)
    assert api("GET", "docs/P001/pages/1.png?zoom=x")[0] == 400
    assert api("GET", "docs/P001/pages/1.png?spans=a-b")[0] == 400
    assert api("GET", "docs/P001/pages/7.png")[0] == 404
    assert api("GET", "docs/P002/pages/1.png")[0] == 404   # no PDF file
    assert api("GET", "docs/NOPE/pages/1.png")[0] == 404


def test_pdf_route(api, served):
    st, hd, data = api("GET", "pdf/P001", raw=True)
    assert st == 200 and hd["Content-Type"] == "application/pdf" and data[:5] == b"%PDF-"
    assert hd["Content-Disposition"].startswith("inline;") and "P001.pdf" in hd["Content-Disposition"]
    assert hd["Cache-Control"] == "max-age=600"
    assert len(data) == (served["project"].pdf_dir / "p001.pdf").stat().st_size
    st, hd, _ = api("GET", "pdf/P001?download=1", raw=True)
    assert st == 200 and hd["Content-Disposition"].startswith("attachment;")
    assert api("GET", "pdf/P002")[0] == 404
    assert api("GET", "pdf/NOPE")[0] == 404


def test_search(api):
    st, _, res = api("GET", "search?phrase=femoral%20stem")
    assert st == 200
    assert res["total"] == 2 and res["documents"] == 2 and res["phrase"] == "femoral stem"
    for r in res["rows"]:
        assert r["ref"] == "S:" + r["sid"] and r["kind"] == "S" and r["status"] == "live"
        assert "<mark>" in r["snippet"] and r["title"] and r["page"] in (1, 2)
    st, _, res = api("GET", "docs/P001/search?phrase=femoral%20stem")
    assert st == 200 and res["total"] == 1 and res["rows"][0]["doc_id"] == "P001" and res["doc_id"] == "P001"
    st, _, res = api("GET", "search?phrase=no%20such%20phrase%20here")
    assert st == 200 and res["total"] == 0 and res["rows"] == []
    st, _, res = api("GET", "search?phrase=")
    assert st == 200 and res["total"] == 0
    st, _, body = api("GET", "search")
    assert st == 400 and "phrase" in body["error"]
    st, _, res = api("GET", "search?phrase=stem&limit=1&offset=1")
    assert st == 200 and len(res["rows"]) == 1 and res["total"] >= 2


def test_sentence(api):
    sid = _sid("P001", "Bone remodelling is governed by strain.")
    st, _, s = api("GET", f"sentences/{sid}")
    assert st == 200
    assert s["ref"] == f"S:{sid}" and s["doc_id"] == "P001" and s["page"] == 1 and s["title"] == "Stem loading study"
    assert s["text"] == "Bone remodelling is governed by strain." and s["status"] == "live"
    assert s["before"] == ["The femoral stem transfers load to the calcar."]
    assert s["after"] == ["Stress shielding reduces periprosthetic bone density."]
    assert s["notes"] == [] and s["tags"] == ["mech"] and s["section"] == "introduction"
    st, _, s0 = api("GET", f"sentences/{sid}?context=0")
    assert st == 200 and s0["before"] == [] and s0["after"] == []
    assert api("GET", "sentences/S1deadbeefdeadbeef")[0] == 404


def test_resolve(api):
    sid = _sid("P002", "The threshold was 150 μm.")
    st, _, r = api("GET", "resolve?ref=S:" + sid)
    assert st == 200 and r["status"] == "live" and r["doc_id"] == "P002" and r["text"] == "The threshold was 150 μm."
    assert r["page"] == 1 and r["kind"] == "S"
    st, _, r = api("GET", "resolve?ref=D:P001")
    assert st == 200 and r["status"] == "live" and r["title"] == "Stem loading study" and r["kind"] == "D"
    assert r["text"] == "Stem loading study" and r["doc_id"] == "P001"
    st, _, r = api("GET", "resolve?ref=U:P001/methods")
    assert st == 200 and r["status"] == "live" and r["unit_id"] == "P001/methods"
    st, _, r = api("GET", "resolve?ref=U:P002/micromotion")     # a chapter unit resolves with its heading and page
    assert st == 200 and r["status"] == "live" and r["unit_id"] == "P002/micromotion" and r["doc_id"] == "P002"
    assert r["text"] == "Micromotion" and r["heading"] == "Micromotion" and r["page"] == 2 and r["unit_kind"] == "results"
    st, _, r = api("GET", "resolve?ref=U:NOPE/x")
    assert st == 200 and r["status"] == "orphan"
    st, _, r = api("GET", "resolve?ref=S:S1nope")
    assert st == 200 and r["status"] == "orphan"
    st, _, r = api("GET", "resolve?ref=N:N_nope")
    assert st == 200 and r["status"] == "orphan"
    st, _, r = api("GET", "resolve?ref=C:some-concept")      # increment 2: C: resolves through a build; none here
    assert st == 200 and r["status"] == "orphan" and "no_build" in r["flags"]
    st, _, r = api("GET", "resolve?ref=A:some-answer")
    assert st == 200 and r["status"] == "orphan" and "unsupported_kind" in r["flags"]
    st, _, body = api("GET", "resolve?ref=garbage")
    assert st == 400 and "ref" in body["error"]
    assert api("GET", "resolve")[0] == 400


# ---- notes ------------------------------------------------------------------------------------------------
def test_notes_crud_conflict_and_lookup(api):
    sid = _sid("P001", "The femoral stem transfers load to the calcar.")
    st, hd, note = api("POST", "notes", {"title": "Calcar", "body_md": "Load path [[D:P002]]", "tags": ["Mech", "load"],
                                          "targets": [f"S:{sid}", "D:P001"]})
    assert st == 201, note
    nid = note["note_id"]
    assert nid.startswith("N_") and note["rev"] == 1 and note["tags"] == ["mech", "load"]
    assert note["links"] == ["D:P002"]
    tg = {t["ref"]: t for t in note["targets"]}
    assert tg[f"S:{sid}"]["snapshot_text"] == "The femoral stem transfers load to the calcar."
    assert tg[f"S:{sid}"]["snapshot_json"]["page"] == 1 and tg[f"S:{sid}"]["status"] == "live"
    assert tg["D:P001"]["snapshot_text"] == "Stem loading study"

    st, _, got = api("GET", f"notes/{nid}")
    assert st == 200 and got["body_md"] == "Load path [[D:P002]]"

    # page/doc now report the note
    st, _, p = api("GET", "docs/P001/pages/1")
    assert [s["notes"] for s in p["sentences"]] == [1, 0]
    assert api("GET", "docs/P001")[2]["notes_count"] == 1
    assert api("GET", "docs")[2]["rows"][0]["notes_count"] == 1
    st, _, s = api("GET", f"sentences/{sid}")
    assert [n["note_id"] for n in s["notes"]] == [nid]

    # update with the right rev
    st, _, upd = api("PUT", f"notes/{nid}", {"rev": 1, "body_md": "Load path v2 [[S:%s]]" % sid, "explicit": True})
    assert st == 200 and upd["note"]["rev"] == 2 and upd["note"]["links"] == [f"S:{sid}"]
    assert upd["note"]["title"] == "Calcar"  # unchanged fields kept
    # stale rev → 409 with the server copy
    st, _, conflict = api("PUT", f"notes/{nid}", {"rev": 1, "body_md": "stale write"})
    assert st == 409 and conflict["conflict"] is True and conflict["server"]["rev"] == 2
    assert conflict["server"]["body_md"].startswith("Load path v2")
    # rev missing → 400
    assert api("PUT", f"notes/{nid}", {"body_md": "x"})[0] == 400
    assert api("PUT", "notes/N_nope", {"rev": 1, "body_md": "x"})[0] == 404

    # lookups
    st, _, rows = api("GET", f"notes/for?ref=S:{sid}")
    assert st == 200 and [n["note_id"] for n in rows] == [nid]
    assert api("GET", "notes/for?ref=D:P001")[2][0]["note_id"] == nid
    assert api("GET", "notes/for?ref=D:P002")[2] == []  # link removed in rev 2
    assert api("GET", "notes/for?ref=bad")[0] == 400
    st, _, lst = api("GET", "notes?q=calcar")
    assert st == 200 and lst["total"] == 1 and lst["rows"][0]["note_id"] == nid and "excerpt" in lst["rows"][0]
    assert api("GET", "notes?tag=mech")[2]["total"] == 1
    assert api("GET", "notes?tag=nothing")[2]["total"] == 0
    st, _, hist = api("GET", f"notes/{nid}/history")
    assert st == 200
    assert {(h["rev"], h["reason"]) for h in hist} >= {(1, "explicit"), (2, "explicit"), (1, "conflict")}
    assert [h for h in hist if h["reason"] == "conflict"][0]["body_md"] == "stale write"
    assert api("GET", "notes/N_nope/history")[0] == 404

    # delete
    st, _, d = api("DELETE", f"notes/{nid}")
    assert st == 200 and d == {"deleted": nid}
    assert api("GET", f"notes/{nid}")[0] == 404
    assert api("DELETE", f"notes/{nid}")[0] == 404
    assert api("GET", f"notes/for?ref=S:{sid}")[2] == []


def test_note_without_targets_and_bad_bodies(api):
    st, _, note = api("POST", "notes", {"title": "Free", "body_md": "standalone"})
    assert st == 201 and note["targets"] == [] and note["tags"] == []
    assert api("POST", "notes", {"targets": ["nonsense"]})[0] == 400
    api("DELETE", f"notes/{note['note_id']}")


# ---- collections ------------------------------------------------------------------------------------------
def test_collections_items_and_export(api):
    assert api("POST", "collections", {"name": "   "})[0] == 400
    st, _, coll = api("POST", "collections", {"name": "Calcar loading", "description": "Evidence on calcar load"})
    assert st == 201 and coll["coll_id"].startswith("K_") and coll["items"] == []
    cid = coll["coll_id"]
    st, _, lst = api("GET", "collections")
    assert st == 200 and [c["coll_id"] for c in lst] == [cid] and lst[0]["n_items"] == 0

    sid1 = _sid("P001", "The femoral stem transfers load to the calcar.")
    sid2 = _sid("P002", "Micromotion above the threshold leads to fibrous tissue.")
    st, _, i1 = api("POST", f"collections/{cid}/items", {"ref": f"S:{sid1}", "comment_md": "Primary load path."})
    assert st == 201 and i1["item_id"].startswith("KI_") and i1["snapshot"]["doc_id"] == "P001"
    assert i1["snapshot"]["text"] == "The femoral stem transfers load to the calcar." and i1["seq"] == 1.0
    st, _, i2 = api("POST", f"collections/{cid}/items", {"ref": f"S:{sid2}"})
    assert st == 201 and i2["seq"] == 2.0
    st, _, i3 = api("POST", f"collections/{cid}/items", {"comment_md": "Free-standing remark."})
    assert st == 201 and i3["ref"] is None and i3["snapshot"] is None
    st, _, i4 = api("POST", f"collections/{cid}/items", {"ref": "D:P001", "after_item": i1["item_id"]})
    assert st == 201 and 1.0 < i4["seq"] < 2.0
    assert api("POST", f"collections/{cid}/items", {"ref": "S:x", "after_item": "KI_nope"})[0] == 404
    assert api("POST", "collections/K_nope/items", {"comment_md": "x"})[0] == 404

    st, _, got = api("GET", f"collections/{cid}")
    assert st == 200 and [i["item_id"] for i in got["items"]] == [i1["item_id"], i4["item_id"], i2["item_id"], i3["item_id"]]

    # reorder (move the free comment to the top) and edit a comment
    st, _, upd = api("PUT", f"collections/items/{i3['item_id']}", {"seq": 0.5, "comment_md": "Edited remark."})
    assert st == 200 and upd["seq"] == 0.5 and upd["comment_md"] == "Edited remark."
    got = api("GET", f"collections/{cid}")[2]
    assert [i["item_id"] for i in got["items"]][0] == i3["item_id"]
    assert api("PUT", "collections/items/KI_nope", {"seq": 1})[0] == 404

    # export
    st, hd, md = api("GET", f"collections/{cid}/export.md", raw=True)
    assert st == 200 and hd["Content-Type"].startswith("text/markdown")
    assert hd["Content-Disposition"].startswith("inline;")
    text = md.decode("utf-8")
    assert text.startswith("# Calcar loading\n\nEvidence on calcar load\n")
    assert "> The femoral stem transfers load to the calcar." in text
    assert "[P001 p.1] [1]" in text and "[P002 p.2] [2]" in text
    assert "   Edited remark." in text and "Primary load path." in text
    ref_section = text.split("## References", 1)[1]
    assert "1. Smith J; Li W (2021) Stem loading study. J Biomech. [10.1000/stem.1](https://doi.org/10.1000/stem.1) `P001`" in ref_section
    assert "2. Wang Q (2019) Porous coatings and micromotion. Bone. `P002`" in ref_section
    st, hd, _ = api("GET", f"collections/{cid}/export.md?download=1", raw=True)
    assert st == 200 and hd["Content-Disposition"].startswith("attachment;")
    assert api("GET", "collections/K_nope/export.md")[0] == 404

    # remove
    st, _, r = api("DELETE", f"collections/items/{i2['item_id']}")
    assert st == 200 and r == {"removed": i2["item_id"]}
    assert len(api("GET", f"collections/{cid}")[2]["items"]) == 3
    assert api("DELETE", "collections/items/KI_nope")[0] == 404
    assert api("GET", "collections/K_nope")[0] == 404


# ---- static, errors, limits -------------------------------------------------------------------------------
def test_static_files(api):
    st, hd, body = api.page("/")
    assert st == 200 and hd["Content-Type"] == "text/html; charset=utf-8" and b'id="view"' in body
    assert hd["Cache-Control"] == "no-cache" and hd["X-Atlas-Build"] == "evidence"
    st, hd, body = api.page("/static/app.js?v=0.1.0")
    assert st == 200 and hd["Content-Type"] == "text/javascript; charset=utf-8" and b"import" in body
    st, hd, _ = api.page("/app.js")
    assert st == 200 and hd["Content-Type"] == "text/javascript; charset=utf-8"
    st, hd, body = api.page("/static/i18n.json")
    assert st == 200 and hd["Content-Type"] == "application/json; charset=utf-8"
    data = json.loads(body.decode("utf-8"))
    assert all(set(v) == {"zh", "en"} for v in data.values())
    st, hd, _ = api.page("/static/style.css")
    assert st == 200 and hd["Content-Type"] == "text/css; charset=utf-8"
    for mod in ("core.js", "editor.js", "reader.js", "views.js"):
        assert api.page("/static/" + mod)[0] == 200
    assert api.page("/nope.css")[0] == 404
    assert api.page("/static/../server.py")[0] == 404
    assert api.page("/static/..%2Fserver.py")[0] == 404
    assert http_server.MIME[".mjs"].startswith("text/javascript")
    assert http_server.MIME[".wasm"] == "application/wasm"
    assert http_server.MIME[".woff2"] == "font/woff2"


def test_spa_has_no_inline_handlers():
    static = Path(http_server.STATIC_DIR)
    html = (static / "index.html").read_text(encoding="utf-8")
    assert " on" not in html.lower().replace(" one", "")  # no onclick= etc.
    assert "javascript:" not in html
    for js in static.glob("*.js"):
        src = js.read_text(encoding="utf-8")
        assert "eval(" not in src and "new Function" not in src, js.name


def test_errors_methods_and_body_limits(api):
    st, hd, body = api("GET", "nothing/here")
    assert st == 404 and body["error"] and hd["Content-Type"].startswith("application/json")
    st, _, body = api("DELETE", "info")
    assert st == 405
    st, _, body = api("POST", "collections", {"name": "x", "description": ["not", "a", "string"]})
    assert st == 400 and "description" in body["error"]  # review fix: wrong JSON types are 400, never 500
    # malformed JSON
    req = urllib.request.Request(api.base + "/api/v1/collections", data=b"{not json", method="POST",
                                 headers={"Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as ei:
        urllib.request.urlopen(req, timeout=10)
    assert ei.value.code == 400
    # a JSON array is not an object
    req = urllib.request.Request(api.base + "/api/v1/collections", data=b"[1,2]", method="POST",
                                 headers={"Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as ei:
        urllib.request.urlopen(req, timeout=10)
    assert ei.value.code == 400
    # body cap: announce 21 MB, send nothing → 413 before any byte is read
    host, port = api.base[len("http://"):].split(":")
    conn = http.client.HTTPConnection(host, int(port), timeout=10)
    conn.putrequest("POST", "/api/v1/notes")
    conn.putheader("Content-Type", "application/json")
    conn.putheader("Content-Length", str(21 * 1024 * 1024))
    conn.endheaders()
    resp = conn.getresponse()
    assert resp.status == 413
    assert json.loads(resp.read().decode("utf-8"))["error"]
    conn.close()


def test_head_and_unicode_paths(api):
    req = urllib.request.Request(api.base + "/api/v1/info", method="HEAD")
    with urllib.request.urlopen(req, timeout=10) as r:
        assert r.status == 200 and r.read() == b"" and int(r.headers["Content-Length"]) > 0
    st, _, res = api("GET", "search?phrase=" + urllib.parse.quote("150 μm"))
    assert st == 200 and res["total"] == 1


def test_service_thread_locality(served):
    import threading
    svc = served["service"]
    seen = {}

    def worker(name):
        ev, us = svc.evidence(), svc.user()
        assert svc.evidence() is ev and svc.user() is us  # cached within the thread
        seen[name] = (ev, us)  # keep them alive so the objects (and ids) cannot be recycled
        svc.close()

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(3)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len({id(v[0]) for v in seen.values()}) == 3 and len({id(v[1]) for v in seen.values()}) == 3
    assert all(v[0].conn is not seen[0][0].conn for k, v in seen.items() if k != 0)


# ---- review fixes: bind, body handling, validation, CSRF, static, pool ---------------------------------------
def _conn(api) -> http.client.HTTPConnection:
    host, port = api.base[len("http://"):].split(":")
    return http.client.HTTPConnection(host, int(port), timeout=10)


def test_bind_is_exclusive(served):
    # review fix: with SO_REUSEADDR on Windows a second server could bind a port that is already listening
    srv2 = http_server.make_server(served["service"], "127.0.0.1", 0)
    try:
        port = srv2.server_address[1]
        with pytest.raises(OSError):
            http_server.make_server(served["service"], "127.0.0.1", port)
    finally:
        srv2.server_close()
    assert http_server.DEFAULT_PORT == 8770
    from atlas.config import load_defaults
    assert load_defaults()["serve"]["port"] == 8770


def test_error_responses_drain_the_body_on_keep_alive(api):
    # review fix: unread body bytes of a 404/405 answer were parsed as the next request line
    conn = _conn(api)
    body = b'{"a": "bcde"}'
    conn.request("POST", "/api/v1/nothing", body=body, headers={"Content-Type": "application/json"})
    r = conn.getresponse()
    assert r.status == 404 and json.loads(r.read())["error"]
    conn.request("GET", "/api/v1/info")
    r = conn.getresponse()
    assert r.status == 200 and json.loads(r.read())["build"] == "evidence"
    conn.request("POST", "/api/v1/info", body=b"{}", headers={"Content-Type": "application/json"})
    r = conn.getresponse()
    assert r.status == 405
    r.read()
    conn.request("GET", "/api/v1/info")
    r = conn.getresponse()
    assert r.status == 200
    r.read()
    conn.close()
    # chunked bodies are refused (411) and the connection closed; negative lengths are 400
    conn = _conn(api)
    conn.putrequest("POST", "/api/v1/notes")
    conn.putheader("Content-Type", "application/json")
    conn.putheader("Transfer-Encoding", "chunked")
    conn.endheaders()
    conn.send(b"2\r\n{}\r\n0\r\n\r\n")
    r = conn.getresponse()
    assert r.status == 411 and json.loads(r.read())["error"] and r.getheader("Connection") == "close"
    conn.close()
    for bad in ("-5", "abc"):
        conn = _conn(api)
        conn.putrequest("POST", "/api/v1/notes")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", bad)
        conn.endheaders()
        r = conn.getresponse()
        assert r.status == 400, bad
        assert json.loads(r.read())["error"]
        conn.close()


def test_unsupported_methods_and_bad_request_lines_answer_json(api):
    conn = _conn(api)
    for method in ("OPTIONS", "PATCH"):
        conn.request(method, "/api/v1/notes")
        r = conn.getresponse()
        assert r.status == 405 and r.getheader("Content-Type").startswith("application/json"), method
        assert json.loads(r.read())["status"] == 405
    conn.request("TRACE", "/api/v1/info")
    r = conn.getresponse()
    assert r.status == 501 and r.getheader("Content-Type").startswith("application/json")
    assert json.loads(r.read())["error"]
    conn.close()


def test_json_body_values_are_type_checked(api):
    sid = _sid("P001", "Bone remodelling is governed by strain.")
    bad_posts = [
        {"tags": 5}, {"tags": "abc"}, {"tags": [1, 2]}, {"title": {"a": 1}}, {"body_md": [1, 2]},
        {"targets": [{"ref": "D:P001"}]}, {"targets": "D:P001"},
    ]
    for body in bad_posts:
        st, _, res = api("POST", "notes", body)
        assert st == 400 and "parameter" in res["error"], body
    st, _, note = api("POST", "notes", {"title": "typed", "targets": [f"S:{sid}"]})
    assert st == 201
    nid = note["note_id"]
    for body in [{"rev": [1]}, {"rev": 1.9}, {"rev": "x"}, {"rev": True}, {"rev": 1, "explicit": "maybe"},
                 {"rev": 1, "tags": "a,b"}]:
        st, _, res = api("PUT", f"notes/{nid}", body)
        assert st == 400, body
    st, _, res = api("PUT", f"notes/{nid}", {"rev": 1.0, "title": "float rev is fine when integral"})
    assert st == 200 and res["note"]["rev"] == 2
    # review fix: query parameters do not feed POST/PUT bodies
    st, _, res = api("PUT", f"notes/{nid}?title=hax", {"rev": 2})
    assert st == 200 and res["note"]["title"] == "float rev is fine when integral" and res["note"]["rev"] == 3
    st, _, coll = api("POST", "collections", {"name": "typed"})
    cid = coll["coll_id"]
    assert api("POST", f"collections/{cid}/items", {"comment_md": {"a": 1}})[0] == 400
    assert api("POST", f"collections/{cid}/items", {"ref": 5})[0] == 400
    st, _, item = api("POST", f"collections/{cid}/items", {"comment_md": "ok"})
    assert st == 201
    for body in [{"seq": "nan"}, {"seq": float("inf") if False else "inf"}, {"seq": [1]}, {"seq": True}]:
        assert api("PUT", f"collections/items/{item['item_id']}", body)[0] == 400, body
    assert api("PUT", f"collections/items/{item['item_id']}", {"seq": "2.5"})[2]["seq"] == 2.5
    # out-of-range page numbers and negative limits never reach SQLite
    assert api("GET", "docs/P001/pages/99999999999999999999")[0] == 400
    assert api("GET", "docs/P001/pages/99999999999999999999.png")[0] == 400
    st, _, res = api("GET", "docs?limit=-1")
    assert st == 200 and len(res["rows"]) == 1          # clamped to 1
    st, _, res = api("GET", "search?phrase=stem&limit=0&offset=-3")
    assert st == 200 and len(res["rows"]) == 1
    assert api("GET", "docs/NOPE/search?phrase=stem")[0] == 404
    api("DELETE", f"notes/{nid}")


def test_cross_site_writes_are_refused(api):
    conn = _conn(api)
    # review fix: a text/plain POST (no CORS preflight) from any web page could create collections
    conn.request("POST", "/api/v1/collections", body=b'{"name":"csrf"}', headers={"Content-Type": "text/plain"})
    r = conn.getresponse()
    assert r.status == 415 and json.loads(r.read())["error"]
    conn.request("POST", "/api/v1/collections", body=b'{"name":"csrf"}',
                 headers={"Content-Type": "application/json", "Sec-Fetch-Site": "cross-site"})
    r = conn.getresponse()
    assert r.status == 403 and json.loads(r.read())["error"]
    conn.request("POST", "/api/v1/collections", body=b'{"name":"csrf"}',
                 headers={"Content-Type": "application/json", "Origin": "http://evil.example"})
    r = conn.getresponse()
    assert r.status == 403
    r.read()
    conn.request("DELETE", "/api/v1/notes/N_nope", headers={"Sec-Fetch-Site": "cross-site"})
    r = conn.getresponse()
    assert r.status == 403
    r.read()
    # same-origin / fetch-metadata-less requests pass
    host = api.base[len("http://"):]
    conn.request("POST", "/api/v1/collections", body=b'{"name":"same origin"}',
                 headers={"Content-Type": "application/json", "Origin": "http://" + host,
                          "Sec-Fetch-Site": "same-origin"})
    r = conn.getresponse()
    assert r.status == 201
    r.read()
    conn.close()
    st, _, cols = api("GET", "collections")
    assert "csrf" not in {c["name"] for c in cols} and "same origin" in {c["name"] for c in cols}


def test_static_trailing_slash_and_version_injection(api):
    assert api.page("/static/app.js/")[0] == 404
    assert api.page("/static/")[0] == 404
    import atlas
    st, _, body = api.page("/")
    assert st == 200
    html = body.decode("utf-8")
    assert f"/static/style.css?v={atlas.__version__}" in html and f"/static/app.js?v={atlas.__version__}" in html
    assert "?v=0.0.0" not in html


def test_export_filename_uses_the_collection_name(api):
    st, _, coll = api("POST", "collections", {"name": "Calcar  loading / 校准"})
    cid = coll["coll_id"]
    st, hd, _ = api("GET", f"collections/{cid}/export.md?download=1", raw=True)
    assert st == 200
    cd = hd["Content-Disposition"]
    assert cd.startswith('attachment; filename="Calcar-loading-_.md"'), cd
    assert "filename*=UTF-8''Calcar-loading-%E6%A0%A1%E5%87%86.md" in cd
    st, _, coll = api("POST", "collections", {"name": "校准"})
    st, hd, _ = api("GET", f"collections/{coll['coll_id']}/export.md", raw=True)
    assert f'inline; filename="{coll["coll_id"]}.md"' in hd["Content-Disposition"]


def test_request_threads_reuse_pooled_stores(api, served):
    svc = served["service"]
    for _ in range(3):
        assert api("GET", "info")[0] == 200
    assert svc.pool_size() >= 1
    pair = svc._pool[-1]
    before = {id(st.conn) for p in svc._pool for st in p if st is not None}
    for _ in range(3):
        assert api("GET", "docs")[0] == 200
    after = {id(st.conn) for p in svc._pool for st in p if st is not None}
    assert before == after and len(svc._pool) <= 3
    assert id(pair[0].conn) in after


def test_handler_errors_are_logged_not_printed(served, caplog, capsys):
    srv = served["server"]
    import logging
    with caplog.at_level(logging.DEBUG, logger="atlas.http.server"):
        try:
            raise ConnectionAbortedError("client gone")
        except ConnectionAbortedError:
            srv.handle_error(None, ("127.0.0.1", 1))
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            srv.handle_error(None, ("127.0.0.1", 2))
    assert capsys.readouterr().err == ""
    msgs = [r.getMessage() for r in caplog.records]
    assert any("dropped" in m for m in msgs) and any("failed" in m for m in msgs)


def test_png_of_unrenderable_pdf_is_404_without_path(api, served):
    project = served["project"]
    empty = project.pdf_dir / "zero pages 零页.pdf"
    empty.write_bytes(b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
                      b"2 0 obj\n<< /Type /Pages /Kids [] /Count 0 >>\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF\n")
    garbage = project.pdf_dir / "garbage.pdf"
    garbage.write_bytes(b"%PDF-1.4 not really a pdf")
    enc = project.pdf_dir / "encrypted.pdf"
    doc = fitz.open()
    doc.new_page()
    doc.save(str(enc), encryption=fitz.PDF_ENCRYPT_AES_256, owner_pw="x", user_pw="x")
    doc.close()
    ev = EvidenceStore.open(project.evidence_db)
    ev.add_document("Z001", "paper", "scan", empty, empty.name, "9" * 64, DocMeta(title="Zero pages"))
    ev.add_document("Z002", "paper", "scan", garbage, garbage.name, "8" * 64, DocMeta(title="Garbage"))
    ev.add_document("Z003", "paper", "scan", enc, enc.name, "7" * 64, DocMeta(title="Encrypted"))
    ev.close()
    for doc_id, name in (("Z001", "零页"), ("Z002", "garbage.pdf"), ("Z003", "encrypted.pdf")):
        st, _, body = api("GET", f"docs/{doc_id}/pages/1.png")
        assert st == 404, (doc_id, st, body)
        assert name not in body["error"] and str(project.pdf_dir) not in body["error"], body
    assert api("GET", "docs/Z001/pages/0.png")[0] == 404


def test_serve_log_setup(served):
    path = http_server.setup_logging(served["project"])
    assert path.name == "serve.log" and path.parent == served["project"].logs_dir
    assert http_server.setup_logging(served["project"]) == path  # idempotent, one handler
    import logging
    handlers = [h for h in logging.getLogger("atlas").handlers
                if isinstance(h, logging.handlers.RotatingFileHandler) and Path(h.baseFilename) == path]
    assert len(handlers) == 1 and handlers[0].maxBytes == 5 * 1024 * 1024 and handlers[0].backupCount == 3
    logging.getLogger("atlas").removeHandler(handlers[0])
    handlers[0].close()


# ================================================================================================================
# Increment 2: concept / framework routes against a fixture project with a finished build (built through the real
# commands: fixture corpus → ingest → curated set written by the test → `atlas build all`).
# ================================================================================================================
import os as _os
import shutil as _shutil
import stat as _stat

from atlas.pipeline import build as _B
from atlas.pipeline import gates as _gates
from atlas.pipeline.runner import run_stage as _run_stage
from atlas.pipeline.stages.ingest import register_documents as _register
from atlas.tools.fixture import make_fixture as _make_fixture
from tests.test_build_pipeline import write_curated as _write_curated


@pytest.fixture(scope="module")
def served_build(tmp_path_factory):
    base = tmp_path_factory.mktemp("http build 测试")
    info = _make_fixture(base / "fx", 1)
    project = init_project(base / "proj", name="HTTP build project", pdf_dir=str(base / "fx" / "pdf"))
    _shutil.copy(info["manifest"], project.manifest_path)
    assert _register(project).items_failed == 0
    assert _run_stage(project, "ingest").items_failed == 0
    docs = [d["doc_id"] for d in info["documents"]]
    _write_curated(project, docs)
    _gates.confirm_all(project)
    r = _B.build_all(project, note="http", workers=1)
    service = AtlasService(project)
    srv, thread = http_server.run_in_thread(service, "127.0.0.1", 0)
    base_url = "http://127.0.0.1:%d" % srv.server_address[1]
    yield {"base": base_url, "project": project, "service": service, "server": srv, "build_id": r["build_id"], "info": info, "docs": docs}
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=5)
    service.shutdown()
    for p in project.builds_dir.glob("*/build.sqlite"):
        _os.chmod(p, _stat.S_IWRITE | _stat.S_IREAD)


@pytest.fixture
def bapi(served_build):
    base = served_build["base"]

    def call(method, path, body=None, raw=False):
        return _req(base, method, "/api/v1/" + path.lstrip("/"), body, raw)
    call.base = base
    return call


def test_build_info_and_header(bapi, served_build):
    bid = served_build["build_id"]
    st, hd, body = bapi("GET", "info")
    assert st == 200 and body["build"] == bid and hd["X-Atlas-Build"] == bid
    assert body["build_info"]["id"] == bid and body["build_info"]["docs"] == 9 and body["build_info"]["concepts"] == 6
    assert body["build_info"]["sentences"] > 0 and body["build_info"]["occurrences"] > 0
    st, _, i18n = bapi("GET", "i18n")
    assert i18n["data"] == {"微动": "Micromotion"} and [d["id"] for d in i18n["domains"]] == ["DOM_ortho", "DOM_fe"]
    assert i18n["tiers"]["A"]["zh"] and i18n["ui"]["nav.concepts"] == {"zh": "概念", "en": "Concepts"}
    assert i18n["sections"]["body"]["en"] == "Body"


def test_no_build_routes_answer_409(api):
    st, hd, body = api("GET", "concepts")
    assert st == 409 and body["error"] == "no build promoted" and hd["X-Atlas-Build"] == "evidence"
    assert api("GET", "concepts/C_x")[0] == 409
    assert api("GET", "framework/terms")[0] == 409 and api("GET", "framework/subdomains")[0] == 409
    assert api("GET", "subdomains/X")[0] == 409
    st, _, d = api("GET", "docs/P001")
    assert st == 200 and d["domains"] == [] and d["subdomains"] == [] and d["top_concepts"] == [] and d["units"][0]["kind"] == "document"
    st, _, p = api("GET", "docs/P001/pages/1?c=C_x")
    assert st == 200 and p["sentences"][0]["concepts"] == [] and p["concepts"] == ["C_x"]


def test_concept_list_sorting_and_filters(bapi):
    st, _, body = bapi("GET", "concepts")
    assert st == 200 and body["total"] == 6 and body["scope"] == "all" and body["sort"] == "papers" and body["dir"] == "desc"
    papers = [r["papers"] for r in body["rows"]]
    assert papers == sorted(papers, reverse=True)
    row = body["rows"][0]
    assert {"id", "label", "zh", "type", "mode", "papers", "coverage", "topic_rate", "tier", "main_section", "notes"} <= set(row)
    st, _, asc = bapi("GET", "concepts?sort=label")
    assert [r["label"] for r in asc["rows"]] == sorted((r["label"] for r in asc["rows"]), key=str.lower) and asc["dir"] == "asc"
    st, _, desc = bapi("GET", "concepts?sort=label&dir=desc")
    assert [r["id"] for r in desc["rows"]] == [r["id"] for r in asc["rows"]][::-1]
    st, _, b = bapi("GET", "concepts?q=" + urllib.parse.quote("微"))
    assert b["total"] == 1 and b["rows"][0]["id"] == "C_micromotion"
    st, _, b = bapi("GET", "concepts?domain=DOM_fe")
    assert {r["id"] for r in b["rows"]} == {"C_micromotion", "C_finiteelement", "C_stressstrainfield"}
    st, _, b = bapi("GET", "concepts?tier=A")
    assert b["total"] >= 1 and all(r["tier"] == "A" for r in b["rows"])
    st, _, b = bapi("GET", "concepts?type=" + urllib.parse.quote("名词术语"))
    assert {r["id"] for r in b["rows"]} == {"C_stem", "C_femoralstem"}
    st, _, b = bapi("GET", "concepts?sub=ORT-MM")
    assert b["total"] >= 1
    st, _, b = bapi("GET", "concepts?cat=O1")
    assert [r["id"] for r in b["rows"]] == ["C_micromotion"]
    st, _, b = bapi("GET", "concepts?scope=DOM_fe")
    assert b["scope"] == "DOM_fe" and max(r["papers"] for r in b["rows"]) == 5
    assert bapi("GET", "concepts?scope=DOM_nope")[0] == 400
    st, _, b = bapi("GET", "concepts?limit=2&offset=2")
    assert len(b["rows"]) == 2 and b["total"] == 6


def test_concept_list_every_sort_is_stable(bapi):
    """Every sort column with both directions; ties broken by label then id, so repeated calls give the same order."""
    for sort, key, default_dir in (("papers", "papers", "desc"), ("coverage", "coverage", "desc"), ("topic_rate", "topic_rate", "desc"),
                                   ("tier", "tier", "asc"), ("label", "label", "asc")):
        st, _, a = bapi("GET", f"concepts?sort={sort}")
        assert st == 200 and a["sort"] == sort and a["dir"] == default_dir, sort
        vals = [(r[key] if r[key] is not None else "Z") for r in a["rows"]]
        if sort == "label":
            vals = [v.lower() for v in vals]
        assert vals == sorted(vals, reverse=default_dir == "desc"), sort
        st, _, b = bapi("GET", f"concepts?sort={sort}")
        assert [r["id"] for r in b["rows"]] == [r["id"] for r in a["rows"]]
        other = "asc" if default_dir == "desc" else "desc"
        st, _, c = bapi("GET", f"concepts?sort={sort}&dir={other}")
        assert c["dir"] == other
        rev = [(r[key] if r[key] is not None else "Z") for r in c["rows"]]
        if sort == "label":
            rev = [v.lower() for v in rev]
        assert rev == sorted(rev, reverse=other == "desc"), sort
        # ties keep the same relative (label, id) order in both directions
        groups_a = [[r["id"] for r in a["rows"] if r[key] == v] for v in dict.fromkeys(r[key] for r in a["rows"])]
        groups_c = [[r["id"] for r in c["rows"] if r[key] == v] for v in dict.fromkeys(r[key] for r in c["rows"])]
        assert sorted(map(tuple, groups_a)) == sorted(map(tuple, groups_c)), sort
    st, _, d = bapi("GET", "concepts?sort=nope")
    assert st == 400 and "sort" in d["error"]
    st, _, d = bapi("GET", "concepts?sort=papers&dir=sideways")
    assert st == 400 and "dir" in d["error"]
    assert bapi("GET", "concepts?sort=&dir=")[2]["sort"] == "papers"
    # paging walks the same order as the full list
    full = [r["id"] for r in bapi("GET", "concepts?sort=label&limit=100")[2]["rows"]]
    paged = []
    for off in range(0, len(full), 2):
        paged += [r["id"] for r in bapi("GET", f"concepts?sort=label&limit=2&offset={off}")[2]["rows"]]
    assert paged == full


def test_concept_list_ids_mode_and_type_facet(bapi):
    """`ids=1` returns the whole filtered list in list order (the UI's prev/next context); `i18n.types` is the type
    facet of the build."""
    st, _, res = bapi("GET", "concepts?ids=1&sort=label&limit=2")
    assert st == 200 and set(res) >= {"ids", "labels", "total", "scope", "sort", "dir", "build"} and "rows" not in res
    assert len(res["ids"]) == res["total"] == 6 and len(res["labels"]) == 6
    assert res["ids"] == [r["id"] for r in bapi("GET", "concepts?sort=label&limit=100")[2]["rows"]]
    assert [x.lower() for x in res["labels"]] == sorted(x.lower() for x in res["labels"])
    st, _, sub = bapi("GET", "concepts?ids=1&domain=DOM_fe&sort=papers&dir=asc")
    assert set(sub["ids"]) == {"C_micromotion", "C_finiteelement", "C_stressstrainfield"} and sub["dir"] == "asc"
    assert bapi("GET", "concepts?ids=1&scope=DOM_nope")[0] == 400
    assert bapi("GET", "concepts?ids=maybe")[0] == 400
    st, _, i18n = bapi("GET", "i18n")
    assert i18n["types"][0] == {"type": "量与指标", "n": 3}
    assert sum(x["n"] for x in i18n["types"]) == 6 and [x["type"] for x in i18n["types"]] == ["量与指标", "名词术语", "方法或模型名"]


def test_concept_page(bapi, served_build):
    st, _, c = bapi("GET", "concepts/C_micromotion")
    assert st == 200 and c["id"] == "C_micromotion" and c["forms"] == ["micromotion", "micromotions"] and c["zh"] == "微动"
    scopes = [m["scope"] for m in c["importance"]]
    assert scopes == ["all", "DOM_ortho", "DOM_fe"]
    all_ = c["importance"][0]
    assert all_["papers"] == 9 and all_["coverage"] == 1.0 and all_["tier"] in ("A", "B") and "year_cov" in all_
    assert any(s["section"] == "body" and s["papers"] == 9 for s in c["sections"])
    assert c["domains_use"][0]["domain"] == "DOM_ortho" and c["domains_use"][-1]["domain"] is None
    assert c["years"] and c["year_totals"] and sum(c["year_totals"].values()) == 9
    assert c["categories"][0]["primary_cat"] == "O1" and c["categories"][0]["primary_zh"] == "指标"
    assert c["subdomains"] and c["subdomains"][0]["sub_id"] in ("ORT-MM", "X-FEMM") and "score" in c["subdomains"][0]
    assert [a["sub_id"] for a in c["anchor_of"]] == ["ORT-MM", "X-FEMM"]
    assert c["term_tree_places"][0]["sub_name"] == "指标"
    assert len(c["examples"]) == 6
    ex = c["examples"][0]
    assert ex["ref"].startswith("S:") and ex["kind"] == "S" and ex["text"] and ex["spans"] and ex["page"] >= 1
    a, b = ex["spans"][0][:2]
    assert ex["text"][a:b].lower() == "micromotion"
    assert c["notes"] == 0 and c["build"] == served_build["build_id"]
    # examples are deterministic (one per document, most standalone body occurrences first) and are body sentences
    again = bapi("GET", "concepts/C_micromotion")[2]["examples"]
    assert [e["sid"] for e in again] == [e["sid"] for e in c["examples"]]
    assert len({e["doc_id"] for e in c["examples"]}) == 6
    assert all(e["section"] not in ("references", "front") for e in c["examples"])
    # the scope parameter is echoed and validated like the list's
    assert bapi("GET", "concepts/C_micromotion?scope=DOM_fe")[2]["scope"] == "DOM_fe"
    assert bapi("GET", "concepts/C_micromotion?scope=DOM_nope")[0] == 400
    assert bapi("GET", "concepts/C_nope")[0] == 404
    st, _, lit = bapi("GET", "concepts/C_stressstrainfield")
    assert st == 200 and lit["mode"] == "literal" and lit["rx"]


def test_concept_sentences_and_docs(bapi, served_build):
    info = served_build["info"]
    st, _, s = bapi("GET", "concepts/C_micromotion/sentences?limit=5")
    assert st == 200 and s["total"] > 9 and len(s["rows"]) == 5
    row = s["rows"][0]
    assert {"sid", "doc_id", "title", "year", "page", "section", "text", "spans", "tags", "ref"} <= set(row)
    for a, b, form, nested in row["spans"]:
        assert row["text"][a:b].lower() == "micromotion"
    st, _, s2 = bapi("GET", "concepts/C_micromotion/sentences?doc=P001&standalone=1")
    assert s2["total"] == info["micromotion_counts"]["P001"] + 1 and all(r["doc_id"] == "P001" for r in s2["rows"])
    st, _, s3 = bapi("GET", "concepts/C_micromotion/sentences?section=results&sort=year")
    assert all(r["section"] == "results" for r in s3["rows"])
    years = [r["year"] for r in s3["rows"]]
    assert years == sorted(years, reverse=True)
    st, _, s4 = bapi("GET", "concepts/C_micromotion/sentences?domain=DOM_fe")
    assert s4["total"] < s["total"]
    st, _, s5 = bapi("GET", "concepts/C_micromotion/sentences?sub=ORT-MM&tag=quant")
    assert all("quant" in r["tags"] for r in s5["rows"])
    assert bapi("GET", "concepts/C_micromotion/sentences?section=nope")[0] == 400
    assert bapi("GET", "concepts/C_nope/sentences")[0] == 404
    st, _, d = bapi("GET", "concepts/C_micromotion/docs")
    assert st == 200 and d["total"] == 9 and d["rows"][0]["body"] >= d["rows"][-1]["body"]
    assert {"doc_id", "title", "year", "keywords", "abstract", "introduction", "methods", "results", "discussion", "body", "refs"} <= set(d["rows"][0])
    st, _, d2 = bapi("GET", "concepts/C_micromotion/docs?scope=DOM_fe&sort=year")
    assert d2["total"] == 5
    assert bapi("GET", "concepts/C_micromotion/docs?use=nope")[0] == 400


def test_framework_routes(bapi):
    st, _, t = bapi("GET", "framework/terms")
    assert st == 200 and t["domains"][0]["id"] == "DOM_ortho"
    sec = t["domains"][0]["sections"][0]
    assert sec["section"] == "title" and sec["subs"][0]["terms"][0]["concept_id"] == "C_micromotion" and sec["n"] == 1
    st, _, c = bapi("GET", "framework/categories")
    assert st == 200 and [d["id"] for d in c["domains"]] == ["DOM_ortho", "DOM_fe"]
    cat = c["domains"][0]["categories"][0]
    assert cat["id"] == "O1" and cat["concepts"][0]["id"] == "C_micromotion" and cat["concepts"][0]["papers"] == 9 and cat["papers"] == 9
    st, _, c2 = bapi("GET", "framework/categories?domain=DOM_fe")
    assert [d["id"] for d in c2["domains"]] == ["DOM_fe"] and c2["domains"][0]["categories"] == []
    st, _, s = bapi("GET", "framework/subdomains")
    groups = {g["id"]: g for g in s["groups"]}
    assert groups["DOM_ortho"]["subs"][0]["id"] == "ORT-MM" and groups["DOM_ortho"]["subs"][0]["papers"] == 9
    assert groups["cross"]["subs"][0]["id"] == "X-FEMM" and groups["cross"]["subs"][0]["base"] == 9


def test_subdomain_page_and_docs(bapi):
    st, _, s = bapi("GET", "subdomains/ORT-MM")
    assert st == 200 and s["id"] == "ORT-MM" and s["kind"] == "within" and s["papers"] == 9 and s["base"] == 9
    assert s["anchors"] == [[{"id": "C_micromotion", "label": "micromotion", "zh": "微动", "type": "量与指标"},
                             {"id": "C_primarystability", "label": "primary stability", "zh": "初始稳定性", "type": "量与指标"}]]
    assert s["concepts"] and s["concepts"][0]["rank"] == 1 and s["n_concepts"] >= 1
    assert s["docs"] and {"doc_id", "tier", "hits", "n_hits", "title"} <= set(s["docs"][0])
    assert s["years"] and s["tiers"] and s["notes"] == 0
    st, _, x = bapi("GET", "subdomains/X-FEMM")
    assert x["kind"] == "cross" and len(x["anchors"]) == 2 and x["overlaps"][0]["id"] == "ORT-MM"
    st, _, d = bapi("GET", "subdomains/ORT-MM/docs?tier=core&limit=3")
    assert st == 200 and len(d["rows"]) <= 3 and all(r["tier"] == "core" for r in d["rows"]) and d["total"] == s["core"]
    st, _, d2 = bapi("GET", "subdomains/ORT-MM/docs?q=review")
    assert d2["total"] == 1 and d2["rows"][0]["doc_id"] == "P009"
    assert bapi("GET", "subdomains/NOPE")[0] == 404 and bapi("GET", "subdomains/NOPE/docs")[0] == 404


def test_doc_extension_and_page_marks(bapi):
    st, _, d = bapi("GET", "docs/P001")
    assert st == 200 and d["domains"] == ["DOM_ortho", "DOM_fe"] and d["in_build"] is True
    assert any(s["sub_id"] == "ORT-MM" for s in d["subdomains"]) and d["top_concepts"][0]["n"] >= 1
    assert d["units"][0]["unit_id"] == "P001" and d["units"][0]["kind"] == "document"
    st, _, p = bapi("GET", "docs/P001/pages/2?c=C_micromotion,C_stem")
    assert st == 200 and p["concepts"] == ["C_micromotion", "C_stem"]
    marked = [s for s in p["sentences"] if s["concepts"]]
    assert marked
    for s in marked:
        for o in s["concepts"]:
            assert o["id"] in ("C_micromotion", "C_stem")
            assert s["text"][o["cstart"]:o["cend"]].lower() in ("micromotion", "micromotions", "stem", "stems")
    st, _, p2 = bapi("GET", "docs/P001/pages/2")
    assert "concepts" not in p2["sentences"][0]


def test_resolve_build_refs(bapi):
    st, _, r = bapi("GET", "resolve?ref=C:C_micromotion")
    assert st == 200 and r["status"] == "live" and r["title"] == "micromotion" and r["zh"] == "微动" and r["kind"] == "C"
    st, _, r = bapi("GET", "resolve?ref=C:C_nope")
    assert st == 200 and r["status"] == "orphan"
    st, _, r = bapi("GET", "resolve?ref=SD:ORT-MM")
    assert st == 200 and r["status"] == "live" and r["zh"] == "微动" and r["en"] == "Micromotion" and r["sub_kind"] == "within"
    st, _, r = bapi("GET", "resolve?ref=U:P001")
    assert st == 200 and r["status"] == "live" and r["unit_id"] == "P001"
    st, _, n = bapi("POST", "notes", {"title": "on micromotion", "body_md": "see [[SD:ORT-MM]]", "tags": ["x"],
                                       "targets": ["C:C_micromotion", "SD:ORT-MM"]})
    assert st == 201 and [t["ref"] for t in n["targets"]] == ["C:C_micromotion", "SD:ORT-MM"]
    assert n["targets"][0]["snapshot_text"] == "micromotion" and n["targets"][0]["status"] == "live"
    st, _, lst = bapi("GET", "concepts?q=micromotion")
    assert lst["rows"][0]["notes"] == 1
    st, _, c = bapi("GET", "concepts/C_micromotion")
    assert c["notes"] == 1
    st, _, s = bapi("GET", "subdomains/ORT-MM")
    assert s["notes"] == 1
    st, _, nf = bapi("GET", "notes/for?ref=SD:ORT-MM")
    assert len(nf) == 1


def test_build_switch_is_picked_up(bapi, served_build):
    """Promoting another build changes the X-Atlas-Build header without a restart."""
    project = served_build["project"]
    bid = served_build["build_id"]
    r = _B.build_all(project, note="second", workers=1)
    try:
        st, hd, body = bapi("GET", "info")
        assert hd["X-Atlas-Build"] == r["build_id"] and body["build_info"]["id"] == r["build_id"]
        assert bapi("GET", "concepts")[2]["build"] == r["build_id"]
    finally:
        _B.promote(project, bid)
    st, hd, _ = bapi("GET", "info")
    assert hd["X-Atlas-Build"] == bid


def test_static_concept_modules(bapi):
    for name in ("concepts.js", "framework.js"):
        st, hd, payload = _req(bapi.base, "GET", "/static/" + name, raw=True)
        assert st == 200 and hd["Content-Type"].startswith("text/javascript") and b"export" in payload
    st, hd, payload = _req(bapi.base, "GET", "/", raw=True)
    assert b'data-view="concepts"' in payload and b'data-view="framework"' in payload


def test_every_chrome_string_has_both_languages():
    """No untranslated chrome: every `t("key")` literal in the SPA (and every data-i18n attribute) has a {zh, en}
    entry in i18n.json; dynamic keys (`t("prefix." + x)`) have at least one entry per prefix."""
    import re
    static = Path(http_server.STATIC_DIR)
    strings = json.loads((static / "i18n.json").read_text(encoding="utf-8"))
    assert all(set(v) == {"zh", "en"} and v["zh"].strip() and v["en"].strip() for v in strings.values())
    used, prefixes = set(), set()
    for js in static.glob("*.js"):
        src = js.read_text(encoding="utf-8")
        used |= set(re.findall(r'\bt\("([^"]+)"', src))
        prefixes |= set(re.findall(r'\bt\("([\w.]+\.)" \+', src))
    html = (static / "index.html").read_text(encoding="utf-8")
    used |= set(re.findall(r'data-i18n(?:-ph|-title)?="([^"]+)"', html))
    missing = sorted(k for k in used if k not in strings and not any(k == p for p in prefixes))
    assert not missing, missing
    for p in prefixes:
        assert any(k.startswith(p) for k in strings), p
    for key in ("unit.kind.intro", "unit.kind.lit_review", "unit.kind.other", "doctype.thesis", "trend.up", "trend.down",
                "mode.literal", "status.orphan", "sd.core", "sd.related"):
        assert key in strings, key
