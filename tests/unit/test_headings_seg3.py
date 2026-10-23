"""seg3 heading rules: loose descriptive headings, numbered-sequence and run-in headings, structured abstracts.
The doctype tests here use the real atlas.core.segment (no fake), unlike test_paper_doctype.py."""
from __future__ import annotations

import pytest

from atlas.core.schemas import PageText, SectionMark
from atlas.core.segment import classify_heading_text, detect_heading_ex, split_numbered_heading
from atlas.doctypes.paper import PaperDocType


# ---- detect_heading_ex -----------------------------------------------------------------------------------------

@pytest.mark.parametrize("line, expected", [
    ("2. Methods", ("methods", "strict")),
    ("Proposed methodology", ("methods", "loose")),
    ("MMGP methodology", ("methods", "loose")),
    ("Kriging method (KG)", ("methods", "loose")),
    ("Experimental methods", ("methods", "loose")),
    ("Problem formulation", ("methods", "loose")),
    ("Numerical Results and Analysis", ("results", "loose")),
    ("Results and comparison", ("results", "loose")),
    ("Results of computer simulation", ("results", "loose")),
    ("Migration results", ("results", "loose")),
    ("Numerical experiments", ("results", "loose")),
    ("Case studies", ("results", "loose")),
    ("Related work", ("introduction", "loose")),
    ("Preliminaries", ("introduction", "loose")),
    ("Limitations", ("discussion", "loose")),
    ("Future work", ("conclusion", "loose")),
    ("Discussion/Conclusion", ("discussion", "strict")),
    ("Notes and references", ("references", "strict")),
    ("1. Introductions", ("introduction", "strict")),
    ("2\u2002 \x07Material and\xa0Methods", ("methods", "strict")),     # Springer: en space + BEL glyph
    ("3\u2002 \x07Results", ("results", "strict")),
    ("\u25a0INTRODUCTION", ("introduction", "strict")),                 # ACS: square bullet glued to the heading
    ("\u25a0 RESULTS AND DISCUSSION", ("results", "strict")),
    ("\u2022 Methods", ("methods", "strict")),
])
def test_detect_heading_ex_positive(line, expected):
    assert detect_heading_ex(line) == expected


@pytest.mark.parametrize("line", [
    "These results",                 # wrapped fragment: determiner first word
    "The experiments",
    "Previous results",
    "the results",
    "results",                       # lower-case: the last word of a wrapped sentence
    "summary",
    "Introduction to the method",
    "Metamodelling techniques",      # no keyword: needs font information we do not have
    "Review",
])
def test_detect_heading_ex_negative(line):
    assert detect_heading_ex(line) is None


# ---- split_numbered_heading -------------------------------------------------------------------------------------

@pytest.mark.parametrize("line, expected", [
    ("1 Introduction", (1, "Introduction")),
    ("IV. Computational Methodology", (4, "Computational Methodology")),
    ("V. NUMERICAL EXPERIMENTS", (5, "NUMERICAL EXPERIMENTS")),
    ("2 | METHODS", (2, "METHODS")),
    ("3 Extrapolating from a pretrained PRNN", (3, "Extrapolating from a pretrained PRNN")),
    ("II. BAYESIAN OPTIMIZATION WITH", (2, "BAYESIAN OPTIMIZATION WITH")),    # heading wrapped onto two lines
    ("1. Introduction. Operator learning has emerged as a methodology", (1, "Introduction")),   # run-in (SIAM)
    ("6. Conclusion. This paper proposes the Fourier Neural Mappings", (6, "Conclusion")),
    ("4\u2002 \x07Geometric multigrid method", (4, "Geometric multigrid method")),
])
def test_split_numbered_heading_positive(line, expected):
    assert split_numbered_heading(line) == expected


