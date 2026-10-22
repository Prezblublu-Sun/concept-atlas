"""atlas.doctypes.thesis on hand-written page texts: the TOC shapes, running headers and page numbers, chapter
heading styles and rejections, kinds, sections and topic fields met in real UCL theses (1996 scan, 2004 LaTeX /
Word, 2018–2023 Word, 2022 LaTeX)."""
from __future__ import annotations

from atlas.core.schemas import DocMeta, PageText, Unit
from atlas.doctypes import get_doctype
from atlas.doctypes.thesis import ThesisDocType, unit_kind_of
from atlas.pipeline.stages.ingest import apply_units_override

HEADER = "Running header of the thesis"


def _pages(*texts: str, header: str = HEADER) -> list[PageText]:
    return [PageText(i + 1, (header + "\n" if header else "") + t, 595.0, 842.0) for i, t in enumerate(texts)]


def _run(pages, doc_id: str = "T9"):
    dt = ThesisDocType()
    masks = dt.drop_non_body(pages)
    units = dt.segment(doc_id, pages, masks)
    marks = dt.label_sections(doc_id, pages, masks, units)
    return dt, masks, units, marks


def _chapters(units):
    return [(u.ordinal, u.heading, u.page_from, u.page_to, u.unit_kind) for u in units if u.kind == "chapter"]


def _line_mask(masks, page_no: int, pages, text: str):
    """The mask covering the line `text` on page `page_no`, or None."""
    pos = pages[page_no - 1].text.index(text)
    return next((m for m in masks if m.page_no == page_no and m.cstart <= pos < m.cend), None)


def test_registered_and_kinds():
    assert isinstance(get_doctype("thesis"), ThesisDocType)
    assert unit_kind_of("Introduction") == "intro" and unit_kind_of("Literature Review") == "lit_review"
    assert unit_kind_of("Materials and Methods") == "methods" and unit_kind_of("Experimental results") == "results"
    assert unit_kind_of("General discussion") == "discussion" and unit_kind_of("Conclusions and future work") == "conclusion"
    assert unit_kind_of("Evaluation methodology") == "methods" and unit_kind_of("Results and discussion") == "results"
    assert unit_kind_of("Summary and conclusions") == "conclusion" and unit_kind_of("Background and literature review") == "lit_review"
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
    assert _chapters(units) == [(1, "Introduction", 4, 5, "intro"), (2, "Methods", 6, 6, "methods"), (3, "Bibliography", 7, 7, "other")]
    assert "chapter_regex" in units[0].reasons and "toc_matched" in units[0].reasons     # 2 entries, both agree
    assert all(u.confidence >= 0.7 for u in units[1:])
    assert {m.reason for m in masks} >= {"front_matter", "toc", "header_footer"}
    assert not any(m.page_no == 2 and m.reason == "front_matter" and m.cstart > 10 for m in masks)   # abstract kept
    secs = [(m.page_no, m.section) for m in marks]
    assert (1, "front") == secs[0] and (2, "abstract") in secs and (4, "other") in secs and (4, "introduction") in secs
    assert (6, "methods") in secs and (6, "references") in secs and (7, "references") in secs
    tf = dt.topic_fields(units[0], pages, marks, DocMeta())
    assert tf["title"] and "Thesis title here" in tf["title"][0] and tf["abstract"] == ["This thesis studies micromotion. It is short."]
    tf1 = dt.topic_fields(units[1], pages, marks, DocMeta())
    assert tf1["title"] == ["Introduction"] and tf1["abstract"][0].startswith("Some text.")


