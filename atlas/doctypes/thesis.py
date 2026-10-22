"""PhD thesis doctype (version `thesis1`): front matter masks, TOC-calibrated chapter units, per-chapter sections,
topic fields. Pure, like `paper` (increment 2 §4; architecture §8).

The rules were shaped on real UCL theses of four generations of layout (a 1996 scan with a text layer, 2004 LaTeX
and Word theses, 2018–2023 Word theses, a 2022 LaTeX thesis):

- Running headers / footers: a normalised line that recurs at the page edges of >= 3 pages is masked
  (`header_footer`), except the FIRST occurrence of a structural heading ('Chapter 2', 'Bibliography',
  'Acknowledgements'): LaTeX repeats the chapter or bibliography title as a running header on every page of that
  chapter, and the first occurrence is the heading itself. Bare page-number lines at the page edges (arabic numbers
  whose value - physical page is the document's dominant offset, and roman numerals) are masked too; the dominant
  offset also calibrates the TOC.
- TOC: starts at a 'Contents' / 'Table of contents' heading in the first 30 % of the pages and extends while the
  lines look like entries (dotted leaders, sub-numbers, page tokens), through the lists of figures / tables; it
  ends at a split entry that never sees a page number or leaders and starts at the top of a page (the first body
  heading), at an 'Abstract' or 'Chapter N' heading, or after 4 stray lines. Top-level entries come in three
  shapes: one line ('Chapter 3. Clinical assessment … 109', '2 Background …… 38'), split lines (Word: '1.' /
  'Introduction and objectives …… 29'; LaTeX: '1' / 'Introduction' / '12'; Word numbering: '1. CHAPTER 1 …… 18' /
  title / '… 18') and wrapped titles; a bare number is the next chapter number when it continues the sequence and
  follows a page number or opens a number / title / page triple, else a page token. The printed → physical page
  offset is voted by entries whose number and title match a detected chapter (or whose title is found on a
  physical page); >= 3 agreeing votes, every entry agreeing, or >= 1 vote that agrees with the page-number offset
  (`footer_offset`) resolve it (`toc_matched`), else `page_offset_unresolved`; without a TOC heading `no_toc`.
- Chapter headings, in order of trust: 'Chapter N [. : –] Title' / 'CHAPTER ONE' / 'Chapter IV: Results' lines at
  the top of a page, Word numbering 'N. CHAPTER N' with the title on the following line(s), LaTeX 'N' alone at
  the top of a page followed by the title line (`chapter_regex`; chapter source `latex`); fallback: numbered
  top-level headings '1 Introduction' / '2 Background' at the top of a page (`numbered_fallback`). A heading with
  an empty title takes the next line(s); wrapped titles are joined (continuation lines start in lower case or
  follow a dangling 'and' / 'of' / 'for' …, <= 70 chars). Rejected: lines followed by dotted leaders (TOC context),
  in-text mentions ('Chapter 2 introduces …', 'Chapter 4 (Establishment …)', '6 I agree to …'), titles of more than
  12 words after 'Chapter N', numbers out of sequence, and (for headings without the word 'Chapter') anything after
  a 'References' / 'Bibliography' heading at the top of a page. The accepted chapters form the longest chain 1, 2,
  3 …; a missing number is tolerated once (`sequence_gap`) unless the expected number appears later; a chapter
  the TOC lists at a page where no heading was found is inserted from the TOC (`toc_filled`, never after the last
  detected chapter).
- Units: one `document` unit + one `chapter` unit per chapter (`unit_id = doc_id/slug(heading)[:24]`, `~k` on
  duplicates; `unit_kind` by heading keywords; confidence by detection path: heading + TOC 0.9, heading only 0.8,
  numbered + TOC 0.75, TOC only 0.7, numbered only 0.6; -0.15 for a sequence gap, -0.1 for a TOC-filled
  chapter). Appendices and the bibliography after the last chapter are units of kind `chapter` with
  `unit_kind: other` (sections `appendix` / `references`).
- Front matter (`reason: front_matter`): everything before the first chapter — title page, ProQuest notice,
  declaration, acknowledgements, lists, impact statement — except the abstract block (`Abstract` / `Summary`
  heading until the next front-matter heading or 600 words), which stays section `abstract`. TOC lines are
  `reason: toc`. Without any chapter the front matter ends after the TOC and the rest is body (`other`).
- Sections: the chapter heading block is `other`; the chapter's default section follows from its unit_kind (intro /
  lit_review → introduction, methods → methods, …; a chapter titled 'References' / 'Appendix' → that section);
  canonical sub-headings inside the chapter (`segment.detect_heading_ex`) override until the next; 'References' /
  'Bibliography' inside a chapter makes the rest of the chapter `references`; a late 'Summary' inside a chapter
  is `conclusion`.
- `topic_fields`: document = manifest title (else the title lines of page 1) + the abstract block (<= 600 words);
  chapter = heading + the first 300 words after the heading block.
- Thresholds: `max_drop_ratio 0.30`, `min_chapter_confidence 0.7` (ingest warns below it and writes a chapter
  draft the user can correct through `curated/units_override.yaml`).
"""
from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field

from atlas.core import segment
from atlas.core.ids import unit_id as make_unit_id
from atlas.core.schemas import DocMeta, LineMask, PageText, SectionMark, Unit
from .base import Thresholds, register
from .paper import HF_MIN_PAGES, REFCUE, REFSTART, _all_lines, _header_footer_keys, _Line, _line_key

_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
          "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
          "eighteen": 18, "nineteen": 19, "twenty": 20}
_ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7, "VIII": 8, "IX": 9, "X": 10, "XI": 11,
          "XII": 12, "XIII": 13, "XIV": 14, "XV": 15, "XVI": 16, "XVII": 17, "XVIII": 18, "XIX": 19, "XX": 20}