@pytest.mark.parametrize("line", [
    "4 MB",                                   # unit
    "2 mm thick plates were used",            # lower case after the number
    "2.1 Material Preparation",               # sub-number
    "1.1. Contributions. In this paper, we make",
    "1 Given: Dn = {(xi, si), yi}n",          # formula
    "12. Smith J, Jones K. Title of paper. J Biomech",   # reference entry
    "3 Fig. 2 shows the",                     # caption-like
    "I think that",
    "2020 Mathematics Subject Classification. Primary: 68T07",
    "Introduction",                           # not numbered
])
def test_split_numbered_heading_negative(line):
    assert split_numbered_heading(line) is None


# ---- classify_heading_text --------------------------------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("PROPOSED FRAMEWORK: NSO", "methods"),
    ("Problem Definition", "methods"),
    ("Experimental methods and boundary conditions", "methods"),
    ("The proposed approach", "methods"),
    ("NUMERICAL EXPERIMENTS", "results"),
    ("Results and discussion", "results"),
    ("Discussion and conclusions", "discussion"),
    ("CONCLUDING REMARKS", "conclusion"),
    ("PRELIMINARIES", "introduction"),
    ("Nomenclature", None),
    ("Extrapolating from a pretrained PRNN", None),
    ("EXTENSIONS AND OPEN", None),
])
def test_classify_heading_text(text, expected):
    assert classify_heading_text(text) == expected


# ---- label_sections with the real detector ---------------------------------------------------------------------

def page(no: int, lines: list[str]) -> PageText:
    return PageText(page_no=no, text="\n".join(lines))


def sections(dt: PaperDocType, pages: list[PageText]) -> list[tuple[int, str]]:
    masks = dt.drop_non_body(pages)
    return [(m.page_no, m.section) for m in dt.label_sections("D1", pages, masks, [])]


@pytest.fixture
def dt() -> PaperDocType:
    return PaperDocType()


ABSTRACT = ("We study the problem in detail and show that the proposed approach works well on several "
            "benchmark problems from the literature. " * 2)
BODY = "This is a body sentence that fills the section with some ordinary prose about the femoral stem."


def test_numbered_headings_without_keywords_become_methods_after_the_introduction(dt):
    p1 = page(1, ["A Paper Title", "Abstract", ABSTRACT, "1 Introduction", BODY,
                  "2 Physically Recurrent Neural Networks", BODY])
    p2 = page(2, ["3 Extrapolating from a pretrained PRNN", BODY, "4 Numerical experiments", BODY,
                  "5 Conclusions", BODY, "References", "[1] Doe J. Paper. J Test. 2020;1:1-2."])
    assert [s for _, s in sections(dt, [p1, p2])] == ["front", "abstract", "introduction", "methods", "results",
                                                      "conclusion", "references"]


def test_roman_numbered_ieee_headings(dt):
    p1 = page(1, ["A Paper Title", "Abstract—Complex piezoelectric systems are foundational in many devices.",
                  "I. INTRODUCTION", BODY, "II. PRELIMINARIES", BODY, "III. PROBLEM FORMULATION", BODY,
                  "IV. PROPOSED FRAMEWORK: NSO", BODY, "V. NUMERICAL EXPERIMENTS", BODY, "VI. DISCUSSION", BODY,
                  "VII. CONCLUSION", BODY, "REFERENCES", "[1] Doe J. Paper. J Test. 2020;1:1-2."])
    assert [s for _, s in sections(dt, [p1])] == ["front", "abstract", "introduction", "methods", "results",
                                                  "discussion", "conclusion", "references"]


def test_run_in_headings(dt):
    p1 = page(1, ["AN OPERATOR LEARNING PERSPECTIVE", ABSTRACT,
                  "1. Introduction. Operator learning has emerged as a methodology that enables", BODY,
                  "2. Neural mappings for finite-dimensional vector data. In this section we", BODY,
                  "3. Fourier neural mappings. The architecture is defined as follows and", BODY,
                  "4. Universal approximation. We prove that the mappings are dense in the", BODY,
                  "5. Numerical experiments. We now perform numerical experiments with the", BODY,
                  "6. Conclusion. This paper proposes the Fourier Neural Mappings (FNMs)", BODY,
                  "REFERENCES", "[1] Doe J. Paper. J Test. 2020;1:1-2."])
    assert [s for _, s in sections(dt, [p1])] == ["front", "abstract", "introduction", "methods", "results",
                                                  "conclusion", "references"]