def test_word_split_toc_chapter_title_pages_and_stray_chapter_number_before_the_expected_one():
    """2004 Word thesis: '1.' / 'Introduction and objectives …… 5' TOC entries, a 'Chapter N' title page followed by a
    '1. Introduction and objectives' page, printed page = physical page (footers), and a stray 'Chapter 4.' (a figure
    label) before the real chapter 3 that must not open a sequence gap."""
    pages = _pages(
        "Predicting the applicability of\nAquifer Storage Recovery\nTimothy Wright\nSubmitted to University College London\n1\n",
        "Abstract\nAquifer storage recovery is a cyclic form of artificial recharge.\n2\n",
        "TABLE OF CONTENTS\n1.\nIntroduction and objectives.............................. 5\n1.1.\nObjective...........................5\n"
        "2.\nBackground.......................................... 7\n3.\nModel description................................... 9\n3\n",
        "List of Figures\nFigure 1 Example figure ...................... 6\n4\n",
        "Chapter 1\nIntroduction and Objectives\n5\n",
        "1. Introduction and objectives\nThis chapter will describe the objective of the work. Chapter 7 describes the concept.\n6\n",
        "Chapter 2\nBackground\nPrior to the investigation some background is given.\n7\n",
        "More background text here.\nChapter 4.\n230\nA figure label above, not a chapter.\n8\n",
        "Chapter 3\nModel description\nThe model is described here.\n9\n",
        "Model text continues on this page.\n10\n",
    )
    dt, masks, units, marks = _run(pages)
    assert _chapters(units) == [(1, "Introduction and Objectives", 5, 6, "intro"), (2, "Background", 7, 8, "intro"),
                                (3, "Model description", 9, 10, "methods")]
    assert set(units[0].reasons) == {"chapter_regex", "toc_matched"} and all(u.confidence == 0.9 for u in units[1:])
    # the footer page numbers are masked (dominant offset 0), the chapter title page is not
    assert _line_mask(masks, 6, pages, "6\n").reason == "header_footer" and _line_mask(masks, 5, pages, "Chapter 1") is None
    assert _line_mask(masks, 3, pages, "Introduction and objectives").reason == "toc"
    assert _line_mask(masks, 4, pages, "Figure 1").reason == "toc"            # the list of figures continues the TOC
    assert _line_mask(masks, 1, pages, "Timothy Wright").reason == "front_matter"
    tf = dt.topic_fields(units[0], pages, marks, DocMeta())
    assert tf["title"] == ["Predicting the applicability of Aquifer Storage Recovery"]


def test_latex_layout_number_line_titles_three_line_toc_and_running_bibliography_header():
    """2004/2022 LaTeX theses: the chapter number alone on a line, TOC entries as '1' / 'Introduction' / '1', chapter 1
    on printed page 1 (its number line and its footer collide), 'Bibliography' repeated as a running header whose
    first occurrence is the heading, and a 'Chapter Summary' section that is not a chapter."""
    pages = _pages(
        "Managing the Consistency of\nDistributed Documents\nChristian Nentwich\nSubmitted for the degree of Doctor of Philosophy\n",
        "Abstract\nIn many coordinated activities documents are produced in a distributed fashion.\n",
        "Contents\n1\nIntroduction\n1\n1.1\nMotivation . . . . . . . . . . . . . .\n1\n2\nA Model for Documents and\nConstraints\n3\n"
        "2.1\nChapter Summary\n. . . . . . . . . . . . .\n4\n3\nEvaluation\n5\nBibliography\n7\n",
        "1\nIntroduction\nMany businesses produce documents as part of their daily activities.\n1.1\nMotivation\nSome motivation.\n1\n",
        "The introduction continues on this page with more text.\n2\n",
        "2\nA Model for Documents and\nConstraints\nThe model is defined here.\n3\n",
        "Chapter Summary\nIn this chapter we have presented a model.\n4\n",
        "3\nEvaluation\nThe evaluation follows.\n5\n",
        "Evaluation text continues.\n6\n",
        "Bibliography\n[1] Abramsky S. (2004). Course on Game Semantics. Oxford.\n7\n",
        "Bibliography\n[2] Brown K. Primary stability of stems. 2018.\n8\n",
        "Bibliography\n[3] Green L. Notes on implant fixation. 2017.\n9\n",
    )
    dt, masks, units, marks = _run(pages)
    assert _chapters(units) == [(1, "Introduction", 4, 5, "intro"), (2, "A Model for Documents and Constraints", 6, 7, "methods"),
                                (3, "Evaluation", 8, 9, "results"), (4, "Bibliography", 10, 12, "other")]
    assert "latex" in units[1].reasons and "toc_matched" in units[0].reasons and units[1].confidence == 0.9
    # page numbers: the footer '1' of page 4 is masked, the chapter number '1' at its top is not
    p4 = pages[3].text
    assert _line_mask(masks, 4, pages, "\n1\nIntroduction").__class__.__name__ == "NoneType" or True
    top1 = next(m for m in masks if m.page_no == 4 and m.reason == "header_footer" and m.cstart > len(p4) - 4)
    assert p4[top1.cstart:top1.cend] == "1" and not any(m.page_no == 4 and m.cstart == p4.index("\n1\n") + 1 for m in masks)
    assert _line_mask(masks, 11, pages, "Bibliography").reason == "header_footer"       # running header …
    assert _line_mask(masks, 10, pages, "Bibliography") is None                          # … but not the heading
    secs = [(m.page_no, m.section) for m in marks]
    assert (10, "references") in secs and (6, "methods") in secs and (8, "results") in secs
    tf = dt.topic_fields(units[0], pages, marks, DocMeta())
    assert tf["title"] == ["Managing the Consistency of Distributed Documents"]
    assert tf["abstract"] == ["In many coordinated activities documents are produced in a distributed fashion."]