_NUM = r"(?:\d{1,2}|[IVXivx]{1,6}|[A-Za-z]{3,9})"
_PAGE = r"(\d{1,3}|[ivxlcIVXLC]{1,6})"
CHAPTER_RE = re.compile(rf"^(?:chapter|CHAPTER|Chapter)\s+({_NUM})\b[\s.:\-–—|]*(.*)$")
#: Word numbering: '1. CHAPTER 1', '2 CHAPTER TWO: Title'
WORD_CHAPTER_RE = re.compile(rf"^(\d{{1,2}})[.:\s]+chapter\s+({_NUM})\b[\s.:\-–—|]*(.*)$", re.I)
TOC_HEAD_RE = re.compile(r"^(?:table of contents|contents|list of contents|table of content)\s*$", re.I)
TOC_SUB_RE = re.compile(r"^(?:\d{1,2}|[A-Z])\.\d")
TOC_LINE_RE = re.compile(rf"^(?:chapter\s+)?({_NUM})(?!\.\d)[.:]?\s+(.+?)[\s.·…_]*\s{_PAGE}\s*$", re.I)
TOC_START_RE = re.compile(rf"^(?:chapter\s+)?({_NUM})(?!\.\d)\.?[\s.·…_]*$", re.I)
TOC_OPEN_RE = re.compile(rf"^(?:chapter\s+)?({_NUM})(?!\.\d)[.:]?\s+([A-Za-z].*?)[\s.·…_]*$", re.I)
_DOTS_RE = re.compile(r"[.·…_]{3,}|(?:\. ){3,}")
_ENDS_PAGE_RE = re.compile(rf"(.*?)[\s.·…_]+{_PAGE}\s*$")
_PAGE_TOKEN_RE = re.compile(rf"^{_PAGE}$")
_ROMAN_PAGE_RE = re.compile(r"^(?=[ivxlc])m{0,3}(c[md]|d?c{0,3})(x[cl]|l?x{0,3})(i[xv]|v?i{0,3})$", re.I)
_BARE_NUMBER_RE = re.compile(r"^\d{1,4}$")
_FIGTAB_RE = re.compile(r"^(?:fig(?:ure)?|table|plate|scheme)\s*[\dA-Z]", re.I)
LIST_HEAD_RE = re.compile(r"^(?:list of (?:figures|tables|plates|abbreviations|symbols|acronyms|publications|appendices|"
                          r"contents|illustrations|equations)|abbreviations|acronyms|nomenclature|glossary|declaration|"
                          r"statement of originality|acknowledg(?:e)?ments?|dedication|impact statement|preface|"
                          r"copyright(?: statement)?|table of contents|contents|publications?|"
                          r"research paper declaration form|ucl research paper declaration form)\s*[.:]?\s*$", re.I)
ABSTRACT_RE = re.compile(r"^(?:abstract|summary|synopsis)\s*[.:]?\s*$", re.I)
TAIL_RE = re.compile(r"^(?:(\d{1,2}|[A-Z]|chapter\s+\d{1,2})[\s.:\-–—]+)?(appendix|appendices|bibliography|references|"
                     r"list of references|works cited|supplementary material|literature cited)\b", re.I)
_AUTHOR_LINE_RE = re.compile(r"^[A-Z][a-z]+(?: [A-Z]\.?)* [A-Z][a-z]+$")      # 'Alice Northwood', 'Mark C. Ransley'
#: title-page formulas that are never the title ('A thesis submitted …', 'Doctor of Philosophy', 'University College London')
_TITLE_PAGE_RE = re.compile(r"\b(?:19|20)\d\d\b|universit|(?:thesis|dissertation) (?:submitted|presented)|submitted (?:to|for|in)\b|"
                            r"for the degree|degree of|doctor of|philosophy|department|faculty|school of|institute of|"
                            r"supervis|proquest|copyright|©|^by\b|\bby$|date of submission|college", re.I)
#: unit kinds by heading keyword, first hit wins; 'Experimental results' is results, 'Evaluation methodology' is
#: methods ('results'/'findings' outrank the methods words, which outrank the looser results words)
KIND_RES: list[tuple[str, re.Pattern[str]]] = [
    ("lit_review", re.compile(r"literature|review|state of the art|related work|previous work|theoretical background", re.I)),
    ("intro", re.compile(r"introduction|background|motivation|overview|aims? and objectives|scope|thesis outline", re.I)),
    ("results", re.compile(r"\bresults?\b|\bfindings\b", re.I)),
    ("discussion", re.compile(r"discussion|implication|limitation", re.I)),
    ("conclusion", re.compile(r"conclusion|summary|future work|outlook|recommendation|contribution", re.I)),
    ("methods", re.compile(r"method|materials|experimental|approach|design|implementation|model(?:ling|ing)?|framework|"
                           r"development|setup|procedure|technique|algorithm|protocol|theory|formulation", re.I)),
    ("results", re.compile(r"experiment|evaluation|validation|case stud|analysis|study|assessment|performance|"
                           r"application|comparison|investigation|characteri[sz]ation|simulation", re.I)),
]
KIND_SECTION = {"intro": "introduction", "lit_review": "introduction", "methods": "methods", "results": "results",
                "discussion": "discussion", "conclusion": "conclusion", "other": "other"}
ABSTRACT_MAX_WORDS = 600
CHAPTER_WORDS = 300
FRONT_MAX_SHARE = 0.5          # the front matter never extends beyond half of the document …
FRONT_GUARD_MIN_PAGES = 20     # … (checked for documents of >= 20 pages; shorter ones are excerpts or fixtures)
TOC_MAX_PAGES = 30             # a TOC (with the lists of figures / tables) spans at most this many pages
TOC_GAP_LINES = 4              # … and ends after this many consecutive non-entry lines
TOC_SEARCH_SHARE = 0.3         # the TOC heading sits in the first 30 % of the pages (at least 8)
TOP_OF_PAGE_LINES = 12         # a chapter heading sits within the first 12 non-empty lines of its page
CHAPTER_MAX = 30               # chapter numbers above this are page numbers, years or list items
EDGE_LINES = 3                 # page numbers sit within the first / last 3 non-empty lines of a page
PAGE_NUMBER_MIN_PAGES = 3      # … and the dominant offset needs at least 3 pages (and 5 % of the pages)
TITLE_MAX_WORDS = 12           # 'Chapter N <title>' on one line: longer tails are in-text mentions
NEXT_TITLE_MAX_WORDS = 20      # a title on the line(s) after 'Chapter N'
CONTINUATION_MAX_CHARS = 70
CONTINUATION_MAX_LINES = 2
TOC_VOTES = 3
TOC_TITLE_OVERLAP = 0.6
CONFIDENCE = {"heading+toc": 0.9, "heading": 0.8, "numbered+toc": 0.75, "toc": 0.7, "numbered": 0.6}
GAP_PENALTY = 0.15
TOC_FILL_PENALTY = 0.1
_FIRST_PERSON_RE = re.compile(r"^I\b|^(?:we|you|my|your)\b", re.I)       # consent-form items: '6 I agree to …'
#: a wrapped heading line that cannot end a title ('A Model for Documents and' / 'Constraints'); the articles only
#: in lower case ('Appendix A' is complete)
_DANGLING_RE = re.compile(r"\b(?i:and|or|of|the|for|in|on|to|with|by|from|into|between|through|using|via|its|their)$|"
                          r"\b(?:a|an)$|[,\-–:]$")