def test_numbered_sequence_ignores_out_of_sequence_numbers(dt):
    p1 = page(1, ["A Paper Title", "Abstract", ABSTRACT, "1 Introduction", BODY,
                  "12 Something Numbered Oddly", BODY, "2 Model Description", BODY, "3 Results", BODY,
                  "References", "[1] Doe J. Paper. J Test. 2020;1:1-2."])
    marks = dt.label_sections("D1", [p1], dt.drop_non_body([p1]), [])
    assert [m.section for m in marks] == ["front", "abstract", "introduction", "methods", "results", "references"]
    assert marks[3].cstart == p1.text.index("2 Model Description")


def test_structured_abstract_is_retracted_by_the_body_introduction(dt):
    p1 = page(1, ["A Clinical Paper", "Abstract", "Background", ABSTRACT, "Methods", ABSTRACT, "Results", ABSTRACT,
                  "Conclusions", ABSTRACT, "Keywords: hip; stem", "Introduction", BODY, "Patients and Methods", BODY])
    p2 = page(2, ["Results", BODY, "Discussion", BODY, "References", "[1] Doe J. Paper. J Test. 2020;1:1-2."])
    marks = dt.label_sections("D1", [p1, p2], dt.drop_non_body([p1, p2]), [])
    # the keywords block ends at the 'Introduction' heading, whose mark replaces the closing 'front' mark
    assert [(m.page_no, m.section) for m in marks] == [
        (1, "front"), (1, "abstract"), (1, "keywords"), (1, "introduction"), (1, "methods"),
        (2, "results"), (2, "discussion"), (2, "references")]


def test_structured_abstract_without_abstract_heading_gets_an_abstract_mark(dt):
    p1 = page(1, ["A Clinical Paper", "Background — " + ABSTRACT, "Methods", ABSTRACT, "Results", ABSTRACT,
                  "Conclusion", ABSTRACT, "Introduction", BODY, "Methods", BODY])
    p2 = page(2, ["Results", BODY, "Discussion", BODY, "References", "[1] Doe J. Paper. J Test. 2020;1:1-2."])
    marks = dt.label_sections("D1", [p1, p2], dt.drop_non_body([p1, p2]), [])
    assert [m.section for m in marks] == ["front", "abstract", "introduction", "methods", "results", "discussion",
                                          "references"]
    assert marks[1].cstart == p1.text.index("Methods")        # the first dropped heading becomes the abstract


def test_a_late_summary_is_a_conclusion_and_a_late_abstract_is_ignored(dt):
    pages = [page(1, ["A Paper Title", "Abstract", ABSTRACT, "1 Introduction", BODY, "2 Methods", BODY])]
    pages += [page(n, [BODY]) for n in range(2, 12)]
    pages.append(page(12, ["3 Results", BODY, "Abstract", BODY, "4 Summary", BODY, "References",
                           "[1] Doe J. Paper. J Test. 2020;1:1-2."]))
    secs = [s for _, s in sections(dt, pages)]
    assert secs == ["front", "abstract", "introduction", "methods", "results", "conclusion", "references"]


def test_strict_methods_retracts_a_loose_early_results_heading(dt):
    p1 = page(1, ["A Paper Title", "Abstract", ABSTRACT, "1 Introduction", BODY, "Simulation results", BODY,
                  "2 Methods", BODY, "3 Results", BODY, "References", "[1] Doe J. Paper. J Test. 2020;1:1-2."])
    marks = dt.label_sections("D1", [p1], dt.drop_non_body([p1]), [])
    assert [m.section for m in marks] == ["front", "abstract", "introduction", "methods", "results", "references"]
    assert marks[3].cstart == p1.text.index("2 Methods")