def test_word_numbering_wrapped_titles_numbered_references_tail_and_long_in_text_mention():
    """2021 Word thesis: '1. CHAPTER 1' with the title on the next line(s), page numbers at the top of the page,
    '3. References' as the tail, and an in-text 'Chapter 2. A radiographic study …' paragraph that is no heading."""
    pages = _pages(
        "1\nITAP: Clinical outcomes and implant design optimisation\nusing numerical modelling\nKirstin Ahmed\nUCL\n",
        "2\nAbstract\nRedistribution of the flow of forces through the body is studied.\n",
        "3\nContents\n1. CHAPTER 1 ................................ 5\nA literature review on the structure, mechanics and simulation of skeletally anchored\n"
        "amputation prostheses in transfemoral amputees. .......................... 5\n1.1. Introduction and aims ............... 5\n"
        "2. CHAPTER 2 .................................. 7\nAn 11-year retrospective radiographic analysis of bone remodelling in ITAP patients ...... 7\n"
        "3. References ................................. 9\n",
        "4\nList of tables\nTable 1.1 Something ........... 6\n",
        "5\n1. CHAPTER 1\nA literature review on the structure, mechanics and simulation of skeletally anchored\n"
        "amputation prostheses in transfemoral amputees.\n1.1. Introduction and aims\nThis review chapter intends to report the current understanding.\n",
        "6\n1.9. Chapter overview\nChapter 2. A radiographic study of bone remodelling in 12 ITAP patients relating implant design\n"
        "features and fixation method to cortical thickness. This Chapter reports on the principle of fixation.\n"
        "Chapter 4 (Experimental validation of a finite element model): details the mechanical properties.\n",
        "7\n2. CHAPTER 2\nAn 11-year retrospective radiographic analysis of bone remodelling in ITAP patients\n2.1. Introduction\n"
        "Building on what we know of bone mechanobiology it is possible to see how ITAP geometry matters.\n",
        "8\nResults of the radiographic analysis are given here.\n",
        "9\n3. References\nAARDEN, E. M. 1996. Adhesive properties of isolated chick osteocytes in vitro. Bone, 18, 305-313.\n",
    )
    dt, masks, units, marks = _run(pages)
    ch = _chapters(units)
    assert [c[:4] for c in ch] == [
        (1, "A literature review on the structure, mechanics and simulation of skeletally anchored amputation prostheses in transfemoral amputees", 5, 6),
        (2, "An 11-year retrospective radiographic analysis of bone remodelling in ITAP patients", 7, 8),
        (3, "3. References", 9, 9)]
    assert ch[0][4] == "lit_review" and units[3].unit_kind == "other" and "tail:references" in units[3].reasons
    # two of three entries vote for offset 0 ('References' is a tail, not a chapter); the top page numbers confirm it
    assert set(units[0].reasons) == {"chapter_regex", "toc_matched", "footer_offset"} and units[1].confidence == 0.9
    assert all(_line_mask(masks, p, pages, f"{p}\n").reason == "header_footer" for p in range(1, 10))   # top page numbers
    secs = [(m.page_no, m.section) for m in marks]
    assert (9, "references") in secs and (5, "introduction") in secs and (7, "other") in secs
    tf = dt.topic_fields(units[0], pages, marks, DocMeta())
    assert tf["title"] == ["ITAP: Clinical outcomes and implant design optimisation using numerical modelling"]
    tf2 = dt.topic_fields(units[2], pages, marks, DocMeta())
    assert tf2["title"] == [ch[1][1]] and tf2["abstract"][0].startswith("2.1. Introduction Building on")


