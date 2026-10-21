"""Unit tests for atlas.core.text (pure): inline ids, number extraction rules, numbers_missing."""
from __future__ import annotations

import pytest

from atlas.core.text import (INLINE_ID_RE, collapse_ws, extract_numbers, nfkc, numbers_missing,
                             strip_inline_ids)


# ---- helpers ----------------------------------------------------------------------------------------------

def test_nfkc_and_collapse_ws_handle_none_and_fullwidth():
    assert nfkc(None) == ""
    assert nfkc("１５％") == "15%"
    assert collapse_ws(None) == ""
    assert collapse_ws("  a \n\t b  ") == "a b"


# ---- inline ids -------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("as shown [E1, E2] here", "as shown here"),
    ("value (R3) and F7", "value and"),
    ("id ST_ab12cd34 used", "id used"),
    ("sid S1ab12cd34ef567890 used", "sid used"),
    ("E1，E2、E3 end", "end"),
    ("nothing to strip", "nothing to strip"),
    (None, ""),
])
def test_strip_inline_ids(text, expected):
    assert strip_inline_ids(text) == expected


def test_inline_id_regex_does_not_eat_words():
    # letters before the id prefix mean it is part of a word, not an id
    assert strip_inline_ids("THE12 stays") == "THE12 stays"
    assert INLINE_ID_RE.search("E12") is not None


# ---- digit rules ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("1,000", {"1000"}),
    ("12,345.67", {"12345.67"}),
    ("1,000,000 cells", {"1000000"}),
    ("0.50", {"0.5"}),
    ("5.0 mm", {"5"}),
    ("007", {"7"}),
    ("15%", {"15"}),
    ("15 ％", {"15"}),
    ("１５", {"15"}),              # full-width digits are NFKC-folded
    ("3.2e-3", {"0.0032"}),
    ("1e3", {"1000"}),
    ("2.5E+2", {"250"}),
    ("50–150", {"50", "150"}),    # ranges → both numbers
    ("5-10%", {"5", "10"}),
    ("±4.1", {"4.1"}),
    ("±45°", {"45"}),
    ("p < .05", {"0.05"}),
    ("Fig.5", {"5"}),
    ("(2019)", {"2019"}),         # years are not special-cased
    ("E12", {"12"}),              # inline ids still count in extract_numbers
    ("30 μm", {"30"}),
    ("CoCr28Mo6", {"28", "6"}),
    ("", set()),
    (None, set()),
    ("no numbers here", set()),
])
def test_extract_digits(text, expected):
    assert extract_numbers(text) == expected


def test_ambiguous_decimal_keeps_both_readings():
    found = extract_numbers("1.000")
    assert {"1.000", "1000"} <= found
    found = extract_numbers("2.500 mm")
    assert {"2.500", "2500", "2.5"} <= found
    # three decimals after a leading zero are not a thousands candidate
    assert extract_numbers("0.125") == {"0.125"}
    # a large integer part cannot be a European thousands group either
    assert extract_numbers("1234.567") == {"1234.567"}


def test_power_of_ten_keeps_raw_and_value():
    assert extract_numbers("10^3") == {"10^3", "1000"}
    assert extract_numbers("10^-3") == {"10^-3", "0.001"}
    assert extract_numbers("10³") == {"10^3", "1000"}
    assert extract_numbers("10⁻³ mm") == {"10^-3", "0.001"}
    assert extract_numbers("3.2 × 10^3") == {"3.2", "10^3", "1000"}


def test_unit_superscripts_are_not_numbers():
    # review fix: NFKC folds 'mm²' to 'mm2', which used to yield the number 2
    assert extract_numbers("mm²") == set()
    assert extract_numbers("an area of 12 mm² and 5 cm³") == {"12", "5"}
    assert extract_numbers("a rate of 3 s⁻¹ and m⁴") == {"3"}
    assert extract_numbers("Smith¹ and Jones²") == set()
    assert extract_numbers("10³ mm²") == {"10^3", "1000"}
    assert numbers_missing("the stiffness was 4 kN/mm²", ["4 kN/mm²"], ignore=()) == []
    assert numbers_missing("an area of 7 mm²", ["7 mm"], ignore=()) == []


