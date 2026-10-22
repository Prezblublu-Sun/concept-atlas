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

SEGMENT_VERSION = "seg5"   # seg3: descriptive (loose) headings, numbered-sequence and run-in headings;
                           # seg4: sentence text dehyphenated / ligatures expanded (atlas.core.text.sentence_text);
                           # seg5: control characters ignored in headings and sentence text

#: Canonical headings → section (strict forms). Matching is case-insensitive on the whole line after stripping
#: numbering ('2.', '2', 'II.', 'Chapter 3', '3.1') and trailing punctuation; the line must be ≤ 8 words.
HEADINGS: dict[str, str] = {
    r"abstract|summary|synopsis|structured abstract": "abstract",
    r"key ?words|index terms": "keywords",
    r"introductions?|background( and (motivation|significance))?": "introduction",
    r"(materials?,? and )?methods?( and materials?)?|methodology|experimental( (section|procedures?|setup))?|materials|"
    r"patients and methods|subjects and methods|study design": "methods",
    r"results?( and discussions?)?|findings": "results",
    r"(general )?discussions?( and conclusions?)?|discussions?\s*/\s*conclusions?": "discussion",
    r"conclusions?( and (future work|outlook|perspectives?|recommendations?))?|concluding remarks|"
    r"summary and conclusions?|outlook": "conclusion",
    r"acknowledge?ments?|funding|acknowledgments|conflicts? of interests?|declaration of (competing )?interests?|"
    r"competing interests?|author contributions?|data availability( statement)?": "acknowledgements",
    r"references?|bibliography|literature cited|works cited|notes and references|references and notes": "references",
    r"appendi(x|ces)(\s+[a-z0-9]{1,2})?(\.\s+.*)?|supplementary( material| materials| information| data)?|"
    r"supporting information": "appendix",
}
_HEADING_RES: list[tuple[re.Pattern[str], str]] = [(re.compile(p, re.I), s) for p, s in HEADINGS.items()]

#: Descriptive headings → section (loose forms: 'Proposed methodology', 'Numerical Results and Analysis',
#: 'Kriging method (KG)', 'Case studies', 'Related work'). Same matching as HEADINGS, but the line must start
#: with a capital and its first word must not be a determiner/pronoun (see _FRAG_FIRST), so wrapped sentence
#: fragments such as 'these results' or 'The experiments' never count. A loose heading ranks below a strict one
#: in the paper doctype's sanity rules (a strict 'Methods' after a loose 'Previous results' wins).
_ADJ = (r"(?:proposed|computational|numerical|experimental|statistical|research|general|overall|theoretical|"
        r"mathematical|analytical|simulation|finite element|fe|clinical|empirical|synthetic|benchmark|"
        r"illustrative|main|key|preliminary|detailed|basic|modelling|modeling|surgical|biomechanical|"
        r"in vitro|in vivo|in silico)")
HEADINGS_LOOSE: dict[str, str] = {
    r"background( and (preliminaries|related works?|motivation))?|motivation|related works?|"
    r"literature (review|survey)|preliminaries|state of the art|prior work|overview|"
    r"(theoretical |clinical |historical )?background": "introduction",
    rf"(?:{_ADJ} )?(methodology|methods?|approach(es)?|framework)( and [a-z\-]+( [a-z\-]+)?)?|"
    r"[a-z][a-z\-]* (methodology|methods?)|"
    r"experimental (design|setup|procedures?|section|details|protocol|study|methods?|techniques?)|"
    r"study (design|population|protocol)|participants|(problem|model) (statement|formulation|setting|definition|"
    r"description|setup)|"
    rf"(?:{_ADJ} )?(model(l?ing)?|implementation|procedures?|formulation|setup|algorithms?|protocol)( details)?|"
    r"theory|implementation( details)?|model development|"
    r"proposed (method|approach|model|framework|algorithm|scheme|technique)( [a-z\-]+)?": "methods",
    rf"(?:{_ADJ} )?results?( and (discussions?|analysis|comparisons?|evaluation|validation|interpretation))?|"
    r"[a-z][a-z\-]* results|results of [a-z][a-z\- ]{2,40}|"
    r"experiments?( and (results?|discussions?|evaluation|analysis))?|"
    rf"(?:{_ADJ} )?(experiments?|examples?|studies|study|tests?|investigations?|evaluations?|validation|"
    r"comparisons?|applications?)|"
    r"evaluation( and results)?|case stud(y|ies)|applications?( and results)?|(model )?validation( and verification)?|"
    r"verification and validation|performance (evaluation|comparison|analysis|assessment)|findings": "results",
    r"(general )?discussions?( and (future work|outlook|limitations?|implications))?|"
    r"limitations?( and future (work|research|directions))?|limitations? of (this|our) study": "discussion",
    r"conclusions?( and (discussion|outlook|future (work|directions|research)))?|closing remarks|final remarks|"
    r"(conclusions?,? )?(and )?future (work|directions|research|perspectives)|summary and (outlook|future work)|"
    r"conclusions? and perspectives?": "conclusion",
    r"ethics (statement|approval)|ethical (approval|statement)|informed consent|credit authorship contribution statement|"
    r"declarations?( of interests?)?|disclosures?( statement)?|funding (statement|sources?|information)|"
    r"availability of data( and materials?)?|(data and )?code availability|authors'? contributions?": "acknowledgements",
    r"references and notes|reference list": "references",
}
_HEADING_LOOSE_RES: list[tuple[re.Pattern[str], str]] = [(re.compile(p, re.I), s) for p, s in HEADINGS_LOOSE.items()]
#: First words that mark a wrapped sentence fragment rather than a heading ('these results', 'our method').
_FRAG_FIRST = frozenset("""the these those our their its this such all and or of in on for with to from as by a an some no
both other similar same different more most many several which whose previous prior earlier above following further
additional recent new two three four five consistent comparable good better best poor promising first second third
each any every either neither few various certain alternative current existing present standard conventional traditional
typical common popular so very not only also then when where while whereas because since although""".split())