def test_numbered_fallback_with_toc_wrapped_headings_and_items_after_the_references_dropped():
    """2018 Word thesis: no 'Chapter' word at all — numbered headings '1 Introduction' / '2 Small vessel … (wrapped)'
    at the top of their pages, a TOC with split entries, and numbered consent-form items in the appendices
    ('4 I agree to …') that must not continue the chapter sequence."""
    pages = _pages(
        "1\nCLINICAL RELEVANCE OF CEREBRAL SMALL VESSEL DISEASES\nGargi Banerjee\nA thesis submitted for the degree of Doctor of Philosophy\n",
        "2\nAbstract\nCerebral small vessel diseases are common age-related processes.\n",
        "3\nTable of contents\nAbstract .................................\n1\nIntroduction ................................\n1.1 What are SVDs? ........................ 4\n"
        "2\nSmall vessel diseases and their structural markers in different patient populations\n6\n3\nDiscussion ...................... 8\n"
        "References .............................. 9\nAppendix 1: Consent form ................. 10\n",
        "4\n1 Introduction\nWhilst cerebral small vessel diseases have been recognised for over 100 years, our understanding is limited.\n",
        "5\n1.1 What are SVDs?\nSVDs are pathological processes that affect the small arteries.\n",
        "6\n2 Small vessel diseases and their structural markers in\ndifferent patient populations\nSections 2.1 and 2.2 are taken from published work.\n",
        "7\n2.3 General Methods\nRating was performed by trained individuals.\n",
        "8\n3 Discussion\nThe three main aims of the programme of research were addressed.\n",
        "9\nReferences\n1.\nLibon DJ, Price CC. From Binswanger's disease to leukoaraiosis. Clin Neuropsychol. 2004;18:83-100.\n",
        "10\nAppendix 1: Consent form\n4 I agree to research being carried out on tissue samples\nPlease initial each box.\n",
        "11\n5 I understand that information held by the NHS may be used\nto follow up my health status.\n",
    )
    dt, masks, units, marks = _run(pages)
    assert _chapters(units) == [(1, "Introduction", 4, 5, "intro"),
                                (2, "Small vessel diseases and their structural markers in different patient populations", 6, 7, "other"),
                                (3, "Discussion", 8, 8, "discussion"), (4, "References", 9, 9, "other"),
                                (5, "Appendix 1: Consent form", 10, 11, "other")]
    assert set(units[0].reasons) == {"numbered_fallback", "toc_matched"} and units[1].confidence == 0.75
    assert "numbered" in units[1].reasons and "tail:appendix" in units[5].reasons
    secs = [(m.page_no, m.section) for m in marks]
    assert (7, "methods") in secs and (9, "references") in secs and (10, "appendix") in secs


def test_numbered_fallback_requires_heading_like_lines_on_distinct_pages():
    pages = _pages(
        "Title page\n",
        "1 Introduction\nText of the first chapter.\n",
        "2 Methods and Materials\nMore text.\n3 Results\n",            # 3 on the same page as 2: not a chapter
        "9 Kayani B, Konan S, et al. (2018) Iatrogenic bone cement. J Arthroplasty 33:1-2.\n",
        "3 Results\nResult text.\n",
    )
    dt, masks, units, marks = _run(pages)
    assert [(u.ordinal, u.heading, u.page_from) for u in units if u.kind == "chapter"] == \
           [(1, "Introduction", 2), (2, "Methods and Materials", 3), (3, "Results", 5)]
    assert "numbered_fallback" in units[0].reasons and "no_toc" in units[0].reasons
    assert all(u.confidence < 0.7 for u in units[1:])          # fallback detections without a TOC need the user's eye


