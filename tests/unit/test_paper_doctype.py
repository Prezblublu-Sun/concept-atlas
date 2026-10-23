"""Unit tests for atlas.doctypes.paper on synthetic page texts (no PDFs, no I/O).

atlas.core.segment.detect_heading is implemented by another agent; these tests replace it with a tiny local
matcher so that the doctype rules are tested in isolation and deterministically.
"""
from __future__ import annotations

import re

import pytest

from atlas.core import segment
from atlas.core.schemas import DocMeta, LineMask, PageText, SectionMark
from atlas.doctypes import get_doctype
from atlas.doctypes.paper import PaperDocType

# ---- tiny heading matcher standing in for atlas.core.segment.detect_heading ----------------------------------

_NUMBERING = re.compile(r"^(?:\d+(?:\.\d+)*\.?|[IVX]+\.)\s+")
_HEADINGS = {
    "abstract": "abstract", "keywords": "keywords", "key words": "keywords",
    "introduction": "introduction", "background": "introduction",
    "methods": "methods", "materials and methods": "methods",
    "results": "results", "results and discussion": "results",
    "discussion": "discussion", "conclusion": "conclusion", "conclusions": "conclusion",
    "acknowledgements": "acknowledgements", "acknowledgments": "acknowledgements", "funding": "acknowledgements",
    "references": "references", "bibliography": "references", "appendix": "appendix",
}


def fake_detect_heading_ex(line: str) -> tuple[str, str] | None:
    s = line.strip()
    if not s or len(s) > 60 or len(s.split()) > 8 or s.endswith("."):
        return None
    s = _NUMBERING.sub("", s).rstrip(" .:").lower()
    sec = _HEADINGS.get(s)
    return (sec, "strict") if sec else None


@pytest.fixture(autouse=True)
def _patch_heading(monkeypatch):
    monkeypatch.setattr(segment, "detect_heading_ex", fake_detect_heading_ex)


# ---- helpers ---------------------------------------------------------------------------------------------------

def page(no: int, lines: list[str]) -> PageText:
    return PageText(page_no=no, text="\n".join(lines))


def span(pg: PageText, line: str) -> tuple[int, int]:
    """(cstart, cend) of a line (unique within the page) in PageText.text."""
    i = pg.text.index(line)
    assert pg.text.find(line, i + 1) < 0, f"line not unique on page {pg.page_no}: {line!r}"
    return i, i + len(line)


def off(pg: PageText, line: str) -> int:
    return span(pg, line)[0]


def blank_after(pg: PageText, line: str) -> int:
    """offset of the empty line that follows `line`"""
    i, j = span(pg, line)
    assert pg.text[j:j + 2] == "\n\n"
    return j + 1


def mask(pg: PageText, line: str, reason: str) -> LineMask:
    a, b = span(pg, line)
    return LineMask(pg.page_no, a, b, reason)


# ---- the 4-page synthetic paper ---------------------------------------------------------------------------------

ABSTRACT_1 = "Stress shielding after total hip arthroplasty remains a concern."
ABSTRACT_2 = "We applied topology optimisation to a cementless femoral stem and compared"
ABSTRACT_3 = "the strain energy density in the proximal femur with a conventional design."
KEYWORDS = "Keywords: femoral stem; topology optimisation; stress shielding"
FUNDING = "Funding: This work was supported by the Example Trust grant 123."
FIG_LINE = "Fig. 1 Overview of the Department of Mechanical Engineering workflow"
REF1 = "[1] Smith A, Jones B. Stress shielding in hip stems. J Biomech. 2012;45(2):112-120."
REF2 = "[2] Huiskes R, Weinans H. The relationship between stress shielding and bone resorption. Clin Orthop. 1992;274:124-134."
HEAD = "Journal of Hip Research"

