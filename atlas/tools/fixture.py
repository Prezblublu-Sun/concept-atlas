"""Synthetic fixture corpus so the test-suite runs without any real paper. Implemented by: increment 1, agent "ingest".

`make_fixture(out_dir, seed=1)` writes `out_dir/pdf/*.pdf` with PyMuPDF (insert_text / insert_textbox) and
`out_dir/manifest.csv`, and returns a dict describing what was planted so tests can assert exactly:
- 8 research papers + 1 review, 3–5 pages each, two-column look not required; each with: a title line (large font),
  an authors line, an affiliation block ('Department of X, University of Y, City, Country' lines with commas), a
  running header with the journal name on every page and a footer page number, an 'Abstract' heading and
  paragraph, 'Keywords: a, b, c', numbered headings '1. Introduction', '2. Materials and methods', '3. Results',
  '4. Discussion', '5. Conclusions', 'Acknowledgements' paragraph, 'Conflict of interest' line, 'References' with
  ≥ 10 reference entries like '[3] Smith J, et al. Title. Journal. 2019;12:34-56.', a figure caption line
  'Fig. 2. Micromotion versus load.', and a 'Received 3 March 2019; accepted 5 May 2019' line on page 1;
- planted sentences with known counts: 'micromotion' in N_i sentences per paper (returned), one sentence with
  'The threshold was 150 μm.' and one with 'a gap of 2 mm' (unit traps), one '±45°' sentence, 'Fig. 3 shows'
  mid-sentence abbreviation, 'et al. (2019) reported' mid-sentence, a sentence duplicated verbatim twice in one
  paper (duplicate-ordinal test), and one sentence split across two lines by a hyphen ('micro-\\nmotion');
- deterministic given `seed` (fixed strings + seeded shuffles, fixed PDF dates and a fixed trailer /ID derived
  from seed + doc_id) so sentence ids, pdf_sha256 and extraction ids are all stable across runs.
Increment 2: `make_fixture(out_dir, seed, kind="thesis")` writes 3 synthetic theses instead (see
`make_thesis_fixture`): title page, declaration, abstract, acknowledgements, a table of contents whose printed
page numbers are offset from the physical pages by the front matter, a list of figures, 4–6 chapters ("Chapter N"
line + title line, numbered sub-headings, per-chapter references, >= 2 pages each), a bibliography and an
appendix; a running header on every page and printed page numbers (roman in the front matter, arabic from
chapter 1) in the footer. The returned dict lists every chapter with its physical page range, unit kind, printed
start page, sub-headings and the first sentence under each, the abstract text and the planted 'micromotion'
sentences. Section traps (thesis2): the Results chapter carries a numbered 'Methods' sub-heading (its text is
`methods`, the next sub-heading returns to `results`), the Discussion chapter a table rendered as the short lines
'Test' / 'Reference' / 'Topology', and the bibliography the legend 'Summary' / '66.9' — `traps` names them.

Layout notes (verified against PyMuPDF text extraction): text is written with the bundled Unicode fallback font
(so μ, ± and ° survive), every line is wrapped to the column width (PyMuPDF clips glyphs outside the page), a
paragraph is never split across pages (so no planted sentence crosses a page), and every paper is padded with
filler paragraphs until it has at least 4 pages (the paper doctype detects headers/footers from 3 pages on;
4 keeps a margin and the planted page counts stable).
The word 'micromotion' (lower-case) occurs only in the planted sentences that are counted, never in titles,
keywords, headers, references or the figure caption ('Micromotion').

Returned dict: {out_dir, pdf_dir, manifest, seed, journal, documents: [{doc_id, file, path, title, year, authors,
doi, venue, doc_type, kind, n_pages, header, footer_pattern, affiliations, received_line, doi_line, keywords_line,
headings, abstract, sentences, micromotion_sentences, references, loose_references, acknowledgements,
conflict_line, caption}], micromotion_counts: {doc_id: N_i}, specials: {threshold|gap|pm45|fig3|etal|duplicate|
hyphen: {doc_id, text[, lines]}}}.
"""
from __future__ import annotations

import csv
import hashlib
import logging
import random
from dataclasses import dataclass
from pathlib import Path

import fitz

from atlas.core.text import sentence_text

log = logging.getLogger(__name__)

PAGE_W, PAGE_H = 595.0, 842.0
MARGIN_X = 60.0
COLUMN_W = 400.0            # narrow single column: fills pages faster, keeps lines well inside the page
HEADER_Y = 40.0
FOOTER_Y = 812.0
BODY_TOP = 80.0
BODY_BOTTOM = 785.0
LINE_H = 14.0
PARA_GAP = 6.0
HEAD_GAP = 12.0
BODY_SIZE = 10.0
HEADING_SIZE = 11.5
TITLE_SIZE = 16.0
SMALL_SIZE = 8.0
MIN_PAGES = 4
MAX_PAGES = 5
N_PAPERS = 8
N_REFERENCES = 12
JOURNAL = "Journal of Example Biomechanics"
FIXED_DATE = "D:20200101120000"

# ---- vocabulary (fixed strings; combinations are sampled with the seeded RNG) ------------------------------------

FIRST = ["Alice", "Bo", "Carla", "Dmitri", "Elena", "Farid", "Grace", "Hiro", "Ingrid", "Jonas", "Keiko", "Luis"]
LAST = ["Northwood", "Chen", "Diaz", "Volkov", "Rossi", "Haddad", "Okafor", "Tanaka", "Lindqvist", "Weber",
        "Sato", "Moreno"]
DEPTS = ["Orthopaedic Engineering", "Biomechanics", "Mechanical Engineering", "Musculoskeletal Science",
         "Medical Physics", "Materials Science"]
