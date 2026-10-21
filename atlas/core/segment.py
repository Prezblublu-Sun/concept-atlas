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
import unicodedata

from atlas.core.schemas import TAGS

SEGMENT_VERSION = "seg1"

#: Canonical headings → section. Matching is case-insensitive on the whole line after stripping numbering
#: ('2.', '2', 'II.', 'Chapter 3', '3.1') and trailing punctuation; the line must be ≤ 8 words.
HEADINGS: dict[str, str] = {
    r"abstract|summary": "abstract",
    r"key ?words|index terms": "keywords",
    r"introduction|background( and motivation)?": "introduction",
    r"(materials?\s+and\s+)?methods?( and materials)?|methodology|experimental( (section|procedures?|setup))?|materials|patients and methods|subjects and methods|study design": "methods",
    r"results?( and discussions?)?|findings": "results",
    r"(general )?discussions?( and conclusions?)?": "discussion",
    r"conclusions?( and (future work|outlook|perspectives?|recommendations?))?|concluding remarks|summary and conclusions?|outlook": "conclusion",
    r"acknowledge?ments?|funding|acknowledgments|conflicts? of interests?|declaration of (competing )?interests?|competing interests?|author contributions?|data availability( statement)?": "acknowledgements",
    r"references?|bibliography|literature cited|works cited": "references",
    r"appendi(x|ces)|supplementary( material| materials| information| data)?|supporting information": "appendix",
}
_HEADING_RES: list[tuple[re.Pattern[str], str]] = [(re.compile(p, re.I), s) for p, s in HEADINGS.items()]

# ---- sentence splitting -----------------------------------------------------------------------------------

#: Abbreviations (lower-case, without the final period) after which a period never ends a sentence.
ABBREVIATIONS: frozenset[str] = frozenset(
    "fig figs tab tabs eq eqs eqn eqns no nos approx ref refs cf ca al et vs v e.g i.e viz resp dr prof mr mrs "
    "ms jr sr st mt inc ltd co corp vol vols pp p ed eds sec sect ch chap suppl wt univ dept proc trans int natl "
    "j".split()
)
#: Initials and dotted acronyms: 'J', 'J.R', 'U.S', 'Ph.D'.
_INITIAL_RE = re.compile(r"^(?:[A-Za-z]{1,2}\.)*[A-Z]$")
_LAST_TOKEN_RE = re.compile(r"(\S+)$")
_OPENERS = "([{\"'“‘"
#: A sentence terminator with its closing quotes/brackets and the whitespace after it, when the next character
#: can start a sentence; CJK terminators always end a sentence (no whitespace needed).
_SENT_END_RE = re.compile(r"[.!?][\"'”’)\]]*\s+(?=[A-Z0-9(\[\"“‘\u4e00-\u9fff])|[。！？][\"'”’)\]」』]*\s*")
_PARA_SEP_RE = re.compile(r"\n[ \t\r\f\v]*\n")
_LEGACY_SENT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\[])")  # matcher.py SENT, kept for reference


def _protected(para: str, dot: int) -> bool:
    """True when the period at `para[dot]` belongs to an abbreviation or an initial."""
    m = _LAST_TOKEN_RE.search(para, 0, dot)
    if not m:
        return False
    tok = m.group(1).lstrip(_OPENERS)
    if not tok:
        return False
    return tok.lower() in ABBREVIATIONS or bool(_INITIAL_RE.match(tok))


def _paragraphs(text: str) -> list[tuple[int, int]]:
    out, pos = [], 0
    for m in _PARA_SEP_RE.finditer(text):
        out.append((pos, m.start()))
        pos = m.end()
    out.append((pos, len(text)))
    return out


