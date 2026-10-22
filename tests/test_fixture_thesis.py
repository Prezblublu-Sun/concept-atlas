"""Integration: the thesis fixture (3 synthetic theses in three layouts) → `atlas fixture make --kind thesis` facts →
ingest with the thesis doctype → units, sections, masks, topic fields, chapter drafts and `curated/units_override.yaml`."""
from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest
import yaml

from atlas import cli
from atlas.config import init_project
from atlas.core.schemas import DocMeta
from atlas.doctypes.thesis import ThesisDocType
from atlas.ingest.pdf import extract_pages
from atlas.pipeline.runner import run_stage
from atlas.pipeline.stages.ingest import register_documents
from atlas.store.evidence import EvidenceStore
from atlas.tools.fixture import THESIS_LAYOUTS, make_fixture

DEFAULT_SECTION = {"intro": "introduction", "lit_review": "introduction", "methods": "methods", "results": "results",
                   "discussion": "discussion", "conclusion": "conclusion"}


@pytest.fixture(scope="module")
def theses(tmp_path_factory):
    base = tmp_path_factory.mktemp("thesis fixture")
    info = make_fixture(base / "fx", 1, kind="thesis")
    project = init_project(base / "proj", name="theses", doc_type="thesis", pdf_dir=str(base / "fx" / "pdf"))
    shutil.copy(info["manifest"], project.manifest_path)
    assert register_documents(project).items_failed == 0
    rep = run_stage(project, "ingest")
    assert rep.items_failed == 0 and rep.items_done == 3
    return {"info": info, "project": project, "report": rep, "base": base}


def test_fixture_facts(theses):
    info = theses["info"]
    assert info["kind"] == "thesis" and len(info["documents"]) == 3
    assert [d["layout"] for d in info["documents"]] == list(THESIS_LAYOUTS)
    for d in info["documents"]:
        assert d["doc_type"] == "thesis" and d["n_front_pages"] >= 5 and 4 <= len(d["chapters"]) <= 6
        assert d["chapters"][0]["page_from"] == d["n_front_pages"] + 1
        assert d["chapters"][0]["printed_from"] == (d["n_front_pages"] + 1 if d["layout"] == "word_numbered" else 1)
        assert d["tail"][0]["kind"] == "references" and d["tail"][1]["heading"] == "Appendix A"
        assert d["micromotion_sentences"] == 2 * (len(d["chapters"]) - 1)
        assert Path(d["path"]).is_file() and d["n_pages"] == d["tail"][1]["page_to"]
        assert 1 < d["abstract_page"] <= d["n_front_pages"] and 1 < d["toc_page"] <= d["n_front_pages"]
    assert info["documents"][1]["toc_page"] < info["documents"][1]["abstract_page"]      # TOC before the abstract
    assert [d["per_chapter_references"] for d in info["documents"]] == [True, False, False]


def test_fixture_is_deterministic(theses, tmp_path):
    info2 = make_fixture(tmp_path / "again", 1, kind="thesis")
    for a, b in zip(theses["info"]["documents"], info2["documents"]):
        assert Path(a["path"]).read_bytes() == Path(b["path"]).read_bytes()
        assert a["chapters"] == b["chapters"]