#: Keyword → section for numbered structural headings ('IV. Computational Methodology'); first hit in this
#: order wins ('Results and discussion' → results, 'Discussion and conclusions' → discussion).
_CLASSIFY: list[tuple[re.Pattern[str], str]] = [(re.compile(p, re.I), s) for p, s in (
    (r"\bresults?\b|\bexperiments?\b|\bexperimental (results|evaluation|validation|stud(y|ies)|comparison)|"
     r"\bevaluations?\b|\bcase stud|\bnumerical (examples?|stud(y|ies)|tests?|investigations?|simulations?)|"
     r"\bvalidation\b|\bbenchmark|\bperformance\b|\bfindings\b|\bapplications?\b|\bexamples?\b|\bcomparisons?\b",
     "results"),
    (r"\bdiscussion|\blimitations?\b", "discussion"),
    (r"\bconclu|\bsummary\b|\boutlook\b|\bfuture (work|directions|research|perspectives)|\bfinal remarks|"
     r"\bclosing remarks|\bperspectives\b", "conclusion"),
    (r"\bmethod|\bmaterials?\b|\bapproach|\bframework|\bformulation|\bmodel(l?ing)?\b|\bmodels?\b|\balgorithm|"
     r"\bimplementation|\bsetup\b|\bdesign\b|\btheor|\bprocedure|\bprotocol|\bproposed\b|\bproblem (statement|"
     r"setting|definition|formulation|description)|\bparticipants|\bpatients|\bsubjects\b|\bmeasurements?\b|"
     r"\bsimulations?\b|\bstatistical analysis|\bdata (collection|acquisition|processing|preparation)|"
     r"\bexperimental\b|\bsystem description|\bnotation\b|\bdefinitions?\b", "methods"),
    (r"\bintroduction|\bbackground|\bmotivation|\brelated work|\bliterature|\bpreliminaries|\bstate of the art|"
     r"\boverview\b|\bprior work", "introduction"),
    (r"\breferences?\b|\bbibliography", "references"),
    (r"\backnowledg|\bfunding\b|\bconflicts? of interest|\bdeclaration|\bauthor contributions|\bdata availability",
     "acknowledgements"),
    (r"\bappendi|\bsupplement", "appendix"),
    (r"\babstract\b", "abstract"),
    (r"\bkey ?words\b|\bindex terms\b", "keywords"),
)]


def classify_heading_text(text: str) -> str | None:
    """Section for the text of a known heading by keyword anywhere in it (used for numbered structural
    headings whose shape is already established); None when no keyword matches ('Nomenclature',
    'Extrapolating from a pretrained PRNN')."""
    s = heading_line(text)
    for rx, section in _CLASSIFY:
        if rx.search(s):
            return section
    return None


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
#: Caption / label words: a period after a bare number that follows one of these at the START of a line is the
#: caption numbering ('Fig. 2. Micromotion versus load.'), not a sentence end. Mid-line ('shown in Fig. 3. The
#: next…') the period still ends the sentence.
CAPTION_LABELS: frozenset[str] = frozenset("fig figs figure figures table tables tab eq eqn equation scheme".split())
_LABEL_NUMBER_RE = re.compile(r"^\d{1,3}[a-z]?$")


def _caption_number(para: str, dot: int, tok_start: int, tok: str) -> bool:
    """True when `tok` (the token before the period at `dot`) is a caption number and the token before it is a
    caption label standing at the start of a line."""
    if not _LABEL_NUMBER_RE.match(tok):
        return False
    head = para[:tok_start].rstrip()
    m = _LAST_TOKEN_RE.search(head)
    if not m:
        return False
    label = m.group(1).lstrip(_OPENERS).rstrip(".").lower()
    if label not in CAPTION_LABELS:
        return False
    before = head[:m.start(1)]
    return not before.strip() or before.rstrip(" \t").endswith("\n")
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
    if tok.lower() in ABBREVIATIONS or _INITIAL_RE.match(tok):
        return True
    return _caption_number(para, dot, m.start(1), tok)


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
    """Canonical section for a line that is a heading, else None (see detect_heading_ex)."""
    hit = detect_heading_ex(line)
    return hit[0] if hit else None