P1 = page(1, [
    "Hip Stem Research 12 (2026) 1-8",
    "Topology Optimisation of Cementless Femoral Stems",
    "for Reduced Stress Shielding",
    "Alice Smith a,*, Bob Jones b",
    "a Department of Mechanical Engineering, University of Example, London, UK",
    "b Institute of Orthopaedics, Example Hospital, Stanmore, UK",
    "* Corresponding author. E-mail: alice@example.ac.uk",
    "Received: 3 January 2026; Accepted: 2 March 2026",
    "",
    "Abstract",
    ABSTRACT_1,
    ABSTRACT_2,
    ABSTRACT_3,
    "",
    KEYWORDS,
    "",
    FUNDING,
    FIG_LINE,
    "1. Introduction",
    "Total hip arthroplasty is a successful procedure. The stem transfers load to the femur.",
    "Page 1",
])
P2 = page(2, [
    HEAD,
    "2. Methods",
    "A finite element model of the femur was built from CT data. Loads of 2.5 kN were applied.",
    "The stem geometry was parameterised by six design variables.",
    "Page 2",
])
P3 = page(3, [
    HEAD,
    "3. Results",
    "The optimised stem reduced the peak strain energy density by 18 %. Figure 2 shows the result.",
    "4. Discussion",
    "These findings agree with Huiskes et al. (1992) who reported similar trends.",
    "Page 3",
])
P4 = page(4, [
    HEAD,
    "5. Conclusion",
    "A lighter stem reduces stress shielding.",
    "Acknowledgements",
    "The authors thank the Example Trust for support.",
    "Declaration of competing interest",
    "The authors declare no competing interests.",
    "References",
    REF1,
    REF2,
    "Page 4",
])
PAPER = [P1, P2, P3, P4]


@pytest.fixture
def dt() -> PaperDocType:
    return PaperDocType()


def test_registered_and_versioned(dt):
    assert isinstance(get_doctype("paper"), PaperDocType)
    assert dt.name == "paper" and dt.version == "paper5"
    assert dt.thresholds.max_drop_ratio == pytest.approx(0.15)


def test_masks_of_the_four_page_paper(dt):
    masks = dt.drop_non_body(PAPER)
    assert masks == [
        mask(P1, "a Department of Mechanical Engineering, University of Example, London, UK", "affiliation"),
        mask(P1, "b Institute of Orthopaedics, Example Hospital, Stanmore, UK", "affiliation"),
        mask(P1, "* Corresponding author. E-mail: alice@example.ac.uk", "pub_info"),
        mask(P1, "Received: 3 January 2026; Accepted: 2 March 2026", "pub_info"),
        mask(P1, FUNDING, "acknowledgement"),
        mask(P1, "Page 1", "header_footer"),
        mask(P2, HEAD, "header_footer"),
        mask(P2, "Page 2", "header_footer"),
        mask(P3, HEAD, "header_footer"),
        mask(P3, "Page 3", "header_footer"),
        mask(P4, HEAD, "header_footer"),
        mask(P4, "Acknowledgements", "acknowledgement"),
        mask(P4, "The authors thank the Example Trust for support.", "acknowledgement"),
        mask(P4, "Declaration of competing interest", "declaration"),
        mask(P4, "The authors declare no competing interests.", "declaration"),
        mask(P4, REF1, "residual_ref"),
        mask(P4, REF2, "residual_ref"),
        mask(P4, "Page 4", "header_footer"),
    ]


def test_masks_are_sorted_disjoint_and_index_the_page_text(dt):
    masks = dt.drop_non_body(PAPER)
    keys = [(m.page_no, m.cstart) for m in masks]
    assert keys == sorted(keys)
    by_page: dict[int, int] = {}
    texts = {p.page_no: p.text for p in PAPER}
    for m in masks:
        assert m.cstart >= by_page.get(m.page_no, 0), "overlap"
        assert m.cend > m.cstart
        piece = texts[m.page_no][m.cstart:m.cend]
        assert piece == piece.strip() and "\n" not in piece
        by_page[m.page_no] = m.cend


def test_segment_returns_one_document_unit(dt):
    masks = dt.drop_non_body(PAPER)
    units = dt.segment("D0001", PAPER, masks)
    assert len(units) == 1
    u = units[0]
    assert (u.unit_id, u.doc_id, u.kind, u.ordinal, u.heading, u.page_from, u.page_to, u.confidence,
            u.parent_unit) == ("D0001", "D0001", "document", 0, None, 1, 4, 1.0, None)