def _stripped(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def split_sentences(text: str, min_chars: int = 3) -> list[tuple[int, int]]:
    """Return (start, end) spans of sentences in `text` (end exclusive), in order, each span stripped of
    surrounding whitespace, dropping spans shorter than `min_chars`. Never splits inside the protected
    abbreviations listed in the module docstring or inside decimals. Newlines inside a page are treated as
    whitespace (PDF line breaks), but a blank line (two consecutive newlines) always ends a sentence."""
    text = text or ""
    spans: list[tuple[int, int]] = []

    def add(a: int, b: int) -> None:
        a, b = _stripped(text, a, b)
        if b - a >= min_chars:
            spans.append((a, b))

    for p0, p1 in _paragraphs(text):
        para = text[p0:p1]
        cur = 0
        for m in _SENT_END_RE.finditer(para):
            if para[m.start()] == "." and _protected(para, m.start()):
                continue
            end = m.start() + len(m.group(0).rstrip())
            add(p0 + cur, p0 + end)
            cur = m.end()
        add(p0 + cur, p1)
    return spans


# ---- heading detection ------------------------------------------------------------------------------------

#: Leading numbering: '2.', '2', '2.1.3', 'II.', 'IV', 'A.', 'Chapter 3', 'Section 2:', '2 | '.
_NUMBERING_RE = re.compile(r"^(?:(?:chapter|section|part)\s+)?(?:\d+(?:\.\d+)*\.?|[ivxlcm]+\.?|[a-z]\.)(?:[\s.:)|\-–—]+|$)",
                           re.I)
_LETTER_SPACED_RE = re.compile(r"^(?:[A-Za-z] )+[A-Za-z]$")


def detect_heading(line: str) -> str | None:
    """Canonical section for a line that is a heading, else None. A heading is a short line (≤ 8 words, ≤ 60
    chars) without a terminal period that matches HEADINGS after numbering is stripped; 'Results and discussion'
    → 'results'; ALL-CAPS variants count. Lines that are clearly sentences (contain ' the ' … or end with '.') are
    not headings."""
    s = " ".join(unicodedata.normalize("NFKC", line or "").split())
    if not s or len(s) > 60 or len(s.split()) > 8:
        return None
    core = _NUMBERING_RE.sub("", s, count=1).strip()
    if not core or core.endswith("."):
        return None
    core = core.rstrip(" :;,-–—")
    if _LETTER_SPACED_RE.match(core):      # 'A B S T R A C T' (letter-spaced Elsevier headings)
        core = core.replace(" ", "")
    if re.search(r"\bthe\b", core, re.I):
        return None
    for rx, section in _HEADING_RES:
        if rx.fullmatch(core):
            return section
    return None


# ---- rule tags --------------------------------------------------------------------------------------------

#: Sentence-type tags; each value is a case-insensitive regex. Ported from the previous TAGS dict, with fixes:
#: 'def' ends with (?!\w) instead of \b so 'i.e.' (period) can match, 'define(d)' tolerates up to three
#: intervening words before 'as'/'by' ('is generally defined here as'); 'method' stems ('approximat',
#: 'discreti[sz]', 'parameteri[sz]') take \w* (the old trailing \b never matched 'approximated').
TAG_PATTERNS: dict[str, str] = {
    "def": r"\b(define[sd]?( \w+){0,3} (as|by)|define[sd]? (the|it|this)|refers? to|referred to as|(is|are) known as|known as|termed|(is|are) called|denote[sd]?|stands? for|i\.e\.|namely|definition|(so|also)[- ]called|abbreviated (as|to)|"
           r"(is|are) (a|an|the) (type|kind|form|class|measure|phenomenon|process|method|technique|approach|quantity|metric|function|model|condition|strategy|framework|family|variant|extension|generali[sz]ation)|"
           r"(is|are) (the|a) (process|phenomenon|ratio|difference|amount|degree|ability|tendency|loss|reduction)|can be (described|defined|understood|viewed|regarded) as|describes? (the|how))(?!\w)",
    "quant": r"(\d+(\.\d+)?\s?(%|μm|µm|um\b|mm\b|cm\b|nm\b|mpa\b|gpa\b|kpa\b|pa\b|kn\b|n\b|hz\b|°|deg|days?\b|weeks?\b|months?\b|years?\b|hu\b|mg|kg\b|ml\b|g/cm|s\b|ms\b|min\b|h\b)|[<>≤≥±]\s?\d|\bthreshold|\branges? (from|between)|\bup to \d|\bapproximately \d|\bp\s?[<=]\s?0)",
    "method": r"\b(calculated|computed|computes?|measured|measures?|evaluated|estimated|quantified|formulated|formulation|given by|equation|eq\.|obtained (by|from|using|with)|assessed|determined|we (use|used|adopt|adopted|employ|employed|propose|proposed)|(is|was|were|are) (used|applied|implemented|adopted|employed)|implemented|algorithm|procedure|approximat\w*|discreti[sz]\w*|parameteri[sz]\w*|trained|training)\b",
    "mech": r"\b(because|due to|leads? to|led to|results? in|resulted in|caused by|causes?|associated with|attributed to|depends? on|dependent on|influenc|affect|increas|decreas|reduc|improv|promot|inhibit|enhanc|correlat|relationship|mechanism|governs?|drives?)\w*",
    "cmp": r"\b(compared (to|with)|whereas|in contrast|however|unlike|than|although|contrary|on the other hand|differ|versus|vs\.|outperform|superior|inferior|better|worse|advantage)\w*",
    "limit": r"\b(limitation|limited|challeng|drawback|disadvantage|remains? (unclear|unknown|a challenge|an open)|not yet|future (work|research|studies)|open (question|problem)|lack of|difficult|cannot|fails? to)\w*",
    "result": r"\b(found|showed|shown|shows|demonstrated|revealed|observed|indicat|suggest|significant|confirmed|predicted)\w*",
}
_TAG_RES: dict[str, re.Pattern[str]] = {k: re.compile(v, re.I) for k, v in TAG_PATTERNS.items()}


def tag_sentence(text: str) -> list[str]:
    """Tags from TAG_PATTERNS that match `text`, in TAGS order (atlas.core.schemas.TAGS)."""
    text = text or ""
    return [t for t in TAGS if t in _TAG_RES and _TAG_RES[t].search(text)]


# ---- garbage flags ----------------------------------------------------------------------------------------

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z\-]+")
CAPTION_RE = re.compile(r"^(fig(ure)?\.?\s*\d|table\s*\d|eq(uation)?\.?\s*\(?\d)", re.I)
REFLIKE_RE = re.compile(r"(et al\.?,?\s*(\(?\d{4}\)?)?.*){3,}|\[\d+(,\s*\d+)*\]\s*\[\d+", re.I)
AFFIL_RE = re.compile(r"\b(Department|Dept\.|University|Hospital|Institute|School|Faculty|Laboratory|College) of\b")


