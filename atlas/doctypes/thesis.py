"""PhD thesis doctype (version `thesis1`): front matter masks, TOC-calibrated chapter units, per-chapter sections,
topic fields. Pure, like `paper` (increment 2 §4; architecture §8).

Rules:
- Front matter (`reason: front_matter`): everything before the first chapter start — title page, declaration,
  copyright, acknowledgements, table of contents (`reason: toc` for the TOC lines), lists of figures / tables /
  abbreviations — except the abstract block, which is kept (section `abstract`). Running headers / footers that
  recur on >= 3 pages are masked (`header_footer`).
- Chapter detection: (1) TOC entries (`Chapter 3 … 57`, `3 Methods … 61`, dotted leaders) calibrate the printed →
  physical page offset from >= 3 entries whose heading text is found on a physical page; (2) `^(Chapter|CHAPTER)
  \\s+(\\d+|[IVX]+|One|Two …)` heading lines outside the TOC and the running headers; (3) fallback: numbered
  top-level headings `^\\d+\\s+[A-Z]` whose numbers run in sequence. Appendices and the bibliography after the last
  chapter are units of kind `chapter` with `unit_kind: other` (sections `appendix` / `references`).
- Units: one `document` unit + one `chapter` unit per chapter (`unit_id = doc_id/slug(heading)[:24]`, `~k` on
  duplicates; `unit_kind` by heading keywords; `confidence` 0–1 with reasons such as `toc_matched`,
  `chapter_regex`, `numbered_fallback`, `no_toc`, `page_offset_unresolved`, `sequence_gap`).
- Sections: the chapter heading line is `other`; the chapter's default section follows from its unit_kind
  (intro / lit_review → introduction, methods → methods, …); canonical sub-headings inside the chapter
  (`segment.detect_heading_ex`) override until the next; a `References` / `Bibliography` heading inside a chapter
  makes the rest of the chapter `references`.
- `topic_fields`: document = manifest title (else the first title-like line of page 1) + the front-matter abstract
  (<= 600 words); chapter = heading + the first 300 words of the chapter.
- Thresholds: `max_drop_ratio 0.30`, `min_chapter_confidence 0.7` (ingest warns below it and writes a chapter
  draft the user can correct through `curated/units_override.yaml`).
"""
from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from atlas.core import segment
from atlas.core.ids import unit_id as make_unit_id
from atlas.core.schemas import DocMeta, LineMask, PageText, SectionMark, Unit
from .base import Thresholds, register
from .paper import REFCUE, REFSTART, _all_lines, _header_footer_keys, _headingish, _line_key, _page_lines

_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
          "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
          "eighteen": 18, "nineteen": 19, "twenty": 20}
_ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7, "VIII": 8, "IX": 9, "X": 10, "XI": 11,
          "XII": 12, "XIII": 13, "XIV": 14, "XV": 15, "XVI": 16, "XVII": 17, "XVIII": 18, "XIX": 19, "XX": 20}
CHAPTER_RE = re.compile(r"^(?:chapter|CHAPTER|Chapter)\s+(\d{1,2}|[IVXivx]{1,6}|[A-Za-z]{3,9})\b[\s.:\-–—|]*(.*)$")
TOC_HEAD_RE = re.compile(r"^(?:table of contents|contents|list of contents)\s*$", re.I)
TOC_ENTRY_RE = re.compile(r"^(?:(?:chapter|CHAPTER|Chapter)\s+)?(\d{1,2}|[IVX]{1,6})(?!\.\d)[\s.:\-–—]*\s(.+?)[\s.·…_]*\s(\d{1,3}|[ivxlc]{1,6})\s*$")
TOC_SUB_RE = re.compile(r"^\d{1,2}\.\d+")
NUM_HEAD_RE = re.compile(r"^(\d{1,2})(?!\.\d)\.?\s+([A-Z].{2,80})$")
LIST_HEAD_RE = re.compile(r"^(?:list of (?:figures|tables|abbreviations|symbols|acronyms|publications|appendices)|"
                          r"abbreviations|acronyms|nomenclature|glossary|declaration|statement of originality|"
                          r"acknowledg(?:e)?ments?|dedication|impact statement|preface|copyright(?: statement)?|"
                          r"table of contents|contents|publications?|research paper declaration form)\s*$", re.I)