def test_abstract_runin_and_keywords_pipe(dt):
    p1 = page(1, ["A Review", "ABSTRACT | Big Data applications are typically associated with systems.",
                  "KEYWORDS | Decision making; design of experiments", "", "I. INTRODUCTION", BODY])
    marks = dt.label_sections("D1", [p1], dt.drop_non_body([p1]), [])
    assert [m.section for m in marks][:4] == ["front", "abstract", "keywords", "front"]
    assert marks[-1] == SectionMark(1, p1.text.index("I. INTRODUCTION"), "introduction")


def test_body_after_keywords_without_any_heading_is_other(dt):
    p1 = page(1, ["A Paper Title", "ABSTRACT: Laser additive manufacturing has led to a paradigm shift in implants.",
                  "KEYWORDS: additive manufacturing, porous titanium", "", BODY, BODY, BODY])
    marks = dt.label_sections("D1", [p1], dt.drop_non_body([p1]), [])
    assert [m.section for m in marks] == ["front", "abstract", "keywords", "other"]
    assert marks[-1].cstart == p1.text.index(BODY) - 1      # the empty line that closes the keywords block


def test_abstract_only_document_keeps_its_abstract(dt):
    p1 = page(1, ["A Paper Title", "Abstract", ABSTRACT, BODY])
    marks = dt.label_sections("D1", [p1], dt.drop_non_body([p1]), [])
    assert [m.section for m in marks] == ["front", "abstract"]


# ---- paper5: methods at the end, heading numbers on their own line, abstract cap (review fixes) -------------------

def test_methods_section_after_the_conclusions_is_accepted(dt):
    """ACS Omega layout (P31): '2. RESULTS AND DISCUSSION', '3. CONCLUSIONS', '4. MATERIALS AND METHODS' — the paper's
    only strict Methods heading arrives after the conclusions and must not be swallowed by the IMRaD order."""
    body = [f"Body sentence {i} about the customized hip prosthesis and its porous structure." for i in range(6)]
    p1 = page(1, ["A Paper Title", "ABSTRACT: Customized hip prostheses were designed and tested in detail here.",
                  "1. INTRODUCTION"] + body)
    p2 = page(2, ["2. RESULTS AND DISCUSSION"] + body)
    p3 = page(3, body[:2] + ["3. CONCLUSIONS"] + body[2:4] + ["4. MATERIALS AND METHODS",
                  "4.1. Customized Hip Prosthesis Design Methodology."] + body[4:] + ["REFERENCES",
                  "[1] Doe J. Paper. J Test. 2020;1:1-2."])
    marks = dt.label_sections("D1", [p1, p2, p3], dt.drop_non_body([p1, p2, p3]), [])
    assert [m.section for m in marks] == ["front", "abstract", "introduction", "results", "conclusion", "methods",
                                          "references"]
    assert marks[5].cstart == p3.text.index("4. MATERIALS AND METHODS")
    # a second methods heading, a loose form or a numbered keyword heading after the conclusions still do not count
    p4 = page(4, ["5. CONCLUSIONS AGAIN"] + body[:2] + ["Proposed methodology"] + body[2:4] + ["6 Model limitations"]
              + body[4:] + ["7 Methods"] + body[:3])
    marks = dt.label_sections("D1", [p1, p2, p3, p4], dt.drop_non_body([p1, p2, p3, p4]), [])
    assert [m.section for m in marks] == ["front", "abstract", "introduction", "results", "conclusion", "methods",
                                          "references"]


