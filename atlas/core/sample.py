"""Deterministic sampling for the curation stages (increment 4 §2.1). Pure: plain dicts in, plain dicts out.

`stratified_sample(rows, strata, n, per_doc_cap, seed)` draws `n` body sentences (`is_body`, never `references`
/ `appendix`) with proportional allocation over the strata (default `unit_kind|section` × `venue` × `year_bin`),
a per-document cap and a seeded shuffle inside every stratum; the input is sorted first, so the same rows and
the same seed always give the same sample, whatever order the store returned them in.

`pilot_docs(docs, n)` picks the documents of a `--pilot N` run: by `document_meta.extra.proximity_tier` when
present (tier 1 first), then by document id. `chunk_rows(rows, size)` cuts the sample into chunks of at most
`size` sentences of one section type; the chunk key is the sha256 of its sorted sentence ids.

`TERM_TYPES` is the v3 term-type vocabulary the `terms` stage asks for and the QA strata use (the two term systems
stay comparable); it lives here because `core/schemas.py` is frozen since increment 1.
"""
from __future__ import annotations

import hashlib
import random
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

#: the term-type vocabulary of the previous viewer (v3)
TERM_TYPES: tuple[str, ...] = ("名词术语", "量与指标", "方法或模型名", "句式短语", "通用句式", "软件、设备或材料品牌",
                               "期刊名", "缩写")
TERM_TYPES_EN: dict[str, str] = {"名词术语": "noun term", "量与指标": "quantity or metric",
                                 "方法或模型名": "method or model name", "句式短语": "domain phrase",
                                 "通用句式": "generic writing phrase", "软件、设备或材料品牌": "software, equipment or material brand",
                                 "期刊名": "journal name", "缩写": "abbreviation"}

DEFAULT_STRATA: tuple[str, ...] = ("unit_kind|section", "venue", "year_bin")
DEFAULT_YEAR_BINS: list = [["≤2015", None, 2015], ["2016–2020", 2016, 2020], ["2021–2023", 2021, 2023],
                           ["2024–2026", 2024, 2026]]
EXCLUDED_SECTIONS = frozenset({"references", "appendix"})
CHUNK_SIZE = 40


def year_bin(year: int | None, bins: Sequence[Sequence] | None = None) -> str:
    """The label of the bin holding `year` ('?' when unknown or outside every bin); bins are
    [label, lo|None, hi|None] like `stats.year_bins`."""
    if year is None:
        return "?"
    try:
        y = int(year)
    except (TypeError, ValueError):
        return "?"
    for label, lo, hi in (bins or DEFAULT_YEAR_BINS):
        if (lo is None or y >= int(lo)) and (hi is None or y <= int(hi)):
            return str(label)
    return "?"


def stratum_value(row: Mapping, dim: str, bins: Sequence[Sequence] | None = None) -> str:
    """The value of one stratum dimension for a row: 'a|b' dimensions join the parts with '|'."""
    parts = []
    for part in dim.split("|"):
        if part == "unit_kind":
            parts.append(str(row.get("unit_kind") or "document"))
        elif part == "year_bin":
            parts.append(year_bin(row.get("year"), bins))
        elif part == "venue":
            parts.append(str(row.get("venue") or "?").strip() or "?")
        else:
            v = row.get(part)
            parts.append("?" if v is None or v == "" else str(v))
    return "|".join(parts)


def is_sampleable(row: Mapping) -> bool:
    return bool(row.get("is_body")) and str(row.get("section") or "") not in EXCLUDED_SECTIONS


def _allocate(sizes: dict[tuple, int], n: int) -> dict[tuple, int]:
    """Proportional allocation with largest remainders (deterministic: ties by stratum key)."""
    total = sum(sizes.values())
    if total == 0 or n <= 0:
        return {k: 0 for k in sizes}
    if n >= total:
        return dict(sizes)
    exact = {k: n * v / total for k, v in sizes.items()}
    quota = {k: int(x) for k, x in exact.items()}
    short = n - sum(quota.values())
    for k in sorted(sizes, key=lambda k: (-(exact[k] - quota[k]), k)):
        if short <= 0:
            break
        if quota[k] < sizes[k]:
            quota[k] += 1
            short -= 1
    return quota