#: words that make 'Chapter N …' an in-text mention ('Chapter 2 introduces the methods'); a title may contain 'the'
_VERBISH = re.compile(r"\b(is|are|was|were|has|have|had|will|can|introduces?|presents?|describes?|discusses?|shows?|"
                      r"provides?|gives?|reports?|covers?|explains?|outlines?|details?|focuses|examines?|explores?|"
                      r"investigates?|addresses|considers?|concludes?|summari[sz]es?|deals|contains?|includes?|begins?|"
                      r"ends?|continues?|extends?|builds?|proposes?|evaluates?|compares?|analy[sz]es|highlights?|"
                      r"demonstrates?|aims?|seeks?|sets out|of this|in this|this thesis|this chapter|we|also|then|"
                      r"therefore|thus|which|where|when|these|those|our)\b", re.I)


def _heading_like(rest: str) -> bool:
    """The text after 'Chapter N' looks like a heading, not a sentence fragment or an in-text mention."""
    if not rest:
        return True
    if rest.endswith(".") or len(rest.split()) > TITLE_MAX_WORDS or not (rest[0].isupper() or rest[0].isdigit()):
        return False
    if (_VERBISH.search(rest) or _FIRST_PERSON_RE.match(rest)) and not rest.isupper():
        return False
    return sum(c.isalpha() for c in rest) >= 3


def _not_reference(s: str) -> bool:
    return not (REFSTART.match(s) and sum(1 for _ in REFCUE.finditer(s)) >= 1)


def _title_line(s: str) -> bool:
    """A line that can be the title of a chapter whose number stands alone on the previous line."""
    if not s or len(s.split()) > NEXT_TITLE_MAX_WORDS or len(s) > 160 or sum(c.isalpha() for c in s) < 3:
        return False
    if not (s[0].isupper() or s[0].isdigit() or s[0] in "‘'\"("):
        return False
    return not (_DOTS_RE.search(s) or _FIGTAB_RE.match(s) or _PAGE_TOKEN_RE.match(s)) and _not_reference(s)


def _chapter_heading(s: str) -> bool:
    """'Chapter 3', 'CHAPTER ONE: Title', '2. CHAPTER 2' with a resolvable number ('Chapter Summary' is not)."""
    if m := CHAPTER_RE.match(s):
        return _chapter_number(m.group(1)) is not None
    if m := WORD_CHAPTER_RE.match(s):
        return _chapter_number(m.group(2)) is not None or _chapter_number(m.group(1)) is not None
    return False


def _structural(s: str) -> bool:
    """A heading that LaTeX / Word may repeat as a running header; its first occurrence is the heading itself."""
    return bool(_chapter_heading(s) or TAIL_RE.match(s) or LIST_HEAD_RE.match(s) or ABSTRACT_RE.match(s)
                or TOC_HEAD_RE.match(s))


@dataclass
class _Chapter:
    number: int
    heading: str
    page_no: int
    cstart: int
    line_idx: int            # the line that starts the heading block ('Chapter 1', '1', '1 Introduction')
    end_idx: int             # the last line of the heading block (title / continuation lines)
    source: str              # chapter_regex | latex | numbered | toc | tail:references | tail:appendix
    kind: str = "chapter"    # chapter | tail (appendix / bibliography)


@dataclass
class _TocEntry:
    number: int
    title: str
    printed: int | None
    line_idx: int


@dataclass
class _Pending:
    """A split-line TOC entry being collected ('1.' / 'Introduction …… 29')."""
    number: int
    parts: list[str]
    line_idx: int
    last_before: int         # TOC end before this entry started (restored when it turns out to be a body heading)
    dots: bool = False       # leader dots or a page token seen
    hint: int | None = None  # page printed on the opening line ('1. CHAPTER 1 …… 18'; Word repeats it on the last)


@dataclass
class _Analysis:
    lines: list[_Line]
    n_pages: int
    hf_idx: set[int]
    toc_idx: set[int]
    toc_entries: list[_TocEntry]
    chapters: list[_Chapter]
    tails: list[_Chapter]
    offset: int | None
    reasons: list[str]
    first_body: int | None
    abstract: tuple[int, int] | None          # line index range [a, b): the heading line and the block
    base: float = 0.0
    page_numbers: set[int] = field(default_factory=set)


def _chapter_number(tok: str) -> int | None:
    if tok.isdigit():
        return int(tok)
    if tok.upper() in _ROMAN:
        return _ROMAN[tok.upper()]
    return _WORDS.get(tok.lower())


def _printed_page(tok: str | None) -> int | None:
    if not tok:
        return None
    if tok.isdigit():
        return int(tok)
    return _ROMAN.get(tok.upper())


def unit_kind_of(heading: str | None) -> str:
    h = heading or ""
    for kind, rx in KIND_RES:
        if rx.search(h):
            return kind
    return "other"


