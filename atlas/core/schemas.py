"""Shared value types and the reference grammar.

Refs are the one way anything points at anything, in notes, [[links]], exports, HTTP and MCP:
    D:<doc_id>   U:<unit_id>   S:<sid>   C:<concept_id>   ST:<statement_id>   SD:<subdomain_id>
    A:<answer_id>   N:<note_id>   K:<collection_id>   Q:<query_id>
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Section vocabulary. BODY_SECTIONS are counted as body text; the rest are stored (so the reader and phrase
# search see them) with is_body = 0.
SECTIONS = ("front", "title", "keywords", "abstract", "introduction", "methods", "results", "discussion",
            "conclusion", "acknowledgements", "references", "appendix", "other")
BODY_SECTIONS = frozenset({"title", "keywords", "abstract", "introduction", "methods", "results", "discussion",
                           "conclusion", "other"})
SECTION_ZH = {"front": "扉页", "title": "标题", "keywords": "关键词", "abstract": "摘要", "introduction": "引言",
              "methods": "方法", "results": "结果", "discussion": "讨论", "conclusion": "结论",
              "acknowledgements": "致谢", "references": "参考文献", "appendix": "附录", "other": "其他"}

# Rule-based sentence type tags (ported from the previous system; see core/segment.py).
TAGS = ("def", "quant", "method", "mech", "cmp", "limit", "result")

REF_KINDS = {"D": "document", "U": "unit", "S": "sentence", "C": "concept", "ST": "statement", "SD": "subdomain",
             "A": "answer", "N": "note", "K": "collection", "Q": "query"}
_REF_RE = re.compile(r"^(D|U|S|C|ST|SD|A|N|K|Q):([^\s\]]+)$")
LINK_RE = re.compile(r"\[\[((?:D|U|S|C|ST|SD|A|N|K|Q):[^\]\s]+)\]\]")


@dataclass(frozen=True)
class Ref:
    kind: str
    id: str

    def __str__(self) -> str:
        return f"{self.kind}:{self.id}"


def parse_ref(s: str) -> Ref:
    m = _REF_RE.match((s or "").strip())
    if not m:
        raise ValueError(f"not a ref: {s!r}")
    return Ref(m.group(1), m.group(2))


def format_ref(kind: str, id: str) -> str:
    if kind not in REF_KINDS:
        raise ValueError(f"unknown ref kind {kind!r}")
    return f"{kind}:{id}"


def find_refs(markdown: str) -> list[Ref]:
    """All [[ref]] links in a Markdown text, in order, duplicates kept."""
    return [parse_ref(m.group(1)) for m in LINK_RE.finditer(markdown or "")]


# ---- ingest value types ----------------------------------------------------------------------------------

@dataclass(frozen=True)
class PageText:
    page_no: int            # 1-based physical page
    text: str               # exactly what the extractor returned for the page; offsets below index into this
    width: float = 0.0
    height: float = 0.0


@dataclass(frozen=True)
class LineMask:
    """A character span of a page that is not body text (dropped before sentence splitting)."""
    page_no: int
    cstart: int
    cend: int
    reason: str             # header_footer | affiliation | pub_info | acknowledgement | declaration | residual_ref | front_matter | toc | caption_block


@dataclass
class Unit:
    unit_id: str
    doc_id: str
    kind: str               # document | chapter
    ordinal: int            # 0 for the document unit; 1.. for chapters in reading order
    heading: str | None
    page_from: int
    page_to: int
    confidence: float = 1.0
    reasons: tuple[str, ...] = ()
    parent_unit: str | None = None
    unit_kind: str | None = None   # thesis chapters: intro | lit_review | methods | results | discussion | conclusion | other


@dataclass(frozen=True)
class SectionMark:
    """From (page_no, cstart) onward the section is `section`, until the next mark."""
    page_no: int
    cstart: int
    section: str


@dataclass
class DocMeta:
    title: str | None = None
    year: int | None = None
    authors: str | None = None
    doi: str | None = None
    venue: str | None = None
    keywords: str | None = None
    abstract: str | None = None
    lang: str | None = None
    extra: dict = field(default_factory=dict)


@dataclass
class SentenceRow:
    """One sentence as stored: identity + this extraction's membership."""
    sid: str
    doc_id: str
    text: str
    ext_id: str
    seq: int                # reading order within the extraction, 0-based
    unit_id: str
    page_no: int
    cstart: int             # offsets into PageText.text of that page
    cend: int
    section: str
    is_body: bool
    tags: tuple[str, ...] = ()
    flags: tuple[str, ...] = ()