UNIS = ["University of Northtown", "University of Westfield", "University of Eastbridge", "University of Southmoor"]
CITIES = ["Northtown", "Westfield", "Eastbridge", "Southmoor", "Riverton", "Hillcrest"]
COUNTRIES = ["United Kingdom", "Germany", "Japan", "Spain", "Sweden", "Canada"]
INSTS = ["Institute of Biomechanics", "Institute of Orthopaedic Research", "Laboratory of Implant Mechanics"]
HOSPS = ["Westfield Hospital", "Riverton General Hospital", "Hillcrest Orthopaedic Hospital"]

TITLE_A = ["Finite element analysis", "Experimental assessment", "Computational evaluation", "A parametric study",
           "Subject-specific modelling", "Probabilistic prediction", "In vitro measurement", "Design optimisation"]
TITLE_B = ["primary stability", "stress shielding", "interface mechanics", "periprosthetic bone remodelling",
           "load transfer", "implant fixation", "proximal strain distribution", "cortical contact"]
TITLE_C = ["cementless femoral stems", "short-stem hip arthroplasty", "collared and collarless stems",
           "porous-coated implants", "tapered wedge stems", "anatomic femoral components"]
REVIEW_TITLE = "A review of primary stability assessment methods for cementless femoral stems"

SUBJECTS = ["The proximal femur", "The cementless stem", "The bone-implant interface", "The cortical shell",
            "The cancellous bone", "The loading protocol", "The mesh density", "The contact algorithm",
            "The implant coating", "The surgical technique", "The stem geometry", "The periprosthetic bone"]
PREDICATES = ["was modelled with", "was compared against", "was evaluated using", "depends strongly on",
              "was represented by", "was validated against", "was discretised with", "was simulated under",
              "was analysed with", "was derived from"]
OBJECTS = ["a linear elastic material law", "the published experimental data", "a quasi-static walking load",
           "a frictional contact formulation", "a coarse hexahedral mesh", "the manufacturer specification",
           "a patient-specific geometry", "a simplified boundary condition", "an isotropic bone model",
           "the clinical follow-up records"]
TAILS = ["in all configurations", "for both stem sizes", "throughout the gait cycle", "without loss of generality",
         "as described previously", "in the present study", "for the stair-climbing case", "with negligible error"]
N_FILLER = len(SUBJECTS) * len(PREDICATES) * len(OBJECTS) * len(TAILS)

MM_TEMPLATES = [
    "The peak micromotion at the {region} reached {n} μm under {load}.",
    "Interface micromotion at the {region} decreased by {n} percent under {load}.",
    "A clear reduction of micromotion was observed at the {region} under {load}.",
    "The distribution of micromotion along the {region} was sensitive to {load}.",
    "Predicted micromotion exceeded the ingrowth limit at the {region} under {load}.",
    "The collar reduced the micromotion measured at the {region} during {load}.",
]
REGIONS = ["calcar", "distal tip", "lateral flare", "medial cortex", "greater trochanter", "metaphyseal flare"]
LOADS = ["walking", "stair climbing", "single-leg stance", "rising from a chair", "cyclic loading"]
N_MM = len(MM_TEMPLATES) * len(REGIONS) * len(LOADS)

ABSTRACT_FIXED = ["Primary stability of cementless stems is a prerequisite for bone ingrowth.",
                  "This study quantifies the mechanical response of the implanted femur with a validated model.",
                  "The results support a design rationale for improved proximal load transfer."]
KEYWORD_SETS = [["cementless stem", "finite element", "primary stability"],
                ["stress shielding", "bone remodelling", "hip arthroplasty"],
                ["interface mechanics", "porous coating", "load transfer"],
                ["femoral component", "contact modelling", "osseointegration"]]
REF_TITLES = ["Primary stability of cementless stems", "Bone ingrowth and interface mechanics",
              "Stress shielding after hip arthroplasty", "Finite element validation of femoral models",
              "Contact modelling of porous coatings", "Load transfer in tapered stems",
              "Subject-specific bone material mapping", "Clinical outcomes of short stems",
              "Experimental strain measurement in composite femurs", "Periprosthetic remodelling simulation",
              "Implant design and proximal fixation", "Gait loading of the implanted femur",
              "Collar effect on axial stability", "Mesh convergence in bone-implant models"]
LOOSE_REFERENCES = ["[{n}] Brown K. Primary stability of stems. 2018.",
                    "[{n}] Green L. Notes on implant fixation. 2017."]
ACK_TEXT = ("The authors thank the Example Trust for financial support and the Northtown workshop for "
            "manufacturing the test specimens.")
CONFLICT_LINE = "Conflict of interest: The authors declare no competing interests."
CAPTION = "Fig. 2. Micromotion versus load."

SPECIAL_TEXT = {
    "threshold": "The threshold was 150 μm.",
    "gap": "The model included a gap of 2 mm between the stem and the cortical bone.",
    "pm45": "The load was applied at ±45° to the long axis of the femur.",
    "fig3": "Fig. 3 shows the micromotion pattern along the stem for the walking case.",
    "etal": "Smith et al. (2019) reported that micromotion above the ingrowth limit delays osseointegration.",
    "duplicate": "Interface micromotion is governed by stem geometry, bone quality and the applied load.",
    "hyphen": "Stem design controls the amount of interface micro-\nmotion under physiological loading.",
}
SPECIAL_SECTION = {"threshold": "results", "gap": "methods", "pm45": "methods", "fig3": "results",
                   "etal": "discussion", "duplicate": "discussion", "hyphen": "discussion"}