#: C0 / DEL control characters (not \t \n): PDF fonts emit them as glyph placeholders ('2 \x07Methods').
_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def heading_line(line: str | None) -> str:
    """NFKC-normalised, control-character-free, whitespace-collapsed form of a line for heading detection."""
    return " ".join(_CTRL_RE.sub("", unicodedata.normalize("NFKC", line or "")).split())


def detect_heading_ex(line: str) -> tuple[str, str] | None:
    """(section, strength) for a heading line, else None. A heading is a short line (≤ 8 words, ≤ 60 chars)
    without a terminal period that matches HEADINGS (strength 'strict') or HEADINGS_LOOSE (strength 'loose')
    after numbering and a short trailing parenthesis are stripped; 'Results and discussion' → 'results';
    ALL-CAPS variants count. Lines that are clearly sentences (contain ' the ' …, end with '.' or start in lower
    case) are not headings; loose forms also need a non-fragment first word ('these results'). Control
    characters are ignored ('2\u2002 \x07Methods' in Springer PDFs)."""
    s = heading_line(line)
    if not s or len(s) > 60 or len(s.split()) > 8:
        return None
    core = _NUMBERING_RE.sub("", s, count=1).strip()
    if not core or core.endswith("."):
        return None
    core = core.rstrip(" :;,-–—")
    core = _TRAIL_PAREN_RE.sub("", core).rstrip(" :;,-–—")      # 'Kriging method (KG)'
    if _LETTER_SPACED_RE.match(core):      # 'A B S T R A C T' (letter-spaced Elsevier headings)
        core = core.replace(" ", "")
    if not core or re.search(r"\bthe\b", core, re.I):
        return None
    if not (core[0].isupper() or core[0].isdigit()):     # 'results' alone on a line is a wrapped sentence's last word
        return None
    for rx, section in _HEADING_RES:
        if rx.fullmatch(core):
            return section, "strict"
    if core.split()[0].lower() in _FRAG_FIRST:
        return None
    for rx, section in _HEADING_LOOSE_RES:
        if rx.fullmatch(core):
            return section, "loose"
    return None


# ---- numbered structural headings ------------------------------------------------------------------------------

_NUM_HEAD_RE = re.compile(r"^(?:(\d{1,2})|([IVX]{1,6}))(?:\.|\s*\|)?\s+(\S.*)$")
_ROMAN_INT = {r: n for n, r in enumerate(
    "I II III IV V VI VII VIII IX X XI XII XIII XIV XV XVI XVII XVIII XIX XX".split(), start=1)}
_TRAIL_PAREN_RE = re.compile(r"\s*\([^()]{1,15}\)$")
_RUNIN_RE = re.compile(r"(.{3,70}?)\.\s+[A-Z(\"'“]")
_NOT_HEAD_RE = re.compile(r"[=<>{}\[\]∑∫≤≥±]|^(fig|figure|table|tab|eq|equation)\b\.?", re.I)
NUMBERED_MAX = 30


def split_numbered_heading(line: str) -> tuple[int, str] | None:
    """(number, heading text) for a top-level numbered heading line: '3 Extrapolating from a pretrained PRNN',
    'IV. Computational Methodology', '2 | METHODS' and the run-in form '1. Introduction. Operator learning has …'
    (heading = text before the first '. ' that is followed by a capital). None for sub-numbers ('2.1 …'), units
    ('4 MB', '2 mm thick plates'), formulas, captions and ordinary sentences. The caller decides whether the
    number fits the sequence of the document."""
    s = heading_line(line)
    m = _NUM_HEAD_RE.match(s)
    if not m:
        return None
    n = int(m.group(1)) if m.group(1) else _ROMAN_INT.get(m.group(2), 0)
    rest = m.group(3)
    if not 1 <= n <= NUMBERED_MAX or not rest[0].isupper() or _NOT_HEAD_RE.search(rest):
        return None
    if mm := _RUNIN_RE.match(rest):          # run-in heading: 'Introduction. Operator learning …'
        head = mm.group(1)
        if len(head.split()) > 8 or "," in head:     # '12. Smith J, Jones K. Title …' (a reference entry)
            return None
    elif rest.endswith(".") or len(rest) > 80 or len(rest.split()) > 10:
        return None
    else:
        head = rest
    head = head.rstrip(" :;,-–—")
    words = head.split()
    if sum(c.isalpha() for c in head) < 4 or not any(len(w.strip(".,;:()")) >= 4 and w[0].isalpha() for w in words):
        return None
    return n, head


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
