"""Unit tests for atlas.core.segment (pure): sentence splitting, heading detection, tags, garbage flags."""
from __future__ import annotations

import pytest

from atlas.core.schemas import TAGS
from atlas.core.segment import (HEADINGS, SEGMENT_VERSION, TAG_PATTERNS, detect_heading, garbage_flags,
                                is_garbage, split_sentences, tag_sentence)


def sentences(text: str, **kw) -> list[str]:
    return [text[a:b] for a, b in split_sentences(text, **kw)]


# ---- split_sentences --------------------------------------------------------------------------------------

def test_spans_index_into_text_and_are_stripped():
    text = "  First one.  Second one!  Third?  "
    spans = split_sentences(text)
    assert spans == [(2, 12), (14, 25), (27, 33)]
    assert [text[a:b] for a, b in spans] == ["First one.", "Second one!", "Third?"]
    for a, b in spans:
        assert not text[a].isspace() and not text[b - 1].isspace()


def test_basic_split_requires_capital_or_bracket_after_terminator():
    assert sentences("A sentence. Another one. a lowercase continuation. (Bracketed) start. [12] cited.") == [
        "A sentence.", "Another one. a lowercase continuation.", "(Bracketed) start.", "[12] cited."]


@pytest.mark.parametrize("text", [
    "As shown in Fig. 3, the stem failed.",
    "As shown in Figs. 3 and 4, the stem failed.",
    "Smith et al. (2019) reported this.",
    "Smith et al. Reported this in a review.",
    "Several materials, e.g. Titanium, were tested.",
    "The outer layer, i.e. The cortex, was modelled.",
    "Values were 3.5 vs. 4.2 mm in the two groups.",
    "This follows from Eq. 2 and Eqs. 3 and 4 above.",
    "Patent No. 5 was cited.",
    "There were approx. 5 cases per year.",
    "This is given in Ref. 5 and Refs. 6 and 7 of the list.",
    "J. Smith and R. J. Lee measured the stems.",
    "Ph.D. students used U.S. Data for the study.",
    "The modulus was 3.5 GPa in all cases.",
    "See Tab. 2 and Sec. 3 for details, cf. Vol. 2, pp. 10 of the series.",
    "Measured by Dr. Smith and Prof. Lee at the clinic.",
])
def test_protected_abbreviations_and_decimals_never_split(text):
    assert sentences(text) == [text]


def test_abbreviation_at_sentence_end_still_splits_after_following_period():
    text = "It is shown in Fig. 3. The next sentence starts here."
    assert sentences(text) == ["It is shown in Fig. 3.", "The next sentence starts here."]


def test_closing_quotes_and_brackets_stay_with_the_sentence():
    text = "He said “it failed.” The stem (which was old). Next one."
    assert sentences(text) == ["He said “it failed.”", "The stem (which was old).", "Next one."]


def test_newlines_inside_paragraph_are_whitespace_but_blank_line_ends_sentence():
    text = "First line\nsecond line of the same sentence.\nNext sentence.\n\nNew paragraph without a period\nstill same"
    assert sentences(text) == ["First line\nsecond line of the same sentence.", "Next sentence.",
                               "New paragraph without a period\nstill same"]
    assert sentences("Heading without period\n \nBody text here.") == ["Heading without period", "Body text here."]
    assert sentences("Windows\r\n\r\nbreaks") == ["Windows", "breaks"]


def test_digit_can_start_a_sentence():
    assert sentences("Healing was complete. 95% of cases recovered.") == ["Healing was complete.", "95% of cases recovered."]


def test_cjk_terminators_split_without_whitespace():
    assert sentences("这是中文句子。第二句！第三句？末尾的句子") == ["这是中文句子。", "第二句！", "第三句？", "末尾的句子"]
    assert sentences("这是中文句子。末尾") == ["这是中文句子。"]      # '末尾' is shorter than min_chars


def test_min_chars_drops_short_spans():
    assert sentences("A. B. Long enough sentence.") == ["A. B. Long enough sentence."]   # initials never split
    assert sentences("Ok. Go! Yes? Done here.", min_chars=3) == ["Ok.", "Go!", "Yes?", "Done here."]
    assert sentences("Ok. Go! Yes? Done here.", min_chars=4) == ["Yes?", "Done here."]
    assert sentences("x. Y") == []
    assert sentences("") == []
    assert sentences("   \n\n  ") == []


def test_ellipsis_and_multiple_terminators():
    assert sentences("Really?! Yes... No.") == ["Really?!", "Yes...", "No."]