# ---- text model --------------------------------------------------------------------------------------------------

@dataclass
class _Line:
    text: str
    size: float = BODY_SIZE
    gap: float = 0.0        # extra space before this line (ignored at the top of a page)
    new_page: bool = False  # start a new page before this line (thesis chapters, front-matter pages)
    mark: str | None = None  # thesis layout marker ('chapter:3', 'tail:Bibliography', 'front:abstract'); never printed


def _font() -> fitz.Font:
    try:
        return fitz.Font("cjk")      # Droid Sans Fallback: Latin + Greek + symbols, bundled with MuPDF
    except Exception as e:  # pragma: no cover - older wheels
        log.warning("bundled unicode font unavailable (%s); falling back to Helvetica", e)
        return fitz.Font("helv")


def _wrap(text: str, size: float, font: fitz.Font, width: float = COLUMN_W) -> list[str]:
    """Greedy wrap by measured width; '\\n' inside `text` forces a line break."""
    out: list[str] = []
    for hard in text.split("\n"):
        cur = ""
        for w in hard.split(" "):
            if not w:
                continue
            cand = w if not cur else f"{cur} {w}"
            if not cur or font.text_length(cand, fontsize=size) <= width:
                cur = cand
            else:
                out.append(cur)
                cur = w
        if cur:
            out.append(cur)
    return out


def _paragraph(sentences: list[str], font: fitz.Font, gap: float = PARA_GAP) -> list[_Line]:
    lines = [_Line(t) for t in _wrap(" ".join(sentences), BODY_SIZE, font)]
    if lines:
        lines[0].gap = gap
    return lines


def _heading(text: str) -> _Line:
    return _Line(text, HEADING_SIZE, HEAD_GAP)


def _paginate(blocks: list[list[_Line]]) -> list[list[tuple[float, _Line]]]:
    """Place blocks top-down; a block never straddles a page."""
    pages: list[list[tuple[float, _Line]]] = []
    cur: list[tuple[float, _Line]] = []
    y = BODY_TOP
    for block in blocks:
        if not block:
            continue
        height = sum(LINE_H + (ln.gap if i or cur else 0.0) for i, ln in enumerate(block))
        if cur and (y + height > BODY_BOTTOM or block[0].new_page):
            pages.append(cur)
            cur, y = [], BODY_TOP
        for ln in block:
            if cur:
                y += ln.gap
            y += ln.size            # baseline
            cur.append((y, ln))
            y += LINE_H - ln.size
    if cur:
        pages.append(cur)
    return pages


def _write_pdf(path: Path, pages: list[list[tuple[float, _Line]]], header: str, font: fitz.Font,
               meta: dict, id_key: str = "", footers: list[str | None] | None = None,
               headers: list[list[str]] | None = None) -> int:
    """Write the pages; `id_key` seeds the PDF trailer /ID so the bytes are identical across runs (MuPDF would
    otherwise write a fresh random /ID on every save). `footers` (one per page, None = no footer) replaces
    'Page i of n'; `headers` (lines per page, stacked from the top) replaces the single `header`."""
    doc = fitz.open()
    n = len(pages)
    for i, lines in enumerate(pages, 1):
        page = doc.new_page(width=PAGE_W, height=PAGE_H)
        tw = fitz.TextWriter(page.rect)
        for h, text in enumerate(headers[i - 1] if headers else [header]):
            tw.append((MARGIN_X, HEADER_Y + h * (SMALL_SIZE + 3.0)), text, font=font, fontsize=SMALL_SIZE)
        for y, ln in lines:
            tw.append((MARGIN_X, y), ln.text, font=font, fontsize=ln.size)
        footer = footers[i - 1] if footers else f"Page {i} of {n}"
        if footer:
            tw.append((MARGIN_X, FOOTER_Y), footer, font=font, fontsize=SMALL_SIZE)
        tw.write_text(page)
    doc.set_metadata({"title": meta["title"], "author": meta["authors"], "subject": "concept-atlas fixture",
                      "keywords": meta["keywords"], "creator": "concept-atlas fixture", "producer": "PyMuPDF",
                      "creationDate": FIXED_DATE, "modDate": FIXED_DATE})
    path.parent.mkdir(parents=True, exist_ok=True)
    fid = hashlib.sha256(id_key.encode("utf-8")).hexdigest()[:32]
    doc.xref_set_key(-1, "ID", f"[<{fid}><{fid}>]")
    doc.save(str(path), garbage=1, deflate=True, no_new_id=True)
    doc.close()
    return n


# ---- content generation --------------------------------------------------------------------------------------------

def _filler(idx: int) -> str:
    s, rest = divmod(idx, len(PREDICATES) * len(OBJECTS) * len(TAILS))
    p, rest = divmod(rest, len(OBJECTS) * len(TAILS))
    o, t = divmod(rest, len(TAILS))
    return f"{SUBJECTS[s]} {PREDICATES[p]} {OBJECTS[o]} {TAILS[t]}."


def _mm_sentence(idx: int) -> str:
    t, rest = divmod(idx, len(REGIONS) * len(LOADS))
    r, l = divmod(rest, len(LOADS))
    n = 20 + (idx * 37) % 120
    if n == 150:
        n = 151
    return MM_TEMPLATES[t].format(region=REGIONS[r], load=LOADS[l], n=n)