def test_section_marks_of_the_four_page_paper(dt):
    masks = dt.drop_non_body(PAPER)
    units = dt.segment("D0001", PAPER, masks)
    marks = dt.label_sections("D0001", PAPER, masks, units)
    assert marks == [
        SectionMark(1, 0, "front"),
        SectionMark(1, off(P1, "Abstract"), "abstract"),
        SectionMark(1, off(P1, KEYWORDS), "keywords"),
        SectionMark(1, blank_after(P1, KEYWORDS), "front"),
        SectionMark(1, off(P1, "1. Introduction"), "introduction"),
        SectionMark(2, off(P2, "2. Methods"), "methods"),
        SectionMark(3, off(P3, "3. Results"), "results"),
        SectionMark(3, off(P3, "4. Discussion"), "discussion"),
        SectionMark(4, off(P4, "5. Conclusion"), "conclusion"),
        SectionMark(4, off(P4, "Acknowledgements"), "acknowledgements"),
        SectionMark(4, off(P4, "References"), "references"),
    ]


def test_topic_fields_without_meta_guess_the_title(dt):
    masks = dt.drop_non_body(PAPER)
    units = dt.segment("D0001", PAPER, masks)
    marks = dt.label_sections("D0001", PAPER, masks, units)
    fields = dt.topic_fields(units[0], PAPER, marks, DocMeta())
    assert fields == {
        "title": ["Topology Optimisation of Cementless Femoral Stems for Reduced Stress Shielding"],
        "keywords": ["femoral stem; topology optimisation; stress shielding"],
        "abstract": [f"{ABSTRACT_1} {ABSTRACT_2} {ABSTRACT_3}"],
    }


def test_topic_fields_prefer_meta_and_keep_extracted_keywords_abstract(dt):
    masks = dt.drop_non_body(PAPER)
    units = dt.segment("D0001", PAPER, masks)
    marks = dt.label_sections("D0001", PAPER, masks, units)
    meta = DocMeta(title="  Meta   Title ", keywords="hip; stem", abstract="Meta abstract.")
    fields = dt.topic_fields(units[0], PAPER, marks, meta)
    assert fields == {
        "title": ["Meta Title"],
        "keywords": ["hip; stem", "femoral stem; topology optimisation; stress shielding"],
        "abstract": ["Meta abstract.", f"{ABSTRACT_1} {ABSTRACT_2} {ABSTRACT_3}"],
    }
    # a meta value equal to the extracted one is not duplicated
    meta2 = DocMeta(keywords="femoral stem; topology optimisation; stress shielding")
    assert dt.topic_fields(units[0], PAPER, marks, meta2)["keywords"] == [
        "femoral stem; topology optimisation; stress shielding"]


# ---- synthetic abstract -----------------------------------------------------------------------------------------

LONG_1 = " ".join(f"w{k}" for k in range(25))
LONG_2 = " ".join(f"w{k}" for k in range(25, 45))
SHORT_1 = " ".join(f"s{k}" for k in range(20))


def _two_page(para1: str, para2: str) -> list[PageText]:
    p1 = page(1, [
        "A Short Paper on Stem Design",
        "Carol White",
        "Department of Engineering, University of Example",
        "",
        para1,
        para2,
        "",
        "Key words: stem; design",
        "",
        "Introduction",
        "Body sentence one.",
    ])
    p2 = page(2, ["Results", "Body sentence two.", "References", "[1] White C. A paper. J Test. 2020;1:1-2."])
    return [p1, p2]


def _marks(dt, pages):
    masks = dt.drop_non_body(pages)
    units = dt.segment("D1", pages, masks)
    return dt.label_sections("D1", pages, masks, units)


def test_synthetic_abstract_from_first_long_paragraph(dt):
    pages = _two_page(LONG_1, LONG_2)
    p1, p2 = pages
    assert _marks(dt, pages) == [
        SectionMark(1, 0, "front"),
        SectionMark(1, off(p1, LONG_1), "abstract"),
        SectionMark(1, off(p1, "Key words: stem; design"), "keywords"),
        SectionMark(1, blank_after(p1, "Key words: stem; design"), "front"),
        SectionMark(1, off(p1, "Introduction"), "introduction"),
        SectionMark(2, 0, "results"),
        SectionMark(2, off(p2, "References"), "references"),
    ]


