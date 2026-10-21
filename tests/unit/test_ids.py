"""Unit tests for atlas.core.ids: norm edge cases, sid stability, duplicate ordinals, unit ids, request hashes."""
from __future__ import annotations

import re

import pytest

from atlas.core.ids import (NORM_VERSION, answer_id, canonical_json, extraction_id, fact_id, norm, random_id,
                            relation_id, request_hash, sentence_id, slug, statement_id, text_sha256, unit_id)


# ---- norm -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    (None, ""),
    ("", ""),
    ("   ", ""),
    ("...", ""),
    ("“Hello, world!”", "Hello, world"),
    ("  Hello   world  ", "Hello world"),
    ("Hello\nworld\t!", "Hello world"),
    ("ﬁne ｆｕｌｌ", "fine full"),               # NFKC folds ligatures and full-width letters
    ("(Fig. 3).", "Fig. 3"),                    # internal punctuation kept, edges stripped
    ("Case Kept", "Case Kept"),
    ("5 mm.", "5 mm"),
    ("—dash—", "dash"),
    ("中文句子。", "中文句子"),
])
def test_norm(text, expected):
    assert norm(text) == expected


def test_norm_is_idempotent():
    for t in ["  (Hello)  ", "ﬁne", "a.b.c."]:
        assert norm(norm(t)) == norm(t)


def test_norm_version_prefix():
    assert NORM_VERSION == "S1"


# ---- sentence_id ------------------------------------------------------------------------------------------

def test_sentence_id_shape():
    sid = sentence_id("D0001", "The stem failed.")
    assert re.fullmatch(r"S1[0-9a-f]{16}", sid)


def test_sentence_id_is_stable_under_normalisation_variants():
    base = sentence_id("D0001", "The stem failed.")
    assert sentence_id("D0001", "The stem failed.") == base
    assert sentence_id("D0001", "  The   stem\nfailed. ") == base
    assert sentence_id("D0001", "“The stem failed.”") == base
    assert sentence_id("D0001", "Ｔhe stem failed.") == base       # full-width T folds to T


def test_sentence_id_depends_only_on_doc_text_and_ordinal():
    base = sentence_id("D0001", "The stem failed.")
    assert sentence_id("D0002", "The stem failed.") != base
    assert sentence_id("D0001", "The stem held.") != base
    assert sentence_id("D0001", "the stem failed.") != base        # case is kept by norm


def test_sentence_id_duplicate_ordinal():
    s0 = sentence_id("D0001", "Repeated sentence.", 0)
    s1 = sentence_id("D0001", "Repeated sentence.", 1)
    s2 = sentence_id("D0001", "Repeated sentence.", k=2)
    assert len({s0, s1, s2}) == 3
    assert sentence_id("D0001", "Repeated sentence.") == s0        # default k = 0


def test_sentence_id_empty_text_still_hashes():
    assert re.fullmatch(r"S1[0-9a-f]{16}", sentence_id("D0001", ""))
    assert sentence_id("D0001", "") == sentence_id("D0001", "...")


# ---- other content ids ------------------------------------------------------------------------------------

def test_extraction_id_and_text_sha():
    e = extraction_id("D0001", "ab" * 32, "seg1+paper:paper1")
    assert re.fullmatch(r"[0-9a-f]{12}", e)
    assert e != extraction_id("D0001", "ab" * 32, "seg2+paper:paper1")
    assert text_sha256("abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def test_slug():
    assert slug("Finite Element Analysis") == "finite-element-analysis"
    assert slug("  Stress--Shielding!! ") == "stress-shielding"
    assert slug("有限元 分析") == "有限元-分析"
    assert slug("") == ""
    assert len(slug("a very long heading that goes on and on", maxlen=10)) <= 10
    assert not slug("abcdefghij-klm", maxlen=11).endswith("-")


def test_unit_id_document_and_chapters():
    assert unit_id("D0001") == "D0001"
    assert unit_id("D0001", None) == "D0001"
    assert unit_id("D0001", "Introduction") == "D0001/introduction"
    assert unit_id("D0001", "!!!") == "D0001/unit"            # empty slug falls back to 'unit'


def test_unit_id_collisions_use_tilde_suffixes():
    taken: set[str] = set()
    ids = []
    for _ in range(3):
        u = unit_id("D0001", "Methods", taken)
        taken.add(u)
        ids.append(u)
    assert ids == ["D0001/methods", "D0001/methods~2", "D0001/methods~3"]
    # without `taken` no suffix is ever added
    assert unit_id("D0001", "Methods") == "D0001/methods"
    # a gap in `taken` is reused
    assert unit_id("D0001", "Methods", {"D0001/methods", "D0001/methods~3"}) == "D0001/methods~2"


def test_relation_fact_statement_answer_ids():
    r = relation_id("C:a", "C:b", "part_of")
    assert r.startswith("R_") and len(r) == 2 + 12
    assert r == relation_id("C:a", "C:b", "part_of", qualifier=None, value=None)
    assert r == relation_id("C:a", "C:b", "part_of", qualifier="", value="")
    assert r != relation_id("C:b", "C:a", "part_of")
    assert relation_id("a", "b", "p", "  q  ") == relation_id("a", "b", "p", "q")

    f = fact_id("c1", ["S1b", "S1a", "S1a"], "A fact.")
    assert f == fact_id("c1", ("S1a", "S1b"), "  A fact ")       # cites de-duplicated and sorted, text normed
    assert f.startswith("F_")

    st = statement_id("c1", "definition", "X is Y.")
    assert st.startswith("ST_") and st != statement_id("c1", "definition", "X is Y.", k=1)

    a = answer_id("What is X?", "x", ["S1b", "S1a"])
    assert a == answer_id(" What is X? ", "x", ["S1a", "S1b", "S1a"])
    assert a.startswith("A_") and len(a) == 2 + 16


def test_random_id_shape_and_order():
    a = random_id("N")
    b = random_id("N")
    assert re.fullmatch(r"N_[0-9a-f]{16}", a)
    assert a != b
    assert a[:12] <= b[:12]                                       # time part is monotone non-decreasing


# ---- request_hash -----------------------------------------------------------------------------------------

def test_canonical_json_is_order_independent_and_compact():
    assert canonical_json({"b": 1, "a": [1, {"d": 2, "c": "中文"}]}) == '{"a":[1,{"c":"中文","d":2}],"b":1}'


def test_request_hash_determinism():
    body1 = {"model": "m", "messages": [{"role": "user", "content": "hi 中文"}], "temperature": 0}
    body2 = {"temperature": 0, "messages": [{"content": "hi 中文", "role": "user"}], "model": "m"}
    h = request_hash("deepseek", body1)
    assert re.fullmatch(r"[0-9a-f]{64}", h)
    assert h == request_hash("deepseek", body2)                  # key order does not matter
    assert h != request_hash("other", body1)                     # provider is part of the hash
    assert h != request_hash("deepseek", {**body1, "temperature": 0.1})
    assert h != request_hash("deepseek", {**body1, "model": "m2"})
