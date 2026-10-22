"""atlas.core.matcher: every rule of the v2 matcher with positive and negative cases, offsets into the stored text,
literal mode with the offset map, and a golden test on 12 real sentences (expectations derived from the previous
viewer's matcher, see _golden_matcher.py)."""
from __future__ import annotations

import pytest

from atlas.core.matcher import DEFAULT_PATTERNS, Matcher, Occurrence, in_caps_run, lit_norm, lit_norm_map, norm_key
from tests.unit._golden_matcher import GOLDEN, GOLDEN_CONCEPTS


def C(id, forms=(), gen=(), case=(), mode="forms", rx=None, probe=""):
    return {"id": id, "label": id, "mode": mode, "forms": list(forms), "gen_forms": list(gen), "case_forms": list(case),
            "rx": rx, "probe": probe}


@pytest.fixture(scope="module")
def m():
    return Matcher.from_curated([
        C("C_fe", ["finite element", "finite elements"], ["finiteelement"], ["FE", "FEs"]),
        C("C_fea", ["finite element analysis"], [], ["FEA"]),
        C("C_femodel", ["fe model", "finite element model"]),
        C("C_element", ["element", "elements"]),
        C("C_stem", ["stem", "stems"]),
        C("C_prior", ["prior"]),
        C("C_gap", ["gap", "gaps"]),
        C("C_to", [], [], ["TO"]),
        C("C_abaqus", [], [], ["ABAQUS", "Abaqus"]),
        C("C_micromotion", ["micromotion", "micromotions"]),
        C("C_lit", mode="literal", rx=r"(?<![^ ¶])(?:stress|strain) ?field(?:s|es)?(?![^ ¶])", probe="field"),
        C("C_lit2", mode="literal", rx=r"(?<![^ ¶])ti 6al 4v(?![^ ¶])", probe="6al"),
        C("C_merged", ["old label"]) | {"merged_into": "C_stem"},
    ], DEFAULT_PATTERNS)


def ids(occ):
    return sorted((o.concept_id, o.cstart, o.cend, o.nested_in) for o in occ)


# ---- tokeniser and keys ----------------------------------------------------------------------------------------
def test_norm_key_tokenises_like_the_text():
    assert norm_key("Finite-Element (FE) Analysis") == "finite element fe analysis"
    assert norm_key("Ti-6Al-4V") == "ti 6al 4v"
    assert norm_key("the stem’s tip") == "the stems tip"
    assert norm_key("3D printing") == "3d printing"
    assert norm_key("2 mm") == "mm"                  # bare numbers are not tokens


def test_tokens_may_start_with_a_digit_and_hyphens_split(m):
    occ = m.sentence("A 10-noded finite-element model.")
    assert ("C_fe", 11, 25, "C_femodel") in ids(occ)            # 'finite-element' = two tokens, offsets index the text
    assert ("C_femodel", 11, 31, None) in ids(occ)


# ---- forms, case, longest match, nesting ---------------------------------------------------------------------
def test_case_insensitive_forms_and_longest_match(m):
    occ = m.sentence("Finite Element Analysis of the stem.")
    assert ids(occ) == [("C_element", 7, 14, "C_fea"), ("C_fe", 0, 14, "C_fea"), ("C_fea", 0, 23, None), ("C_stem", 31, 35, None)]
    assert all(isinstance(o, Occurrence) for o in occ)


def test_gen_forms_count_as_forms(m):
    assert ("C_fe", 4, 17, None) in ids(m.sentence("The finiteelement mesh."))


def test_case_forms_are_case_sensitive(m):
    assert ("C_fe", 4, 6, None) in ids(m.sentence("The FE mesh."))
    assert ids(m.sentence("The fe mesh.")) == []                 # 'fe' is not a key, 'FE' is a case form only
    assert ("C_abaqus", 8, 14, None) in ids(m.sentence("Used in Abaqus 2021."))
    assert ("C_abaqus", 8, 14, None) in ids(m.sentence("Used in ABAQUS 2021."))


def test_short_caps_form_inside_a_caps_run_does_not_match(m):
    assert ids(m.sentence("A GUIDE TO FE ANALYSIS")) == []          # TO and FE inside a run of all-caps words
    assert ("C_to", 16, 18, None) in ids(m.sentence("THE TO CASE and TO here"))
    assert ("C_fe", 4, 6, "C_femodel") in ids(m.sentence("The FE model was built."))
    assert ("C_fe", 4, 6, None) in ids(m.sentence("The FE mesh was built."))
    assert in_caps_run(["GUIDE", "TO", "FE"], 1) and not in_caps_run(["the", "TO", "FE"], 1)