def test_no_synthetic_abstract_when_no_paragraph_reaches_40_words(dt):
    pages = _two_page(SHORT_1, "and a few more words.")
    assert [m.section for m in _marks(dt, pages)] == ["front", "keywords", "front", "introduction", "results",
                                                       "references"]


def test_masked_lines_do_not_count_towards_the_40_words(dt):
    # a 45-word affiliation paragraph before the real paragraph is masked and therefore skipped
    aff = "Department of Engineering, University of Example, " + " ".join(f"a{k}" for k in range(40))
    p1 = page(1, ["Title Of The Paper", "", aff, "", LONG_1, LONG_2, "", "Introduction", "Body."])
    p2 = page(2, ["Results", "Body.", "References"])
    marks = _marks(dt, [p1, p2])
    assert marks[1] == SectionMark(1, off(p1, LONG_1), "abstract")


ABS_L1 = "Primary stability of cementless stems is a prerequisite for bone ingrowth and depends on"
ABS_L2 = "the interface micromotion under physiological loading, which this study quantifies with a"
ABS_L3 = "validated finite element model of the implanted femur under walking and stair climbing loads"
ABS_L4 = "for two stem designs and three bone qualities, giving a design rationale for load transfer."


def test_synthetic_abstract_without_blank_lines_keeps_title_and_authors_in_front(dt):
    # review fix: PyMuPDF text has no blank lines; title + author lines must not become the 'abstract'
    p1 = page(1, ["A Very Fine Title About Stems and Their Stability", "Alice Author, Bob Author",
                  ABS_L1, ABS_L2, ABS_L3, ABS_L4, "1. Introduction", "Body sentence one here."])
    p2 = page(2, ["2. Results", "Body sentence two.", "References", "[1] White C. A paper. J Test. 2020;1:1-2."])
    marks = _marks(dt, [p1, p2])
    assert marks[:3] == [SectionMark(1, 0, "front"), SectionMark(1, off(p1, ABS_L1), "abstract"),
                         SectionMark(1, off(p1, "1. Introduction"), "introduction")]
    units = dt.segment("D1", [p1, p2], dt.drop_non_body([p1, p2]))
    fields = dt.topic_fields(units[0], [p1, p2], marks, DocMeta())
    assert fields["title"] == ["A Very Fine Title About Stems and Their Stability"]
    assert fields["abstract"] == [f"{ABS_L1} {ABS_L2} {ABS_L3} {ABS_L4}"]
    assert "Alice Author" not in fields["abstract"][0]


def test_synthetic_abstract_after_long_author_list_and_affiliations(dt):
    authors = "Alice Northwood, Dmitri Volkov, Farid Haddad and Keiko Sato"
    aff = "Department of Biomechanics, University of Northtown, Northtown, United Kingdom"
    p1 = page(1, ["Finite element analysis of primary stability in cementless femoral stems", authors, aff,
                  ABS_L1, ABS_L2, ABS_L3, ABS_L4, "Introduction", "Body."])
    marks = _marks(dt, [p1, page(2, ["Results", "R.", "References"])])
    assert marks[1] == SectionMark(1, off(p1, ABS_L1), "abstract")
    # no affiliation line between the author list and the abstract: the author-list shape still ends the run
    q1 = page(1, ["Finite element analysis of primary stability in cementless femoral stems", authors,
                  ABS_L1, ABS_L2, ABS_L3, ABS_L4, "Introduction", "Body."])
    marks = _marks(dt, [q1, page(2, ["Results", "R.", "References"])])
    assert marks[1] == SectionMark(1, off(q1, ABS_L1), "abstract")


def test_synthetic_abstract_never_starts_at_offset_zero(dt):
    p1 = page(1, [ABS_L1, ABS_L2, ABS_L3, ABS_L4, "Introduction", "Body."])
    marks = _marks(dt, [p1, page(2, ["Results", "R.", "References"])])
    assert [m.section for m in marks] == ["front", "introduction", "results", "references"]


# ---- heading sanity rules --------------------------------------------------------------------------------------

