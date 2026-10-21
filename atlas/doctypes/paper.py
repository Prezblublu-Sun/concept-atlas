"""Journal/conference paper (IMRaD): one document unit, non-body masks, section marks, topic fields.

Implemented by: increment 1, agent "doctypes". Port the proven filters from the previous system:
  H:/UCL_theses/2610 opt/术语整理_v2_2026-10-17/_scripts/rules_v2.py  (acknowledgement/declaration blocks,
  page-aware header/footer detection, affiliation lines before the Introduction, publication-info lines,
  residual reference lines) — keep their behaviour and known fixes (e.g. 'Funding:' on page 1 is pub_info/
  declaration only if it starts with an uppercase word or digit; header/footer = a normalised line that appears
  in the first or last 4 lines of at least 3 pages; affiliation rule only before the Introduction heading and
  never on Fig/Table lines; no bare http/www patterns in the pub_info rule).
Use atlas.core.segment.detect_heading for section marks.

Masks are character spans of PageText.text (the stripped content of one physical line, never the newline);
one line yields at most one mask, so masks never overlap and are sorted by (page_no, cstart).

Section marks (label_sections):
  * (1, 0, 'front') always first.
  * Heading lines (atlas.core.segment.detect_heading) become marks subject to the sanity rules: body sections
    must be monotone in IMRaD order (abstract < introduction < methods < results < discussion < conclusion);
    tail sections (acknowledgements, references, appendix) are only accepted after a 'results' or later
    section has been seen; 'keywords' only before the introduction.
  * No 'abstract' heading but an 'introduction' heading: the first paragraph on page 1 with >= 40 unmasked
    words (before the introduction) starts the 'abstract'.
  * A 'Keywords:'/'Key words' line (or a bare 'Keywords' heading) starts a 'keywords' mark that ends at the
    next blank line, heading or page end; after a blank line / page end the section returns to 'front'
    (a heading at the end point simply takes over).
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from atlas.core import segment
from atlas.core.schemas import DocMeta, LineMask, PageText, SectionMark, Unit
from .base import Thresholds, register

# ---- ported rules (rules_v2.py, R7 non-body lines) -------------------------------------------------------------

ACK = re.compile(r"^\s*(?:\d+(?:\.\d+)*\.?\s*)?(acknowledge?ments?|acknowledgments?|funding\b|financial support|"
                 r"declaration of (?:competing )?interests?|declarations?\b|conflicts? of interests?|"
                 r"competing interests?|disclosures?\b|author contributions?|authors'? contributions?|"
                 r"credit authorship|data (?:and code )?availability|code availability|ethics (?:approval|statement)|"
                 r"ethical approval|consent for publication|supplementary (?:material|materials|information|data)|"
                 r"appendix [a-z]\. supplementary)\b", re.I)
#: heading merged with its paragraph ("Funding: This work was supported by ...") — masked at any length
ACK_LONG = re.compile(r"^\s*(?:\d+(?:\.\d+)*\.?\s*)?(acknowledge?ments?|acknowledgments?|funding)\b", re.I)
BODY_HEAD = re.compile(r"^\s*(?:\d+(?:\.\d+)*\.?\s*)?(abstract|introduction|background|related work|methods?|"
                       r"materials and methods|methodology|results?|experiments?|discussion|conclusions?|appendix|"
                       r"appendices|references)\b", re.I)
AFF = re.compile(r"\b(universit(?:y|ies|ät|at|é|à|ario|ad)|department|dept\.|institute|institut|laborator(?:y|ies)|"
                 r"college|hospital|school of|faculty of|centre for|center for|research cent(?:er|re)|academy|clinic|"
                 r"GmbH|Inc\.|Ltd)\b", re.I)
SENT_VERB = re.compile(r"\b(we|was|were|is|are|has|have|had|been|this|these|our|using|used|showed|shown|performed|"
                       r"obtained|approved)\b", re.I)
#: publication / copyright information; deliberately no bare http/www pattern (URLs occur in body text)
PUB = re.compile(r"©|\bcopyright\b|all rights reserved|creative commons|licen[cs]ed under|open access article|"
                 r"doi\.org/|\bdoi:\s*10\.|journal homepage|\breceived:?\s.{0,40}\b(?:19|20)\d\d|"
                 r"\baccepted:?\s.{0,40}\b(?:19|20)\d\d|available online|corresponding author|e-?mail|"
                 r"@[a-z0-9\-]+\.[a-z]", re.I)
INTRO_HEAD = re.compile(r"^\s*(?:\d+\.?\s*|I\.\s*)?(introduction|background)\b", re.I)
FIGTAB = re.compile(r"^\s*(fig\.?|figure|table|tab\.)\s*\d", re.I)
REFSTART = re.compile(r"^\s*(?:\[\d{1,3}\]|\(\d{1,3}\)|\d{1,3}\.\s|[A-Z][A-Za-z'\-]+,\s*(?:[A-Z]\.|[A-Z][a-z]+)|"
                      r"[A-Z][A-Za-z'\-]+\s+[A-Z]{1,3}[,.])")
REFCUE = re.compile(r"\bet al\b|\bvol\.?\s*\d|\bpp\.?\s*\d|\b\d{1,4}\s*[-–]\s*\d{1,4}\b|\bJ\.?\s|Journal|Proceedings|"
                    r"Proc\.|arXiv|Trans\.|\bdoi\b|\(\s*(?:19|20)\d\d[a-z]?\s*\)|\b(?:19|20)\d\d[a-z]?\s*[;,:]", re.I)
YEAR = re.compile(r"\b(?:19|20)\d\d\b")
SMALL = frozenset({"of", "and", "for", "the", "in", "on", "with", "to", "a", "an", "by", "via", "from", "under", "vs"})
NUMBERED_HEAD = re.compile(r"^(?:[A-Z]|\d+)(?:\.\d+)*\.?\s+[A-Z]")
KEYWORDS_LINE = re.compile(r"^(?:key\s?words|index terms)\b\s*[:：\-–—]?\s*", re.I)

ACK_MAX_LINES = 60          # an acknowledgement/declaration heading in the second half masks at most this many lines
HF_EDGE = 4                 # header/footer candidates: first/last 4 non-empty lines of a page
HF_MIN_PAGES = 3            # ... that recur on at least 3 pages
HF_MIN_DOC_PAGES = 4        # ... in documents of at least 4 pages
ABSTRACT_MIN_WORDS = 40

# IMRaD ranks for the heading sanity rules; tail sections share one rank and need a results-or-later section first
_RANK = {"abstract": 1, "introduction": 2, "methods": 3, "results": 4, "discussion": 5, "conclusion": 6,
         "acknowledgements": 7, "references": 7, "appendix": 7}
_TAIL_RANK = 7
_TAIL_NEEDS_RANK = 4
_KEYWORDS_MAX_RANK = 2      # keywords events are only accepted before the introduction


@dataclass(frozen=True)
class _Line:
    page_no: int
    cstart: int     # offset of the stripped text in PageText.text (== cend for an empty line)
    cend: int
    text: str       # stripped


def _page_lines(page: PageText) -> list[_Line]:
    """Physical lines of a page with the offsets of their stripped content (newline-scanned, '\\r' stripped)."""
    out: list[_Line] = []
    text = page.text
    pos, n = 0, len(text)
    while True:
        nl = text.find("\n", pos)
        end = n if nl < 0 else nl
        raw = text[pos:end]
        s = raw.strip()
        if s:
            cs = pos + (len(raw) - len(raw.lstrip()))
            out.append(_Line(page.page_no, cs, cs + len(s), s))
        else:
            out.append(_Line(page.page_no, pos, pos, ""))
        if nl < 0:
            return out
        pos = nl + 1


def _all_lines(pages: Sequence[PageText]) -> list[_Line]:
    out: list[_Line] = []
    for p in pages:
        out.extend(_page_lines(p))
    return out


def _line_key(s: str) -> str:
    return " ".join(re.sub("[0-9]+", "#", s.lower()).split())


def _header_footer_keys(pages: Sequence[PageText]) -> set[str]:
    """Normalised lines sitting in the first or last HF_EDGE lines of at least HF_MIN_PAGES pages."""
    if len(pages) < HF_MIN_DOC_PAGES:
        return set()
    seen: dict[str, set[int]] = {}
    for p in pages:
        ls = [ln.text for ln in _page_lines(p) if ln.text]
        for s in ls[:HF_EDGE] + ls[-HF_EDGE:]:
            if 4 <= len(s) <= 200:
                seen.setdefault(_line_key(s), set()).add(p.page_no)
    return {k for k, v in seen.items() if len(v) >= HF_MIN_PAGES}


def _headingish(s: str) -> bool:
    """A short line that looks like a section heading (numbered, Title Case or ALL CAPS, no closing period)."""
    if not s or len(s) >= 60 or s[-1] in ".,;:" or not (s[0].isupper() or s[0].isdigit()):
        return False
    if NUMBERED_HEAD.match(s):
        return True
    words = [w for w in re.split(r"\s+", s) if w]
    if len(words) < 2:
        return False
    alpha = [w for w in words if w[0].isalpha()]
    return bool(alpha) and all(w[0].isupper() or w.lower() in SMALL for w in alpha)


def _classify_line(s: str, hf: set[str], front: bool) -> str | None:
    """Mask reason for a non-body line, else None. `front` = before the Introduction heading."""
    L = len(s)
    if not s:
        return None
    if 4 <= L <= 200 and _line_key(s) in hf:
        return "header_footer"
    if L < 300 and PUB.search(s):
        return "pub_info"
    if front and L < 250 and AFF.search(s) and not SENT_VERB.search(s) and not FIGTAB.match(s):
        return "affiliation"
    if 30 <= L <= 400 and REFSTART.match(s) and YEAR.search(s) and sum(1 for _ in REFCUE.finditer(s)) >= 2:
        return "residual_ref"
    return None


def _front_end(lines: Sequence[_Line], pages: Sequence[PageText]) -> int:
    """Index of the first line that is no longer front matter: the Introduction/Background heading, else the
    first line of page 2, else line 80."""
    for i, ln in enumerate(lines):
        if ln.text and len(ln.text) < 80 and INTRO_HEAD.match(ln.text):
            return i
    if len(pages) > 1:
        return sum(1 for ln in lines if ln.page_no == pages[0].page_no)
    return min(len(lines), 80)


def _ack_reason(s: str) -> str:
    return "acknowledgement" if ACK_LONG.match(s) else "declaration"


@register
class PaperDocType:
    name = "paper"
    version = "paper1"
    thresholds = Thresholds()

    # ---- masks ---------------------------------------------------------------------------------------------
    def drop_non_body(self, pages: Sequence[PageText]) -> list[LineMask]:
        lines = _all_lines(pages)
        hf = _header_footer_keys(pages)
        front_end = _front_end(lines, pages)
        n_lines = max(1, len(lines))
        masks: list[LineMask] = []
        skip_left = 0
        block_reason = "acknowledgement"
        for i, ln in enumerate(lines):
            s = ln.text
            reason: str | None = None
            if s and (s[0].isupper() or s[0].isdigit()) and ACK.match(s) and (len(s) < 80 or ACK_LONG.match(s)):
                reason = _ack_reason(s)                      # the line itself is always dropped
                if len(s) < 80 and i / n_lines >= 0.5:       # a heading in the second half: drop its block too
                    skip_left = ACK_MAX_LINES
                    block_reason = reason
            elif skip_left > 0:
                if s and ((len(s) < 80 and BODY_HEAD.match(s)) or _headingish(s)) and not ACK.match(s):
                    skip_left = 0
                else:
                    skip_left -= 1
                    reason = block_reason
            if reason is None:
                reason = _classify_line(s, hf, i < front_end)
            if reason and s:
                masks.append(LineMask(ln.page_no, ln.cstart, ln.cend, reason))
        return masks

    # ---- units ---------------------------------------------------------------------------------------------
    def segment(self, doc_id: str, pages: Sequence[PageText], masks: Sequence[LineMask]) -> list[Unit]:
        """Exactly one Unit(unit_id=doc_id, kind='document', ordinal=0, heading=None, page_from=1,
        page_to=len(pages), confidence=1.0)."""
        return [Unit(unit_id=doc_id, doc_id=doc_id, kind="document", ordinal=0, heading=None, page_from=1,
                     page_to=len(pages), confidence=1.0)]

    # ---- section marks -------------------------------------------------------------------------------------
    def label_sections(self, doc_id: str, pages: Sequence[PageText], masks: Sequence[LineMask],
                       units: Sequence[Unit]) -> list[SectionMark]:
        """Heading-driven marks with sanity rules: order must be monotone in IMRaD order (a later 'introduction'
        after 'results' is ignored); 'references' must come after 'discussion'/'results' (a 'References' heading
        on page 1 is ignored); if no 'abstract' heading exists but an 'introduction' heading does, the text
        between the first paragraph of ≥ 40 words on page 1 and the 'introduction' heading is 'abstract'; before
        that it is 'front' (title, authors). A 'Keywords:'/'Key words' line yields a 'keywords' mark that ends at
        the next blank line or heading."""
        lines = _all_lines(pages)
        hf_masked = {(m.page_no, m.cstart) for m in masks if m.reason == "header_footer"}
        masked = {(m.page_no, m.cstart) for m in masks}

        # 1. heading events in reading order
        events: list[tuple[int, str]] = []
        for i, ln in enumerate(lines):
            if not ln.text or (ln.page_no, ln.cstart) in hf_masked:
                continue
            sec = "keywords" if KEYWORDS_LINE.match(ln.text) else segment.detect_heading(ln.text)
            if sec:
                events.append((i, sec))
        heading_idx = {i for i, _ in events}

        # 2. sanity rules
        accepted: list[tuple[int, str]] = []
        max_rank = 0
        for i, sec in events:
            if sec == "keywords":
                if max_rank < _KEYWORDS_MAX_RANK:
                    accepted.append((i, sec))
                continue
            rank = _RANK.get(sec)
            if rank is None:
                continue
            if rank >= _TAIL_RANK:
                if max_rank < _TAIL_NEEDS_RANK:
                    continue
            elif rank < max_rank:
                continue
            accepted.append((i, sec))
            max_rank = max(max_rank, rank)

        # 3. synthetic abstract
        sections = {s for _, s in accepted}
        if "abstract" not in sections and "introduction" in sections:
            intro_i = next(i for i, s in accepted if s == "introduction")
            j = _first_long_paragraph(lines, masked, before=intro_i, min_words=ABSTRACT_MIN_WORDS)
            if j is not None:
                accepted.append((j, "abstract"))
                accepted.sort(key=lambda e: (e[0], e[1] == "keywords"))

        # 4. emit
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

        for i, sec in accepted:
            ln = lines[i]
            if sec == "keywords":
                if current == "keywords":
                    continue
                emit(ln.page_no, ln.cstart, "keywords")
                end = _keywords_end(lines, i, heading_idx)
                if end is not None:
                    emit(end[0], end[1], "front")
            else:
                emit(ln.page_no, ln.cstart, sec)
        marks.sort(key=lambda m: (m.page_no, m.cstart))
        return marks

    # ---- topic fields --------------------------------------------------------------------------------------
    def topic_fields(self, unit: Unit, pages: Sequence[PageText], marks: Sequence[SectionMark],
                     meta: DocMeta) -> dict[str, list[str]]:
        """{'title': [...], 'keywords': [...], 'abstract': [...]}: title = meta title, else a first-page guess
        (the first front-matter line of >= 3 words that is not publication info / affiliation / numbers, joined
        with up to two continuation lines that do not look like an author line); keywords = meta keywords (if
        any) + every 'keywords' range (label stripped); abstract = meta abstract (if any) + every 'abstract'
        range (leading heading line dropped). Whitespace collapsed; duplicates and empties removed."""
        page_text = {p.page_no: p.text for p in pages}
        umarks = sorted((m for m in marks if unit.page_from <= m.page_no <= unit.page_to),
                        key=lambda m: (m.page_no, m.cstart))
        last_page = max((p.page_no for p in pages if p.page_no <= unit.page_to), default=unit.page_to)

        def ranges(section: str) -> list[str]:
            out: list[str] = []
            for k, m in enumerate(umarks):
                if m.section != section:
                    continue
                nxt = umarks[k + 1] if k + 1 < len(umarks) else None
                parts: list[str] = []
                end_page = nxt.page_no if nxt else last_page
                for pn in range(m.page_no, end_page + 1):
                    t = page_text.get(pn, "")
                    a = m.cstart if pn == m.page_no else 0
                    b = nxt.cstart if (nxt and pn == nxt.page_no) else len(t)
                    parts.append(t[a:b])
                txt = "\n".join(parts)
                first, _, rest = txt.partition("\n")
                fs = first.strip()
                if fs and (KEYWORDS_LINE.fullmatch(fs) if section == "keywords"
                           else segment.detect_heading(fs) == section):
                    txt = rest
                if section == "keywords":
                    txt = KEYWORDS_LINE.sub("", txt.lstrip(), count=1)
                txt = " ".join(txt.split())
                if txt:
                    out.append(txt)
            return out

        def merged(meta_value: str | None, extracted: list[str]) -> list[str]:
            vals: list[str] = []
            for v in [" ".join((meta_value or "").split())] + extracted:
                if v and v not in vals:
                    vals.append(v)
            return vals

        title = [" ".join(meta.title.split())] if meta.title and meta.title.strip() else _guess_title(pages, umarks)
        return {"title": title,
                "keywords": merged(meta.keywords, ranges("keywords")),
                "abstract": merged(meta.abstract, ranges("abstract"))}


# ---- helpers for label_sections / topic_fields ---------------------------------------------------------------

def _first_long_paragraph(lines: Sequence[_Line], masked: set[tuple[int, int]], before: int,
                          min_words: int) -> int | None:
    """Index of the first line of the first page-1 paragraph (run of non-empty lines) before line `before`
    whose unmasked lines hold at least `min_words` words."""
    start: int | None = None
    words = 0
    first_page = lines[0].page_no if lines else 1

    def flush() -> int | None:
        return start if start is not None and words >= min_words else None

    for i, ln in enumerate(lines):
        if i >= before or ln.page_no != first_page:
            break
        if not ln.text:
            if (hit := flush()) is not None:
                return hit
            start, words = None, 0
            continue
        if start is None:
            start = i
        if (ln.page_no, ln.cstart) not in masked:
            words += len(ln.text.split())
    return flush()


def _keywords_end(lines: Sequence[_Line], i: int, heading_idx: set[int]) -> tuple[int, int] | None:
    """(page_no, cstart) where the keywords block starting at line i ends: next blank line, heading or page."""
    for j in range(i + 1, len(lines)):
        ln = lines[j]
        if ln.page_no != lines[i].page_no:
            return ln.page_no, 0
        if not ln.text or j in heading_idx:
            return ln.page_no, ln.cstart
    return None


_AUTHORISH = re.compile(r"[,*†‡§¶]|[\u00b9\u00b2\u00b3\u2070-\u2079]|\b[A-Z]\.")


def _title_candidate(s: str) -> bool:
    words = s.split()
    if len(words) < 3 or "@" in s or not any(c.isalpha() for c in s):
        return False
    if PUB.search(s) or AFF.search(s) or ACK.match(s) or KEYWORDS_LINE.match(s) or FIGTAB.match(s):
        return False
    digits = sum(c.isdigit() for c in s)
    return digits / len(s) <= 0.15


def _guess_title(pages: Sequence[PageText], marks: Sequence[SectionMark]) -> list[str]:
    """First-page title guess from the 'front' range (page 1 up to the first following mark)."""
    page1 = next((p for p in pages if p.page_no == 1), None)
    if page1 is None:
        return []
    end = next((m.cstart for m in marks if m.page_no == 1 and m.cstart > 0), len(page1.text))
    lines = [ln for ln in _page_lines(page1) if ln.cstart < end]
    for k, ln in enumerate(lines):
        if not ln.text or not _title_candidate(ln.text):
            continue
        parts = [ln.text]
        for nxt in lines[k + 1:k + 3]:
            if not nxt.text or not _title_candidate(nxt.text) or _AUTHORISH.search(nxt.text):
                break
            parts.append(nxt.text)
        return [" ".join(" ".join(parts).split())]
    return []
