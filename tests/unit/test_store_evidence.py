"""EvidenceStore unit tests: hand-made rows, no PDFs."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from atlas.core.ids import extraction_id, sentence_id, text_sha256
from atlas.core.schemas import DocMeta, LineMask, PageText, SentenceRow, Unit
from atlas.store import db
from atlas.store.evidence import EvidenceStore

DOC = "P001"
SHA = "a" * 64

PAGE1_V1 = "Running head 1\nBone remodelling is governed by strain. The femoral stem transfers load to the calcar."
PAGE2_V1 = "Stress shielding reduces periprosthetic bone density. Shorter stems load the metaphysis more."
# v2: page 1 identical, page 2 re-extracted with one sentence changed (ligature fix) and one unchanged
PAGE2_V2 = "Stress shielding reduces peri-prosthetic bone density. Shorter stems load the metaphysis more."


def _span(page: str, sentence: str) -> tuple[int, int]:
    i = page.index(sentence)
    return i, i + len(sentence)


def _sentences(ext_id: str, pages: list[PageText], texts_by_page: dict[int, list[str]]) -> list[SentenceRow]:
    rows: list[SentenceRow] = []
    seq = 0
    for p in pages:
        for t in texts_by_page.get(p.page_no, []):
            cs, ce = _span(p.text, t)
            rows.append(SentenceRow(sid=sentence_id(DOC, t), doc_id=DOC, text=t, ext_id=ext_id, seq=seq,
                                    unit_id=DOC, page_no=p.page_no, cstart=cs, cend=ce, section="introduction",
                                    is_body=True, tags=("mech",) if "governed" in t else (), flags=()))
            seq += 1
    return rows


@pytest.fixture
def store(tmp_path: Path) -> EvidenceStore:
    # a path with a space and CJK characters exercises the URI builder
    path = tmp_path / "证据 store" / "evidence.sqlite"
    s = EvidenceStore.open(path)
    s.add_document(DOC, "paper", "manifest", tmp_path / "missing.pdf", "p001.pdf", SHA,
                   DocMeta(title="Stem loading study", year=2021, authors="Smith J; Li W", doi="10.1/x"))
    yield s
    s.close()


def _ingest_v1(store: EvidenceStore) -> str:
    ext = extraction_id(DOC, SHA, "v1")
    pages = [PageText(1, PAGE1_V1, 595.0, 842.0), PageText(2, PAGE2_V1, 595.0, 842.0)]
    sents = _sentences(ext, pages, {
        1: ["Bone remodelling is governed by strain.", "The femoral stem transfers load to the calcar."],
        2: ["Stress shielding reduces periprosthetic bone density.", "Shorter stems load the metaphysis more."],
    })
    store.add_extraction(ext_id=ext, doc_id=DOC, pdf_sha256=SHA, extractor_version="v1", doc_type="paper",
                         pages=pages, masks=[LineMask(1, 0, 14, "header_footer")],
                         units=[Unit(DOC, DOC, "document", 0, None, 1, 2)], sentences=sents,
                         text_ratio=0.9, drop_ratio=0.05, stats={"n": 4})
    return ext


def _ingest_v2(store: EvidenceStore) -> str:
    ext = extraction_id(DOC, SHA, "v2")
    pages = [PageText(1, PAGE1_V1, 595.0, 842.0), PageText(2, PAGE2_V2, 595.0, 842.0)]
    sents = _sentences(ext, pages, {
        1: ["Bone remodelling is governed by strain.", "The femoral stem transfers load to the calcar."],
        2: ["Stress shielding reduces peri-prosthetic bone density.", "Shorter stems load the metaphysis more."],
    })
    store.add_extraction(ext_id=ext, doc_id=DOC, pdf_sha256=SHA, extractor_version="v2", doc_type="paper",
                         pages=pages, masks=[LineMask(1, 0, 14, "header_footer")],
                         units=[Unit(DOC, DOC, "document", 0, None, 1, 2)], sentences=sents,
                         text_ratio=0.9, drop_ratio=0.05)
    return ext


# ---- db.py ------------------------------------------------------------------------------------------------

def test_file_uri_windows_shapes(tmp_path: Path):
    p = tmp_path / "a b" / "中文" / "x.sqlite"
    uri = db.file_uri(p, "rw")
    assert uri.startswith("file:///") or uri.startswith("file://")
    assert " " not in uri and "中" not in uri  # percent-encoded
    assert uri.endswith("?mode=rwc")
    assert db.file_uri(p, "ro").endswith("?mode=ro")
    assert db.file_uri(p, "immutable").endswith("?immutable=1")
    with pytest.raises(ValueError):
        db.file_uri(p, "bogus")


def test_file_uri_unc_path_has_empty_authority():
    # review fix: Path.as_uri() put the server into the URI authority, which SQLite rejects
    uri = db.file_uri(Path(r"\\DESKTOP-NOPE\share\sub dir\x.sqlite"), "ro")
    assert uri == "file:////DESKTOP-NOPE/share/sub%20dir/x.sqlite?mode=ro"
    assert db.file_uri(Path(r"\\srv\证据\x.sqlite"), "rw").startswith("file:////srv/%E8%AF%81%E6%8D%AE/x.sqlite")
    assert db.file_uri(Path(r"C:\data\x.sqlite"), "ro").startswith("file:///C:/data/x.sqlite")


def test_connect_sets_pragmas_and_migrates(store: EvidenceStore):
    c = store.conn
    assert c.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert c.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert c.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
    assert db.user_version(c) == db.EVIDENCE_SCHEMA_VERSION
    assert c.row_factory is sqlite3.Row
    # migrate is idempotent
    db.migrate(c, "evidence.sql", db.EVIDENCE_SCHEMA_VERSION)
    assert db.user_version(c) == db.EVIDENCE_SCHEMA_VERSION


def test_read_only_connection(tmp_path: Path, store: EvidenceStore):
    path = tmp_path / "证据 store" / "evidence.sqlite"
    ro = EvidenceStore.open(path, mode="ro")
    assert ro.get_document(DOC)["title"] == "Stem loading study"
    with pytest.raises(sqlite3.OperationalError):
        ro.conn.execute("INSERT INTO documents(doc_id, doc_type, added) VALUES ('X','paper','now')")
    ro.close()
    with pytest.raises(FileNotFoundError):
        db.connect(tmp_path / "nope.sqlite", mode="ro")


def test_attach(tmp_path: Path, store: EvidenceStore):
    other = db.open_user(tmp_path / "user dir" / "user.sqlite")
    other.close()
    db.attach(store.conn, "u", tmp_path / "user dir" / "user.sqlite", mode="ro")
    assert store.conn.execute("SELECT COUNT(*) FROM u.notes").fetchone()[0] == 0


# ---- documents --------------------------------------------------------------------------------------------

def test_add_document_idempotent_and_new_file(store: EvidenceStore, tmp_path: Path):
    d = store.get_document(DOC)
    assert d["pdf_sha256"] == SHA and d["rel_path"] == "p001.pdf" and d["ext_id"] is None
    assert d["n_pages"] == 0 and d["n_sentences"] == 0
    store.add_document(DOC, "paper", "manifest", tmp_path / "missing.pdf", "p001.pdf", SHA)  # no-op
    assert store.conn.execute("SELECT COUNT(*) FROM document_files").fetchone()[0] == 1
    store.add_document(DOC, "paper", "manifest", tmp_path / "missing.pdf", "p001_v2.pdf", "b" * 64)
    files = store.conn.execute("SELECT pdf_sha256, is_current FROM document_files ORDER BY added, rowid").fetchall()
    assert [(f[0][0], f[1]) for f in files] == [("a", 0), ("b", 1)]
    assert store.get_document(DOC)["rel_path"] == "p001_v2.pdf"
    assert store.conn.execute("SELECT COUNT(*) FROM document_fts WHERE doc_id=?", (DOC,)).fetchone()[0] == 1
    # review fix: the file reverted to the earlier sha (possibly under a new name) becomes current again
    store.add_document(DOC, "paper", "manifest", tmp_path / "missing.pdf", "p001_back.pdf", SHA)
    files = store.conn.execute("SELECT pdf_sha256, is_current, rel_path FROM document_files ORDER BY added, rowid").fetchall()
    assert [(f[0][0], f[1], f[2]) for f in files] == [("a", 1, "p001_back.pdf"), ("b", 0, "p001_v2.pdf")]
    assert store.get_document(DOC)["rel_path"] == "p001_back.pdf" and store.get_document(DOC)["pdf_sha256"] == SHA
    assert store.conn.execute("SELECT COUNT(*) FROM document_files").fetchone()[0] == 2


def test_upsert_meta_merges_and_list_documents(store: EvidenceStore, tmp_path: Path):
    store.upsert_meta(DOC, DocMeta(venue="JBiomech", extra={"tier": "A"}))
    d = store.get_document(DOC)
    assert d["title"] == "Stem loading study" and d["venue"] == "JBiomech" and d["extra"] == {"tier": "A"}
    store.add_document("P002", "paper", "scan", tmp_path / "x.pdf", "x.pdf", "c" * 64,
                       DocMeta(title="Cup fixation", year=2019))
    r = store.list_documents()
    assert r["total"] == 2 and [x["doc_id"] for x in r["rows"]] == [DOC, "P002"]  # year desc
    assert store.list_documents(q="fixation")["rows"][0]["doc_id"] == "P002"
    assert store.list_documents(q="smith")["total"] == 1
    assert store.list_documents(q="P00")["total"] == 2  # LIKE fallback on doc_id
    assert store.list_documents(q='"unbalanced (syntax')["total"] == 0
    assert store.list_documents(limit=1, offset=1)["rows"][0]["doc_id"] == "P002"


def test_add_extraction_requires_document(store: EvidenceStore):
    with pytest.raises(KeyError):
        store.add_extraction(ext_id="e", doc_id="NOPE", pdf_sha256=SHA, extractor_version="v", doc_type="paper",
                             pages=[], masks=[], units=[], sentences=[], text_ratio=0, drop_ratio=0)


# ---- two extractions of one document ------------------------------------------------------------------------

def test_two_extractions_current_flags_and_aliases(store: EvidenceStore):
    e1 = _ingest_v1(store)
    assert store.extraction_exists(e1)
    cur = store.current_extraction(DOC)
    assert cur["ext_id"] == e1 and cur["is_current"] is True and cur["stats"] == {"n": 4}
    assert store.get_document(DOC)["n_sentences"] == 4
    assert store.stats() == {"documents": 1, "extractions": 1, "pages": 2, "sentences": 4, "body_sentences": 4}

    e2 = _ingest_v2(store)
    flags = dict(store.conn.execute("SELECT ext_id, is_current FROM extractions").fetchall())
    assert flags == {e1: 0, e2: 1}
    assert store.current_extraction(DOC)["ext_id"] == e2
    # page text is content-addressed: 3 distinct texts for 4 page rows
    assert store.conn.execute("SELECT COUNT(*) FROM page_text").fetchone()[0] == 3
    assert store.conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0] == 4
    # sentences: 4 from v1 + 1 new in v2; the shared ones keep first_ext = e1
    assert store.conn.execute("SELECT COUNT(*) FROM sentences").fetchone()[0] == 5
    shared = sentence_id(DOC, "Shorter stems load the metaphysis more.")
    assert store.conn.execute("SELECT first_ext FROM sentences WHERE sid=?", (shared,)).fetchone()[0] == e1
    assert len(store.sentences_of_extraction(e2)) == 4
    assert [s.seq for s in store.sentences_of_extraction(e1)] == [0, 1, 2, 3]
    assert store.sentences_of_extraction(e1)[0].tags == ("mech",)
    # stats count only current extractions
    assert store.stats()["sentences"] == 4 and store.stats()["extractions"] == 2

    old = sentence_id(DOC, "Stress shielding reduces periprosthetic bone density.")
    new = sentence_id(DOC, "Stress shielding reduces peri-prosthetic bone density.")
    store.add_aliases([(old, new, e1, e2, 0.98)])
    store.add_aliases([(old, new, e1, e2, 0.98)])  # OR IGNORE
    assert store.conn.execute("SELECT COUNT(*) FROM sentence_aliases").fetchone()[0] == 1

    assert store.resolve_sid(shared) == (shared, "live")
    assert store.resolve_sid(old) == (new, "aliased")
    assert store.resolve_sid("S1deadbeefdeadbeef") == ("", "missing")
    assert store.resolve_sid("") == ("", "missing")

    s = store.sentence(old, context=1)
    assert s["sid"] == new and s["requested_sid"] == old and s["status"] == "aliased"
    assert s["ext_id"] == e2 and s["page_no"] == 2 and s["section"] == "introduction" and s["is_body"] is True
    assert s["text"].startswith("Stress shielding reduces peri-prosthetic")
    assert s["before"] == ["The femoral stem transfers load to the calcar."]
    assert s["after"] == ["Shorter stems load the metaphysis more."]
    assert s["doc"] == {"title": "Stem loading study", "year": 2021, "doi": "10.1/x"}
    assert store.sentence("S1deadbeefdeadbeef") is None
    assert store.sentence(shared, context=0)["before"] == [] and store.sentence(shared)["ext_id"] == e2

    # alias chains follow to the newest sid; cycles terminate
    e3 = "e3fake"
    store.conn.execute("INSERT INTO extractions(ext_id, doc_id, pdf_sha256, extractor_version, doc_type, n_pages, "
                       "n_sentences, created, is_current) VALUES (?,?,?,?,?,0,0,'2030-01-01T00:00:00+00:00',0)",
                       (e3, DOC, SHA, "v3", "paper"))
    store.conn.execute("INSERT INTO sentences(sid, doc_id, text, first_ext) VALUES ('S1chain', ?, 'x', ?)", (DOC, e3))
    store.add_aliases([(new, "S1chain", e2, e3, 0.9), ("S1chain", old, e3, e1, 0.9)])
    assert store.resolve_sid(old) == ("S1chain", "aliased")
    # cycle S1chain → old → new → S1chain: terminates at the last unvisited sid
    assert store.resolve_sid("S1chain") == (new, "aliased")
    assert store.sentence("S1chain")["sid"] == new  # resolved through the alias, membership from e2
    store.conn.execute("INSERT INTO sentences(sid, doc_id, text, first_ext) VALUES ('S1lonely', ?, 'y', ?)", (DOC, e3))
    lonely = store.sentence("S1lonely")  # known sentence without a membership row
    assert lonely["status"] == "live" and lonely["ext_id"] is None and lonely["text"] == "y"
    assert lonely["before"] == [] and lonely["doc"]["title"] == "Stem loading study"


def test_reextract_same_ext_id_replaces_rows(store: EvidenceStore):
    e1 = _ingest_v1(store)
    _ingest_v1(store)
    assert store.conn.execute("SELECT COUNT(*) FROM extractions").fetchone()[0] == 1
    assert store.conn.execute("SELECT COUNT(*) FROM pages WHERE ext_id=?", (e1,)).fetchone()[0] == 2
    assert store.current_extraction(DOC)["ext_id"] == e1


# ---- reading -------------------------------------------------------------------------------------------------

def test_pages_and_page(store: EvidenceStore):
    assert store.pages_of(DOC) == [] and store.page(DOC, 1) is None
    e1 = _ingest_v1(store)
    assert store.pages_of(DOC) == [{"page_no": 1, "width": 595.0, "height": 842.0, "n_sentences": 2},
                                   {"page_no": 2, "width": 595.0, "height": 842.0, "n_sentences": 2}]
    p = store.page(DOC, 1)
    assert p["ext_id"] == e1 and p["text"] == PAGE1_V1 and p["width"] == 595.0
    assert p["masks"] == [{"cstart": 0, "cend": 14, "reason": "header_footer"}]
    assert [s["cstart"] for s in p["sentences"]] == sorted(s["cstart"] for s in p["sentences"])
    assert p["sentences"][0]["tags"] == ["mech"] and p["sentences"][0]["flags"] == []
    assert p["sentences"][0]["sid"] == sentence_id(DOC, "Bone remodelling is governed by strain.")
    assert PAGE1_V1[p["sentences"][1]["cstart"]:p["sentences"][1]["cend"]] == \
        "The femoral stem transfers load to the calcar."
    assert store.page(DOC, 3) is None
    assert store.page("NOPE", 1) is None


def test_phrase_search(store: EvidenceStore):
    _ingest_v1(store)
    r = store.phrase_search("femoral stem")
    assert r["total"] == 1 and r["documents"] == 1
    row = r["rows"][0]
    assert row["doc_id"] == DOC and row["page_no"] == 1 and row["title"] == "Stem loading study" and row["year"] == 2021
    assert "<mark>femoral stem</mark>" in row["snippet"]  # FTS5 snippet() merges adjacent hits
    assert row["sid"] == sentence_id(DOC, "The femoral stem transfers load to the calcar.")
    # quoting: user quotes stripped, FTS operators are not interpreted
    assert store.phrase_search('"femoral stem"')["total"] == 1
    assert store.phrase_search("femoral OR calcar")["total"] == 0
    assert store.phrase_search("stem femoral")["total"] == 0  # a phrase, not a bag of words
    assert store.phrase_search("")["total"] == 0 and store.phrase_search('""')["total"] == 0
    assert store.phrase_search("bone")["total"] == 2
    assert store.phrase_search("bone", doc_id="P999")["total"] == 0
    assert store.phrase_search("bone", limit=1)["rows"][0]["page_no"] == 1
    assert store.phrase_search("bone", limit=1, offset=1)["rows"][0]["page_no"] == 2
    # only current extractions are searched
    _ingest_v2(store)
    assert store.phrase_search("periprosthetic")["total"] == 0
    assert store.phrase_search("peri-prosthetic")["total"] == 1
    assert store.phrase_search("bone")["total"] == 2


def test_page_search(store: EvidenceStore):
    _ingest_v1(store)
    r = store.page_search("Running head")
    assert r["total"] == 1 and r["rows"][0] == {"doc_id": DOC, "page_no": 1,
                                                 "snippet": r["rows"][0]["snippet"]}
    assert "<mark>Running head</mark>" in r["rows"][0]["snippet"]
    assert store.page_search("Running head", doc_id="P999")["total"] == 0
    _ingest_v2(store)  # page 1 text shared by both extractions → still one hit
    assert store.page_search("Running head")["total"] == 1
    assert store.page_search("periprosthetic")["total"] == 0


def test_fts_follows_deletes_and_survives_vacuum_with_rebuild(store: EvidenceStore):
    e1 = _ingest_v1(store)
    gone = sentence_id(DOC, "The femoral stem transfers load to the calcar.")
    assert store.phrase_search("femoral stem")["total"] == 1
    # the AFTER DELETE triggers keep the external-content FTS tables in step with their content tables
    store.conn.execute("DELETE FROM extraction_sentences WHERE sid=?", (gone,))
    store.conn.execute("DELETE FROM sentences WHERE sid=?", (gone,))
    assert store.phrase_search("femoral stem")["total"] == 0
    assert store.conn.execute("SELECT COUNT(*) FROM sentence_fts WHERE sentence_fts MATCH '\"femoral stem\"'").fetchone()[0] == 0
    store.conn.execute("DELETE FROM pages WHERE ext_id=? AND page_no=1", (e1,))
    store.conn.execute("DELETE FROM page_text WHERE text_sha256=?", (text_sha256(PAGE1_V1),))
    assert store.page_search("Running head")["total"] == 0
    # VACUUM may renumber rowids of TEXT-keyed tables; rebuild_fts() restores a consistent index
    store.conn.execute("VACUUM")
    store.rebuild_fts()
    r = store.phrase_search("bone")
    assert r["total"] == 2 and {x["sid"] for x in r["rows"]} == {
        sentence_id(DOC, "Bone remodelling is governed by strain."),
        sentence_id(DOC, "Stress shielding reduces periprosthetic bone density.")}
    assert store.page_search("Shorter stems")["rows"][0]["page_no"] == 2


def test_text_sha_shared_between_extractions(store: EvidenceStore):
    e1 = _ingest_v1(store)
    e2 = _ingest_v2(store)
    shas = store.conn.execute("SELECT ext_id, text_sha256 FROM pages WHERE page_no=1").fetchall()
    assert {r[0] for r in shas} == {e1, e2} and len({r[1] for r in shas}) == 1
    assert shas[0][1] == text_sha256(PAGE1_V1)