def test_references_on_page_one_is_ignored_and_later_introduction_is_ignored(dt):
    p1 = page(1, ["Title Of The Paper", "References", "Abstract", "Some abstract text here.", "Introduction",
                  "Intro text.", "Results", "Result text.", "Introduction", "Not a new section.",
                  "References", "[1] Doe J. Paper. J Test. 2020;1:1-2."])
    marks = _marks(dt, [p1])
    assert marks == [
        SectionMark(1, 0, "front"),
        SectionMark(1, off(p1, "Abstract"), "abstract"),
        SectionMark(1, p1.text.index("Introduction"), "introduction"),
        SectionMark(1, off(p1, "Results"), "results"),
        SectionMark(1, p1.text.rindex("References"), "references"),
    ]


def test_tail_sections_need_results_or_later(dt):
    p1 = page(1, ["Title Of The Paper", "Introduction", "Text.", "Acknowledgements", "Thanks.", "Conclusions",
                  "Done.", "Acknowledgements", "Thanks again.", "Appendix", "Extra."])
    marks = _marks(dt, [p1])
    assert [m.section for m in marks] == ["front", "introduction", "conclusion", "acknowledgements", "appendix"]
    assert marks[3].cstart == p1.text.rindex("Acknowledgements")


def test_keywords_block_ends_at_heading_or_page_end(dt):
    p1 = page(1, ["Title Of The Paper", "Abstract", "Text of the abstract.", "Keywords: a; b", "c; d",
                  "Introduction", "Intro."])
    marks = _marks(dt, [p1, page(2, ["Results", "R.", "References"])])
    assert marks[2:4] == [SectionMark(1, off(p1, "Keywords: a; b"), "keywords"),
                          SectionMark(1, off(p1, "Introduction"), "introduction")]
    # block runs to the end of page 1: it ends at (2, 0), where the Introduction heading takes over
    q1 = page(1, ["Title Of The Paper", "Abstract", "Text of the abstract.", "Keywords: a; b", "c; d"])
    q2 = page(2, ["Introduction", "Intro.", "Results", "R.", "References"])
    marks = _marks(dt, [q1, q2])
    assert marks[2:5] == [SectionMark(1, off(q1, "Keywords: a; b"), "keywords"),
                          SectionMark(2, 0, "introduction"),
                          SectionMark(2, off(q2, "Results"), "results")]
    # ... and without a heading there, page 2 starts as 'front' again
    q2b = page(2, ["More front matter.", "Introduction", "Intro.", "Results", "R.", "References"])
    marks = _marks(dt, [q1, q2b])
    assert marks[3:5] == [SectionMark(2, 0, "front"), SectionMark(2, off(q2b, "Introduction"), "introduction")]


def test_keywords_after_introduction_are_ignored(dt):
    p1 = page(1, ["Title Of The Paper", "Introduction", "Keywords: not here", "Text.", "Results", "R."])
    assert [m.section for m in _marks(dt, [p1])] == ["front", "introduction", "results"]


def test_first_mark_is_always_front_even_when_page_starts_with_a_heading(dt):
    p1 = page(1, ["Abstract", "Text.", "Introduction", "T.", "Results", "R."])
    marks = _marks(dt, [p1])
    assert marks[0] == SectionMark(1, 0, "front")
    assert marks[1] == SectionMark(1, 0, "abstract")


# ---- acknowledgement / declaration block rule ---------------------------------------------------------------

def test_ack_block_in_second_half_is_capped_at_60_lines(dt):
    filler = [f"Filler sentence number {k}." for k in range(80)]
    thanks = [f"Thanks to helper {k}." for k in range(70)]
    p1 = page(1, filler)
    p2 = page(2, ["Acknowledgements"] + thanks)
    masks = dt.drop_non_body([p1, p2])
    assert [m.page_no for m in masks] == [2] * 61
    assert masks[0] == mask(p2, "Acknowledgements", "acknowledgement")
    assert {m.reason for m in masks} == {"acknowledgement"}
    assert masks[-1] == mask(p2, "Thanks to helper 59.", "acknowledgement")


