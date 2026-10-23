"""Journal/conference paper (IMRaD): one document unit, non-body masks, section marks, topic fields.

Implemented by: increment 1, agent "doctypes". Port the proven filters from the previous system:
  the previous system's rules_v2.py (acknowledgement/declaration blocks,
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
    words (before the introduction) starts the 'abstract'. Since PDF text carries no blank lines, a paragraph
    is a run of lines that starts after the last masked / short / author-looking line (see
    _first_long_paragraph); the title and author lines therefore stay 'front', and a synthetic abstract never
    starts at (1, 0).
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
                 r"doi\.org/|\bdoi:\s*10\.|journal homepage|available online|corresponding author|e-?mail|"
                 r"@[a-z0-9\-]+\.[a-z]", re.I)
#: 'Received 3 March 2019', 'Accepted: March 3, 2019', 'Received in revised form 2019-03-03' — capitalised and
#: followed by a day/month token, so a body sentence like 'each specimen received a coating between 2015 and
#: 2019' is not publication info (review fix)
_MONTH = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?"
PUB_DATE = re.compile(rf"\b(?:Received|Accepted|Revised)(?: in revised form)?:?\s+"
                      rf"(?:\d{{1,2}}(?:st|nd|rd|th)?\s+{_MONTH}|{_MONTH}\s+\d{{1,2}},?|(?:19|20)\d\d-\d\d-\d\d)")
INTRO_HEAD = re.compile(r"^\s*(?:\d+\.?\s*|I\.\s*)?(introduction|background)\b", re.I)
INTRO_LITERAL = re.compile(r"^\s*(?:\d+\.?\s*|I\.\s*)?introductions?\b", re.I)
FIGTAB = re.compile(r"^\s*(fig\.?|figure|table|tab\.)\s*\d", re.I)
REFSTART = re.compile(r"^\s*(?:\[\d{1,3}\]|\(\d{1,3}\)|\d{1,3}\.\s|[A-Z][A-Za-z'\-]+,\s*(?:[A-Z]\.|[A-Z][a-z]+)|"
                      r"[A-Z][A-Za-z'\-]+\s+[A-Z]{1,3}[,.])")
REFCUE = re.compile(r"\bet al\b|\bvol\.?\s*\d|\bpp\.?\s*\d|\b\d{1,4}\s*[-–]\s*\d{1,4}\b|\bJ\.?\s|Journal|Proceedings|"
                    r"Proc\.|arXiv|Trans\.|\bdoi\b|\(\s*(?:19|20)\d\d[a-z]?\s*\)|\b(?:19|20)\d\d[a-z]?\s*[;,:]", re.I)
YEAR = re.compile(r"\b(?:19|20)\d\d\b")
SMALL = frozenset({"of", "and", "for", "the", "in", "on", "with", "to", "a", "an", "by", "via", "from", "under", "vs"})
NUMBERED_HEAD = re.compile(r"^(?:[A-Z]|\d+)(?:\.\d+)*\.?\s+[A-Z]")
KEYWORDS_LINE = re.compile(r"^(?:key\s?words|index terms)\b\s*[:：\-–—|]?\s*", re.I)
ABSTRACT_RUNIN = re.compile(r"^(?:abstract|summary)\s*[—–\-:|]\s*\S", re.I)   # 'Abstract—Complex …', 'ABSTRACT | Big'
SUMMARY_HEAD = re.compile(r"^[\divx.\s|:]*summary\b", re.I)

ACK_MAX_LINES = 60          # an acknowledgement/declaration heading in the second half masks at most this many lines
HF_EDGE = 4                 # header/footer candidates: first/last 4 non-empty lines of a page
HF_MIN_PAGES = 3            # ... that recur on at least 3 pages
HF_MIN_DOC_PAGES = HF_MIN_PAGES   # ... so documents of 3 pages (letters, conference papers) qualify (review fix)
ABSTRACT_MIN_WORDS = 40
PROSE_MIN_WORDS = 8         # a page-1 line of >= 8 words ending with .?! closes the affiliation window
SHORT_LINE_CHARS = 40       # lines shorter than this cannot start the synthetic abstract (title / author lines)
AFFIL_MIN_TITLECASE = 0.5   # an affiliation line that ends with a period must be mostly Title Case

# IMRaD ranks for the heading sanity rules; tail sections share one rank and need a results-or-later section first
_RANK = {"abstract": 1, "introduction": 2, "methods": 3, "results": 4, "discussion": 5, "conclusion": 6,
         "acknowledgements": 7, "references": 7, "appendix": 7}
_TAIL_RANK = 7
_TAIL_NEEDS_RANK = 4
_KEYWORDS_MAX_RANK = 2      # keywords events are only accepted before the introduction
ABSTRACT_MAX_PAGE = 3       # an 'Abstract' heading counts on the first 3 pages or the first quarter of the paper
ABSTRACT_MAX_WORDS = 600    # an abstract range longer than this (no heading closes it in time) is cut: the rest is
ABSTRACT_MAX_PAGES = 2      # 'introduction'; it also ends with the page after the one it starts on (v2 capped at 6,000 chars)
STRUCTURED_MAX_PAGE = 2     # a structured abstract (Background / Methods / Results / Conclusions) sits on pages 1–2 …
STRUCTURED_MAX_GAP = 30     # … with at most this many lines between consecutive headings …
STRUCTURED_MAX_TAIL = 80    # … and at most this many lines (keywords, affiliations, footnotes) before the body heading


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


_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'\-]*")


def _titlecase_ratio(s: str) -> float:
    """Share of capitalised words among the words of >= 3 letters that are not stop words."""
    words = [w for w in _WORD_RE.findall(s) if len(w) >= 3 and w.lower() not in SMALL]
    if not words:
        return 0.0
    return sum(1 for w in words if w[0].isupper()) / len(words)


def _affiliation_shape(s: str) -> bool:
    """Affiliation lines are name lists ('Department of X, University of Y, City, Country'); a line that ends like a
    sentence must at least be mostly Title Case, so an abstract sentence mentioning a hospital is kept (review
    fix)."""
    if s[-1] not in ".?!":
        return True
    return _titlecase_ratio(s) >= AFFIL_MIN_TITLECASE


def _prose_line(s: str) -> bool:
    """A page-1 line that reads like body/abstract prose: >= PROSE_MIN_WORDS words, terminal punctuation, and
    neither an affiliation, publication-info nor keywords line."""
    return (len(s.split()) >= PROSE_MIN_WORDS and s[-1] in ".?!" and not PUB.search(s)
            and not PUB_DATE.search(s) and not KEYWORDS_LINE.match(s)
            and not (AFF.search(s) and _affiliation_shape(s)))


def _classify_line(s: str, hf: set[str], front: bool) -> str | None:
    """Mask reason for a non-body line, else None. `front` = inside the affiliation window (see _front_end)."""
    L = len(s)
    if not s:
        return None
    if 4 <= L <= 200 and _line_key(s) in hf:
        return "header_footer"
    if L < 300 and (PUB.search(s) or PUB_DATE.search(s)):
        return "pub_info"
    if (front and L < 250 and AFF.search(s) and not SENT_VERB.search(s) and not FIGTAB.match(s)
            and _affiliation_shape(s)):
        return "affiliation"
    if 30 <= L <= 400 and REFSTART.match(s) and YEAR.search(s) and sum(1 for _ in REFCUE.finditer(s)) >= 2:
        return "residual_ref"
    return None


def _front_end(lines: Sequence[_Line], pages: Sequence[PageText]) -> int:
    """Index of the first line that is no longer front matter (the end of the affiliation window): the
    Introduction/Background heading, else the first line of page 2, else line 80 — but never beyond the first
    prose line of page 1 (the abstract has started there; review fix)."""
    end: int | None = None
    for i, ln in enumerate(lines):
        if ln.text and len(ln.text) < 80 and INTRO_HEAD.match(ln.text):
            end = i
            break
    if end is None:
        end = sum(1 for ln in lines if ln.page_no == pages[0].page_no) if len(pages) > 1 else min(len(lines), 80)
    first_page = pages[0].page_no if pages else 1
    for i, ln in enumerate(lines[:end]):
        if ln.page_no != first_page:
            break
        if ln.text and _prose_line(ln.text):
            return i
    return end


def _ack_reason(s: str) -> str:
    return "acknowledgement" if ACK_LONG.match(s) else "declaration"


@register
class PaperDocType:
    name = "paper"
    version = "paper5"      # paper5: methods-at-the-end layouts, heading numbers on their own line, abstract cap
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
        """Heading-driven marks with sanity rules.

        Heading events come from three detectors: canonical headings (segment.HEADINGS, strength 'strict'),
        descriptive headings ('Proposed methodology', strength 'loose') and top-level numbered headings whose
        numbers run in sequence ('IV. Computational Methodology', run-in '1. Introduction. Text …', strength
        'numbered'; classified by keyword, or 'methods' when they carry no keyword and follow the introduction).
        Rules: the order must be monotone in IMRaD order — a lower-ranked heading after higher ones is ignored,
        unless the higher ones are all loose (a strict 'Methods' retracts a loose 'Previous results') or form a
        structured abstract on the first pages (then they become 'abstract'), or it is the paper's only strict
        'Methods' / 'Materials and methods' heading after the results / conclusions (methods-at-the-end layouts
        such as ACS Omega's '2. Results and discussion, 3. Conclusions, 4. Materials and methods'); 'references'
        needs 'results' or later first; an 'Abstract' heading counts only on the first pages, a late 'Summary' is
        a conclusion; if no 'abstract' heading exists but a body heading does, the text between the first
        paragraph of ≥ 40 words on page 1 and that heading is 'abstract'; before that it is 'front' (title,
        authors); an abstract range longer than ABSTRACT_MAX_WORDS words or running past the page after its own
        is cut there and the remainder is 'introduction' (the previous system capped the abstract at 6,000
        chars). A 'Keywords:'/'Key words' line yields a 'keywords' mark that ends at the next blank line or
        heading. A heading number standing on its own line ('3' / 'Mechanics of Lattice Materials', ASME
        journals) is joined with the next line (see _numbered_headings)."""
        lines = _all_lines(pages)
        hf_masked = {(m.page_no, m.cstart) for m in masks if m.reason == "header_footer"}
        masked = {(m.page_no, m.cstart) for m in masks}
        n_pages = pages[-1].page_no if pages else 1
        abstract_max_page = max(ABSTRACT_MAX_PAGE, -(-n_pages // 4))
        numbered = _numbered_headings(lines, hf_masked)

        # 1. heading events in reading order
        events: list[_Event] = []
        for i, ln in enumerate(lines):
            if not ln.text or (ln.page_no, ln.cstart) in hf_masked:
                continue
            if KEYWORDS_LINE.match(ln.text):
                events.append(_Event(i, "keywords", "strict", 0))
            elif ABSTRACT_RUNIN.match(ln.text):
                events.append(_Event(i, "abstract", "strict", _RANK["abstract"]))
            elif hit := segment.detect_heading_ex(ln.text):
                sec, strength = hit
                events.append(_Event(i, sec, "numbered" if i in numbered else strength, _RANK.get(sec, 0)))
            elif i in numbered:
                sec = segment.classify_heading_text(numbered[i])
                events.append(_Event(i, sec, "numbered", _RANK.get(sec, 0) if sec else 0))
        heading_idx = {e.idx for e in events}

        # 2. sanity rules
        accepted: list[_Event] = []
        max_rank = 0
        for ev in events:
            ln = lines[ev.idx]
            sec = ev.section
            if sec == "keywords":
                if max_rank < _KEYWORDS_MAX_RANK:
                    accepted.append(ev)
                continue
            if sec is None:                     # numbered heading without a keyword: body text after the introduction
                if max_rank != _RANK["introduction"]:
                    continue
                sec = "methods"
            if sec == "abstract":
                if max_rank >= _RANK["methods"] and SUMMARY_HEAD.match(ln.text):
                    sec = "conclusion"          # a late 'Summary' closes the paper
                elif ln.page_no > abstract_max_page:
                    continue
            rank = _RANK.get(sec)
            if rank is None:
                continue
            if rank >= _TAIL_RANK:
                if max_rank < _TAIL_NEEDS_RANK:
                    continue
            elif rank < max_rank:
                if ev.strength == "loose":
                    continue
                higher = [a for a in accepted if a.rank > rank]
                if all(a.strength == "loose" for a in higher):
                    accepted = [a for a in accepted if not (a.rank > rank and a.strength == "loose")]
                elif _late_methods(accepted, ev, sec, max_rank, ln.text):
                    pass                        # methods at the end of the paper: accepted, the order stays
                elif _structured_abstract(accepted, ev.idx, rank, sec, lines):
                    dropped = [a for a in accepted if a.rank >= rank]
                    accepted = [a for a in accepted if a.rank < rank]
                    if not any(a.section == "abstract" for a in accepted):
                        accepted.append(_Event(dropped[0].idx, "abstract", "strict", _RANK["abstract"]))
                    accepted.extend(k for k in events                 # a 'Keywords:' line after the abstract
                                    if k.section == "keywords" and dropped[0].idx < k.idx < ev.idx)
                else:
                    continue
            accepted.append(_Event(ev.idx, sec, ev.strength, rank))
            max_rank = max(a.rank for a in accepted)

        # 3. synthetic abstract
        if not any(e.section == "abstract" for e in accepted):
            body_i = next((e.idx for e in accepted if e.section in ("introduction", "methods")), None)
            if body_i is not None:
                j = _first_long_paragraph(lines, masked, before=body_i, min_words=ABSTRACT_MIN_WORDS)
                if j is not None:
                    accepted.append(_Event(j, "abstract", "strict", _RANK["abstract"]))
        accepted.sort(key=lambda e: (e.idx, e.section == "keywords"))

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

        for e in accepted:
            ln = lines[e.idx]
            if e.section == "keywords":
                if current == "keywords":
                    continue
                emit(ln.page_no, ln.cstart, "keywords")
                end = _keywords_end(lines, e.idx, heading_idx)
                if end is not None:
                    emit(end[0], end[1], "front")
            else:
                emit(ln.page_no, ln.cstart, e.section)
        # 5. abstract cap: a range that no heading closes in time ends after ABSTRACT_MAX_WORDS words or with the
        # page after its own; the remainder is 'introduction'
        for cut in _abstract_cuts(marks, lines):
            marks.append(cut)
        marks.sort(key=lambda m: (m.page_no, m.cstart))
        # Fallback: when no body heading was detected at all, the document must not vanish as 'front'
        # (is_body = 0). Without even an abstract the whole document becomes 'other' (a mark at (1, 0) after the
        # mandatory 'front' mark; the ingest stage takes the last mark at a position). With an abstract and/or a
        # keywords block but no heading after them, the text that follows the block is the body: its closing
        # 'front' mark becomes 'other'. The stage reports both cases as 'no headings detected'.
        if not any(m.section not in ("front", "keywords", "abstract") for m in marks):
            if not any(m.section == "abstract" for m in marks):
                marks.append(SectionMark(1, 0, "other"))
            elif marks[-1].section == "front":
                marks[-1] = SectionMark(marks[-1].page_no, marks[-1].cstart, "other")
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

    def guess_title(self, pages: Sequence[PageText]) -> str | None:
        """First-page title guess (the same lines topic_fields uses when the metadata has no title)."""
        found = _guess_title(pages, ())
        return found[0] if found else None


# ---- helpers for label_sections / topic_fields ---------------------------------------------------------------

@dataclass(frozen=True)
class _Event:
    idx: int                # index into _all_lines(pages)
    section: str | None     # None: numbered heading without a section keyword
    strength: str           # 'strict' | 'loose' | 'numbered'
    rank: int


_BARE_NUMBER_RE = re.compile(r"^\d{1,2}$")


def _heading_text_line(s: str) -> bool:
    """A line that can carry the text of a heading whose number stands on the line before it: short, capitalised,
    no terminal period, no formula / caption shape."""
    return (0 < len(s) <= 60 and len(s.split()) <= 8 and s[0].isupper() and not s.endswith(".")
            and not segment._NOT_HEAD_RE.search(s))


def _numbered_headings(lines: Sequence[_Line], hf_masked: set[tuple[int, int]]) -> dict[int, str]:
    """Line index → heading text for top-level numbered headings whose numbers run in sequence (1, 2, 3 … with
    at most one skipped; the first may be 1 or 2). A strict heading line resets the sequence to its own number
    ('1 Introduction' after affiliation markers '1 Department …', '2 Department …'). A bare number on its own
    line followed by a heading-shaped line ('3' / 'Mechanics of Lattice Materials', ASME journals) counts as the
    numbered heading '3 Mechanics of Lattice Materials' at the number's line."""
    out: dict[int, str] = {}
    prev = 0
    for i, ln in enumerate(lines):
        if not ln.text or (ln.page_no, ln.cstart) in hf_masked:
            continue
        text = ln.text
        if _BARE_NUMBER_RE.match(text):
            nxt = next((x for x in lines[i + 1:i + 3] if x.text), None)
            if nxt is None or nxt.page_no != ln.page_no or not _heading_text_line(nxt.text):
                continue
            text = f"{text} {nxt.text}"
        hit = segment.split_numbered_heading(text)
        if hit is None:
            continue
        n, head = hit
        strict = (h := segment.detect_heading_ex(text)) is not None and h[1] == "strict"
        if strict or (prev == 0 and n <= 2) or n in (prev + 1, prev + 2):
            out[i] = head
            prev = n
    return out


def _late_methods(accepted: Sequence[_Event], ev: _Event, sec: str, max_rank: int, line: str) -> bool:
    """True for the paper's first strict 'Methods' / 'Materials and methods' / 'Experimental' heading arriving after
    the results or later (methods-at-the-end layouts); numbered keyword headings ('5 Model limitations') and loose
    forms do not qualify."""
    if sec != "methods" or ev.strength == "loose" or max_rank < _RANK["results"]:
        return False
    if any(a.section == "methods" for a in accepted):
        return False
    h = segment.detect_heading_ex(line)
    return h is not None and h[0] == "methods" and h[1] == "strict"


def _abstract_cuts(marks: Sequence[SectionMark], lines: Sequence[_Line]) -> list[SectionMark]:
    """'introduction' marks that cut every abstract range exceeding ABSTRACT_MAX_WORDS words or running past the
    page after its own (R2129: abstract on page 2, next heading on page 44; P2022: no Introduction heading, the
    abstract ran to the 'Methods' on page 14). The cut sits at the first line past the limit, before the next mark."""
    ordered = sorted(marks, key=lambda m: (m.page_no, m.cstart))
    pos = {(ln.page_no, ln.cstart): i for i, ln in enumerate(lines)}
    cuts: list[SectionMark] = []
    for k, m in enumerate(ordered):
        if m.section != "abstract":
            continue
        start = pos.get((m.page_no, m.cstart))
        if start is None:
            continue
        nxt = ordered[k + 1] if k + 1 < len(ordered) else None
        words = 0
        for ln in lines[start:]:
            if nxt is not None and (ln.page_no, ln.cstart) >= (nxt.page_no, nxt.cstart):
                break
            if ln.page_no > m.page_no + ABSTRACT_MAX_PAGES - 1 or words > ABSTRACT_MAX_WORDS:
                if ln.text:
                    cuts.append(SectionMark(ln.page_no, ln.cstart, "introduction"))
                    break
                continue
            words += len(ln.text.split())
    return cuts


def _structured_abstract(accepted: Sequence[_Event], idx: int, rank: int, sec: str,
                         lines: Sequence[_Line]) -> bool:
    """True when the accepted body headings of rank ≥ `rank` (the incoming 'introduction'/'methods' heading at
    line `idx`) look like a
    structured abstract (Background / Methods / Results / Conclusions): all on the first STRUCTURED_MAX_PAGE pages,
    none a literal 'Introduction', at most STRUCTURED_MAX_GAP lines apart and at most STRUCTURED_MAX_TAIL lines
    before the incoming heading."""
    if sec not in ("introduction", "methods"):
        return False
    idxs = [a.idx for a in accepted if a.rank >= rank and a.section != "keywords"]
    if not idxs or any(lines[i].page_no > STRUCTURED_MAX_PAGE for i in idxs):
        return False
    if any(INTRO_LITERAL.match(lines[i].text) for i in idxs):  # a literal 'Introduction' is never part of an abstract
        return False
    return (all(b - a <= STRUCTURED_MAX_GAP for a, b in zip(idxs, idxs[1:]))
            and idx - idxs[-1] <= STRUCTURED_MAX_TAIL)


_FRONT_MARKERS = re.compile(r"[*†‡§¶]|[¹²³⁰-⁹]")    # author affiliation markers
_AUTHOR_SPLIT = re.compile(r",|;|&|\band\b")


def _author_list(s: str) -> bool:
    """'Alice Northwood, Dmitri Volkov and Farid Haddad': >= 2 short name chunks, Title Case, no verb, no period."""
    if len(s) > 200 or s[-1] in ".?!" or SENT_VERB.search(s):
        return False
    chunks = [c.split() for c in _AUTHOR_SPLIT.split(s)]
    if len(chunks) < 2 or any(not c or len(c) > 4 for c in chunks):
        return False
    return _titlecase_ratio(s) >= 0.6


def _frontish(s: str) -> bool:
    """A page-1 line that cannot start the abstract paragraph: short (title / author / label lines), carrying author
    markers, or shaped like an author list."""
    return len(s) < SHORT_LINE_CHARS or bool(_FRONT_MARKERS.search(s)) or _author_list(s)


def _first_long_paragraph(lines: Sequence[_Line], masked: set[tuple[int, int]], before: int,
                          min_words: int) -> int | None:
    """Index of the first line of the first page-1 paragraph before line `before` whose unmasked lines hold at
    least `min_words` words.

    PDF text has no blank lines between blocks (review fix), so a paragraph is a run of lines that starts after
    the last masked, empty or front-matter-looking line (see _frontish) and ends at the next masked or empty
    line, at an author-marker / author-list line, at a short line (the last line of a wrapped paragraph) or at
    `before`. A run can never start at offset 0 of page 1 (that position is the mandatory 'front' mark)."""
    start: int | None = None
    words = 0
    first_page = lines[0].page_no if lines else 1

    def flush() -> int | None:
        if start is not None and words >= min_words and lines[start].cstart > 0:
            return start
        return None

    for i, ln in enumerate(lines):
        if i >= before or ln.page_no != first_page:
            break
        s = ln.text
        if not s or (ln.page_no, ln.cstart) in masked:
            if (hit := flush()) is not None:
                return hit
            start, words = None, 0
            continue
        if start is None:
            if _frontish(s):
                continue
            start, words = i, len(s.split())
            continue
        if _FRONT_MARKERS.search(s) or _author_list(s):
            if (hit := flush()) is not None:
                return hit
            start, words = None, 0
            continue
        words += len(s.split())
        if len(s) < SHORT_LINE_CHARS:            # a short line closes a wrapped paragraph
            if (hit := flush()) is not None:
                return hit
            start, words = None, 0
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
    if (PUB.search(s) or PUB_DATE.search(s) or AFF.search(s) or ACK.match(s) or KEYWORDS_LINE.match(s)
            or FIGTAB.match(s)):
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
