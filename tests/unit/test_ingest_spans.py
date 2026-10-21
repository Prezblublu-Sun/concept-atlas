"""Unit tests for the pure page → sentences helpers of atlas.pipeline.stages.ingest (no PDFs, no database)."""
from __future__ import annotations

from atlas.core.schemas import LineMask, PageText, SectionMark
from atlas.pipeline.stages.ingest import build_sentences, clean_page_text, page_sentence_spans


def texts(spans):
    return [t for _, _, t in spans]


def test_heading_at_offset_zero_of_a_page_is_its_own_sentence():
    # review fix: a section mark with cstart == 0 (page without a running header) was not a hard boundary, so
    # '3. Results' merged into the first sentence of its section and lost its numbering
    page = PageText(2, "3. Results\nThe peak was high under load. More text here.\n")
    marks = [SectionMark(1, 0, "front"), SectionMark(2, 0, "results")]
    spans = page_sentence_spans(page, [], marks)
    assert texts(spans) == ["3. Results", "The peak was high under load.", "More text here."]
    assert spans[0][:2] == (0, len("3. Results"))
    rows = build_sentences("D1", "e1", [PageText(1, "Title line\nAbstract text here."), page], [], [], marks)
    assert [(r.page_no, r.text, r.section) for r in rows if r.page_no == 2] == [
        (2, "3. Results", "results"), (2, "The peak was high under load.", "results"), (2, "More text here.", "results")]


def test_keywords_block_ending_at_page_start_then_introduction_heading():
    p2 = PageText(2, "1. Introduction\nIntro sentence here. Second one.\n")
    marks = [SectionMark(1, 0, "front"), SectionMark(1, 20, "keywords"), SectionMark(2, 0, "introduction")]
    assert texts(page_sentence_spans(p2, [], marks)) == ["1. Introduction", "Intro sentence here.", "Second one."]
    # a 'front' mark at (2, 0) (keywords block ends at the page end, no heading there) is not a heading: the first
    # line is split like ordinary text
    marks = [SectionMark(1, 0, "front"), SectionMark(1, 20, "keywords"), SectionMark(2, 0, "front")]
    p2b = PageText(2, "More front matter\nthat wraps. Then a sentence.\n")
    assert texts(page_sentence_spans(p2b, [], marks)) == ["More front matter\nthat wraps.", "Then a sentence."]


def test_heading_mid_page_and_masked_lines():
    text = "Header line\nFirst sentence. 2. Methods\nWe did things. Next."
    page = PageText(3, text)
    masks = [LineMask(3, 0, len("Header line"), "header_footer")]
    marks = [SectionMark(1, 0, "front"), SectionMark(3, text.index("2. Methods"), "methods")]
    spans = page_sentence_spans(page, masks, marks)
    assert texts(spans) == ["First sentence.", "2. Methods", "We did things.", "Next."]
    cleaned, to_orig, to_clean = clean_page_text(text, masks)
    assert cleaned == text[len("Header line"):]
    assert all(text[a:b].replace("Header line", "").strip().startswith(t.split()[0]) for a, b, t in spans)