def test_thousands_separator_only_for_groups_of_three():
    assert extract_numbers("1,2,3") == {"1", "2", "3"}
    assert extract_numbers("1,0000") == {"1", "0"}
    assert extract_numbers("1,23") == {"1", "23"}
    assert extract_numbers("1.000.000") == {"1000000"}


def test_version_like_strings_are_not_numbers():
    assert extract_numbers("3.1.2") == set()
    assert extract_numbers("v2.0.1") == set()


# ---- number words -----------------------------------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("eighty-nine", {"89"}),
    ("eighty nine", {"89"}),
    ("Twenty-One patients", {"21"}),
    ("one", {"1"}),
    ("zero", {"0"}),
    ("ten", {"10"}),
    ("seventeen", {"17"}),
    ("forty", {"40"}),
    ("a hundred", {"100"}),
    ("thousand", {"1000"}),
    ("a million", {"1000000"}),
    ("two hundred", {"2", "100"}),     # combining 'two hundred' is not required
    ("tenth tenets", set()),           # whole words only
    ("one-way ANOVA", {"1"}),
])
def test_number_words(text, expected):
    assert extract_numbers(text) == expected


# ---- Chinese numerals -------------------------------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("三", {"3"}),
    ("十五", {"15"}),
    ("二十", {"20"}),
    ("二十三", {"23"}),
    ("一百零五", {"105"}),
    ("三万五千", {"35000"}),
    ("一亿二千万", {"120000000"}),
    ("二〇一九年", {"2019"}),
    ("两", {"2"}),
    ("十", {"10"}),
    ("3万", {"30000"}),
    ("1.5亿", {"150000000"}),
    ("3千万", {"30000000"}),
    ("15 万", {"150000"}),
    ("百分之十", {"10"}),
    ("百分之十五", {"15"}),
    ("百分之3.5", {"3.5"}),
    ("百分比", set()),                 # bare multipliers are words, not numbers
    ("千万", set()),
])
def test_cjk_numbers(text, expected):
    assert extract_numbers(text) == expected


# ---- canonical form ---------------------------------------------------------------------------------------

def test_canonical_strings_have_no_leading_or_trailing_zeros():
    found = extract_numbers("007 and 0.500 and 100 and 1.0")
    assert found == {"7", "0.5", "100", "1"}
    assert all(not (v.startswith("0") and len(v) > 1 and v[1] != ".") for v in found)


def test_mixed_sentence():
    s = "In 1,000 cases (Fig. 3) the modulus was 110 GPa ± 4.1, i.e. eighty-nine percent (89%) of 3.2e-3 m."
    assert extract_numbers(s) == {"1000", "3", "110", "4.1", "89", "0.0032"}


# ---- numbers_missing --------------------------------------------------------------------------------------

def test_numbers_missing_basic():
    assert numbers_missing("The stem was 1000 mm and 15 % porous", ["length 1,000 mm", "porosity: 15%"]) == []
    assert numbers_missing("The stem was 1000 mm and 20 % porous", ["length 1,000 mm", "porosity: 15%"]) == ["20"]


def test_numbers_missing_strips_inline_ids_and_ignores_trivial():
    assert numbers_missing("Three arms [E12] with 2 screws (S1ab12cd34ef567890)", ["three arms, two screws"]) == []
    assert numbers_missing("[E12] 5 screws", []) == ["5"]
    assert numbers_missing("1 and 2 and 3 and 4", []) == ["4"]
    assert numbers_missing("1 and 2 and 3 and 4", [], ignore=()) == ["1", "2", "3", "4"]


def test_numbers_missing_alternative_readings_count_as_present():
    assert numbers_missing("1.000 mm", ["1000 mm"]) == []
    assert numbers_missing("1.000 mm", ["1 mm"]) == []
    assert numbers_missing("1.000 mm", ["2 mm"]) == ["1.000"]
    assert numbers_missing("10^3 cycles", ["1000 cycles"]) == []
    assert numbers_missing("3万 cases", ["30000 cases"]) == []
    assert numbers_missing("eighty-nine patients", ["89 patients"]) == []


def test_numbers_missing_sorted_and_handles_none():
    assert numbers_missing("50 then 7 then 1000", []) == sorted(["50", "7", "1000"])
    assert numbers_missing(None, ["anything"]) == []
    assert numbers_missing("5 items", []) == ["5"]
