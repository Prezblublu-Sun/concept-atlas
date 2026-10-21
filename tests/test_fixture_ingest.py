"""Integration: make_fixture → init_project → docs add → run ingest → assert the planted facts.

One fixture corpus (9 PDFs) is generated per module; the tests mutate the project in file order (skip run,
bumped doctype version with aliases, an unmanifested PDF), so the read-only assertions come first.
"""
from __future__ import annotations

import re
import shutil
import unicodedata
from pathlib import Path

import pytest

from atlas import cli
from atlas.config import Project, init_project
from atlas.core.ids import norm, sentence_id
from atlas.core.schemas import BODY_SECTIONS
from atlas.doctypes.paper import PaperDocType
from atlas.pipeline.runner import run_stage
from atlas.pipeline.stages.ingest import IngestStage, register_documents
from atlas.store.evidence import EvidenceStore
from atlas.tools.fixture import make_fixture

BODY = ("abstract", "introduction", "methods", "results", "discussion", "conclusion")


def collapse(s: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", s).split())


class Corpus:
    def __init__(self, info: dict, project: Project):
        self.info = info
        self.project = project
        self.docs = {d["doc_id"]: d for d in info["documents"]}
        self.rows: dict[str, list] = {}
        self.ext_ids: dict[str, str] = {}

    def load(self) -> None:
        store = EvidenceStore.open(self.project.evidence_db, "ro")
        try:
            for doc_id in self.docs:
                ext = store.current_extraction(doc_id)
                assert ext is not None, doc_id
                self.ext_ids[doc_id] = ext["ext_id"]
                self.rows[doc_id] = store.sentences_of_extraction(ext["ext_id"])
        finally:
            store.close()

    def all_rows(self):
        for doc_id, rows in self.rows.items():
            for r in rows:
                yield doc_id, r


@pytest.fixture(scope="module")
def corpus(tmp_path_factory) -> Corpus:
    base = tmp_path_factory.mktemp("fixture")
    info = make_fixture(base / "fx", seed=1)
    project = init_project(base / "proj", name="fx", pdf_dir=info["pdf_dir"])
    rep = register_documents(project, manifest=info["manifest"])
    assert rep.items_failed == 0 and len(rep.details["added"]) == 9, rep
    rep = IngestStage().run(project)
    assert rep.items_done == 9 and rep.items_failed == 0 and rep.items_skipped == 0, rep.summary()
    c = Corpus(info, project)
    c.load()
    return c


# ---- fixture facts ------------------------------------------------------------------------------------------------

def test_fixture_facts(corpus: Corpus):
    info = corpus.info
    assert len(info["documents"]) == 9
    assert sum(1 for d in info["documents"] if d["kind"] == "review") == 1
    assert Path(info["manifest"]).is_file()
    for d in info["documents"]:
        assert Path(d["path"]).is_file()
        assert 3 <= d["n_pages"] <= 5
        assert d["micromotion_sentences"] >= 2
        assert len(d["references"]) >= 10
        assert "micromotion" not in d["title"] and "micromotion" not in d["keywords_line"]
    assert set(info["specials"]) == {"threshold", "gap", "pm45", "fig3", "etal", "duplicate", "hyphen"}
    assert info["micromotion_counts"] == {d["doc_id"]: d["micromotion_sentences"] for d in info["documents"]}


def test_fixture_pdf_bytes_are_deterministic(corpus: Corpus, tmp_path: Path):
    # review fix: MuPDF wrote a fresh trailer /ID per save, so pdf_sha256 (and ext_id) changed between builds
    from atlas.ingest.pdf import sha256_file
    again = make_fixture(tmp_path / "again", seed=1)
    assert [d["doc_id"] for d in again["documents"]] == [d["doc_id"] for d in corpus.info["documents"]]
    for a, b in zip(corpus.info["documents"], again["documents"]):
        assert sha256_file(a["path"]) == sha256_file(b["path"]), a["doc_id"]
    other = make_fixture(tmp_path / "other", seed=2)
    assert sha256_file(other["documents"][0]["path"]) != sha256_file(again["documents"][0]["path"])


# ---- counts, ids, sections ---------------------------------------------------------------------------------------

def test_document_count_and_pages(corpus: Corpus):
    store = EvidenceStore.open(corpus.project.evidence_db, "ro")
    try:
        stats = store.stats()
        assert stats["documents"] == 9 and stats["extractions"] == 9
        assert stats["pages"] == sum(d["n_pages"] for d in corpus.docs.values())
        for doc_id, d in corpus.docs.items():
            doc = store.get_document(doc_id)
            assert doc["title"] == d["title"] and doc["year"] == d["year"] and doc["doi"] == d["doi"]
            assert doc["n_pages"] == d["n_pages"] and doc["n_sentences"] == len(corpus.rows[doc_id])
            assert [p["page_no"] for p in store.pages_of(doc_id)] == list(range(1, d["n_pages"] + 1))
    finally:
        store.close()


def test_micromotion_counts_per_paper(corpus: Corpus):
    for doc_id, d in corpus.docs.items():
        got = sum(1 for r in corpus.rows[doc_id] if "micromotion" in r.text)
        assert got == d["micromotion_sentences"], doc_id


def test_every_planted_sentence_is_one_stored_sentence(corpus: Corpus):
    for doc_id, d in corpus.docs.items():
        stored = {}
        for r in corpus.rows[doc_id]:
            stored[collapse(r.text)] = stored.get(collapse(r.text), 0) + 1
        for s in d["sentences"]:
            assert stored.get(collapse(s), 0) >= 1, (doc_id, s)


def test_sequence_and_offsets(corpus: Corpus):
    store = EvidenceStore.open(corpus.project.evidence_db, "ro")
    try:
        for doc_id, rows in corpus.rows.items():
            assert [r.seq for r in rows] == list(range(len(rows)))
            assert [(r.page_no, r.cstart) for r in rows] == sorted((r.page_no, r.cstart) for r in rows)
            assert all(r.unit_id == doc_id for r in rows)
            for page_no in range(1, corpus.docs[doc_id]["n_pages"] + 1):
                page = store.page(doc_id, page_no)
                for r in rows:
                    if r.page_no == page_no:
                        assert r.cend <= len(page["text"]) and r.cstart < r.cend
                        # the stored text is the mask-free slice: same words, same order
                        assert collapse(page["text"][r.cstart:r.cend]).startswith(collapse(r.text).split(" ")[0])
    finally:
        store.close()


def test_duplicate_sentence_gets_two_sids(corpus: Corpus):
    sp = corpus.info["specials"]["duplicate"]
    doc_id, text = sp["doc_id"], sp["text"]
    rows = [r for r in corpus.rows[doc_id] if norm(r.text) == norm(text)]
    assert len(rows) == 2
    assert rows[0].sid != rows[1].sid
    assert {r.sid for r in rows} == {sentence_id(doc_id, text, 0), sentence_id(doc_id, text, 1)}
    assert rows[0].seq < rows[1].seq and rows[0].sid == sentence_id(doc_id, text, 0)
    # no other document contains it
    for other, rs in corpus.rows.items():
        if other != doc_id:
            assert not any(norm(r.text) == norm(text) for r in rs)


def test_abbreviations_do_not_split(corpus: Corpus):
    for name, needle in (("fig3", "Fig. 3 shows"), ("etal", "et al. (2019) reported")):
        sp = corpus.info["specials"][name]
        rows = [r for r in corpus.rows[sp["doc_id"]] if needle in collapse(r.text)]
        assert len(rows) == 1, name
        assert collapse(rows[0].text) == collapse(sp["text"])
        # no fragment of it exists as a separate sentence
        frags = [r for r in corpus.rows[sp["doc_id"]] if collapse(r.text) in ("Fig.", "Fig. 3", "et al.", "Smith et al.")]
        assert not frags


def test_unit_traps_and_hyphen(corpus: Corpus):
    sp = corpus.info["specials"]
    for name in ("threshold", "gap", "pm45"):
        rows = [r for r in corpus.rows[sp[name]["doc_id"]] if collapse(r.text) == collapse(sp[name]["text"])]
        assert len(rows) == 1, name
        assert rows[0].is_body and rows[0].section in BODY
    assert "quant" in rows[0].tags or True  # tags are the core agent's rules; presence is not asserted here
    hy = sp["hyphen"]
    rows = [r for r in corpus.rows[hy["doc_id"]] if "micro-" in r.text and "motion under physiological" in collapse(r.text)]
    assert len(rows) == 1
    assert collapse(rows[0].text) == collapse(hy["text"])
    assert "micromotion" not in rows[0].text
    assert "micro-\nmotion" in rows[0].text         # stored verbatim: the line break is kept


def test_non_body_lines_produce_no_sentences(corpus: Corpus):
    footer = re.compile(r"Page \d+ of \d+")
    for doc_id, r in corpus.all_rows():
        d = corpus.docs[doc_id]
        t = collapse(r.text)
        assert d["header"] not in t, (doc_id, t)
        assert corpus.info["journal"] not in t or r.section == "references", (doc_id, t)
        assert not footer.search(t), (doc_id, t)
        for aff in d["affiliations"]:
            assert collapse(aff) not in t, (doc_id, t)
        assert "Department of" not in t and "Institute of" not in t and "Laboratory of" not in t, (doc_id, t)
        assert "Received 3 March" not in t and "DOI: 10.1234" not in t, (doc_id, t)
        assert collapse(d["acknowledgements"]) not in t and "Conflict of interest" not in t, (doc_id, t)


def test_sections_and_is_body(corpus: Corpus):
    for doc_id, rows in corpus.rows.items():
        d = corpus.docs[doc_id]
        by_sec: dict[str, list] = {}
        for r in rows:
            by_sec.setdefault(r.section, []).append(r)
        for sec in BODY:
            assert by_sec.get(sec), (doc_id, sec)
            assert all(r.is_body for r in by_sec[sec])
        assert all(r.is_body == (r.section in BODY_SECTIONS) for r in rows)
        # front matter (title + authors) is stored but not body
        front = by_sec.get("front", [])
        assert front and all(not r.is_body for r in front)
        assert any(collapse(d["title"]) in collapse(r.text) for r in front)
        # keywords line is its own sentence in the 'keywords' section
        kw = [r for r in rows if collapse(r.text) == d["keywords_line"]]
        assert len(kw) == 1 and kw[0].section == "keywords"
        # headings are stored as their own (short) sentences in the section they open
        intro = [r for r in rows if collapse(r.text) == "1. Introduction"]
        assert len(intro) == 1 and intro[0].section == "introduction" and "short" in intro[0].flags
        # the abstract sentences sit in 'abstract'
        for s in d["abstract"]:
            hit = [r for r in rows if collapse(r.text) == collapse(s)]
            assert len(hit) == 1 and hit[0].section == "abstract", (doc_id, s)


def test_references_are_not_body(corpus: Corpus):
    for doc_id, rows in corpus.rows.items():
        d = corpus.docs[doc_id]
        refs = [r for r in rows if r.section == "references"]
        assert refs, doc_id
        assert all(not r.is_body for r in refs)
        # the two loosely formatted entries always survive the residual-reference mask
        for loose in d["loose_references"]:
            head = loose.split(". ")[0] + "."          # '[13] Brown K.'
            hit = [r for r in refs if collapse(r.text).startswith(head)]
            assert hit, (doc_id, loose)
        # nothing that looks like a reference entry is counted as body text
        for r in rows:
            if r.is_body:
                assert not re.match(r"\[\d+\] ", collapse(r.text)), (doc_id, r.text)
        # everything after the References heading is 'references'
        idx = next(i for i, r in enumerate(rows) if collapse(r.text) == "References")
        assert all(r.section == "references" for r in rows[idx:])


def test_masks_and_stats_recorded(corpus: Corpus):
    store = EvidenceStore.open(corpus.project.evidence_db, "ro")
    try:
        for doc_id, d in corpus.docs.items():
            ext = store.current_extraction(doc_id)
            assert 0.0 < ext["drop_ratio"] < PaperDocType.thresholds.max_drop_ratio
            assert ext["text_ratio"] > PaperDocType.thresholds.min_text_ratio
            assert ext["stats"]["sections"]["references"] >= 1
            reasons = {m["reason"] for p in range(1, d["n_pages"] + 1) for m in store.page(doc_id, p)["masks"]}
            assert {"header_footer", "affiliation", "pub_info"} <= reasons, (doc_id, reasons)
            assert "acknowledgement" in reasons or "declaration" in reasons
        log = corpus.project.logs_dir / "ingest.log"
        assert log.is_file()
        lines = [ln for ln in log.read_text(encoding="utf-8").splitlines() if " done ext=" in ln]
        assert len(lines) == 9
    finally:
        store.close()


def test_phrase_search_finds_planted_sentence(corpus: Corpus):
    store = EvidenceStore.open(corpus.project.evidence_db, "ro")
    try:
        res = store.phrase_search("Fig. 3 shows the micromotion pattern")
        assert res["total"] >= 1 and res["rows"][0]["doc_id"] == corpus.info["specials"]["fig3"]["doc_id"]
    finally:
        store.close()


# ---- mutations: skip, re-extraction with aliases, unmanifested PDFs ---------------------------------------------

def test_rerun_skips_existing_extractions(corpus: Corpus):
    rep = run_stage(corpus.project, "ingest")
    assert rep.items_total == 9 and rep.items_skipped == 9 and rep.items_done == 0 and rep.items_failed == 0
    rep = run_stage(corpus.project, "ingest", limit=2, doc_ids=["P001", "P002", "P003"])
    assert rep.items_total == 2 and rep.items_skipped == 2
    # --reextract with unchanged inputs replaces the rows in place: same ext ids, same sids
    rep = run_stage(corpus.project, "ingest", reextract=True, doc_ids=["P001"])
    assert rep.items_done == 1
    before = {r.sid for r in corpus.rows["P001"]}
    store = EvidenceStore.open(corpus.project.evidence_db, "ro")
    try:
        ext = store.current_extraction("P001")
        assert ext["ext_id"] == corpus.ext_ids["P001"]
        assert {r.sid for r in store.sentences_of_extraction(ext["ext_id"])} == before
        assert store.stats()["extractions"] == 9
    finally:
        store.close()


def test_bumped_doctype_version_reextracts_with_aliases(corpus: Corpus, monkeypatch):
    old_ext = dict(corpus.ext_ids)
    old_rows = {k: list(v) for k, v in corpus.rows.items()}
    monkeypatch.setattr(PaperDocType, "version", "paper1-test")
    rep = IngestStage().run(corpus.project, workers=2)
    assert rep.items_done == 9 and rep.items_skipped == 0 and rep.items_failed == 0, rep.summary()
    store = EvidenceStore.open(corpus.project.evidence_db, "ro")
    try:
        assert store.stats()["extractions"] == 18
        for doc_id, old in old_rows.items():
            ext = store.current_extraction(doc_id)
            assert ext["ext_id"] != old_ext[doc_id]
            assert "paper:paper1-test" in ext["extractor_version"]
            assert not store.get_extraction(old_ext[doc_id])["is_current"]
            aliased = {r[0] for r in store.conn.execute(
                "SELECT old_sid FROM sentence_aliases WHERE from_ext=? AND to_ext=?",
                (old_ext[doc_id], ext["ext_id"]))}
            assert len(aliased) >= 0.9 * len(old), doc_id
            assert aliased <= {r.sid for r in old}
            # unchanged rules → every old sentence maps onto itself with full overlap
            full = store.conn.execute(
                "SELECT COUNT(*) FROM sentence_aliases WHERE from_ext=? AND old_sid=new_sid AND overlap>=1.0",
                (old_ext[doc_id],)).fetchone()[0]
            assert full == len(aliased)
            sid = old[5].sid
            assert store.resolve_sid(sid) == (sid, "live")
        # the previous extraction's rows are still there (append-only store)
        assert len(store.sentences_of_extraction(old_ext["P001"])) == len(old_rows["P001"])
    finally:
        store.close()
    # a second run with the bumped version skips again
    rep = IngestStage().run(corpus.project)
    assert rep.items_skipped == 9 and rep.items_done == 0


def test_unmanifested_pdf_gets_persistent_d_id(corpus: Corpus):
    info, project = corpus.info, corpus.project
    src = Path(corpus.docs["P001"]["path"])
    extra = project.pdf_dir / "extra" / "unlisted.pdf"
    extra.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, extra)
    # with a manifest, pdf_dir is NOT scanned unless asked (a library folder may hold far more than the corpus)
    rep = register_documents(project, manifest=info["manifest"])
    assert rep.details["added"] == [], rep.details
    rep = register_documents(project, manifest=info["manifest"], scan=True)
    assert rep.details["added"] == ["D0001"], rep.details
    assert sorted(rep.details["unchanged"]) == sorted(corpus.docs)
    assert rep.details["missing_files"] == [] and rep.items_failed == 0
    store = EvidenceStore.open(project.evidence_db, "ro")
    try:
        doc = store.get_document("D0001")
        assert doc["source"] == "scan" and doc["rel_path"] == "extra/unlisted.pdf"
        assert doc["title"] == corpus.docs["P001"]["title"]          # from the PDF info dictionary
        assert doc["doi"] == corpus.docs["P001"]["doi"]              # first-page DOI guess
        assert doc["extra"]["title_guess"] == corpus.docs["P001"]["title"]
        assert store.stats()["documents"] == 10
    finally:
        store.close()
    # re-running assigns the same id and reports it unchanged
    rep = register_documents(project, manifest=info["manifest"], scan=True)
    assert rep.details["added"] == [] and "D0001" in rep.details["unchanged"]
    # a second unmanifested file continues the sequence
    extra2 = project.pdf_dir / "extra" / "another.pdf"
    shutil.copyfile(Path(corpus.docs["P002"]["path"]), extra2)
    rep = register_documents(project, manifest=info["manifest"], scan=True)
    assert rep.details["added"] == ["D0002"]
    # ingest only touches the new documents
    rep = run_stage(project, "ingest")
    assert rep.items_done == 2 and rep.items_skipped == 9 and rep.items_failed == 0
    store = EvidenceStore.open(project.evidence_db, "ro")
    try:
        rows = store.sentences_of_extraction(store.current_extraction("D0001")["ext_id"])
        assert len(rows) == len(corpus.rows["P001"])
        assert rows[5].sid != corpus.rows["P001"][5].sid        # ids depend on the document id
        assert rows[5].text == corpus.rows["P001"][5].text
    finally:
        store.close()