def test_doctype_units_sections_masks_and_topic_fields(theses):
    dt = ThesisDocType()
    assert dt.version == "thesis1" and dt.thresholds.max_drop_ratio == 0.30 and dt.thresholds.min_chapter_confidence == 0.7
    for d in theses["info"]["documents"]:
        pages = extract_pages(d["path"])
        masks = dt.drop_non_body(pages)
        units = dt.segment(d["doc_id"], pages, masks)
        marks = dt.label_sections(d["doc_id"], pages, masks, units)
        # units
        assert units[0].kind == "document" and units[0].unit_id == d["doc_id"]
        assert set(units[0].reasons) == {"chapter_regex", "toc_matched"}, d["layout"]
        chapters = [u for u in units if u.kind == "chapter"]
        assert [(u.ordinal, u.page_from, u.page_to, u.unit_kind) for u in chapters[:len(d["chapters"])]] == \
               [(c["number"], c["page_from"], c["page_to"], c["unit_kind"]) for c in d["chapters"]]
        assert [u.heading for u in chapters[:len(d["chapters"])]] == [c["heading"] for c in d["chapters"]]
        assert all(u.confidence == 0.9 and u.parent_unit == d["doc_id"] for u in chapters)
        assert all(u.unit_id.startswith(d["doc_id"] + "/") for u in chapters) and len({u.unit_id for u in chapters}) == len(chapters)
        assert all(("latex" if d["layout"] == "latex" else "chapter_regex") in u.reasons for u in chapters[:len(d["chapters"])])
        tails = chapters[len(d["chapters"]):]
        assert [(u.heading, u.page_from, u.page_to, u.unit_kind) for u in tails] == \
               [(t["heading"], t["page_from"], t["page_to"], "other") for t in d["tail"]]
        # masks: running header and page numbers on every page, front matter and TOC masked, abstract kept
        reasons = {m.reason for m in masks}
        assert {"header_footer", "front_matter", "toc"} <= reasons
        n_hf = sum(1 for m in masks if m.reason == "header_footer")
        if d["layout"] == "word_numbered":    # top page numbers + the constant header outside the chapters + repeated chapter headers
            assert n_hf >= d["n_pages"] + d["n_front_pages"] + 2 * len(d["chapters"])
        else:                                 # the constant header and the footer page number on every page
            assert n_hf >= 2 * d["n_pages"]
        assert all(m.page_no <= d["n_front_pages"] for m in masks if m.reason in ("front_matter", "toc"))
        page_text = {p.page_no: p.text for p in pages}
        abstract_text = page_text[d["abstract_page"]]
        kept = [m for m in masks if m.page_no == d["abstract_page"] and m.reason == "front_matter"]
        for sent in d["abstract"]:
            pos = re.search(re.escape(sent).replace(r"\ ", r"\s+"), abstract_text).start()   # sentences wrap over lines
            assert not any(m.cstart <= pos < m.cend for m in kept), "abstract sentence masked"
        assert any(m.page_no == d["toc_page"] and m.reason == "toc" for m in masks)
        masks_sorted = sorted(masks, key=lambda m: (m.page_no, m.cstart))
        for a, b in zip(masks_sorted, masks_sorted[1:]):
            assert a.page_no < b.page_no or a.cend <= b.cstart
        if d["layout"] == "word_numbered":        # the running chapter header is masked after the chapter's first page
            second = d["chapters"][1]
            line = f"Chapter 2. {second['heading']}"
            assert second["page_to"] - second["page_from"] >= 2
            later = second["page_from"] + 1
            pos = page_text[later].index(line)
            assert any(m.page_no == later and m.reason == "header_footer" and m.cstart <= pos < m.cend for m in masks)
            first_pos = [m.start() for m in re.finditer(re.escape(line), page_text[second["page_from"]])]
            assert len(first_pos) == 2 and not all(any(m.page_no == second["page_from"] and m.cstart <= p < m.cend for m in masks)
                                                   for p in first_pos)
        # sections: first mark is front; the abstract has its own section; chapter defaults and references inside chapters
        assert (marks[0].page_no, marks[0].cstart, marks[0].section) == (1, 0, "front")
        secs = {m.section for m in marks}
        assert {"abstract", "introduction", "methods", "results", "references", "appendix", "other"} <= secs
        by_page: dict[int, list[str]] = {}
        for m in marks:
            by_page.setdefault(m.page_no, []).append(m.section)
        assert by_page[d["abstract_page"]][0] == "abstract"
        for c in d["chapters"]:
            assert "other" in by_page[c["page_from"]]            # the chapter heading block
            assert DEFAULT_SECTION[c["unit_kind"]] in by_page[c["page_from"]]
            if d["per_chapter_references"]:
                assert "references" in by_page[c["page_to"]] or "references" in by_page.get(c["page_to"] - 1, [])
        assert "references" in by_page[d["tail"][0]["page_from"]] and "appendix" in by_page[d["tail"][1]["page_from"]]
        # topic fields
        tf = dt.topic_fields(units[0], pages, marks, DocMeta(title=d["title"]))
        assert tf["title"] == [d["title"]] and tf["keywords"] == []
        assert len(tf["abstract"]) == 1 and tf["abstract"][0].startswith(d["abstract"][0]) and "micromotion" in tf["abstract"][0]
        tf0 = dt.topic_fields(units[0], pages, marks, DocMeta())
        assert tf0["title"] == [d["title"]]                                   # title guessed from the title page
        ch2 = chapters[1]
        tf2 = dt.topic_fields(ch2, pages, marks, DocMeta())
        assert tf2["title"] == [ch2.heading] and tf2["abstract"] and len(tf2["abstract"][0].split()) <= 300
        assert tf2["abstract"][0].startswith(d["chapters"][1]["sections"][0])      # the words after the heading block