class _Paper:
    """Content of one fixture document; paragraphs are lists of sentences keyed by section."""

    def __init__(self, rng: random.Random, doc_id: str, kind: str, ordinal: int):
        self.rng = rng
        self.doc_id = doc_id
        self.kind = kind
        self.year = 2015 + (ordinal * 5 + rng.randrange(9)) % 9
        if kind == "review":
            self.title = REVIEW_TITLE
        else:
            self.title = f"{rng.choice(TITLE_A)} of {rng.choice(TITLE_B)} in {rng.choice(TITLE_C)}"
        names = rng.sample(range(len(FIRST)), 3)
        self.author_names = [f"{FIRST[i]} {LAST[(i + ordinal) % len(LAST)]}" for i in names]
        self.authors = ", ".join(self.author_names[:-1]) + " and " + self.author_names[-1]
        self.affiliations = [
            f"Department of {rng.choice(DEPTS)}, {rng.choice(UNIS)}, {rng.choice(CITIES)}, {rng.choice(COUNTRIES)}",
            f"{rng.choice(INSTS)}, {rng.choice(HOSPS)}, {rng.choice(CITIES)}, {rng.choice(COUNTRIES)}",
        ]
        self.volume = 10 + ordinal
        p0 = 1 + 40 * ordinal
        self.header = f"{JOURNAL} {self.volume} ({self.year}) {p0}-{p0 + 23}"
        self.doi = f"10.1234/jeb.{self.year}.{1000 + ordinal * 7:04d}"
        self.doi_line = f"DOI: {self.doi}"
        self.received_line = f"Received 3 March {self.year}; accepted 5 May {self.year}"
        self.keywords = rng.choice(KEYWORD_SETS)
        self.keywords_line = "Keywords: " + ", ".join(self.keywords)
        self.abstract = list(ABSTRACT_FIXED)
        self.n_mm = rng.randint(2, 6)
        self.filler_ids = iter(rng.sample(range(N_FILLER), 400))
        mm_ids = rng.sample(range(N_MM), self.n_mm)
        self.mm_sentences = [_mm_sentence(i) for i in mm_ids]
        self.methods_heading = "2. Methods" if kind == "review" else "2. Materials and methods"
        self.sections: dict[str, list[list[str]]] = {
            "introduction": [self._par() for _ in range(3)],
            "methods": [self._par() for _ in range(3)],
            "results": [self._par() for _ in range(3)],
            "discussion": [self._par() for _ in range(3)],
            "conclusion": [self._par(3)],
        }
        for text in self.mm_sentences:
            self._insert(rng.choice(["introduction", "results", "discussion"]), text)
        self.references = [self._reference(k) for k in range(1, N_REFERENCES + 1)]
        self.loose_references = [t.format(n=N_REFERENCES + k) for k, t in enumerate(LOOSE_REFERENCES, 1)]
        self.specials: dict[str, str] = {}

    def _par(self, n: int | None = None) -> list[str]:
        n = n or self.rng.randint(4, 6)
        return [_filler(next(self.filler_ids)) for _ in range(n)]

    def _insert(self, section: str, text: str) -> None:
        par = self.rng.choice(self.sections[section])
        par.insert(self.rng.randrange(len(par) + 1), text)

    def add_special(self, name: str, text: str) -> None:
        self.specials[name] = text
        if name == "duplicate":
            self._insert("introduction", text)
            self._insert("discussion", text)
        else:
            self._insert(SPECIAL_SECTION[name], text)

    def add_filler_paragraph(self) -> None:
        self.sections["discussion"].append(self._par())

    def _reference(self, k: int) -> str:
        rng = self.rng
        last = rng.choice(LAST)
        title = REF_TITLES[(k - 1 + rng.randrange(3)) % len(REF_TITLES)]
        year = 2005 + rng.randrange(15)
        vol = 5 + rng.randrange(30)
        p0 = 1 + rng.randrange(900)
        return f"[{k}] {last} {rng.choice(FIRST)[0]}, et al. {title}. {JOURNAL}. {year};{vol}:{p0}-{p0 + 12}."

    @property
    def headings(self) -> list[str]:
        return ["Abstract", "1. Introduction", self.methods_heading, "3. Results", "4. Discussion",
                "5. Conclusions", "Acknowledgements", "References"]

    def sentences(self) -> list[str]:
        """Every planted sentence that should become exactly one stored sentence, in reading order."""
        out = list(self.abstract)
        for sec in ("introduction", "methods", "results", "discussion", "conclusion"):
            for par in self.sections[sec]:
                out.extend(par)
        return out

    def blocks(self, font: fitz.Font) -> list[list[_Line]]:
        title_lines = [_Line(t, TITLE_SIZE, 4.0) for t in _wrap(self.title, TITLE_SIZE, font, COLUMN_W + 60)]
        front = title_lines + [_Line(self.authors, BODY_SIZE, 8.0)] + [_Line(a, SMALL_SIZE + 1) for a in self.affiliations]
        front += [_Line(self.doi_line, SMALL_SIZE + 1, 4.0), _Line(self.received_line, SMALL_SIZE + 1)]
        blocks: list[list[_Line]] = [front]
        blocks.append([_heading("Abstract")] + _paragraph(self.abstract, font, 2.0))
        blocks.append([_Line(self.keywords_line, BODY_SIZE, PARA_GAP)])

        def section(heading: str, pars: list[list[str]], extra_after_first: str | None = None) -> None:
            first = [_heading(heading)] + _paragraph(pars[0], font, 2.0)
            blocks.append(first)
            if extra_after_first:
                blocks.append([_Line(extra_after_first, BODY_SIZE, PARA_GAP)])
            for par in pars[1:]:
                blocks.append(_paragraph(par, font))

        section("1. Introduction", self.sections["introduction"])
        section(self.methods_heading, self.sections["methods"])
        section("3. Results", self.sections["results"], CAPTION)
        section("4. Discussion", self.sections["discussion"])
        section("5. Conclusions", self.sections["conclusion"])
        blocks.append([_heading("Acknowledgements")] + _paragraph([ACK_TEXT], font, 2.0))
        blocks.append([_Line(CONFLICT_LINE, BODY_SIZE, PARA_GAP)])
        blocks.append([_heading("References")])
        for ref in self.references + self.loose_references:
            blocks.append([_Line(t) for t in _wrap(ref, BODY_SIZE, font)])
        return blocks