def test_segment_version_is_a_short_tag():
    assert SEGMENT_VERSION == "seg2"


# ---- caption labels (review fix: 'Fig. 2. Text' was split after '2.') ------------------------------------------

@pytest.mark.parametrize("text", [
    "Fig. 2. Micromotion versus load.",
    "Figure 2. Micromotion versus load.",
    "Table 3. Mean micromotion (μm) for each stem under walking and stair climbing loads.",
    "Fig. 2a. Micromotion versus load.",
    "Scheme 1. Synthesis route.",
])
def test_caption_number_at_line_start_does_not_split(text):
    assert sentences(text) == [text]


def test_caption_label_mid_line_still_splits_and_line_start_inside_page_is_protected():
    assert sentences("It is shown in Fig. 3. The next sentence starts here.") == [
        "It is shown in Fig. 3.", "The next sentence starts here."]
    page = "Loads were applied in all configurations.\nFig. 2. Micromotion versus load.\nThe shell was modelled."
    assert sentences(page) == ["Loads were applied in all configurations.", "Fig. 2. Micromotion versus load.",
                               "The shell was modelled."]
    # a bare number after a non-label word is an ordinary sentence end
    assert sentences("The count was 2. Next sentence.") == ["The count was 2.", "Next sentence."]


# ---- detect_heading ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("line, expected", [
    ("Abstract", "abstract"),
    ("ABSTRACT", "abstract"),
    ("A B S T R A C T", "abstract"),
    ("Summary", "abstract"),
    ("Keywords:", "keywords"),
    ("Key words", "keywords"),
    ("Index Terms", "keywords"),
    ("1. Introduction", "introduction"),
    ("1 Introduction", "introduction"),
    ("1.Introduction", "introduction"),
    ("I. INTRODUCTION", "introduction"),
    ("Background", "introduction"),
    ("2 Materials and Methods", "methods"),
    ("2. Methods", "methods"),
    ("2.1 Study design", "methods"),
    ("2.1.3. Experimental procedure", "methods"),
    ("Chapter 3: Methods", "methods"),
    ("Chapter 3 Methodology", "methods"),
    ("Section 2 - Materials", "methods"),
    ("Patients and methods", "methods"),
    ("2 | METHODS", "methods"),
    ("3. Results", "results"),
    ("III. RESULTS AND DISCUSSION", "results"),
    ("Results and discussion", "results"),
    ("Findings", "results"),
    ("4. Discussion", "discussion"),
    ("Discussion and conclusions", "discussion"),
    ("5. Conclusions", "conclusion"),
    ("Concluding remarks", "conclusion"),
    ("Summary and conclusions", "conclusion"),
    ("Conclusions and future work", "conclusion"),
    ("Acknowledgements", "acknowledgements"),
    ("Acknowledgments", "acknowledgements"),
    ("Funding", "acknowledgements"),
    ("Declaration of competing interest", "acknowledgements"),
    ("References", "references"),
    ("REFERENCES", "references"),
    ("Bibliography", "references"),
    ("Appendix A", "appendix"),
    ("Appendix B.", None),                        # terminal period → not a heading line
    ("Appendix A. Supplementary data", "appendix"),
    ("APPENDIX 1", "appendix"),
    ("Appendices", "appendix"),
    ("Appendix", "appendix"),
    ("Supplementary material", "appendix"),
    ("Supporting Information", "appendix"),
    ("  Results  ", "results"),
])
def test_detect_heading_positive(line, expected):
    assert detect_heading(line) == expected


@pytest.mark.parametrize("line", [
    "Results.",                                   # terminal period → sentence
    "The results show that the stem failed",      # contains ' the '
    "Introduction to the method",
    "Results obtained from the study are listed below here",  # > 8 words
    "In this study the methods were compared with the results of earlier work on stems",  # > 60 chars
    "Mix of things",
    "Introduction and overview of everything",
    "",
    "   ",
    "3.5",
    "Conclusively",
    "Abstracted",
])
def test_detect_heading_negative(line):
    assert detect_heading(line) is None


def test_numbering_is_not_stripped_from_words_starting_with_roman_letters():
    # 'I' of Introduction, 'M' of Methods, 'C' of Conclusion must survive the roman-numeral stripper
    assert detect_heading("Introduction") == "introduction"
    assert detect_heading("Methods") == "methods"
    assert detect_heading("Conclusion") == "conclusion"
    assert detect_heading("Materials") == "methods"