ABSTRACT_RE = re.compile(r"^(?:abstract|summary|synopsis)\s*$", re.I)
TAIL_RE = re.compile(r"^(?:(\d{1,2}|[A-Z])[\s.:\-–—]+)?(appendix|appendices|bibliography|references|list of references|"
                     r"works cited|supplementary material)\b", re.I)
KIND_RES: list[tuple[str, re.Pattern[str]]] = [
    ("lit_review", re.compile(r"literature|review|state of the art|related work|previous work|theoretical background", re.I)),
    ("intro", re.compile(r"introduction|background|motivation|overview|aims? and objectives|scope", re.I)),
    ("methods", re.compile(r"method|materials|experimental|approach|design|implementation|model(?:ling|ing)?|framework|"
                           r"development|setup|procedure|technique|algorithm|protocol", re.I)),
    ("results", re.compile(r"result|finding|experiment|evaluation|validation|case stud|analysis|study|assessment|"
                           r"performance|application|comparison|investigation", re.I)),
    ("discussion", re.compile(r"discussion|implication|limitation", re.I)),
    ("conclusion", re.compile(r"conclusion|summary|future work|outlook|recommendation|contribution", re.I)),
]
KIND_SECTION = {"intro": "introduction", "lit_review": "introduction", "methods": "methods", "results": "results",
                "discussion": "discussion", "conclusion": "conclusion", "other": "other"}
ABSTRACT_MAX_WORDS = 600
CHAPTER_WORDS = 300
FRONT_MAX_SHARE = 0.5          # the front matter never extends beyond half of the document
TOC_MAX_PAGES = 12
TOP_OF_PAGE_LINES = 12         # a chapter heading sits within the first 12 non-empty lines of its page
_VERBISH = re.compile(r"\b(is|are|was|were|has|have|will|introduces?|presents?|describes?|discusses?|shows?|provides?|"
                      r"gives?|reports?|covers?|explains?|outlines?|of this|in this|this thesis|we|the)\b", re.I)


def _heading_like(rest: str) -> bool:
    """The text after 'Chapter N' (or the whole numbered line) looks like a heading, not a sentence fragment."""
    if not rest:
        return True
    if rest.endswith(".") or len(rest.split()) > 12 or not (rest[0].isupper() or rest[0].isdigit()):
        return False
    if _VERBISH.search(rest) and not rest.isupper():
        return False
    return True


def _not_reference(s: str) -> bool:
    return not (REFSTART.match(s) and sum(1 for _ in REFCUE.finditer(s)) >= 1)


@dataclass
class _Chapter:
    number: int
    heading: str
    page_no: int
    cstart: int
    line_idx: int
    source: str              # toc | chapter_regex | numbered
    kind: str = "chapter"    # chapter | tail (appendix / bibliography)


def _chapter_number(tok: str) -> int | None:
    if tok.isdigit():
        return int(tok)
    if tok.upper() in _ROMAN:
        return _ROMAN[tok.upper()]
    return _WORDS.get(tok.lower())


def _printed_page(tok: str) -> int | None:
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