def make_fixture(out_dir: str | Path, seed: int = 1, kind: str = "paper") -> dict:
    if kind == "thesis":
        return make_thesis_fixture(out_dir, seed)
    if kind != "paper":
        raise ValueError(f"unknown fixture kind {kind!r} (paper | thesis)")
    out = Path(out_dir)
    pdf_dir = out / "pdf"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    font = _font()

    papers: list[_Paper] = []
    for k in range(N_PAPERS + 1):
        kind = "review" if k == N_PAPERS else "paper"
        papers.append(_Paper(rng, f"P{k + 1:03d}", kind, k))

    specials: dict[str, dict] = {}
    for name in ("threshold", "gap", "pm45", "fig3", "etal", "duplicate", "hyphen"):
        paper = rng.choice(papers)
        text = SPECIAL_TEXT[name]
        paper.add_special(name, text)
        specials[name] = {"doc_id": paper.doc_id, "text": text}
        if name == "hyphen":
            specials[name]["lines"] = text.split("\n")

    documents: list[dict] = []
    counts: dict[str, int] = {}
    rows: list[list[str]] = []
    for paper in papers:
        pages = _paginate(paper.blocks(font))
        guard = 0
        while len(pages) < MIN_PAGES and guard < 40:
            paper.add_filler_paragraph()
            pages = _paginate(paper.blocks(font))
            guard += 1
        if not MIN_PAGES <= len(pages) <= MAX_PAGES:  # pragma: no cover - layout constants guarantee this
            raise RuntimeError(f"{paper.doc_id}: layout produced {len(pages)} pages")
        first_last = paper.author_names[0].split()[-1]
        file = f"{paper.doc_id}_{first_last}_{paper.year}.pdf"
        path = pdf_dir / file
        n_pages = _write_pdf(path, pages, paper.header, font,
                             {"title": paper.title, "authors": paper.authors, "keywords": ", ".join(paper.keywords)},
                             id_key=f"concept-atlas fixture {seed} {paper.doc_id}")
        sentences = paper.sentences()
        n_mm = sum(1 for s in sentences if "micromotion" in sentence_text(s))   # the hyphen special counts
        counts[paper.doc_id] = n_mm
        documents.append({
            "doc_id": paper.doc_id, "file": file, "path": str(path), "title": paper.title, "year": paper.year,
            "authors": paper.authors, "doi": paper.doi, "venue": JOURNAL, "doc_type": "paper", "kind": paper.kind,
            "n_pages": n_pages, "header": paper.header, "footer_pattern": "Page {n} of {N}",
            "affiliations": list(paper.affiliations), "received_line": paper.received_line,
            "doi_line": paper.doi_line, "keywords_line": paper.keywords_line, "headings": paper.headings,
            "abstract": list(paper.abstract), "sentences": sentences, "micromotion_sentences": n_mm,
            "references": list(paper.references), "loose_references": list(paper.loose_references),
            "acknowledgements": ACK_TEXT, "conflict_line": CONFLICT_LINE, "caption": CAPTION,
            "specials": dict(paper.specials),
        })
        rows.append([paper.doc_id, paper.title, str(paper.year), paper.authors, paper.doi, JOURNAL, "paper", file])

    manifest = out / "manifest.csv"
    with open(manifest, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["doc_id", "title", "year", "authors", "doi", "venue", "doc_type", "file"])
        w.writerows(rows)
    log.info("fixture: %d documents written to %s", len(documents), out)
    return {"out_dir": str(out), "pdf_dir": str(pdf_dir), "manifest": str(manifest), "seed": seed,
            "journal": JOURNAL, "documents": documents, "micromotion_counts": counts, "specials": specials}


# ---- theses (increment 2) --------------------------------------------------------------------------------------
#
# Three synthetic theses in the three layouts met in the UCL library (see atlas/doctypes/thesis.py):
#   T001 `word_plain`    'Chapter N' line + title line, one-line dotted TOC entries ('Chapter 1 Introduction …… 1'),
#                        per-chapter 'References', constant running header, roman / arabic page numbers in the
#                        footer (printed page = physical page - front pages);
#   T002 `word_numbered` 'Chapter N. Title' on one line, running header 'Chapter N. Title' on every page of the
#                        chapter (three pages per chapter, so the header recurs; the first occurrence is the
#                        heading), page numbers at the TOP of the page, printed page = physical page, the TOC before
#                        the abstract, a final 'References' instead of a bibliography;
#   T003 `latex`         the chapter number alone on a line + title line, three-line TOC entries ('1' / 'Introduction'
#                        / '1'), chapter 1 on printed page 1 (its number line and its page number collide), no
#                        per-chapter references.

THESIS_TITLES = ["Primary stability of cementless femoral stems: a computational and experimental study",
                 "Surrogate-assisted optimisation of short-stem hip implants",
                 "Bone remodelling around porous femoral components after total hip arthroplasty"]
THESIS_CHAPTERS = [("Introduction", "intro"), ("Literature review", "lit_review"), ("Materials and methods", "methods"),
                   ("Results", "results"), ("Discussion", "discussion"), ("Conclusions and future work", "conclusion")]