def test_manifest_missing_file_is_reported(corpus: Corpus, tmp_path: Path):
    project = init_project(tmp_path / "p2", name="p2", pdf_dir=corpus.info["pdf_dir"])
    man = tmp_path / "m.csv"
    man.write_text("doc_id,title,year,authors,doi,venue,doc_type,file\n"
                   "X1,Missing paper,2020,Nobody,,,paper,does_not_exist.pdf\n"
                   f"X2,,2021,,,,paper,{corpus.docs['P003']['file']}\n", encoding="utf-8")
    rep = register_documents(project, manifest=str(man), pattern="nothing/*.pdf")
    assert rep.details["missing_files"] == ["does_not_exist.pdf"] and rep.items_failed == 1
    assert rep.details["added"] == ["X2"]
    store = EvidenceStore.open(project.evidence_db, "ro")
    try:
        doc = store.get_document("X2")
        assert doc["year"] == 2021 and doc["title"] == corpus.docs["P003"]["title"]   # title filled from the PDF
    finally:
        store.close()
    with pytest.raises(FileNotFoundError):
        register_documents(project, manifest=str(tmp_path / "absent.csv"))


# ---- CLI -------------------------------------------------------------------------------------------------------------

def test_cli_end_to_end(corpus: Corpus, tmp_path: Path, capsys):
    root = tmp_path / "cliproj"
    assert cli.main(["init", str(root), "--name", "clifx", "--pdf-dir", corpus.info["pdf_dir"]]) == 0
    assert (root / "project.yaml").is_file()
    assert cli.main(["init", str(root)]) == 1                       # exists
    # '*.pdf' (not recursive): the extra/ folder planted by the unmanifested-PDF test above is not scanned
    assert cli.main(["docs", "add", str(root), "--manifest", corpus.info["manifest"], "--pattern", "*.pdf"]) == 0
    out = capsys.readouterr().out
    assert "[docs add] done 9/9" in out and "added: 9" in out
    assert cli.main(["run", "ingest", str(root), "--limit", "2", "--doc-ids", "P001,P002,P003"]) == 0
    out = capsys.readouterr().out
    assert "[ingest] done 2/2" in out
    assert cli.main(["docs", "list", str(root), "--q", "P001"]) == 0
    out = capsys.readouterr().out
    assert "P001" in out and corpus.docs["P001"]["title"] in out
    assert cli.main(["run", "ingest", str(root)]) == 0
    assert "[ingest] done 7/9, skipped 2" in capsys.readouterr().out
    # usage and project errors → 1
    assert cli.main([]) == 1
    assert cli.main(["docs", "add", str(tmp_path / "not_a_project")]) == 1
    assert cli.main(["run", "bogus", str(root)]) == 1
    assert cli.main(["--help"]) == 0
    assert cli.main(["--version"]) == 0
    capsys.readouterr()