def _norm(s: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", s.lower()).split())


def _clean_title(s: str) -> str:
    s = _DOTS_RE.sub(" ", s)
    s = re.sub(r"[\s.·…_:\-–—|]+$", "", " ".join(s.split()))
    return s.strip()


def _join_heading(parts: Sequence[str]) -> str:
    """The lines of a wrapped heading as one line: a soft hyphen vanishes, a line that ends with a hyphen joins
    its continuation without a space ('endo-' / 'prostheses'), trailing punctuation goes."""
    out = ""
    for part in parts:
        out = out + part if (out.endswith(("-", "\xad")) or not out) else out + " " + part
    return " ".join(out.replace("\xad", "").split()).rstrip(" .:;,-–—")


def _titles_match(a: str, b: str) -> bool:
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    if na == nb or na in nb or nb in na:
        return True
    wa, wb = set(na.split()), set(nb.split())
    return len(wa & wb) / max(1, min(len(wa), len(wb))) >= TOC_TITLE_OVERLAP


def _ranks(lines: Sequence[_Line], skip: set[int]) -> dict[int, int]:
    """Line index → 1-based rank among the content lines of its page (lines in `skip` do not count)."""
    out: dict[int, int] = {}
    rank, cur_page = 0, None
    for i, ln in enumerate(lines):
        if ln.page_no != cur_page:
            cur_page, rank = ln.page_no, 0
        if ln.text and i not in skip:
            rank += 1
            out[i] = rank
    return out


def _top_lines(lines: Sequence[_Line], skip: set[int], n: int) -> set[int]:
    return {i for i, r in _ranks(lines, skip).items() if r <= n}


def _unmask_structural(lines: Sequence[_Line], hf_raw: set[int], exclude: set[int]) -> set[int]:
    """Running-header lines minus the structural headings among them. LaTeX and Word repeat 'Chapter 2' /
    'Bibliography' / 'Acknowledgements' as a running header on consecutive pages: the first page of each run
    holds the heading itself. A text that occurs on fewer than HF_MIN_PAGES pages ('Chapter 6' in a chapter list
    and as the heading) is no running header at all. Lines in `exclude` (the TOC) do not open a run."""
    groups: dict[str, list[int]] = {}
    for i in sorted(hf_raw - exclude):
        if _structural(lines[i].text):
            groups.setdefault(_norm(lines[i].text), []).append(i)
    out = set(hf_raw)
    for idxs in groups.values():
        if len({lines[i].page_no for i in idxs}) < HF_MIN_PAGES:
            out -= set(idxs)
            continue
        prev_page = -2
        for i in idxs:
            if lines[i].page_no > prev_page + 1:
                out.discard(i)
            prev_page = lines[i].page_no
    return out


def _page_number_lines(lines: Sequence[_Line]) -> tuple[set[int], int | None]:
    """Indexes of bare page-number lines at the page edges and the dominant printed - physical offset. A page
    holds one page number: when two edge lines carry the same value, the outermost one is the page number and the
    other a chapter number ('1' opening a LaTeX TOC on printed page 1)."""
    by_page: dict[int, list[int]] = {}
    for i, ln in enumerate(lines):
        if ln.text:
            by_page.setdefault(ln.page_no, []).append(i)
    arabic: dict[int, list[tuple[int, int]]] = {}        # page → [(idx, offset)]
    roman: list[int] = []
    for pno, idxs in by_page.items():
        for i in sorted(set(idxs[:EDGE_LINES] + idxs[-EDGE_LINES:])):
            s = lines[i].text
            if _BARE_NUMBER_RE.match(s):
                arabic.setdefault(pno, []).append((i, int(s) - pno))
            elif _ROMAN_PAGE_RE.match(s):
                roman.append(i)
    offset: int | None = None
    if arabic:
        off, n = Counter(o for rows in arabic.values() for _, o in rows).most_common(1)[0]
        if n >= max(PAGE_NUMBER_MIN_PAGES, 0.05 * len(by_page)):
            offset = off
    idx = set(roman)
    if offset is not None:
        for pno, rows in arabic.items():
            hits = [i for i, o in rows if o == offset]
            if len(hits) > 1:
                first, last = by_page[pno][0], by_page[pno][-1]
                hits = [i for i in hits if i in (first, last)] or hits[:1]
            idx.update(hits)
    return idx, offset


def _entry_start(s: str) -> tuple[str, int, list[str], int | None] | None:
    """How a TOC line opens a top-level entry: ('line', number, [title], printed) for a complete one-line entry,
    ('open', number, [title so far], printed hint) for a split-line entry whose title / page follow; None for
    other lines (sub-entries, captions, prose). Numbers above CHAPTER_MAX are never chapters."""
    ends_page = _ENDS_PAGE_RE.match(s)
    hint = _printed_page(ends_page.group(2)) if ends_page and _DOTS_RE.search(s) else None
    if m := WORD_CHAPTER_RE.match(s):
        num = _chapter_number(m.group(2)) or _chapter_number(m.group(1))
        rest = _clean_title(re.sub(rf"[\s.·…_]+{_PAGE}\s*$", "", m.group(3)) if hint is not None else m.group(3))
        kind, parts, printed = "open", [rest] if rest else [], hint
    elif m := TOC_LINE_RE.match(s):
        num = _chapter_number(m.group(1))
        kind, parts, printed = "line", [_clean_title(m.group(2))], _printed_page(m.group(3))
    elif m := TOC_START_RE.match(s):
        num = _chapter_number(m.group(1))
        kind, parts, printed = "open", [], None
    elif (m := TOC_OPEN_RE.match(s)) and not ends_page:
        num = _chapter_number(m.group(1))
        kind, parts, printed = "open", [_clean_title(m.group(2))], None
    else:
        return None
    if num is None or not 0 <= num <= CHAPTER_MAX:
        return None
    return kind, num, parts, printed


def _toc_triple(lines: Sequence[_Line], j: int, skip: set[int]) -> bool:
    """Line j (a bare number) opens a split TOC entry: the next content line is a title (a letter first, not a
    sub-entry or a list heading) and the one after it is a page token, a sub-entry or a dotted line."""
    nxt = [k for k in range(j + 1, min(j + 12, len(lines))) if lines[k].text and k not in skip][:2]
    if len(nxt) < 2:
        return False
    t1, t2 = lines[nxt[0]].text, lines[nxt[1]].text
    if not t1[0].isalpha() or TOC_SUB_RE.match(t1) or LIST_HEAD_RE.match(t1) or _PAGE_TOKEN_RE.match(t1):
        return False
    return bool(_PAGE_TOKEN_RE.match(t2) or TOC_SUB_RE.match(t2) or _DOTS_RE.search(t2) or _ENDS_PAGE_RE.match(t1))


def _parse_toc(lines: Sequence[_Line], start: int, skip: set[int], top: set[int]) -> tuple[list[_TocEntry], int]:
    """Top-level entries of the TOC that starts at line `start` and the index of its last line. `top` holds the
    indexes of the first two content lines of every page: a split-line entry that starts there and never sees a
    page number or leader dots is a body chapter heading ('1' / 'Introduction'), where the TOC ends."""
    entries: list[_TocEntry] = []
    seen: set[int] = set()
    last = start                      # last line that belongs to the TOC
    gap = 0                           # consecutive content lines that do not
    start_page = lines[start].page_no
    pending: _Pending | None = None
    prev_kind = "page"                # what the previous TOC line was: 'page' (ends with / is a page number) | 'other'
    last_number = 0                   # the last chapter number opened

    def commit(printed: int | None) -> bool:
        """Close the pending split-line entry; False (with the TOC end rolled back) when it never saw a page
        number or leader dots."""
        nonlocal pending, last
        p, pending = pending, None
        if p is None:
            return True
        if printed is None:
            printed = p.hint
        if printed is None and not p.dots:
            last = p.last_before
            return False
        title = _clean_title(" ".join(p.parts))
        if title and p.number not in seen:
            seen.add(p.number)
            entries.append(_TocEntry(p.number, title, printed, p.line_idx))
        return True

    for j in range(start + 1, len(lines)):
        ln = lines[j]
        if ln.page_no > start_page + TOC_MAX_PAGES:
            break
        s = ln.text
        if not s or j in skip:
            continue
        ends_page = _ENDS_PAGE_RE.match(s)
        page_tok = bool(_PAGE_TOKEN_RE.match(s))
        dots = bool(_DOTS_RE.search(s))
        is_sub = bool(TOC_SUB_RE.match(s))
        opening = None if is_sub else _entry_start(s)
        if pending is None and page_tok and _BARE_NUMBER_RE.match(s) and j not in top:
            # LaTeX TOC: '1' / 'Introduction' / '12' — a bare number is the next chapter number when it continues
            # the chapter sequence and either follows a page number or opens a number / title / page triple;
            # otherwise it is a page number (at the top of a page it stays a possible chapter heading, so that
            # the TOC ends where the body starts)
            if not (int(s) == last_number + 1 and (prev_kind == "page" or _toc_triple(lines, j, skip))):
                opening = None
        if pending is not None:
            if page_tok:
                pending.dots = True
                commit(_printed_page(s))
                last, prev_kind = j, "page"
                continue
            if re.fullmatch(r"[.·…_ ]+", s):
                pending.dots = True                       # LaTeX leader line
                last, prev_kind = j, "other"
                continue
            new_entry = opening is not None or is_sub or LIST_HEAD_RE.match(s)
            if not new_entry and ends_page and dots:      # the title's last line with leaders and the page
                pending.parts.append(ends_page.group(1))
                pending.dots = True
                commit(_printed_page(ends_page.group(2)))
                last, prev_kind = j, "page"
                continue
            if not new_entry and len(pending.parts) <= CONTINUATION_MAX_LINES and len(s) < 160:
                pending.parts.append(s)
                pending.dots = pending.dots or dots
                last, prev_kind = j, "other"
                continue
            head_idx = pending.line_idx
            if not commit(None):
                if head_idx in top:
                    break                                 # a body chapter heading at the top of a page
                gap += 1
                if gap >= TOC_GAP_LINES:
                    break
        tocish = True
        if is_sub:
            pass
        elif opening is not None:
            kind, num, parts, printed = opening
            last_number = num
            if kind == "line":
                if num not in seen:
                    seen.add(num)
                    entries.append(_TocEntry(num, parts[0], printed, j))
            else:
                pending = _Pending(num, parts, j, last, dots, printed)
        elif dots or (_FIGTAB_RE.match(s) and ends_page):
            pass
        elif (ends_page or page_tok) and j <= last + 2:
            pass
        else:
            tocish = False
        if tocish:
            last = j
            gap = 0
            prev_kind = "page" if (page_tok or (ends_page and (dots or is_sub or opening))) else "other"
            continue
        if _chapter_heading(s) or ABSTRACT_RE.match(s):
            break
        gap += 1                      # list headings ('List of Figures') and stray lines; the entries may go on
        if gap >= TOC_GAP_LINES:
            break
    if pending is not None:
        commit(None)
    return entries, last


class ThesisDocType:
    name = "thesis"
    version = "thesis1"
    thresholds = Thresholds(max_drop_ratio=0.30, min_chapter_confidence=0.7)

    def __init__(self) -> None:
        self._cache: tuple[tuple, _Analysis] | None = None

    # ---- analysis shared by the four methods (pure; memoised on the page texts) --------------------------------
    def _analyse(self, pages: Sequence[PageText]) -> _Analysis:
        key = (id(pages), len(pages), sum(len(p.text) for p in pages), hash(pages[0].text) if pages else 0,
               hash(pages[-1].text) if pages else 0)
        if self._cache is not None and self._cache[0] == key:
            return self._cache[1]
        a = self._compute(pages)
        self._cache = (key, a)
        return a

    def _compute(self, pages: Sequence[PageText]) -> _Analysis:
        lines = _all_lines(pages)
        n_pages = pages[-1].page_no if pages else 0
        hf = _header_footer_keys(pages)
        hf_raw = {i for i, ln in enumerate(lines) if ln.text and 4 <= len(ln.text) <= 200 and _line_key(ln.text) in hf}
        page_numbers, footer_offset = _page_number_lines(lines)
        hf_idx = _unmask_structural(lines, hf_raw, set())
        # --- TOC region (the running header 'Contents' of LaTeX TOC pages is in hf; its first line is the heading)
        toc_start: int | None = None
        for i, ln in enumerate(lines):
            if ln.page_no > max(8, int(n_pages * TOC_SEARCH_SHARE)):
                break
            if ln.text and TOC_HEAD_RE.match(ln.text) and i not in page_numbers:
                toc_start = i
                break
        toc_entries: list[_TocEntry] = []
        toc_end: int | None = None
        if toc_start is not None:
            hf_idx.discard(toc_start)
            toc_entries, toc_end = _parse_toc(lines, toc_start, hf_idx | page_numbers,
                                              _top_lines(lines, hf_idx | page_numbers, 2))
        toc_idx = set(range(toc_start, toc_end + 1)) if toc_start is not None else set()
        hf_idx = _unmask_structural(lines, hf_raw, toc_idx)       # the TOC's 'Chapter 3.' line is no heading
        if toc_start is not None:
            hf_idx.discard(toc_start)
        skip = hf_idx | page_numbers
        rank_of = _ranks(lines, skip)
        top_idx = {i for i, r in rank_of.items() if r <= TOP_OF_PAGE_LINES}

        def next_content(i: int, same_page: bool = True) -> int | None:
            for k in range(i + 1, len(lines)):
                if same_page and lines[k].page_no != lines[i].page_no:
                    return None
                if lines[k].text and k not in skip:
                    return k
            return None

        def toc_context(i: int) -> bool:
            k, n = i, 0
            while n < 3 and (k := next_content(k)) is not None:
                if _DOTS_RE.search(lines[k].text):
                    return True
                n += 1
            return False

        # --- chapter heading candidates in the body (not TOC, not running headers)
        def title_block(i: int, rest: str) -> tuple[str, int]:
            """Heading text and the last line of the heading block for a heading line i whose own text is `rest`."""
            parts = [rest] if rest else []
            end = i
            k = next_content(i)
            if not parts:
                if k is None or not _title_line(lines[k].text) or CHAPTER_RE.match(lines[k].text) \
                        or WORD_CHAPTER_RE.match(lines[k].text) or segment.split_numbered_heading(lines[k].text) \
                        or TOC_SUB_RE.match(lines[k].text):
                    return "", i
                parts.append(lines[k].text)
                end = k
                k = next_content(k)
            n = 0
            while k is not None and n < CONTINUATION_MAX_LINES:
                s = lines[k].text
                if not (s[0].islower() or _DANGLING_RE.search(parts[-1])) or len(s) > CONTINUATION_MAX_CHARS \
                        or s.endswith(".") and len(s.split()) > 6 or _DOTS_RE.search(s) or TOC_SUB_RE.match(s) \
                        or segment.split_numbered_heading(s) or _PAGE_TOKEN_RE.match(s):
                    break
                parts.append(s)
                end = k
                n += 1
                k = next_content(k)
            heading = _join_heading(parts)
            if len(heading.split()) > NEXT_TITLE_MAX_WORDS:
                heading = " ".join(heading.split()[:NEXT_TITLE_MAX_WORDS])
            return heading, end

        cands: list[_Chapter] = []
        for i, ln in enumerate(lines):
            s = ln.text
            if not s or i in skip or i in toc_idx or i not in top_idx or len(s) > 120:
                continue
            m = CHAPTER_RE.match(s)
            mw = WORD_CHAPTER_RE.match(s)
            if m or mw:
                num = _chapter_number(mw.group(2)) if mw else _chapter_number(m.group(1))
                if num is None and mw:
                    num = _chapter_number(mw.group(1))
                rest = (mw.group(3) if mw else m.group(2)).strip(" .:-–—|")
                if num is None or _DOTS_RE.search(s) or (rest and re.search(r"\s\d{1,3}$", rest)) or toc_context(i):
                    continue                                          # a stray TOC line
                if not _heading_like(rest):
                    continue                                          # 'Chapter 2 introduces an automated …'
                heading, end = title_block(i, rest)
                cands.append(_Chapter(num, heading or f"Chapter {num}", ln.page_no, ln.cstart, i, end, "chapter_regex"))
            elif _BARE_NUMBER_RE.match(s) and rank_of.get(i, 99) <= 2 and 1 <= int(s) <= 30:
                k = next_content(i)                                   # LaTeX: '4' / 'A Model for Documents'
                if k is None or not _title_line(lines[k].text) or CHAPTER_RE.match(lines[k].text) \
                        or segment.split_numbered_heading(lines[k].text) or toc_context(i):
                    continue
                k2 = next_content(k)
                if k2 is not None and _PAGE_TOKEN_RE.match(lines[k2].text):
                    continue
                heading, end = title_block(i, "")
                if heading:
                    cands.append(_Chapter(int(s), heading, ln.page_no, ln.cstart, i, end, "latex"))
        chapters = self._sequence(self._before_references(cands, lines, rank_of, skip))
        reasons: list[str] = []
        if chapters:
            reasons.append("chapter_regex")

        # --- numbered fallback: '1 Introduction' / '2. Background' at the top of a page, numbers in sequence
        if not chapters:
            fb: list[_Chapter] = []
            for i, ln in enumerate(lines):
                s = ln.text
                if not s or i in skip or i in toc_idx or rank_of.get(i, 99) > 3 or len(s) > 120:
                    continue
                if toc_end is not None and i <= toc_end:
                    continue
                hit = segment.split_numbered_heading(s)
                if hit is None or re.search(r"\s\d{1,3}$", s) or not _not_reference(s) or toc_context(i):
                    continue
                n, head = hit
                if not _heading_like(head) or s.rstrip().endswith("."):
                    continue
                heading, end = title_block(i, head)
                fb.append(_Chapter(n, heading, ln.page_no, ln.cstart, i, end, "numbered"))
            chapters = self._sequence(self._before_references(fb, lines, rank_of, skip))
            if chapters:
                reasons.append("numbered_fallback")

        # --- TOC calibration: printed → physical page offset
        offset: int | None = None
        if toc_entries:
            votes: Counter[int] = Counter()
            for e in toc_entries:
                if e.printed is None:
                    continue
                ch = next((c for c in chapters if c.number == e.number), None)
                if ch is not None and _titles_match(ch.heading, e.title):
                    votes[ch.page_no - e.printed] += 1
                    continue
                key = _norm(e.title)
                for i, ln in enumerate(lines):
                    if i in toc_idx or i in skip or i not in top_idx or (toc_end is not None and i <= toc_end):
                        continue
                    if ln.text and _norm(ln.text) == key:
                        votes[ln.page_no - e.printed] += 1
                        break
            if votes:
                best, n = votes.most_common(1)[0]
                n_printed = sum(1 for e in toc_entries if e.printed is not None)
                if n >= TOC_VOTES or (n >= 2 and n == n_printed):
                    offset = best                             # >= 3 agreeing entries, or every entry agrees
                elif footer_offset is not None and votes.get(footer_offset):
                    offset = footer_offset
                    reasons.append("footer_offset")
            if offset is not None:
                reasons.append("toc_matched")
            else:
                reasons.append("page_offset_unresolved")
        else:
            reasons.append("no_toc")

        # --- chapters from the TOC: all of them when nothing was found, else the numbers the chain misses before
        # its last chapter (entries after it are appendices and forms the TOC numbers on)
        if offset is not None:
            known = {c.number for c in chapters}
            last_number = max(known) if known else CHAPTER_MAX
            filled: list[_Chapter] = []
            for e in toc_entries:
                if e.printed is None or e.number in known or e.number > last_number:
                    continue
                pno = e.printed + offset
                if not 1 <= pno <= n_pages or (toc_end is not None and pno <= lines[toc_end].page_no):
                    continue
                k = next((i for i, ln in enumerate(lines) if ln.page_no == pno and ln.text and i not in skip), None)
                if k is None:
                    continue
                filled.append(_Chapter(e.number, e.title, pno, lines[k].cstart, k, k, "toc"))
            if filled:
                merged = self._sequence(chapters + filled)
                if len(merged) > len(chapters):
                    chapters = merged
                    reasons.append("toc_filled")
        if chapters and "chapter_regex" not in reasons and "numbered_fallback" not in reasons:
            reasons.append("toc_only")
        if chapters and any(b.number != a.number + 1 for a, b in zip(chapters, chapters[1:])):
            reasons.append("sequence_gap")

        # --- tail units (appendix / bibliography after the last chapter)
        tails: list[_Chapter] = []
        if chapters:
            last_ch = chapters[-1]
            for i in range(last_ch.end_idx + 1, len(lines)):
                ln = lines[i]
                s = ln.text
                if not s or i in skip or i in toc_idx or len(s) > 80 or s.endswith(".") and len(s.split()) > 3:
                    continue
                m = TAIL_RE.match(s)
                if not (m and len(s.split()) <= 8 and ln.page_no > last_ch.page_no and rank_of.get(i, 99) <= 3):
                    continue
                if any(_norm(t.heading) == _norm(s) or t.page_no == ln.page_no for t in tails):
                    continue
                word = m.group(2).lower()
                sec = "references" if ("bib" in word or "ref" in word or "cited" in word) else "appendix"
                heading, end = title_block(i, s.rstrip(" .:"))
                tails.append(_Chapter(len(chapters) + len(tails) + 1, heading, ln.page_no, ln.cstart, i, end,
                                      "tail:" + sec, kind="tail"))

        # --- front matter end and abstract block
        first_body = chapters[0].line_idx if chapters else None
        if first_body is None and toc_end is not None:
            first_body = toc_end + 1
        if (first_body is not None and n_pages >= FRONT_GUARD_MIN_PAGES
                and lines[min(first_body, len(lines) - 1)].page_no > n_pages * FRONT_MAX_SHARE):
            first_body = None
        abstract: tuple[int, int] | None = None
        limit = first_body if first_body is not None else min(len(lines), max(1, int(len(lines) * 0.2)))
        for i in range(0, limit):
            ln = lines[i]
            if not (ln.text and ABSTRACT_RE.match(ln.text) and i not in skip and i not in toc_idx):
                continue
            words, b = 0, i + 1
            while b < limit:
                s = lines[b].text
                if s and b not in skip and (LIST_HEAD_RE.match(s) or TOC_HEAD_RE.match(s) or CHAPTER_RE.match(s)
                                            or re.match(r"^key\s?words\b", s, re.I)):
                    break
                words += len(s.split())
                b += 1
                if words >= ABSTRACT_MAX_WORDS:
                    break
            if words:
                abstract = (i, b)
                break

        # --- confidence base
        if "numbered_fallback" in reasons:
            base = CONFIDENCE["numbered+toc"] if offset is not None else CONFIDENCE["numbered"]
        elif "chapter_regex" in reasons:
            base = CONFIDENCE["heading+toc"] if offset is not None else CONFIDENCE["heading"]
        else:
            base = CONFIDENCE["toc"]
        if "sequence_gap" in reasons:
            base -= GAP_PENALTY
        return _Analysis(lines=lines, n_pages=n_pages, hf_idx=hf_idx, toc_idx=toc_idx, toc_entries=toc_entries,
                         chapters=chapters, tails=tails, offset=offset, reasons=reasons, first_body=first_body,
                         abstract=abstract, base=base, page_numbers=page_numbers)

    @staticmethod
    def _sequence(cands: list[_Chapter]) -> list[_Chapter]:
        """The chain of candidates whose numbers run upward from the first (1, 0 or 2), one per page; a number may be
        skipped once when the expected number never appears later; repeats and out-of-order numbers are dropped."""
        cands = sorted(cands, key=lambda c: (c.page_no, c.cstart, c.source == "toc"))
        out: list[_Chapter] = []
        expected: int | None = None
        for k, c in enumerate(cands):
            if expected is None:
                if c.number > 2:
                    continue
                expected = c.number
            if out and c.page_no <= out[-1].page_no:
                continue                 # a chapter starts a new page (numbered lists on one page are not chapters)
            if c.number == expected:
                out.append(c)
                expected += 1
            elif c.number == expected + 1 and out and not any(d.number == expected for d in cands[k + 1:]):
                out.append(c)            # one missed heading: accept with a sequence gap
                expected = c.number + 1
        return out

    @staticmethod
    def _before_references(cands: list[_Chapter], lines: Sequence[_Line], rank_of: dict[int, int],
                           skip: set[int]) -> list[_Chapter]:
        """Candidates without the word 'Chapter' (LaTeX numbers, numbered headings) that follow a 'References' /
        'Bibliography' heading at the top of a page are numbered items of the appendices (consent forms, lists),
        not chapters; candidates headed 'Chapter N' are kept ('Chapter 14 Glossary' after 'Chapter 13 References')."""
        first = min((c.line_idx for c in cands), default=None)
        if first is None:
            return cands
        refs = next((i for i in range(first + 1, len(lines))
                     if lines[i].text and i not in skip and rank_of.get(i, 99) <= 3 and len(lines[i].text) <= 40
                     and (m := TAIL_RE.match(lines[i].text)) and m.group(2).lower() in ("references", "bibliography",
                                                                                         "list of references")), None)
        if refs is None:
            return cands
        return [c for c in cands if c.line_idx < refs or c.source == "chapter_regex"]

    @staticmethod
    def _default_section(ch: _Chapter, unit_kind: str | None) -> str:
        if ch.kind == "tail":
            return ch.source.split(":", 1)[1]
        m = TAIL_RE.match(ch.heading)
        if m and len(ch.heading.split()) <= 4:
            word = m.group(2).lower()
            return "references" if ("bib" in word or "ref" in word or "cited" in word) else "appendix"
        return KIND_SECTION.get(unit_kind or unit_kind_of(ch.heading), "other")

    # ---- masks ---------------------------------------------------------------------------------------------
    def drop_non_body(self, pages: Sequence[PageText]) -> list[LineMask]:
        a = self._analyse(pages)
        lines = a.lines
        masks: list[LineMask] = []
        keep = set(range(a.abstract[0], a.abstract[1])) if a.abstract else set()   # heading line included, as in paper
        for i, ln in enumerate(lines):
            if not ln.text:
                continue
            if i in a.hf_idx or i in a.page_numbers:
                masks.append(LineMask(ln.page_no, ln.cstart, ln.cend, "header_footer"))
            elif i in a.toc_idx:
                masks.append(LineMask(ln.page_no, ln.cstart, ln.cend, "toc"))
            elif a.first_body is not None and i < a.first_body and i not in keep:
                masks.append(LineMask(ln.page_no, ln.cstart, ln.cend, "front_matter"))
        masks.sort(key=lambda m: (m.page_no, m.cstart))
        return masks

    # ---- units ---------------------------------------------------------------------------------------------
    def segment(self, doc_id: str, pages: Sequence[PageText], masks: Sequence[LineMask]) -> list[Unit]:
        a = self._analyse(pages)
        n_pages = a.n_pages or 1
        units = [Unit(unit_id=doc_id, doc_id=doc_id, kind="document", ordinal=0, heading=None, page_from=1,
                      page_to=n_pages, confidence=1.0, reasons=tuple(a.reasons))]
        chapters: list[_Chapter] = list(a.chapters) + list(a.tails)
        taken: set[str] = set()
        for k, ch in enumerate(chapters):
            nxt = chapters[k + 1] if k + 1 < len(chapters) else None
            page_to = nxt.page_no - 1 if nxt else n_pages
            uid = make_unit_id(doc_id, ch.heading, taken)
            taken.add(uid)
            kind = "other" if ch.kind == "tail" else unit_kind_of(ch.heading)
            conf = a.base - (TOC_FILL_PENALTY if ch.source == "toc" and "toc_only" not in a.reasons else 0.0)
            units.append(Unit(unit_id=uid, doc_id=doc_id, kind="chapter", ordinal=k + 1, heading=ch.heading,
                              page_from=ch.page_no, page_to=max(ch.page_no, page_to),
                              confidence=round(max(0.1, min(1.0, conf)), 2), reasons=tuple(a.reasons) + (ch.source,),
                              parent_unit=doc_id, unit_kind=kind))
        return units

    # ---- section marks -------------------------------------------------------------------------------------
    def label_sections(self, doc_id: str, pages: Sequence[PageText], masks: Sequence[LineMask],
                       units: Sequence[Unit]) -> list[SectionMark]:
        a = self._analyse(pages)
        lines = a.lines
        marks: list[SectionMark] = [SectionMark(1, 0, "front")]
        current = "front"

        def emit(page_no: int, cstart: int, sec: str) -> None:
            nonlocal current
            if sec == current:
                return
            last = marks[-1]
            if len(marks) > 1 and last.page_no == page_no and last.cstart == cstart:
                marks[-1] = SectionMark(page_no, cstart, sec)
            else:
                marks.append(SectionMark(page_no, cstart, sec))
            current = sec

        def after_line(i: int, sec: str) -> None:
            """Section `sec` starts at the content line after line i (the heading block itself stays `other`)."""
            nxt = next((j for j in range(i + 1, len(lines)) if lines[j].text), None)
            if nxt is None:
                return
            if lines[nxt].page_no == lines[i].page_no:
                emit(lines[nxt].page_no, lines[nxt].cstart, sec)
            else:
                emit(lines[nxt].page_no, 0, sec)

        if a.abstract:
            i0, i1 = a.abstract
            emit(lines[i0].page_no, lines[i0].cstart, "abstract")
            if i1 < len(lines):
                emit(lines[i1].page_no, lines[i1].cstart, "front")
        chapters: list[_Chapter] = list(a.chapters) + list(a.tails)
        chapter_units = [u for u in units if u.kind == "chapter"]
        for k, ch in enumerate(chapters):
            u = chapter_units[k] if k < len(chapter_units) and chapter_units[k].page_from == ch.page_no else None
            default = self._default_section(ch, u.unit_kind if u else None)
            emit(ch.page_no, ch.cstart, "other")
            after_line(ch.end_idx, default)
            end = chapters[k + 1].line_idx if k + 1 < len(chapters) else len(lines)
            for i in range(ch.end_idx + 1, end):
                ln = lines[i]
                if not ln.text or i in a.hf_idx or i in a.page_numbers:
                    continue
                hit = segment.detect_heading_ex(ln.text)
                if hit is None:
                    continue
                sec, strength = hit
                if sec == "keywords":
                    continue
                if sec == "abstract":
                    if i - ch.end_idx < 20 or not re.search(r"summary", ln.text, re.I):
                        continue
                    sec = "conclusion"                      # a late 'Summary' closes the chapter
                if sec in ("references", "acknowledgements", "appendix"):
                    emit(ln.page_no, ln.cstart, sec)
                elif strength == "strict" or sec != default:
                    emit(ln.page_no, ln.cstart, "other")
                    after_line(i, sec)
        if not any(m.section not in ("front", "abstract") for m in marks):
            # no chapter at all: the text after the front matter (or everything) is body of unknown structure
            if a.first_body is not None and a.first_body < len(lines):
                ln = lines[a.first_body]
                emit(ln.page_no, ln.cstart, "other")
            elif not a.abstract:
                marks.append(SectionMark(1, 0, "other"))
            elif marks[-1].section == "front":
                marks[-1] = SectionMark(marks[-1].page_no, marks[-1].cstart, "other")
        marks.sort(key=lambda m: (m.page_no, m.cstart))
        return marks

    # ---- topic fields --------------------------------------------------------------------------------------
    def topic_fields(self, unit: Unit, pages: Sequence[PageText], marks: Sequence[SectionMark],
                     meta: DocMeta) -> dict[str, list[str]]:
        a = self._analyse(pages)
        lines = a.lines
        skip = a.hf_idx | a.page_numbers
        if unit.kind == "document":
            title = [" ".join(meta.title.split())] if meta.title and meta.title.strip() else _guess_title(lines, skip)
            abstract: list[str] = []
            if meta.abstract and meta.abstract.strip():
                abstract.append(" ".join(meta.abstract.split()))
            if a.abstract:
                i0, i1 = a.abstract
                words = " ".join(lines[j].text for j in range(i0 + 1, i1) if lines[j].text and j not in skip).split()
                if words:
                    abstract.append(" ".join(words[:ABSTRACT_MAX_WORDS]))
            return {"title": title, "keywords": [" ".join(meta.keywords.split())] if meta.keywords else [],
                    "abstract": abstract}
        # chapter: heading + the first 300 words after the heading block
        ch = next((c for c in list(a.chapters) + list(a.tails)
                   if c.page_no == unit.page_from and _norm(c.heading) == _norm(unit.heading or "")), None)
        if ch is not None:
            start = ch.end_idx
        else:
            start = next((i for i, ln in enumerate(lines) if ln.page_no == unit.page_from and ln.text and i not in skip
                          and _norm(unit.heading or "") and _norm(unit.heading or "") in _norm(ln.text)), None)
            if start is None:
                start = next((i for i, ln in enumerate(lines) if ln.page_no == unit.page_from), 0) - 1
        words: list[str] = []
        head_key = _norm(unit.heading or "")
        for i in range(start + 1, len(lines)):
            ln = lines[i]
            if ln.page_no > unit.page_to:
                break
            if not ln.text or i in skip or (head_key and head_key in _norm(ln.text) and len(ln.text) < 120):
                continue                        # a running header that repeats the heading on its first page
            words.extend(ln.text.split())
            if len(words) >= CHAPTER_WORDS:
                break
        return {"title": [unit.heading] if unit.heading else [], "keywords": [],
                "abstract": [" ".join(words[:CHAPTER_WORDS])] if words else []}


def _guess_title(lines: Sequence[_Line], skip: set[int]) -> list[str]:
    """The title lines of page 1: the first line of >= 3 words that is not a title-page formula ('A thesis
    submitted …', 'University College London', a year), joined with up to two following lines of the same shape."""
    page1 = [i for i, ln in enumerate(lines) if ln.page_no == lines[0].page_no and ln.text and i not in skip] if lines else []
    texts = [lines[i].text for i in page1]
    for k, s in enumerate(texts[:12]):
        if len(s.split()) < 3 or _TITLE_PAGE_RE.search(s) or _PAGE_TOKEN_RE.match(s):
            continue
        parts = [s]
        for j in range(k + 1, min(k + 3, len(texts))):
            t = texts[j]
            after = texts[j + 1] if j + 1 < len(texts) else ""
            if len(t.split()) < 2 or _TITLE_PAGE_RE.search(t) or t.endswith("."):
                break
            if _AUTHOR_LINE_RE.match(t) and (not after or _TITLE_PAGE_RE.search(after) or len(after.split()) == 1):
                break                                   # 'Alice Northwood' before 'A thesis submitted …' / 'UCL'
            parts.append(t)
        return [" ".join(" ".join(parts).split())]
    return []


register(ThesisDocType)