def test_heading_number_on_its_own_line_is_joined_with_the_heading_text(dt):
    """ASME layout (P36): '2' / 'Optimization Scheme for Graded-Density Hip', '3' / 'Mechanics of Lattice Materials',
    sub-headings '3.1' / 'Homogenized Properties. A fully resolved …'."""
    p1 = page(1, ["A Paper Title", "Abstract", ABSTRACT, "1", "Introduction", BODY,
                  "2", "Optimization Scheme for Graded-Density Hip", "Implant With Cellular Material", BODY])
    p2 = page(2, [BODY, "3", "Mechanics of Lattice Materials", "3.1", "Homogenized Properties. A fully resolved resolution of",
                  BODY, "3.2", "Fatigue Failure of Lattice Materials. Since the femur is", BODY])
    p3 = page(3, ["4", "Results", BODY, "5", "Conclusions", BODY, "References", "[1] Doe J. Paper. J Test. 2020;1:1-2."])
    marks = dt.label_sections("D1", [p1, p2, p3], dt.drop_non_body([p1, p2, p3]), [])
    assert [(m.page_no, m.section) for m in marks] == [(1, "front"), (1, "abstract"), (1, "introduction"), (1, "methods"),
                                                        (3, "results"), (3, "conclusion"), (3, "references")]
    assert marks[3].cstart == p1.text.index("2\nOptimization")      # the mark sits on the number's line
    # a bare number followed by body text, or by a line on the next page, is no heading
    p4 = page(1, ["A Paper Title", "Abstract", ABSTRACT, "1 Introduction", BODY, "2", BODY, "3", "Results", BODY])
    assert [s for _, s in sections(dt, [p4])] == ["front", "abstract", "introduction", "results"]


def test_abstract_is_capped_at_600_words_or_the_next_page(dt):
    """R2129: abstract on page 2, next heading on page 44; P2022: no Introduction heading, the abstract ran to the
    'Methods' on page 14 — the range is cut after ABSTRACT_MAX_WORDS words or with the page after its own and the
    remainder is 'introduction'; a normal abstract is untouched."""
    long_lines = [f"Sentence number {i} of a very long abstract that goes on and on about the implant." for i in range(80)]
    p1 = page(1, ["A Long Report", "Abstract"] + long_lines[:50])
    p2 = page(2, long_lines[50:])
    p3 = page(3, [BODY, BODY])
    p4 = page(4, ["Methods", BODY, "Results", BODY, "References", "[1] Doe J. Paper. J Test. 2020;1:1-2."])
    marks = dt.label_sections("D1", [p1, p2, p3, p4], dt.drop_non_body([p1, p2, p3, p4]), [])
    assert [(m.page_no, m.section) for m in marks] == [(1, "front"), (1, "abstract"), (1, "introduction"), (4, "methods"),
                                                        (4, "results"), (4, "references")]
    words = 0
    cut = marks[2]
    for ln in p1.text.split("\n"):
        if p1.text.index(ln) >= cut.cstart:
            break
        words += len(ln.split())
    assert 600 < words <= 600 + 16 and p1.text[cut.cstart:].startswith("Sentence number")
    # a short abstract that only crosses the page boundary is cut at the start of the page after next
    p1 = page(1, ["A Report", "Abstract"] + long_lines[:10])
    p2 = page(2, long_lines[10:20])
    p3 = page(3, long_lines[20:30])
    p4 = page(4, ["Methods", BODY, "Results", BODY])
    marks = dt.label_sections("D1", [p1, p2, p3, p4], dt.drop_non_body([p1, p2, p3, p4]), [])
    assert [(m.page_no, m.section, m.cstart) for m in marks][:3] == [(1, "front", 0), (1, "abstract", p1.text.index("Abstract")),
                                                                     (3, "introduction", 0)]
    # the ordinary case is untouched
    p1 = page(1, ["A Paper Title", "Abstract", ABSTRACT, "1 Introduction", BODY, "2 Methods", BODY])
    assert [s for _, s in sections(dt, [p1])] == ["front", "abstract", "introduction", "methods"]
    assert PaperDocType.version == "paper5"