def test_headings_table_covers_every_section_it_claims():
    assert set(HEADINGS.values()) == {"abstract", "keywords", "introduction", "methods", "results", "discussion",
                                      "conclusion", "acknowledgements", "references", "appendix"}


# ---- tag_sentence -----------------------------------------------------------------------------------------

def test_tag_patterns_cover_all_tags_in_order():
    assert tuple(TAG_PATTERNS) == TAGS


@pytest.mark.parametrize("text, expected", [
    ("Stress shielding is defined as the reduction of periprosthetic bone density.", ["def", "mech"]),
    ("Strain energy density, i.e. the energy per unit volume, was used.", ["def", "method"]),
    ("Stiffness is generally defined here as the slope of the curve.", ["def"]),
    ("We define the ratio as the quotient of the two loads.", ["def"]),
    ("This quantity is known as the elastic modulus.", ["def"]),
    ("The modulus was 110 GPa in the cortical region.", ["quant"]),
    ("Values ranged from 5 to 10 and p < 0.05 was significant.", ["quant", "result"]),
    ("The stress was computed with a finite element model.", ["method"]),
    ("The response was approximated by a polynomial surrogate.", ["method"]),
    ("The mesh was discretised with tetrahedral elements.", ["method"]),
    ("Stiffer stems lead to more shielding than flexible ones.", ["mech", "cmp"]),
    ("In contrast, the collar had no effect.", ["cmp"]),
    ("In contrast, the collar affected the strain.", ["mech", "cmp"]),
    ("A limitation of this work is the lack of validation.", ["limit"]),
    ("It remains unclear whether this holds in vivo.", ["limit"]),
    ("We found that the collar showed a significant benefit.", ["result"]),
    ("Plain words about a hip and a stem.", []),
    ("", []),
])
def test_tag_sentence(text, expected):
    assert tag_sentence(text) == expected


def test_tags_are_case_insensitive_and_in_tags_order():
    tags = tag_sentence("IT WAS FOUND THAT X, WHICH IS DEFINED AS Y, LEADS TO Z BECAUSE OF 5 MM.")
    assert tags == [t for t in TAGS if t in tags]
    assert {"def", "quant", "mech", "result"} <= set(tags)


# ---- garbage_flags ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("A proper sentence with enough words in it.", []),
    ("ab", ["short"]),
    ("Only four words here", ["short"]),
    ("Five proper words are here", []),
    ("12 34 56 78 90 ab", ["short", "low_alpha", "numeric"]),
    ("see https://doi.org/10.1000/xyz for the paper", ["link"]),
    ("see https://doi.org/10.1000/xyz", ["low_alpha", "link"]),
    ("Available at http://example.org for all readers of this paper", ["link"]),
    ("Department of Surgery, University of London, Institute of Ortho, School of X, Faculty of Y", ["affiliation"]),
    ("Department of Surgery and University of London collaborated on this work", []),   # < 4 commas
    ("Fig. 3 Stress distribution in the femoral stem", ["caption"]),
    ("Figure 3. Stress distribution in the femoral stem", ["caption"]),
    ("Table 2 Mean values of the measured quantities", ["caption"]),
    ("Eq. (3) gives the strain energy density of the element", ["caption"]),
    ("Equation 3 gives the strain energy density of the element", ["caption"]),
    ("Smith et al. (2019), Jones et al. (2020) and Lee et al. (2021) all reported similar findings in their work",
     ["reflike"]),
    ("Smith et al. (2019) and Jones et al. (2020) reported similar findings", []),       # only two
    ("[12] [13] [14]", ["short", "low_alpha", "numeric", "reflike"]),
    ("Several authors [12, 13] [14] reported similar findings in the literature", ["reflike"]),
    ("", ["short"]),
])
def test_garbage_flags(text, expected):
    assert garbage_flags(text) == expected


def test_garbage_flag_order_is_fixed():
    flags = garbage_flags("[1] [2] http://x Department of A, University of B, Institute of C, D, E, F 1 2 3")
    order = ["short", "low_alpha", "numeric", "link", "affiliation", "caption", "reflike"]
    assert flags == [f for f in order if f in flags]
    assert {"link", "affiliation", "reflike"} <= set(flags)


def test_is_garbage():
    assert is_garbage(["short"]) and is_garbage(["low_alpha"]) and is_garbage(["numeric"])
    assert is_garbage(["link"]) and is_garbage(["affiliation"])
    assert not is_garbage(["caption"]) and not is_garbage(["reflike"]) and not is_garbage([])
    assert is_garbage(["caption", "short"])
