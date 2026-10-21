"""Sentence splitting, heading detection, rule tags and garbage flags (pure).

Implemented by: increment 1, agent "core". Port the proven rules from the previous system:
  - sentence regex and tag regexes: H:/UCL_theses/2610 opt/术语阅览系统_v3_2026-10-20/_scripts/concept_pipeline.py
    (TAGS, CAPTION, REFLIKE, AFFIL, garbage())
  - matcher sentence split: H:/UCL_theses/2610 opt/术语阅览系统_v3_2026-10-20/matcher.py (SENT)
Improve where the old rules were known to fail: 'Fig. 3', 'et al.', 'e.g.', 'i.e.', 'vs.', 'Eq.', 'No.', 'approx.',
decimals '3.5', initials 'J. Smith', and 'Ref.' must not split a sentence.

SEGMENT_VERSION changes whenever any rule here changes (it enters extractor_version → new extraction ids).
"""
from __future__ import annotations

import re

SEGMENT_VERSION = "seg1"

#: Canonical headings → section. Matching is case-insensitive on the whole line after stripping numbering
#: ('2.', '2', 'II.', 'Chapter 3', '3.1') and trailing punctuation; the line must be ≤ 8 words.
HEADINGS: dict[str, str] = {
    r"abstract|summary": "abstract",
    r"key ?words|index terms": "keywords",
    r"introduction|background": "introduction",
    r"(materials?\s+and\s+)?methods?|methodology|experimental( (section|procedure|setup))?|materials|patients and methods|subjects and methods|study design": "methods",
    r"results?( and discussions?)?|findings": "results",
    r"discussions?( and conclusions?)?": "discussion",
    r"conclusions?|concluding remarks|summary and conclusions?|outlook": "conclusion",
    r"acknowledge?ments?|funding|acknowledgments": "acknowledgements",
    r"references?|bibliography|literature cited|works cited": "references",
    r"appendi(x|ces)|supplementary( material| information)?|supporting information": "appendix",
}


def split_sentences(text: str, min_chars: int = 3) -> list[tuple[int, int]]:
    """Return (start, end) spans of sentences in `text` (end exclusive), in order, each span stripped of
    surrounding whitespace, dropping spans shorter than `min_chars`. Never splits inside the protected
    abbreviations listed in the module docstring or inside decimals. Newlines inside a page are treated as
    whitespace (PDF line breaks), but a blank line (two consecutive newlines) always ends a sentence."""
    raise NotImplementedError("increment 1 / core")


def detect_heading(line: str) -> str | None:
    """Canonical section for a line that is a heading, else None. A heading is a short line (≤ 8 words, ≤ 60
    chars) without a terminal period that matches HEADINGS after numbering is stripped; 'Results and discussion'
    → 'results'; ALL-CAPS variants count. Lines that are clearly sentences (contain ' the ' … or end with '.') are
    not headings."""
    raise NotImplementedError("increment 1 / core")


#: Sentence-type tags; each value is a case-insensitive regex. Port from the previous TAGS dict verbatim, then
#: fix obvious misses (e.g. 'is defined as' with intervening adverbs).
TAG_PATTERNS: dict[str, str] = {}


def tag_sentence(text: str) -> list[str]:
    """Tags from TAG_PATTERNS that match `text`, in TAGS order (atlas.core.schemas.TAGS)."""
    raise NotImplementedError("increment 1 / core")


def garbage_flags(text: str) -> list[str]:
    """Flags for text that is not a usable sentence: 'short' (< 5 word tokens), 'low_alpha' (letters < 55 % of
    chars), 'numeric' (digits > 25 % of chars), 'link' (http/doi.org), 'affiliation' (≥ 2 'Department of /
    University of …' plus ≥ 4 commas), 'caption' (starts with Fig./Figure/Table/Eq. + number), 'reflike'
    (≥ 3 'et al. (year)' patterns or '[12] [13]' runs). Order fixed as listed."""
    raise NotImplementedError("increment 1 / core")


def is_garbage(flags: list[str]) -> bool:
    """True for short, low_alpha, numeric, link, affiliation (caption and reflike are kept but flagged)."""
    return any(f in ("short", "low_alpha", "numeric", "link", "affiliation") for f in flags)