def test_bad_next_and_bad_prev(m):
    assert ids(m.sentence("Prior to loading the stems from the box.")) == []   # prior+to, stems+from
    assert ("C_prior", 2, 7, None) in ids(m.sentence("A prior distribution was used."))
    assert ids(m.sentence("This gap and the research gap remain.")) == []    # gap after this/research
    assert ("C_gap", 2, 5, None) in ids(m.sentence("A gap of 2 mm."))
    assert ids(m.sentence("Stem cells were cultured.")) == []                   # stem+cell


def test_stem_caps_rule(m):
    assert ids(m.sentence("The STEM programme.")) == []
    assert ("C_stem", 4, 8, None) in ids(m.sentence("The Stem was implanted."))


def test_overlapping_matches_of_different_concepts_are_both_kept(m):
    occ = ids(m.sentence("finite element analysis"))
    assert ("C_fea", 0, 23, None) in occ and ("C_fe", 0, 14, "C_fea") in occ and ("C_element", 7, 14, "C_fea") in occ


def test_nested_in_names_the_longest_container(m):
    occ = {o.concept_id: o for o in m.sentence("The FE model and the finite element model.")}
    assert occ["C_femodel"].nested_in is None
    assert occ["C_fe"].nested_in == "C_femodel"


def test_merged_concepts_are_ignored(m):
    assert ids(m.sentence("An old label here.")) == []


def test_deterministic_order_and_counts(m):
    s = "Micromotion of the stem; micromotion again. FE and FE."
    a, b = m.sentence(s), m.sentence(s)
    assert a == b and [o.cstart for o in a] == sorted(o.cstart for o in a)
    assert m.counts(s) == {"C_micromotion": (2, 0), "C_stem": (1, 0), "C_fe": (2, 0)}
    assert m.counts("finite element analysis") == {"C_fea": (1, 0), "C_fe": (1, 1), "C_element": (1, 1)}
    assert m.sentence("") == [] and m.sentence(None) == [] and m.text("  ") == []


# ---- literal mode ---------------------------------------------------------------------------------------------
def test_lit_norm_matches_the_previous_system():
    assert lit_norm("The Stress–Field: a/b ‘quoted’ Ti-6Al-4V (x)") == "the stress field : a b quoted ti 6al 4v x"
    assert lit_norm("ﬁne") == "fine"


def test_lit_norm_map_offsets():
    s = "Ab-c: ﬁne  “q” x"
    n, idx = lit_norm_map(s)
    assert n == "ab c : fine q x"
    assert len(idx) == len(n)
    assert s[idx[n.index("fine")]:idx[n.index("fine")] + 1] == "ﬁ"        # the ligature expands to two mapped chars
    assert idx[-1] == s.index("x")


def test_literal_offsets_map_back_to_the_stored_text(m):
    s = "The stress-field and strain fields: ok"
    occ = [o for o in m.sentence(s) if o.concept_id == "C_lit"]
    assert [(s[o.cstart:o.cend], o.form) for o in occ] == [("stress-field", "stress field"), ("strain fields", "strain fields")]
    s2 = "Made of Ti‑6Al‑4V alloy."       # non-breaking hyphens
    occ = [o for o in m.sentence(s2) if o.concept_id == "C_lit2"]
    assert len(occ) == 1 and s2[occ[0].cstart:occ[0].cend] == "Ti‑6Al‑4V"


def test_literal_probe_is_a_precheck_only(m):
    assert not [o for o in m.sentence("no probe word here") if o.concept_id == "C_lit"]
    assert not [o for o in m.sentence("a magnetic field only") if o.concept_id == "C_lit"]   # probe present, rx fails


# ---- golden -------------------------------------------------------------------------------------------------
def test_golden_sentences_match_the_previous_viewer():
    gm = Matcher.from_curated(GOLDEN_CONCEPTS, DEFAULT_PATTERNS)
    assert len(GOLDEN) == 12
    for text, expected in GOLDEN:
        got = sorted((o.cstart, o.cend, o.concept_id, o.nested_in) for o in gm.sentence(text))
        assert got == sorted(expected), text
        for a, b, cid, _ in expected:
            assert text[a:b].strip()