def test_ack_block_stops_at_body_heading_and_takes_heading_reason(dt):
    filler = [f"Filler sentence number {k}." for k in range(20)]
    p1 = page(1, filler)
    p2 = page(2, ["Data availability", "Data are available on request.", "Appendix A", "Appendix text."])
    masks = dt.drop_non_body([p1, p2])
    assert masks == [mask(p2, "Data availability", "declaration"),
                     mask(p2, "Data are available on request.", "declaration")]


def test_ack_heading_in_first_half_masks_only_its_line(dt):
    p1 = page(1, ["Author contributions", "Carol did the work.", "Introduction"] +
              [f"Body line {k}." for k in range(30)])
    masks = dt.drop_non_body([p1])
    assert masks == [mask(p1, "Author contributions", "declaration")]


def test_funding_rule_needs_uppercase_or_digit_start(dt):
    long_funding = "Funding: " + " ".join(f"grant{k}" for k in range(15))
    long_data = "Data availability: " + " ".join(f"item{k}" for k in range(15))
    assert len(long_funding) >= 80 and len(long_data) >= 80
    p1 = page(1, ["Title Of The Paper", "funding: lower case start is body text", "Funding was provided by the Trust.",
                  "2 Funding", long_funding, long_data, "Introduction", "Body."])
    masks = dt.drop_non_body([p1])
    assert masks == [mask(p1, "Funding was provided by the Trust.", "acknowledgement"),
                     mask(p1, "2 Funding", "acknowledgement"),
                     mask(p1, long_funding, "acknowledgement")]


# ---- header / footer rule -----------------------------------------------------------------------------------

W = {1: "Alpha", 2: "Beta", 3: "Gamma", 4: "Delta"}     # page-specific words (digits alone are normalised away)


def test_header_footer_needs_three_pages_and_three_occurrences(dt):
    # review fix: the docstring rule is 'at least 3 pages'; a 3-page letter gets its running header masked too
    two = [page(n, ["Running Head", f"{W[n]} body text.", f"Page {n}"]) for n in (1, 2)]
    assert dt.drop_non_body(two) == []
    three = [page(n, ["Running Head", f"{W[n]} body text.", f"Page {n}"]) for n in (1, 2, 3)]
    masks = dt.drop_non_body(three)
    assert [(m.page_no, three[m.page_no - 1].text[m.cstart:m.cend]) for m in masks] == [
        (1, "Running Head"), (1, "Page 1"), (2, "Running Head"), (2, "Page 2"), (3, "Running Head"), (3, "Page 3")]
    assert {m.reason for m in masks} == {"header_footer"}
    four = [page(n, ["Running Head" if n <= 2 else "Other", f"{W[n]} body text.", f"Page {n}"])
            for n in (1, 2, 3, 4)]
    masks = dt.drop_non_body(four)
    assert [(m.page_no, m.reason) for m in masks] == [(n, "header_footer") for n in (1, 2, 3, 4)]
    assert all(four[m.page_no - 1].text[m.cstart:m.cend] == f"Page {m.page_no}" for m in masks)


def test_header_footer_only_in_the_edge_lines(dt):
    def body(n: int) -> list[str]:
        return [f"{W[n]} sentence {k} about the stem." for k in range(8)]
    pages = [page(n, body(n)[:4] + ["Repeated Middle Line"] + body(n)[4:] + [f"Page {n}"]) for n in (1, 2, 3, 4)]
    masks = dt.drop_non_body(pages)
    assert [pages[m.page_no - 1].text[m.cstart:m.cend] for m in masks] == [f"Page {n}" for n in (1, 2, 3, 4)]


# ---- affiliation / pub_info / residual reference rules -------------------------------------------------------

def test_affiliation_only_before_introduction_and_never_on_fig_table_lines(dt):
    p1 = page(1, ["Title Of The Paper", "Department of Surgery, University of Example",
                  "Table 1 University of Example cohort", "We thank the University of Example.", "Introduction",
                  "Department of Surgery, University of Example", "Fig. 2 Institute of Orthopaedics layout"])
    masks = dt.drop_non_body([p1])
    first = p1.text.index("Department of Surgery")      # the line occurs twice; only the one before the intro
    assert masks == [LineMask(1, first, first + len("Department of Surgery, University of Example"), "affiliation")]


