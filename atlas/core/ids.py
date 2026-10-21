"""Every identifier in the system is built here, and only here.

Stability rules (see docs/ARCHITECTURE.md §identity):
- a sentence id depends only on (document id, normalised sentence text, duplicate ordinal) — never on page,
  position, extraction or build;
- the normaliser is frozen; changing it means a new NORM_VERSION prefix, never a silent change;
- document ids come from the manifest or are assigned once; they never depend on file bytes;
- content-derived ids (relation, fact, statement, answer) hash normalised content; authored ids (concepts,
  categories, sub-domains) are slugs owned by the user; user-object ids are time-sortable random strings.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
import unicodedata
from collections.abc import Iterable

NORM_VERSION = "S1"

_EDGE_PUNCT = re.compile(r"^[^\w]+|[^\w]+$", re.UNICODE)


def norm(text: str | None) -> str:
    """Frozen normaliser S1: NFKC → collapse whitespace → strip → strip leading/trailing punctuation. Case kept."""
    s = unicodedata.normalize("NFKC", text or "")
    s = " ".join(s.split())
    return _EDGE_PUNCT.sub("", s)


def _h(parts: Iterable[str], size: int) -> str:
    h = hashlib.blake2b(digest_size=size)
    for p in parts:
        h.update(p.encode("utf-8"))
        h.update(b"\x1f")
    return h.hexdigest()


def sentence_id(doc_id: str, text: str, k: int = 0) -> str:
    """'S1' + 16 hex chars. `k` = rank among sentences of this document with identical normalised text (0-based,
    document order), so repeated sentences get distinct ids."""
    return NORM_VERSION + _h([doc_id, norm(text), str(k)], 8)


def extraction_id(doc_id: str, pdf_sha256: str, extractor_version: str) -> str:
    return _h([doc_id, pdf_sha256, extractor_version], 6)


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def slug(text: str, maxlen: int = 24) -> str:
    s = unicodedata.normalize("NFKC", text or "").lower()
    s = re.sub(r"[^a-z0-9一-鿿]+", "-", s).strip("-")
    s = re.sub(r"-{2,}", "-", s)
    return s[:maxlen].strip("-")


def unit_id(doc_id: str, heading: str | None = None, taken: set[str] | None = None) -> str:
    """Document unit id == doc_id. Chapter unit id = '<doc_id>/<slug(heading)>', with '~2', '~3' … appended for
    repeated slugs (checked against `taken`, which the caller updates)."""
    if heading is None:
        return doc_id
    base = f"{doc_id}/{slug(heading) or 'unit'}"
    cand, k = base, 1
    while taken is not None and cand in taken:
        k += 1
        cand = f"{base}~{k}"
    return cand


_EPOCH_MS = 1_600_000_000_000  # 2020-09-13; keeps the timestamp part at 10 hex chars for decades


def random_id(prefix: str) -> str:
    """Time-sortable random id: '<prefix>_<10 hex ms since 2020><6 hex random>' (e.g. N_0123456789abcdef)."""
    ms = max(0, int(time.time() * 1000) - _EPOCH_MS)
    return f"{prefix}_{ms:010x}{secrets.token_hex(3)}"


def relation_id(src: str, dst: str, predicate: str, qualifier: str | None = None, value: str | None = None) -> str:
    return "R_" + _h([src, dst, predicate, norm(qualifier), norm(value)], 6)


def fact_id(concept_id: str, cites: Iterable[str], text: str) -> str:
    return "F_" + _h([concept_id, ",".join(sorted(set(cites))), norm(text)], 6)


def statement_id(concept_id: str, section: str, en_text: str, k: int = 0) -> str:
    return "ST_" + _h([concept_id, section, norm(en_text), str(k)], 6)


def answer_id(question: str, canonical_query: str, sids: Iterable[str]) -> str:
    return "A_" + _h([norm(question), canonical_query or "", ",".join(sorted(set(sids)))], 8)


def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def request_hash(provider_id: str, body: dict) -> str:
    """sha256 over the provider id and the canonical JSON of the full request body (model, messages, params)."""
    return hashlib.sha256((provider_id + "\x1f" + canonical_json(body)).encode("utf-8")).hexdigest()
