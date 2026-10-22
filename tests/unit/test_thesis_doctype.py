"""atlas.doctypes.thesis on hand-written page texts: chapter detection paths, rejection rules, kinds, sections."""
from __future__ import annotations

from atlas.core.schemas import DocMeta, PageText
from atlas.doctypes import get_doctype
from atlas.doctypes.thesis import ThesisDocType, unit_kind_of
from atlas.pipeline.stages.ingest import apply_units_override
from atlas.core.schemas import Unit

HEADER = "Running header of the thesis"


def _pages(*texts: str) -> list[PageText]:
    return [PageText(i + 1, HEADER + "\n" + t, 595.0, 842.0) for i, t in enumerate(texts)]


def _run(pages):
    dt = ThesisDocType()
    masks = dt.drop_non_body(pages)
    units = dt.segment("T9", pages, masks)
    marks = dt.label_sections("T9", pages, masks, units)
    return dt, masks, units, marks


def test_registered_and_kinds():
    assert isinstance(get_doctype("thesis"), ThesisDocType)
    assert unit_kind_of("Introduction") == "intro" and unit_kind_of("Literature Review") == "lit_review"
    assert unit_kind_of("Materials and Methods") == "methods" and unit_kind_of("Experimental results") == "results"
    assert unit_kind_of("General discussion") == "discussion" and unit_kind_of("Conclusions and future work") == "conclusion"
    assert unit_kind_of("Wear") == "other" and unit_kind_of(None) == "other"


def test_chapter_regex_with_title_on_the_next_line_and_in_text_mentions_rejected():
    pages = _pages(
        "Thesis title here\nAuthor Name\nA thesis submitted for the degree of Doctor of Philosophy\n",
        "Abstract\nThis thesis studies micromotion. It is short.\n",
        "Contents\nChapter 1 Introduction ........ 1\n1.1 Motivation ....... 2\nChapter 2 Methods ........ 3\nBibliography ....... 9\n",
        "Chapter 1\nIntroduction\nSome text. Chapter 2 introduces the methods used here.\n1.1 Motivation\nMore text.\n",
        "Continued text of the introduction.\n",
        "Chapter 2\nMethods\nMethods text here.\n2.1 Specimens\nSpecimen text.\nReferences\n[1] Smith J. A paper. J Biomech. 2019;1:1-2.\n",
        "Bibliography\n[1] Smith J. A paper. J Biomech. 2019;1:1-2.\n",
    )
    dt, masks, units, marks = _run(pages)
    ch = [u for u in units if u.kind == "chapter"]
    assert [(u.ordinal, u.heading, u.page_from, u.page_to, u.unit_kind) for u in ch] == \
           [(1, "Introduction", 4, 5, "intro"), (2, "Methods", 6, 6, "methods"), (3, "Bibliography", 7, 7, "other")]
    assert "chapter_regex" in units[0].reasons and "toc_matched" in units[0].reasons
    assert all(u.confidence >= 0.7 for u in ch)
    assert {m.reason for m in masks} >= {"front_matter", "toc", "header_footer"}
    assert not any(m.page_no == 2 and m.reason == "front_matter" and m.cstart > 10 for m in masks)   # abstract kept
    secs = [(m.page_no, m.section) for m in marks]
    assert (1, "front") == secs[0] and (2, "abstract") in secs and (4, "other") in secs and (4, "introduction") in secs
    assert (6, "methods") in secs and (6, "references") in secs and (7, "references") in secs
    tf = dt.topic_fields(units[0], pages, marks, DocMeta())
    assert tf["title"] and "Thesis title here" in tf["title"][0] and tf["abstract"] == ["This thesis studies micromotion. It is short."]
    tf1 = dt.topic_fields(ch[0], pages, marks, DocMeta())
    assert tf1["title"] == ["Introduction"] and "Some text." in tf1["abstract"][0]


def test_numbered_fallback_requires_heading_like_lines_on_distinct_pages():
    pages = _pages(
        "Title page\n",
        "1 Introduction\nText of the first chapter.\n",
        "2 Methods and Materials\nMore text.\n3 Results\n",            # 3 on the same page as 2: not a chapter
        "9 Kayani B, Konan S, et al. (2018) Iatrogenic bone cement. J Arthroplasty 33:1-2.\n",
        "3 Results\nResult text.\n",
    )
    dt, masks, units, marks = _run(pages)
    ch = [u for u in units if u.kind == "chapter"]
    assert [(u.ordinal, u.heading, u.page_from) for u in ch] == [(1, "Introduction", 2), (2, "Methods and Materials", 3), (3, "Results", 5)]
    assert "numbered_fallback" in units[0].reasons and "no_toc" in units[0].reasons
    assert all(u.confidence < 0.7 for u in ch)          # fallback detections need the user's eye


def test_no_chapters_gives_only_the_document_unit():
    pages = _pages("Title\n", "Just some prose without any structure at all.\n", "More prose here.\n")
    dt, masks, units, marks = _run(pages)
    assert len(units) == 1 and units[0].kind == "document" and units[0].page_to == 3
    assert any(m.section == "other" for m in marks)     # the text does not vanish as 'front'


def test_roman_and_word_chapter_numbers_and_sequence_gap():
    pages = _pages(
        "Title\n", "Abstract\nShort.\n",
        "CHAPTER ONE\nINTRODUCTION\nText.\n", "Text continues.\n",
        "CHAPTER TWO\nBACKGROUND\nText.\n",
        "Chapter IV: Results\nText.\n",                   # chapter three missing → sequence gap
    )
    dt, masks, units, marks = _run(pages)
    ch = [u for u in units if u.kind == "chapter"]
    assert [(u.ordinal, u.heading, u.page_from, u.page_to) for u in ch] == \
           [(1, "INTRODUCTION", 3, 4), (2, "BACKGROUND", 5, 5), (3, "Results", 6, 6)]
    assert "sequence_gap" in units[0].reasons and all(u.confidence < 0.9 for u in ch)


def test_apply_units_override():
    base = [Unit("T1", "T1", "document", 0, None, 1, 30), Unit("T1/x", "T1", "chapter", 1, "x", 5, 10, 0.4, ("numbered_fallback",), "T1", "other")]
    out = apply_units_override("T1", base, {"chapters": [{"heading": "Introduction", "page_from": 5, "page_to": 12, "unit_kind": "intro"},
                                                         {"heading": "Introduction", "page_from": 13, "page_to": 30}]}, 30)
    assert out[0].kind == "document" and len(out) == 3
    assert out[1].unit_id == "T1/introduction" and out[2].unit_id == "T1/introduction~2"
    assert out[1].confidence == 1.0 and out[1].reasons == ("override",) and out[1].unit_kind == "intro" and out[2].unit_kind == "other"
    assert apply_units_override("T1", base, None, 30) == base and apply_units_override("T1", base, {"chapters": []}, 30) == base