def test_front_ends_at_page_one_without_introduction_heading(dt):
    p1 = page(1, ["Title Of The Paper", "Department of Surgery, University of Example", "Body."])
    p2 = page(2, ["Department of Surgery, University of Example", "More body."])
    masks = dt.drop_non_body([p1, p2])
    assert masks == [mask(p1, "Department of Surgery, University of Example", "affiliation")]


def test_affiliation_rule_spares_abstract_sentences_and_stops_at_prose(dt):
    # review fix: an abstract sentence that mentions a hospital is not an affiliation line …
    sent = "Twelve femurs from the University Hospital biobank underwent CT scanning."
    p1 = page(1, ["Title Of The Paper", "Department of Surgery, University of Example", sent,
                  "Institute of Orthopaedics, Example Hospital, Stanmore, UK", "Introduction", "Body."])
    masks = dt.drop_non_body([p1])
    # … and the affiliation window closes at the first prose line, so the later institute line is body text too
    assert masks == [mask(p1, "Department of Surgery, University of Example", "affiliation")]
    # an affiliation line ending with a period is still masked when it is Title Case
    q1 = page(1, ["Title Of The Paper", "Department of Mechanical Engineering, University of Example, London, UK.",
                  "Introduction", "Body."])
    assert [m.reason for m in dt.drop_non_body([q1])] == ["affiliation"]


def test_received_accepted_dates_only_when_capitalised_with_a_date(dt):
    # review fix: 'received … 2019' inside a body sentence is not publication info
    body = "Each specimen received a porous coating between 2015 and 2019."
    p1 = page(1, ["Title Of The Paper", "Received 3 March 2019; accepted 5 May 2019",
                  "Accepted: March 3, 2019", "Received in revised form 2019-03-03", "Introduction", body,
                  "Accepted values were taken from the literature of 2019."])
    masks = dt.drop_non_body([p1])
    assert [p1.text[m.cstart:m.cend] for m in masks] == ["Received 3 March 2019; accepted 5 May 2019",
                                                         "Accepted: March 3, 2019",
                                                         "Received in revised form 2019-03-03"]
    assert {m.reason for m in masks} == {"pub_info"}


def test_pub_info_rule_has_no_bare_url_pattern(dt):
    p1 = page(1, ["Title Of The Paper", "See http://www.example.com for details.",
                  "https://doi.org/10.1000/xyz123", "© 2026 Example Publishing Ltd. All rights reserved.",
                  "Available online 3 March 2026", "Introduction", "Body."])
    masks = dt.drop_non_body([p1])
    assert masks == [mask(p1, "https://doi.org/10.1000/xyz123", "pub_info"),
                     mask(p1, "© 2026 Example Publishing Ltd. All rights reserved.", "pub_info"),
                     mask(p1, "Available online 3 March 2026", "pub_info")]


def test_residual_reference_lines_in_the_body(dt):
    ref = "Smith A, Jones B. Stress shielding in hip stems. J Biomech. 2012;45(2):112-120."
    short_ref = "[3] Doe J. 2012;1:1-2."
    p1 = page(1, ["Title Of The Paper", "Introduction", ref, short_ref,
                  "In 2012 the authors reported a value of 5 (see Table 2).",
                  "Huiskes et al. (1992) found that stress shielding depends on stiffness."])
    masks = dt.drop_non_body([p1])
    assert masks == [mask(p1, ref, "residual_ref")]


def test_empty_pages_and_whitespace_lines(dt):
    pages = [page(1, ["", "   ", "Title Of The Paper", "  Department of Surgery, University of Example  ", ""]),
             PageText(page_no=2, text="")]
    masks = dt.drop_non_body(pages)
    assert len(masks) == 1
    m = masks[0]
    assert pages[0].text[m.cstart:m.cend] == "Department of Surgery, University of Example"
    assert m.reason == "affiliation"
    units = dt.segment("D2", pages, masks)
    assert dt.label_sections("D2", pages, masks, units) == [SectionMark(1, 0, "front"), SectionMark(1, 0, "other")]  # no headings → whole doc is body ("other")
    assert dt.topic_fields(units[0], pages, [SectionMark(1, 0, "front")], DocMeta()) == {
        "title": ["Title Of The Paper"], "keywords": [], "abstract": []}