def garbage_flags(text: str) -> list[str]:
    """Flags for text that is not a usable sentence: 'short' (< 5 word tokens), 'low_alpha' (letters < 55 % of
    chars), 'numeric' (digits > 25 % of chars), 'link' (http/doi.org), 'affiliation' (≥ 2 'Department of /
    University of …' plus ≥ 4 commas), 'caption' (starts with Fig./Figure/Table/Eq. + number), 'reflike'
    (≥ 3 'et al. (year)' patterns or '[12] [13]' runs). Order fixed as listed."""
    text = (text or "").strip()
    flags: list[str] = []
    n = len(text)
    letters = sum(ch.isalpha() for ch in text)
    digits = sum(ch.isdigit() for ch in text)
    low = text.lower()
    if len(_WORD_RE.findall(text)) < 5:
        flags.append("short")
    if letters < 0.55 * n:
        flags.append("low_alpha")
    if digits > 0.25 * n:
        flags.append("numeric")
    if "http" in low or "doi.org" in low:
        flags.append("link")
    if len(AFFIL_RE.findall(text)) >= 2 and text.count(",") >= 4:
        flags.append("affiliation")
    if CAPTION_RE.match(text):
        flags.append("caption")
    if REFLIKE_RE.search(text):
        flags.append("reflike")
    return flags


def is_garbage(flags: list[str]) -> bool:
    """True for short, low_alpha, numeric, link, affiliation (caption and reflike are kept but flagged)."""
    return any(f in ("short", "low_alpha", "numeric", "link", "affiliation") for f in flags)