THESIS_SUBS = {"Introduction": ["Motivation", "Aims and objectives", "Thesis outline"],
               "Literature review": ["Cementless fixation", "Interface micromotion", "Computational models"],
               "Materials and methods": ["Specimens", "Finite element model", "Loading and boundary conditions"],
               "Results": ["Interface micromotion", "Strain distribution", "Methods", "Sensitivity"],
               "Discussion": ["Comparison with the literature", "Limitations"],
               "Conclusions and future work": ["Conclusions", "Future work"]}
#: section traps (thesis2): table cells that look like headings inside the Discussion chapter, and a figure legend
#: inside the bibliography — none of them may switch the section
THESIS_TRAP_CELLS = ["Test", "Reference", "Topology"]
THESIS_TAIL_TRAP = ["Summary", "66.9"]
THESIS_ABSTRACT = ["Cementless femoral stems rely on primary stability for bone ingrowth.",
                   "This thesis quantifies interface micromotion of cementless stems with validated finite element models.",
                   "A surrogate model is used to explore the design space efficiently.",
                   "The results support a design rationale for improved proximal load transfer."]
THESIS_ACK = ("I thank my supervisors for their guidance and the Example Trust for funding this work; the workshop staff "
              "manufactured the test specimens.")
THESIS_DECL = ("I, {author}, confirm that the work presented in this thesis is my own. Where information has been derived "
               "from other sources, I confirm that this has been indicated in the thesis.")
THESIS_PARAS_PER_SECTION = 4
THESIS_MM_PER_CHAPTER = 2
THESIS_HEADER = "Doctoral thesis — University of Example"
THESIS_LAYOUTS = ("word_plain", "word_numbered", "latex")


def _dots(left: str, right: str, width: int = 86) -> str:
    n = max(3, width - len(left) - len(right))
    return f"{left} {'.' * n} {right}"