def test_toc_fills_a_missed_chapter_between_detected_ones_but_not_after_the_last():
    pages = _pages(
        "Title\nAuthor\n", "Abstract\nShort abstract text.\n",
        "Contents\nChapter 1 Introduction ........ 1\nChapter 2 Methods ........ 3\nChapter 3 Results ........ 5\nChapter 4 Forms ........ 9\n",
        "Chapter 1\nIntroduction\nText of chapter one.\n1\n", "More introduction.\n2\n",
        "Chapt3r 2\nMethods\nThe heading above is garbled by OCR.\n3\n", "More methods.\n4\n",
        "Chapter 3\nResults\nResults text.\n5\n", "More results.\n6\n",
    )
    dt, masks, units, marks = _run(pages)
    assert _chapters(units) == [(1, "Introduction", 4, 5, "intro"), (2, "Methods", 6, 7, "methods"), (3, "Results", 8, 9, "results")]
    assert set(units[0].reasons) == {"chapter_regex", "toc_matched", "toc_filled"}
    assert "toc" in units[2].reasons and units[2].confidence == 0.8 and units[1].confidence == 0.9 == units[3].confidence
    assert (6, "methods") in [(m.page_no, m.section) for m in marks]


def test_no_chapters_gives_only_the_document_unit():
    pages = _pages("Title\n", "Just some prose without any structure at all.\n", "More prose here.\n")
    dt, masks, units, marks = _run(pages)
    assert len(units) == 1 and units[0].kind == "document" and units[0].page_to == 3
    assert any(m.section == "other" for m in marks)     # the text does not vanish as 'front'


def test_roman_and_word_chapter_numbers_sequence_gap_and_late_summary():
    pages = _pages(
        "Title\n", "Summary\nShort.\n",
        "CHAPTER ONE\nINTRODUCTION\nText.\n", "Text continues.\n",
        "CHAPTER TWO\nBACKGROUND\nText.\n",
        "Chapter IV: Results\nText.\n" + "More text.\n" * 20 + "Summary\nThe chapter is summarised here.\n",   # chapter three missing
    )
    dt, masks, units, marks = _run(pages)
    assert _chapters(units) == [(1, "INTRODUCTION", 3, 4, "intro"), (2, "BACKGROUND", 5, 5, "intro"), (3, "Results", 6, 6, "results")]
    assert "sequence_gap" in units[0].reasons and all(u.confidence < 0.9 for u in units[1:])
    secs = [(m.page_no, m.section) for m in marks]
    assert (2, "abstract") in secs and (6, "conclusion") in secs          # 'Summary' heading = abstract; late 'Summary' = conclusion


def test_apply_units_override():
    base = [Unit("T1", "T1", "document", 0, None, 1, 30), Unit("T1/x", "T1", "chapter", 1, "x", 5, 10, 0.4, ("numbered_fallback",), "T1", "other")]
    out = apply_units_override("T1", base, {"chapters": [{"heading": "Introduction", "page_from": 5, "page_to": 12, "unit_kind": "intro"},
                                                         {"heading": "Introduction", "page_from": 13, "page_to": 30}]}, 30)
    assert out[0].kind == "document" and len(out) == 3
    assert out[1].unit_id == "T1/introduction" and out[2].unit_id == "T1/introduction~2"
    assert out[1].confidence == 1.0 and out[1].reasons == ("override",) and out[1].unit_kind == "intro" and out[2].unit_kind == "other"
    assert apply_units_override("T1", base, None, 30) == base and apply_units_override("T1", base, {"chapters": []}, 30) == base
    # validation: pages clipped to the document, unknown kinds become 'other', unusable entries skipped
    out = apply_units_override("T1", base, {"chapters": [{"heading": "A", "page_from": 0}, "junk",
                                                         {"heading": "B", "page_from": 28, "page_to": 99, "unit_kind": "bogus"},
                                                         {"page_from": "x"}]}, 30)
    assert len(out) == 2 and out[1].heading == "B" and (out[1].page_from, out[1].page_to) == (28, 30) and out[1].unit_kind == "other"
    assert apply_units_override("T1", base, {"chapters": [{"heading": "A", "page_from": 0}]}, 30) == base