class ThesisDocType:
    name = "thesis"
    version = "thesis1"
    thresholds = Thresholds(max_drop_ratio=0.30, min_chapter_confidence=0.7)

    # ---- analysis shared by the three methods (pure; recomputed per call, documents are small enough) ---------
    def _analyse(self, pages: Sequence[PageText]) -> dict:
        lines = _all_lines(pages)
        hf = _header_footer_keys(pages)
        hf_idx = {i for i, ln in enumerate(lines) if ln.text and 4 <= len(ln.text) <= 200 and _line_key(ln.text) in hf}
        n_pages = pages[-1].page_no if pages else 0
        # 'Chapter 1', 'Chapter 2', … normalise to the same key ('chapter #') and would look like a running header;
        # a real heading occurs on one page per exact text, a running header repeats its exact text on >= 3 pages
        exact_pages: dict[str, set[int]] = {}
        for i in hf_idx:
            if CHAPTER_RE.match(lines[i].text):
                exact_pages.setdefault(_norm(lines[i].text), set()).add(lines[i].page_no)
        hf_idx = {i for i in hf_idx if not (CHAPTER_RE.match(lines[i].text) and len(exact_pages.get(_norm(lines[i].text), ())) < 3)}
        # rank of each non-empty line on its page (chapter headings start pages)
        top_idx: set[int] = set()
        rank = 0
        cur_page = None
        for i, ln in enumerate(lines):
            if ln.page_no != cur_page:
                cur_page, rank = ln.page_no, 0
            if ln.text and i not in hf_idx:
                rank += 1
                if rank <= TOP_OF_PAGE_LINES:
                    top_idx.add(i)
        # --- TOC region
        toc_start = toc_end = None
        for i, ln in enumerate(lines):
            if ln.page_no > max(8, int(n_pages * 0.3)):
                break
            if ln.text and TOC_HEAD_RE.match(ln.text) and i not in hf_idx:
                toc_start = i
                break
        toc_entries: list[tuple[int, str, int | None, int]] = []     # (number, title, printed page, line idx)
        if toc_start is not None:
            last = toc_start
            start_page = lines[toc_start].page_no
            for j in range(toc_start + 1, len(lines)):
                ln = lines[j]
                if ln.page_no > start_page + TOC_MAX_PAGES:
                    break
                s = ln.text
                if not s or j in hf_idx:
                    continue
                m = TOC_ENTRY_RE.match(s)
                if m and not TOC_SUB_RE.match(s):
                    num = _chapter_number(m.group(1))
                    if num is not None:
                        toc_entries.append((num, m.group(2).strip(" ."), _printed_page(m.group(3)), j))
                    last = j
                elif re.search(r"[.·…_]{3,}\s*\d{1,3}\s*$", s) or re.search(r"\s\d{1,3}\s*$", s) and TOC_SUB_RE.match(s):
                    last = j
                elif LIST_HEAD_RE.match(s) and j > last + 1:
                    break
            toc_end = last
        toc_idx = set(range(toc_start, toc_end + 1)) if toc_start is not None else set()
        # --- chapter heading candidates in the body (not TOC, not running headers)
        cands: list[_Chapter] = []
        for i, ln in enumerate(lines):
            s = ln.text
            if not s or i in hf_idx or i in toc_idx or len(s) > 120:
                continue
            m = CHAPTER_RE.match(s)
            if m:
                num = _chapter_number(m.group(1))
                if num is None or i not in top_idx:
                    continue
                rest = m.group(2).strip(" .:-–—|")
                if re.search(r"[.·…_]{3,}\s*\d+\s*$", s) or re.search(r"\s\d{1,3}\s*$", s) and not rest:
                    continue        # a stray TOC line
                if not _heading_like(rest):
                    continue        # 'Chapter 2 introduces an automated …' inside running text
                heading = rest
                if not heading:     # title on the next non-empty line(s)
                    for k in range(i + 1, min(i + 4, len(lines))):
                        if lines[k].text and k not in hf_idx and len(lines[k].text) < 120:
                            heading = lines[k].text.strip()
                            break
                if heading and len(heading.split()) > 14:
                    heading = " ".join(heading.split()[:14])
                cands.append(_Chapter(num, heading or f"Chapter {num}", ln.page_no, ln.cstart, i, "chapter_regex"))
        chapters = self._sequence(cands)
        reasons: list[str] = []
        if chapters:
            reasons.append("chapter_regex")
        # --- TOC calibration: printed → physical page offset
        offset: int | None = None
        if toc_entries:
            votes: Counter[int] = Counter()
            for num, title, printed, j in toc_entries:
                if printed is None:
                    continue
                key = _norm(title)
                for ch in chapters:
                    if ch.number == num and (_norm(ch.heading) == key or key in _norm(ch.heading) or _norm(ch.heading) in key):
                        votes[ch.page_no - printed] += 1
                        break
                else:
                    for i, ln in enumerate(lines):
                        if i in toc_idx or i in hf_idx or ln.page_no <= lines[toc_end].page_no:
                            continue
                        if ln.text and _norm(ln.text) == key:
                            votes[ln.page_no - printed] += 1
                            break
            if votes and votes.most_common(1)[0][1] >= 3:
                offset = votes.most_common(1)[0][0]
                reasons.append("toc_matched")
            else:
                reasons.append("page_offset_unresolved")
            if not chapters and offset is not None:
                # chapters from the TOC alone
                for num, title, printed, j in toc_entries:
                    if printed is None:
                        continue
                    pno = printed + offset
                    if 1 <= pno <= n_pages:
                        pg = next((k for k, ln in enumerate(lines) if ln.page_no == pno), None)
                        cands.append(_Chapter(num, title, pno, 0, pg if pg is not None else 0, "toc"))
                chapters = self._sequence(cands)
        else:
            reasons.append("no_toc")
        if not chapters:
            # fallback: numbered top-level headings in sequence
            fb: list[_Chapter] = []
            for i, ln in enumerate(lines):
                s = ln.text
                if not s or i in hf_idx or i in toc_idx or ln.page_no <= (lines[toc_end].page_no if toc_end is not None else 0):
                    continue
                m = NUM_HEAD_RE.match(s)
                if (m and i in top_idx and len(s.split()) <= 10 and not re.search(r"\s\d{1,3}\s*$", s)
                        and _headingish(s) and _not_reference(s) and _heading_like(m.group(2).strip())):
                    fb.append(_Chapter(int(m.group(1)), m.group(2).strip(), ln.page_no, ln.cstart, i, "numbered"))
            chapters = self._sequence(fb)
            if chapters:
                reasons.append("numbered_fallback")
        if chapters and any(b.number != a.number + 1 for a, b in zip(chapters, chapters[1:])):
            reasons.append("sequence_gap")
        # --- tail units (appendix / bibliography after the last chapter)
        tails: list[_Chapter] = []
        if chapters:
            last_line = chapters[-1].line_idx
            for i in range(last_line + 1, len(lines)):
                ln = lines[i]
                s = ln.text
                if not s or i in hf_idx or len(s) > 80 or s.endswith("."):
                    continue
                m = TAIL_RE.match(s)
                if m and len(s.split()) <= 8 and ln.page_no > chapters[-1].page_no and i in top_idx and i not in toc_idx:
                    kind_word = m.group(2).lower()
                    if any(_norm(t.heading) == _norm(s) for t in tails):
                        continue
                    tails.append(_Chapter(len(chapters) + len(tails) + 1, s, ln.page_no, ln.cstart, i,
                                          "tail:" + ("references" if "bib" in kind_word or "ref" in kind_word or "cited" in kind_word
                                                     else "appendix"), kind="tail"))
        # --- front matter end and abstract block
        first_body = chapters[0].line_idx if chapters else None
        if first_body is None and toc_end is not None:
            first_body = toc_end + 1
        if first_body is not None and lines[min(first_body, len(lines) - 1)].page_no > n_pages * FRONT_MAX_SHARE:
            first_body = None
        abstract: tuple[int, int] | None = None     # line index range [a, b)
        limit = first_body if first_body is not None else min(len(lines), max(1, int(len(lines) * 0.2)))
        for i in range(0, limit):
            ln = lines[i]
            if ln.text and ABSTRACT_RE.match(ln.text) and i not in hf_idx:
                words = 0
                b = i + 1
                while b < limit:
                    s = lines[b].text
                    if s and (LIST_HEAD_RE.match(s) or TOC_HEAD_RE.match(s) or CHAPTER_RE.match(s)):
                        break
                    words += len(s.split())
                    b += 1
                    if words >= ABSTRACT_MAX_WORDS:
                        break
                abstract = (i, b)
                break
        return {"lines": lines, "hf_idx": hf_idx, "toc_idx": toc_idx, "chapters": chapters, "tails": tails,
                "offset": offset, "reasons": reasons, "first_body": first_body, "abstract": abstract, "n_pages": n_pages}

    @staticmethod
    def _sequence(cands: list[_Chapter]) -> list[_Chapter]:
        """Keep candidates whose numbers run upward from the first (1 or 0); drop repeats and out-of-order ones."""
        cands = sorted(cands, key=lambda c: (c.page_no, c.cstart))
        out: list[_Chapter] = []
        expected: int | None = None
        for c in cands:
            if expected is None:
                if c.number > 2:
                    continue
                expected = c.number
            if out and c.page_no <= out[-1].page_no:
                continue                 # a chapter starts a new page (numbered lists on one page are not chapters)
            if c.number == expected:
                out.append(c)
                expected += 1
            elif c.number == expected + 1 and out:
                out.append(c)            # one missed heading: accept with a sequence gap
                expected = c.number + 1
        return out

    # ---- masks ---------------------------------------------------------------------------------------------
    def drop_non_body(self, pages: Sequence[PageText]) -> list[LineMask]:
        a = self._analyse(pages)
        lines = a["lines"]
        masks: list[LineMask] = []
        abstract = a["abstract"]
        keep = set(range(abstract[0] + 1, abstract[1])) if abstract else set()
        first_body = a["first_body"]
        for i, ln in enumerate(lines):
            if not ln.text:
                continue
            if i in a["hf_idx"]:
                masks.append(LineMask(ln.page_no, ln.cstart, ln.cend, "header_footer"))
            elif i in a["toc_idx"]:
                masks.append(LineMask(ln.page_no, ln.cstart, ln.cend, "toc"))
            elif first_body is not None and i < first_body and i not in keep:
                masks.append(LineMask(ln.page_no, ln.cstart, ln.cend, "front_matter"))
        masks.sort(key=lambda m: (m.page_no, m.cstart))
        return masks

    # ---- units ---------------------------------------------------------------------------------------------
    def segment(self, doc_id: str, pages: Sequence[PageText], masks: Sequence[LineMask]) -> list[Unit]:
        a = self._analyse(pages)
        n_pages = a["n_pages"] or 1
        units = [Unit(unit_id=doc_id, doc_id=doc_id, kind="document", ordinal=0, heading=None, page_from=1,
                      page_to=n_pages, confidence=1.0, reasons=tuple(a["reasons"]))]
        chapters: list[_Chapter] = list(a["chapters"]) + list(a["tails"])
        if not chapters:
            return units
        base = 0.9 if "toc_matched" in a["reasons"] else 0.8 if "chapter_regex" in a["reasons"] else 0.6
        if "sequence_gap" in a["reasons"]:
            base -= 0.15
        if "page_offset_unresolved" in a["reasons"] and "chapter_regex" not in a["reasons"]:
            base -= 0.2
        taken: set[str] = set()
        for k, ch in enumerate(chapters):
            nxt = chapters[k + 1] if k + 1 < len(chapters) else None
            page_to = (nxt.page_no - 1 if nxt and nxt.page_no > ch.page_no else (nxt.page_no if nxt else n_pages))
            uid = make_unit_id(doc_id, ch.heading, taken)
            taken.add(uid)
            kind = "other" if ch.kind == "tail" else unit_kind_of(ch.heading)
            reasons = tuple(a["reasons"]) + ((ch.source,) if ch.kind == "tail" else ())
            units.append(Unit(unit_id=uid, doc_id=doc_id, kind="chapter", ordinal=k + 1, heading=ch.heading,
                              page_from=ch.page_no, page_to=max(ch.page_no, page_to),
                              confidence=round(max(0.1, min(1.0, base)), 2), reasons=reasons, parent_unit=doc_id,
                              unit_kind=kind))
        return units

    # ---- section marks -------------------------------------------------------------------------------------
    def label_sections(self, doc_id: str, pages: Sequence[PageText], masks: Sequence[LineMask],
                       units: Sequence[Unit]) -> list[SectionMark]:
        a = self._analyse(pages)
        lines = a["lines"]
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

        def line_end_mark(i: int, sec: str) -> None:
            """Section `sec` starts at the line after line i (the heading line itself stays `other`)."""
            ln = lines[i]
            nxt = next((lines[j] for j in range(i + 1, len(lines)) if lines[j].text), None)
            if nxt is not None and nxt.page_no == ln.page_no:
                emit(nxt.page_no, nxt.cstart, sec)
            elif nxt is not None:
                emit(nxt.page_no, 0, sec)

        if a["abstract"]:
            i0, i1 = a["abstract"]
            emit(lines[i0].page_no, lines[i0].cstart, "abstract")
            if i1 < len(lines):
                emit(lines[i1].page_no, lines[i1].cstart, "front")
        chapters: list[_Chapter] = list(a["chapters"]) + list(a["tails"])
        chapter_units = [u for u in units if u.kind == "chapter"]
        for k, ch in enumerate(chapters):
            u = chapter_units[k] if k < len(chapter_units) else None
            if ch.kind == "tail":
                default = ch.source.split(":", 1)[1]
            else:
                default = KIND_SECTION.get(u.unit_kind if u else unit_kind_of(ch.heading), "other")
            emit(ch.page_no, ch.cstart, "other")
            line_end_mark(ch.line_idx, default)
            end = chapters[k + 1].line_idx if k + 1 < len(chapters) else len(lines)
            for i in range(ch.line_idx + 1, end):
                ln = lines[i]
                if not ln.text or i in a["hf_idx"]:
                    continue
                hit = segment.detect_heading_ex(ln.text)
                if hit is None:
                    continue
                sec, strength = hit
                if sec in ("abstract", "keywords"):
                    continue
                if sec in ("references", "acknowledgements", "appendix"):
                    emit(ln.page_no, ln.cstart, sec)
                elif strength == "strict" or sec != default:
                    emit(ln.page_no, ln.cstart, "other")
                    line_end_mark(i, sec)
        if not any(m.section not in ("front", "abstract") for m in marks):
            # no chapter at all: the text after the front matter (or everything) is body of unknown structure
            if a["first_body"] is not None and a["first_body"] < len(lines):
                ln = lines[a["first_body"]]
                emit(ln.page_no, ln.cstart, "other")
            elif not a["abstract"]:
                marks.append(SectionMark(1, 0, "other"))
            elif marks[-1].section == "front":
                marks[-1] = SectionMark(marks[-1].page_no, marks[-1].cstart, "other")
        marks.sort(key=lambda m: (m.page_no, m.cstart))
        return marks

    # ---- topic fields --------------------------------------------------------------------------------------
    def topic_fields(self, unit: Unit, pages: Sequence[PageText], marks: Sequence[SectionMark],
                     meta: DocMeta) -> dict[str, list[str]]:
        a = self._analyse(pages)
        lines = a["lines"]
        if unit.kind == "document":
            title = [" ".join(meta.title.split())] if meta.title and meta.title.strip() else []
            if not title:
                for ln in lines[:40]:
                    s = ln.text
                    if s and len(s.split()) >= 4 and not re.search(r"\d{4}|university|thesis|submitted|degree|doctor", s, re.I):
                        title = [s]
                        break
            abstract: list[str] = []
            if meta.abstract and meta.abstract.strip():
                abstract.append(" ".join(meta.abstract.split()))
            if a["abstract"]:
                i0, i1 = a["abstract"]
                words = " ".join(lines[j].text for j in range(i0 + 1, i1) if lines[j].text).split()
                if words:
                    abstract.append(" ".join(words[:ABSTRACT_MAX_WORDS]))
            return {"title": title, "keywords": [" ".join(meta.keywords.split())] if meta.keywords else [], "abstract": abstract}
        # chapter: heading + first 300 words from the chapter start
        start = next((i for i, ln in enumerate(lines) if ln.page_no == unit.page_from and ln.text and
                      _norm(ln.text) and (_norm(unit.heading or "") in _norm(ln.text) or CHAPTER_RE.match(ln.text))), None)
        if start is None:
            start = next((i for i, ln in enumerate(lines) if ln.page_no == unit.page_from), 0)
        words: list[str] = []
        for i in range(start + 1, len(lines)):
            ln = lines[i]
            if ln.page_no > unit.page_to:
                break
            if not ln.text or i in a["hf_idx"]:
                continue
            words.extend(ln.text.split())
            if len(words) >= CHAPTER_WORDS:
                break
        return {"title": [unit.heading] if unit.heading else [], "keywords": [],
                "abstract": [" ".join(words[:CHAPTER_WORDS])] if words else []}


register(ThesisDocType)