def _roman(n: int) -> str:
    out = ""
    for v, r in ((10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i")):
        while n >= v:
            out += r
            n -= v
    return out


class _Thesis:
    def __init__(self, rng: random.Random, doc_id: str, k: int):
        self.rng = rng
        self.doc_id = doc_id
        self.layout = THESIS_LAYOUTS[k % len(THESIS_LAYOUTS)]
        self.title = THESIS_TITLES[k % len(THESIS_TITLES)]
        self.author = f"{FIRST[k % len(FIRST)]} {LAST[(k * 3) % len(LAST)]}"
        self.year = 2019 + k
        self.n_chapters = 4 + k % 3
        self.chapters = [THESIS_CHAPTERS[i] for i in range(self.n_chapters)]
        self.per_chapter_refs = self.layout == "word_plain"
        self.bib_heading = "References" if self.layout == "word_numbered" else "Bibliography"
        self.abstract = list(THESIS_ABSTRACT)
        self.mm_sentences: list[str] = []
        self.body_sentences: list[str] = []
        self.refs = [f"[{i}] {LAST[i % len(LAST)]} {FIRST[i % len(FIRST)][0]}. {REF_TITLES[i % len(REF_TITLES)]}. "
                     f"J Biomech. {2005 + i};{i + 1}:{10 * i + 1}-{10 * i + 9}." for i in range(1, 9)]
        mm_idx = k * 100
        n_paras = THESIS_PARAS_PER_SECTION + (2 if self.layout == "word_numbered" else 0)   # 3 pages per chapter
        self.chapter_text: list[list[tuple[str, list[list[str]]]]] = []     # per chapter: [(sub heading, [paragraphs])]
        for ci, (heading, kind) in enumerate(self.chapters, start=1):
            subs = []
            for si, sub in enumerate(THESIS_SUBS[heading], start=1):
                paras = []
                for pi in range(n_paras):
                    sents = [_filler((mm_idx * 7 + ci * 50 + si * 10 + pi * 3 + j) % N_FILLER) for j in range(5)]
                    if ci >= 2 and pi == 0 and si <= THESIS_MM_PER_CHAPTER:
                        mm = _mm_sentence((mm_idx + ci * 10 + si) % N_MM)
                        sents[2] = mm
                        self.mm_sentences.append(mm)
                    paras.append(sents)
                    self.body_sentences.extend(sents)
                subs.append((f"{ci}.{si} {sub}", paras))
            self.chapter_text.append(subs)

    # ---- front matter -------------------------------------------------------------------------------------------
    def _toc_blocks(self, toc_pages: dict[int, int], tail_pages: dict[str, int]) -> list[list[_Line]]:
        """The TOC as one block per chapter (so a long LaTeX-style TOC paginates like a real one)."""
        blocks: list[list[_Line]] = [[_Line("Contents", HEADING_SIZE, new_page=True, mark="front:contents")]]
        for ci, (heading, kind) in enumerate(self.chapters, start=1):
            printed = str(toc_pages.get(ci, 0))
            if self.layout == "latex":
                toc = [_Line(str(ci), gap=PARA_GAP), _Line(heading), _Line(printed)]
            elif self.layout == "word_numbered":
                toc = [_Line(_dots(f"Chapter {ci}. {heading}", printed), gap=PARA_GAP)]
            else:
                toc = [_Line(_dots(f"Chapter {ci} {heading}", printed), gap=PARA_GAP)]
            for si, sub in enumerate(THESIS_SUBS[heading], start=1):
                if self.layout == "latex":
                    toc += [_Line(f"{ci}.{si}"), _Line(f"{sub} " + ". " * 20), _Line(printed)]
                elif self.layout == "word_numbered":
                    toc.append(_Line(_dots(f"{ci}.{si} {sub}", printed)))
                else:
                    toc.append(_Line(_dots(f"    {ci}.{si} {sub}", printed)))
            blocks.append(toc)
        tail: list[_Line] = []
        for name in (self.bib_heading, "Appendix A"):
            printed = str(tail_pages.get(name, 0))
            if self.layout == "latex":
                tail += [_Line(name, gap=PARA_GAP), _Line(printed)]
            else:
                tail.append(_Line(_dots(name, printed), gap=PARA_GAP))
        blocks.append(tail)
        return blocks

    def front_blocks(self, font: fitz.Font, toc_pages: dict[int, int] | None, tail_pages: dict[str, int] | None) -> list[list[_Line]]:
        title_lines = [_Line(t, TITLE_SIZE, mark="front:title") for t in _wrap(self.title, TITLE_SIZE, font, COLUMN_W + 60)]
        title_lines[0].new_page = True
        blocks = {
            "title": title_lines + [_Line(self.author, HEADING_SIZE, HEAD_GAP),
                                    _Line("A thesis submitted to the University of Example", gap=HEAD_GAP),
                                    _Line("for the degree of Doctor of Philosophy"),
                                    _Line("Department of Orthopaedic Engineering", gap=PARA_GAP), _Line(str(self.year), gap=PARA_GAP)],
            "declaration": [_Line("Declaration", HEADING_SIZE, new_page=True, mark="front:declaration")]
                           + _paragraph([THESIS_DECL.format(author=self.author)], font),
            "abstract": [_Line("Abstract", HEADING_SIZE, new_page=True, mark="front:abstract")] + _paragraph(self.abstract, font),
            "acknowledgements": [_Line("Acknowledgements", HEADING_SIZE, new_page=True, mark="front:acknowledgements")]
                                + _paragraph([THESIS_ACK], font),
            "figures": [_Line("List of Figures", HEADING_SIZE, new_page=True, mark="front:figures")]
                       + [_Line(_dots(f"Figure {ci}.1 Example figure of chapter {ci}", str((toc_pages or {}).get(ci, 0) + 1)))
                          for ci in range(1, self.n_chapters + 1)],
        }
        order = {"word_plain": ["title", "declaration", "abstract", "acknowledgements", "contents", "figures"],
                 "word_numbered": ["title", "declaration", "contents", "abstract", "acknowledgements", "figures"],
                 "latex": ["title", "abstract", "declaration", "acknowledgements", "contents", "figures"]}[self.layout]
        out: list[list[_Line]] = []
        for name in order:
            out.extend(self._toc_blocks(toc_pages or {}, tail_pages or {}) if name == "contents" else [blocks[name]])
        return out

    # ---- body -----------------------------------------------------------------------------------------------------
    def chapter_heading_lines(self, ci: int, heading: str) -> list[_Line]:
        mark = f"chapter:{ci}"
        if self.layout == "word_numbered":
            return [_Line(f"Chapter {ci}. {heading}", TITLE_SIZE, new_page=True, mark=mark)]
        first = str(ci) if self.layout == "latex" else f"Chapter {ci}"
        return [_Line(first, TITLE_SIZE, new_page=True, mark=mark), _Line(heading, HEADING_SIZE, HEAD_GAP)]

    def body_blocks(self, font: fitz.Font) -> list[list[_Line]]:
        blocks: list[list[_Line]] = []
        for ci, (heading, kind) in enumerate(self.chapters, start=1):
            blocks.append(self.chapter_heading_lines(ci, heading))
            for si, (sub, paras) in enumerate(self.chapter_text[ci - 1], start=1):
                blocks.append([_heading(sub)])
                for pi, para in enumerate(paras):
                    blocks.append(_paragraph(para, font))
                    if heading == "Discussion" and si == 1 and pi == 0:
                        blocks.append([_Line(t) for t in THESIS_TRAP_CELLS])      # a table rendered as short lines
            if self.per_chapter_refs:       # two references of its own per chapter (a line recurring on three
                k = 2 * ((ci - 1) % 4)      # page bottoms would be a running footer)
                blocks.append([_heading("References")] + [_Line(r) for r in self.refs[k:k + 2]])
        blocks.append([_Line(self.bib_heading, TITLE_SIZE, new_page=True, mark=f"tail:{self.bib_heading}")]
                      + [_Line(r, gap=2.0) for r in self.refs] + [_Line(t, gap=2.0) for t in THESIS_TAIL_TRAP])
        blocks.append([_Line("Appendix A", TITLE_SIZE, new_page=True, mark="tail:Appendix A"),
                       _Line("Supplementary tables", HEADING_SIZE, HEAD_GAP)]
                      + _paragraph([_filler((900 + j) % N_FILLER) for j in range(6)], font))
        return blocks

    # ---- page furniture ---------------------------------------------------------------------------------------------
    def furniture(self, n_front: int, body: list[list[tuple[float, _Line]]]) -> tuple[list[list[str]], list[str | None]]:
        """(header lines per page, footer per page) for the whole document."""
        n_body = len(body)
        if self.layout == "word_numbered":
            headers: list[list[str]] = [[str(i), THESIS_HEADER] for i in range(1, n_front + 1)]
            current = THESIS_HEADER
            for pi, lines in enumerate(body, start=1):
                first = lines[0][1] if lines else None
                if first is not None and first.mark and first.mark.startswith("chapter:"):
                    current = first.text
                elif first is not None and first.mark and first.mark.startswith("tail:"):
                    current = THESIS_HEADER
                headers.append([str(n_front + pi), current])
            return headers, [None] * (n_front + n_body)
        headers = [[THESIS_HEADER]] * (n_front + n_body)
        footers: list[str | None] = [_roman(i) for i in range(1, n_front + 1)] + [str(i) for i in range(1, n_body + 1)]
        return headers, footers

    def printed(self, n_front: int, body_page: int) -> int:
        return n_front + body_page if self.layout == "word_numbered" else body_page


def _thesis_layout(th: _Thesis, font: fitz.Font, toc_pages: dict[int, int] | None, tail_pages: dict[str, int] | None):
    front = _paginate(th.front_blocks(font, toc_pages, tail_pages))
    body = _paginate(th.body_blocks(font))
    return front, body


def _starts(pages: list[list[tuple[float, _Line]]], prefix: str) -> dict[str, int]:
    """{mark payload: 1-based page} for the pages whose first line carries a mark with `prefix`."""
    out: dict[str, int] = {}
    for pi, lines in enumerate(pages, start=1):
        first = lines[0][1] if lines else None
        if first is not None and first.mark and first.mark.startswith(prefix):
            out.setdefault(first.mark.split(":", 1)[1], pi)
    return out


def make_thesis_fixture(out_dir: str | Path, seed: int = 1) -> dict:
    out = Path(out_dir)
    pdf_dir = out / "pdf"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    font = _font()
    documents: list[dict] = []
    counts: dict[str, int] = {}
    rows: list[list[str]] = []
    for k in range(3):
        th = _Thesis(rng, f"T{k + 1:03d}", k)
        # pass 1: physical pages without TOC numbers → chapter start pages → pass 2 with the printed numbers
        front, body = _thesis_layout(th, font, None, None)
        n_front = len(front)
        ch_pages = {int(n): p for n, p in _starts(body, "chapter:").items()}
        tail_pages = _starts(body, "tail:")
        printed_ch = {ci: th.printed(n_front, p) for ci, p in ch_pages.items()}
        printed_tail = {name: th.printed(n_front, p) for name, p in tail_pages.items()}
        front, body = _thesis_layout(th, font, printed_ch, printed_tail)
        assert len(front) == n_front and {int(n): p for n, p in _starts(body, "chapter:").items()} == ch_pages
        assert _starts(body, "tail:") == tail_pages
        pages = front + body
        headers, footers = th.furniture(n_front, body)
        file = f"{th.doc_id}_{th.author.split()[-1]}_{th.year}.pdf"
        path = pdf_dir / file
        n_pages = _write_pdf(path, pages, THESIS_HEADER, font, {"title": th.title, "authors": th.author, "keywords": ""},
                             id_key=f"concept-atlas thesis fixture {seed} {th.doc_id}", footers=footers, headers=headers)
        chapters = []
        for ci in sorted(ch_pages):
            nxt = ch_pages.get(ci + 1) or tail_pages[th.bib_heading]
            chapters.append({"number": ci, "heading": th.chapters[ci - 1][0], "unit_kind": th.chapters[ci - 1][1],
                             "page_from": n_front + ch_pages[ci], "page_to": n_front + nxt - 1, "printed_from": printed_ch[ci],
                             "sections": [sub for sub, _ in th.chapter_text[ci - 1]],
                             "sub_first_sentences": [paras[0][0] for _, paras in th.chapter_text[ci - 1]]})
        disc = next((ci for ci, (h, _) in enumerate(th.chapters, start=1) if h == "Discussion"), None)
        traps = {"cells": list(THESIS_TRAP_CELLS), "tail": list(THESIS_TAIL_TRAP),
                 "after_cells": th.chapter_text[disc - 1][0][1][1][0] if disc else None}
        tail = [{"heading": th.bib_heading, "kind": "references", "page_from": n_front + tail_pages[th.bib_heading],
                 "page_to": n_front + tail_pages["Appendix A"] - 1},
                {"heading": "Appendix A", "kind": "appendix", "page_from": n_front + tail_pages["Appendix A"], "page_to": n_pages}]
        front_pages = _starts(front, "front:")
        counts[th.doc_id] = len(th.mm_sentences)
        documents.append({"doc_id": th.doc_id, "file": file, "path": str(path), "title": th.title, "year": th.year,
                          "authors": th.author, "doi": "", "venue": "University of Example", "doc_type": "thesis",
                          "kind": "thesis", "layout": th.layout, "n_pages": n_pages, "n_front_pages": n_front,
                          "header": THESIS_HEADER, "abstract": list(th.abstract), "abstract_page": front_pages["abstract"],
                          "toc_page": front_pages["contents"], "chapters": chapters, "tail": tail,
                          "per_chapter_references": th.per_chapter_refs, "micromotion_sentences": len(th.mm_sentences),
                          "mm_sentences": list(th.mm_sentences), "sentences": list(th.body_sentences),
                          "references": list(th.refs), "acknowledgements": THESIS_ACK,
                          "declaration": THESIS_DECL.format(author=th.author), "traps": traps})
        rows.append([th.doc_id, th.title, str(th.year), th.author, "", "University of Example", "thesis", file])
    manifest = out / "manifest.csv"
    with open(manifest, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["doc_id", "title", "year", "authors", "doi", "venue", "doc_type", "file"])
        w.writerows(rows)
    log.info("thesis fixture: %d documents written to %s", len(documents), out)
    return {"out_dir": str(out), "pdf_dir": str(pdf_dir), "manifest": str(manifest), "seed": seed, "kind": "thesis",
            "documents": documents, "micromotion_counts": counts, "specials": {}}


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="write the synthetic fixture corpus")
    ap.add_argument("out_dir")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--kind", choices=("paper", "thesis"), default="paper")
    a = ap.parse_args(argv)
    info = make_fixture(a.out_dir, a.seed, kind=a.kind)
    print(f"fixture written to {a.out_dir}: {len(info.get('documents', []))} documents")
    return 0