def test_ingested_sentences_units_and_counts(theses):
    info, project = theses["info"], theses["project"]
    store = EvidenceStore.open(project.evidence_db, "ro")
    try:
        for d in info["documents"]:
            ext = store.current_extraction(d["doc_id"])
            assert ext is not None and ext["doc_type"] == "thesis" and "thesis:thesis1" in ext["extractor_version"]
            units = store.units_of(d["doc_id"])
            assert units[0]["kind"] == "document" and len(units) == 1 + len(d["chapters"]) + len(d["tail"])
            rows = store.sentences_of_extraction(ext["ext_id"])
            body = [r for r in rows if r.is_body]
            # every planted micromotion sentence is one body sentence inside the right chapter
            texts = {r.text: r for r in rows}
            n_mm = 0
            for s in d["mm_sentences"]:
                r = texts[s]
                assert r.is_body and r.unit_id != d["doc_id"] and r.section in ("introduction", "methods", "results", "discussion", "conclusion")
                n_mm += 1
            assert n_mm == d["micromotion_sentences"]
            # front matter yields no body sentences except the abstract; the TOC yields none; page numbers are masked
            for r in rows:
                if r.page_no <= d["n_front_pages"]:
                    assert r.section in ("front", "abstract"), (r.page_no, r.section, r.text[:40])
                    assert "....." not in r.text and ". . . ." not in r.text
                assert not re.fullmatch(r"\d{1,3}|[ivx]+", r.text), (d["layout"], r.page_no, r.text)
            assert any(r.section == "abstract" and "micromotion" in r.text for r in rows)
            assert all(r.unit_id == d["doc_id"] for r in rows if r.page_no <= d["n_front_pages"])
            # references inside chapters and the bibliography are not body
            assert all(not r.is_body for r in rows if r.text.startswith("[") and "J Biomech" in r.text)
            assert len(body) > 40
            # chapter units carry the pages: sentences of chapter 2 have its unit id
            ch2 = units[2]
            assert ch2["kind"] == "chapter" and ch2["unit_kind"] == "lit_review" and ch2["confidence"] >= 0.7
            assert all(r.unit_id == ch2["unit_id"] for r in rows if ch2["page_from"] <= r.page_no <= ch2["page_to"])
            st = ext["stats"]
            assert st["masks"]["front_matter"] > 0 and st["masks"]["toc"] > 0 and ext["drop_ratio"] <= 0.30
    finally:
        store.close()
    assert not [w for w in theses["report"].warnings if "chapter" in w]


def test_units_override_and_chapter_draft(theses, monkeypatch):
    info, project = theses["info"], theses["project"]
    d = info["documents"][0]
    # 1. a low-confidence detection writes a draft and warns
    from atlas.doctypes import thesis as thesis_mod
    orig = thesis_mod.ThesisDocType.segment

    def low(self, doc_id, pages, masks):
        units = orig(self, doc_id, pages, masks)
        for u in units:
            if u.kind == "chapter":
                u.confidence = 0.4
        return units
    monkeypatch.setattr(thesis_mod.ThesisDocType, "segment", low)
    monkeypatch.setattr(thesis_mod.ThesisDocType, "version", "thesis1-test-low")
    rep = run_stage(project, "ingest", doc_ids=[d["doc_id"]])
    assert rep.items_done == 1 and any("below confidence" in w for w in rep.warnings)
    draft = project.root / "work" / "drafts" / "chapters" / f"{d['doc_id']}.yaml"
    assert draft.is_file()
    data = yaml.safe_load(draft.read_text(encoding="utf-8"))
    assert data["doc_id"] == d["doc_id"] and len(data["chapters"]) == len(d["chapters"]) + len(d["tail"])
    assert data["chapters"][0]["heading"] == d["chapters"][0]["heading"] and data["chapters"][0]["confidence"] == 0.4
    assert data["chapters"][0]["page_from"] == d["chapters"][0]["page_from"] and "reasons" in data["chapters"][0]
    monkeypatch.undo()
    # 2. the user corrects the chapters through curated/units_override.yaml → re-extracted with confidence 1.0
    override = {d["doc_id"]: {"chapters": [{"heading": c["heading"], "page_from": c["page_from"], "page_to": c["page_to"],
                                             "unit_kind": c["unit_kind"]} for c in d["chapters"][:2]]}}
    (project.curated_dir / "units_override.yaml").write_text(yaml.safe_dump(override, allow_unicode=True), encoding="utf-8")
    rep = run_stage(project, "ingest", doc_ids=[d["doc_id"]])
    assert rep.items_done == 1
    store = EvidenceStore.open(project.evidence_db, "ro")
    try:
        ext = store.current_extraction(d["doc_id"])
        assert "ovr:-" not in ext["extractor_version"]
        units = store.units_of(d["doc_id"])
        assert len(units) == 3 and all(u["confidence"] == 1.0 and u["reasons"] == ["override"] for u in units[1:])
        assert units[1]["heading"] == d["chapters"][0]["heading"] and units[2]["unit_kind"] == d["chapters"][1]["unit_kind"]
    finally:
        store.close()
    # 3. an unreadable override file is a warning, not a failure
    (project.curated_dir / "units_override.yaml").write_text("chapters: [unclosed\n  - :", encoding="utf-8")
    rep = run_stage(project, "ingest", doc_ids=[d["doc_id"]])
    assert rep.items_failed == 0 and any("units_override.yaml" in w for w in rep.warnings)
    (project.curated_dir / "units_override.yaml").unlink()


def test_cli_fixture_kind(tmp_path, capsys):
    assert cli.main(["fixture", "make", str(tmp_path / "t"), "--kind", "thesis"]) == 0
    out = capsys.readouterr().out
    assert "3 documents" in out and (tmp_path / "t" / "manifest.csv").is_file()