def stratified_sample(rows: Iterable[Mapping], strata: Sequence[str] = DEFAULT_STRATA, n: int = 1000,
                      per_doc_cap: int = 40, seed: int = 1, year_bins: Sequence[Sequence] | None = None) -> list[dict]:
    """Draw up to `n` body sentences. Rows need sid, doc_id, seq, section, is_body and the stratum fields
    (unit_kind, venue, year). Returns copies of the chosen rows with `stratum` (tuple of the dimension values)
    added, sorted by (doc_id, seq). Deterministic for the same rows, strata, n, cap and seed."""
    # the rows are not copied here (a corpus has a million of them); only the chosen ones are
    pool = sorted((r for r in rows if is_sampleable(r)), key=lambda r: (str(r["doc_id"]), int(r.get("seq") or 0), str(r["sid"])))
    if not pool:
        return []
    by_stratum: dict[tuple, list[Mapping]] = {}
    for r in pool:
        by_stratum.setdefault(tuple(stratum_value(r, d, year_bins) for d in strata), []).append(r)
    del pool
    quota = _allocate({k: len(v) for k, v in by_stratum.items()}, n)
    rng = random.Random(int(seed))
    for key in sorted(by_stratum):
        rng.shuffle(by_stratum[key])
    chosen: list[dict] = []
    per_doc: dict[str, int] = {}
    cap = int(per_doc_cap) if per_doc_cap and per_doc_cap > 0 else None
    leftovers: list[tuple[tuple, Mapping]] = []

    def take(key: tuple, r: Mapping) -> bool:
        d = str(r["doc_id"])
        if cap is not None and per_doc.get(d, 0) >= cap:
            return False
        per_doc[d] = per_doc.get(d, 0) + 1
        chosen.append(dict(r, stratum=key))
        return True

    for key in sorted(by_stratum):
        want = quota[key]
        taken = 0
        for r in by_stratum[key]:
            if taken >= want:
                leftovers.append((key, r))
            elif take(key, r):
                taken += 1
    # a second pass fills what the per-document cap left open, in stratum order (still deterministic)
    for key, r in leftovers:
        if len(chosen) >= n:
            break
        take(key, r)
    chosen.sort(key=lambda r: (str(r["doc_id"]), int(r.get("seq") or 0), str(r["sid"])))
    return chosen


def composition(rows: Iterable[Mapping], strata: Sequence[str] = DEFAULT_STRATA,
                year_bins: Sequence[Sequence] | None = None) -> dict[str, dict[str, int]]:
    """{dimension: {value: n}} over the rows (for the plan / report)."""
    out: dict[str, dict[str, int]] = {d: {} for d in strata}
    for r in rows:
        for d in strata:
            v = stratum_value(r, d, year_bins)
            out[d][v] = out[d].get(v, 0) + 1
    return {d: dict(sorted(v.items(), key=lambda kv: (-kv[1], kv[0]))) for d, v in out.items()}


def pilot_docs(docs: Mapping[str, Mapping] | Iterable[str], n: int) -> list[str]:
    """The `--pilot N` documents: by `extra.proximity_tier` (ascending, unknown last), then by id."""
    if isinstance(docs, Mapping):
        def tier(d: str):
            extra = (docs[d] or {}).get("extra") if isinstance(docs[d], Mapping) else None
            t = (extra or {}).get("proximity_tier") if isinstance(extra, Mapping) else None
            try:
                return (0, float(t)) if t is not None else (1, 0.0)
            except (TypeError, ValueError):
                return (1, 0.0)
        ids = sorted(docs, key=lambda d: (tier(d), str(d)))
    else:
        ids = sorted(str(d) for d in docs)
    return ids[:max(0, int(n))]


@dataclass
class Chunk:
    key: str
    section: str
    rows: list[dict] = field(default_factory=list)

    @property
    def sids(self) -> list[str]:
        return [str(r["sid"]) for r in self.rows]

    @property
    def doc_ids(self) -> list[str]:
        return sorted({str(r["doc_id"]) for r in self.rows})


def chunk_key(sids: Iterable[str]) -> str:
    return hashlib.sha256("\n".join(sorted(str(s) for s in sids)).encode("utf-8")).hexdigest()[:16]


def chunk_rows(rows: Iterable[Mapping], size: int = CHUNK_SIZE) -> list[Chunk]:
    """Chunks of at most `size` sentences of one section type, balanced (a section with 41 sentences gives 21 + 20),
    sentences inside a chunk in (doc_id, seq) order; chunks sorted by section then key."""
    size = max(1, int(size))
    by_section: dict[str, list[dict]] = {}
    for r in rows:
        by_section.setdefault(str(r.get("section") or "other"), []).append(dict(r))
    out: list[Chunk] = []
    for section in sorted(by_section):
        rs = sorted(by_section[section], key=lambda r: (str(r["doc_id"]), int(r.get("seq") or 0), str(r["sid"])))
        k = -(-len(rs) // size)
        base, extra = divmod(len(rs), k)
        pos = 0
        for i in range(k):
            take = base + (1 if i < extra else 0)
            part = rs[pos:pos + take]
            pos += take
            out.append(Chunk(chunk_key(r["sid"] for r in part), section, part))
    out.sort(key=lambda c: (c.section, c.key))
    return out
