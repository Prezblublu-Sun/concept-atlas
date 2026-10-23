"""core/sample: deterministic stratified sampling, strata, caps, pilot documents, chunks."""
from __future__ import annotations

import random

from atlas.core.sample import (TERM_TYPES, chunk_key, chunk_rows, composition, pilot_docs, stratified_sample, stratum_value,
                               year_bin)


def rows(n_docs: int = 6, per_doc: int = 50, seed: int = 3) -> list[dict]:
    rng = random.Random(seed)
    out = []
    venues = ["J Biomech", "CMBBE", "Thesis"]
    for d in range(n_docs):
        doc = f"P{d:03d}"
        for s in range(per_doc):
            section = rng.choice(["introduction", "methods", "results", "discussion", "references", "appendix"])
            out.append({"sid": f"S1{d:02d}{s:04d}", "doc_id": doc, "seq": s, "section": section,
                        "is_body": section not in ("references", "appendix"), "unit_kind": None if d % 2 else "methods",
                        "venue": venues[d % 3], "year": 2010 + d * 3})
    rng.shuffle(out)       # the store order must not matter
    return out


def test_sample_is_deterministic_and_sorted():
    rs = rows()
    a = stratified_sample(rs, n=60, per_doc_cap=20, seed=7)
    b = stratified_sample(list(reversed(rs)), n=60, per_doc_cap=20, seed=7)
    assert [r["sid"] for r in a] == [r["sid"] for r in b]
    assert len(a) == 60
    keys = [(r["doc_id"], r["seq"]) for r in a]
    assert keys == sorted(keys)
    c = stratified_sample(rs, n=60, per_doc_cap=20, seed=8)
    assert [r["sid"] for r in c] != [r["sid"] for r in a]
    assert all("stratum" in r and len(r["stratum"]) == 3 for r in a)
    assert not any(r["section"] in ("references", "appendix") for r in a)


def test_per_doc_cap_and_strata_allocation():
    rs = rows(n_docs=4, per_doc=80)
    s = stratified_sample(rs, n=100, per_doc_cap=10, seed=1)
    per_doc = {}
    for r in s:
        per_doc[r["doc_id"]] = per_doc.get(r["doc_id"], 0) + 1
    assert max(per_doc.values()) <= 10 and len(s) == 40        # 4 docs × cap 10 bounds the sample
    big = stratified_sample(rs, n=10 ** 6, per_doc_cap=0, seed=1)
    assert len(big) == sum(1 for r in rs if r["is_body"])
    comp = composition(big, ("unit_kind|section", "venue", "year_bin"))
    assert set(comp) == {"unit_kind|section", "venue", "year_bin"}
    assert "methods|methods" in comp["unit_kind|section"] or "document|methods" in comp["unit_kind|section"]
    # proportional: a stratum with half of the pool gets about half of the sample
    pool = [dict(r, venue="A" if i % 2 else "B") for i, r in enumerate(rs)]
    s2 = stratified_sample(pool, strata=("venue",), n=100, per_doc_cap=0, seed=1)
    n_a = sum(1 for r in s2 if r["venue"] == "A")
    assert 45 <= n_a <= 55 and len(s2) == 100


def test_year_bins_and_stratum_values():
    assert year_bin(2015) == "≤2015" and year_bin(2016) == "2016–2020" and year_bin(2030) == "?" and year_bin(None) == "?"
    assert year_bin(2019, [["old", None, 2000], ["new", 2001, None]]) == "new"
    r = {"unit_kind": None, "section": "results", "venue": " ", "year": 2022}
    assert stratum_value(r, "unit_kind|section") == "document|results"
    assert stratum_value(r, "venue") == "?" and stratum_value(r, "year_bin") == "2021–2023"
    assert stratum_value({"section": None}, "section") == "?"


def test_pilot_docs_by_tier_then_id():
    docs = {"T3": {"extra": {"proximity_tier": 2}}, "T1": {"extra": {"proximity_tier": 1}}, "T2": {"extra": {}},
            "T0": {"extra": {"proximity_tier": "1"}}, "T9": None}
    assert pilot_docs(docs, 3) == ["T0", "T1", "T3"]
    assert pilot_docs(docs, 10) == ["T0", "T1", "T3", "T2", "T9"]
    assert pilot_docs(["b", "a", "c"], 2) == ["a", "b"] and pilot_docs(docs, 0) == []


def test_chunks_of_one_section_and_stable_keys():
    rs = [r for r in rows(n_docs=3, per_doc=30) if r["is_body"]]
    chunks = chunk_rows(rs, 40)
    assert all(len(c.rows) <= 40 for c in chunks)
    assert all(len({r["section"] for r in c.rows}) == 1 for c in chunks)
    by_section = {}
    for c in chunks:
        by_section.setdefault(c.section, []).append(len(c.rows))
    for section, sizes in by_section.items():
        n = sum(1 for r in rs if r["section"] == section)
        assert sum(sizes) == n and max(sizes) - min(sizes) <= 1
    keys = [c.key for c in chunks]
    assert keys == [c.key for c in chunk_rows(list(reversed(rs)), 40)] and len(set(keys)) == len(keys)
    assert all(len(k) == 16 for k in keys) and chunk_key(["b", "a"]) == chunk_key(["a", "b"])
    assert [(r["doc_id"], r["seq"]) for r in chunks[0].rows] == sorted((r["doc_id"], r["seq"]) for r in chunks[0].rows)
    assert len(TERM_TYPES) == 8 and "名词术语" in TERM_TYPES and "缩写" in TERM_TYPES
