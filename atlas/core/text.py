"""Text utilities (pure): normalisation helpers and number extraction for the verification checks.

Implemented by: increment 1, agent "core". Must be covered by tests/unit/test_text.py.

Number extraction works on *occurrences*: every occurrence yields an ordered list of alternative canonical
readings (primary first). `extract_numbers` flattens them into a set; `numbers_missing` treats an occurrence as
present when any of its readings appears in a source, so '1.000' in a statement is satisfied by '1000' or '1'
in a source.
"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from decimal import Decimal, InvalidOperation

TEXT_VERSION = "t1"

#: English number words → digits. Compounds like "eighty-nine" / "eighty nine" must resolve to "89";
#: "two hundred" → "200" is NOT required (keep it simple: only 0–20, tens, hundred, thousand, million as single
#: tokens or tens-units compounds).
NUMBER_WORDS: dict[str, int] = {
    **{w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve thirteen "
                                  "fourteen fifteen sixteen seventeen eighteen nineteen twenty".split())},
    "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
    "hundred": 100, "thousand": 1000, "million": 1_000_000,
}
#: Chinese numerals (single characters) and multipliers used by extract_numbers.
CJK_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
CJK_MULT = {"十": 10, "百": 100, "千": 1000, "万": 10_000, "亿": 100_000_000}


def nfkc(s: str | None) -> str:
    return unicodedata.normalize("NFKC", s or "")


def collapse_ws(s: str | None) -> str:
    return " ".join((s or "").split())


# ---- document titles ------------------------------------------------------------------------------------------

_TITLE_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_TITLE_FILE_RE = re.compile(r"\.(?:pdf|docx?|dvi|tex|ps|rtf|odt|wpd|txt|pptx?)\s*$", re.I)
_TITLE_JUNK_PREFIX_RE = re.compile(r"^(?:microsoft (?:word|powerpoint|office)|proquest|untitled|print to file|"
                                   r"pdf document|scanned document|new document|document\d*\b|slide \d)", re.I)
#: words that never make a title on their own ('PhD thesis final version', 'University of London thesis')
GENERIC_TITLE_WORDS = frozenset("""thesis theses dissertation phd dphil doctoral doctorate final version draft copy
corrected corrections correction submitted submission full complete vol volume whole document file main body text
university college london ucl of the a an and for degree doctor philosophy md mphil msc ma ba library hard bound
hardbound soft electronic e print eprint pdf deposited deposit edition revised""".split())


def junk_title(title: str | None) -> bool:
    """True when a manifest / PDF-metadata title is unusable as the document's title: empty, carrying control
    characters, fewer than three words, a file name ('main.dvi', 'Schauer_2008_PHD_vol1'), an application
    artefact ('Microsoft Word - Thesis correction.docx', 'ProQuest Dissertations', 'Print to file, Job 124') or
    nothing but generic thesis words ('PhD thesis final version', 'UNIVERSITY OF LONDON THESIS')."""
    t = collapse_ws(title)
    if not t or _TITLE_CTRL_RE.search(t) or _TITLE_JUNK_PREFIX_RE.match(t) or _TITLE_FILE_RE.search(t):
        return True
    words = re.findall(r"[^\W_]+", t)
    if len(words) < 3 or " " not in t:
        return True
    return all(w.lower() in GENERIC_TITLE_WORDS or w.isdigit() for w in words)


# ---- sentence text ------------------------------------------------------------------------------------------------

#: Ligature glyphs that PDF fonts emit for letter pairs ('ﬁnding' → 'finding'); NFKC would expand them too, but
#: NFKC also rewrites superscripts and the micro sign, which evidence text must keep.
LIGATURES = {"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "ft",
             "ﬆ": "st"}
_LIG_RE = re.compile("|".join(map(re.escape, LIGATURES)))
_HYPHEN_WORD_RE = re.compile(r"[^\W\d_]+(?:-[^\W\d_]+)+")           # 'long-term', 'state-of-the-art' inside one line
_SOFT_BREAK_RE = re.compile("\u00ad[ \t]*\n[ \t]*")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")                 # C0 / DEL glyph placeholders ('\x07')
_HARD_BREAK_RE = re.compile(r"([^\W\d_]+)-[ \t]*\n[ \t]*([^\W\d_]+)")


def hyphen_vocab(text: str | None) -> frozenset[str]:
    """Lower-cased hyphenated words that occur inside a line ('long-term', 'state-of-the-art'): the evidence that
    a hyphen at a line end belongs to the word rather than to the line break."""
    return frozenset(w.lower() for line in (text or "").split("\n") for w in _HYPHEN_WORD_RE.findall(line))


def dehyphenate(text: str | None, vocab: frozenset[str] | None = None) -> str:
    """Join words broken across lines. A soft hyphen (U+00AD) before the line break always joins
    ('Peripros<U+00AD>\\nthetic' → 'Periprosthetic'). A hard hyphen joins unless the hyphenated word is written
    inside a line elsewhere in the document ('long-\\nterm' → 'long-term' when `vocab` holds 'long-term', else
    'longterm'; 'state-of-\\nthe-art' keeps its hyphen when 'state-of-the-art' is in `vocab`)."""
    s = _SOFT_BREAK_RE.sub("", text or "")

    def join(m: re.Match[str]) -> str:
        a, b = m.group(1), m.group(2)
        word = f"{a}-{b}".lower()
        if vocab and any(word in v for v in vocab):
            return f"{a}-{b}"
        return a + b

    return _HARD_BREAK_RE.sub(join, s)


def sentence_text(raw: str | None, vocab: frozenset[str] | None = None) -> str:
    """Stored form of a sentence cut from page text: dehyphenated (see dehyphenate), stray soft hyphens and
    control characters removed, ligature glyphs expanded, line breaks and runs of whitespace collapsed to one
    space, stripped. Offsets into the page text are unaffected (they are kept separately)."""
    s = _CTRL_RE.sub("", dehyphenate(raw, vocab).replace("\u00ad", ""))
    s = _LIG_RE.sub(lambda m: LIGATURES[m.group(0)], s)
    return " ".join(s.split())


#: Inline evidence ids that LLM text sometimes contains and that must never count as numbers:
#: E12, R3, F7, ST_ab12cd, S1 + 16 hex, optionally wrapped in [] or () and separated by , ， 、 or spaces.
INLINE_ID_RE = re.compile(r"(?<![A-Za-z0-9])(?:\[|\()?\s*(?:(?:E|R|F)\d{1,4}|ST_[0-9a-f]{6,}|S1[0-9a-f]{16})"
                          r"(?:\s*[,，、]\s*(?:(?:E|R|F)\d{1,4}|ST_[0-9a-f]{6,}|S1[0-9a-f]{16}))*\s*(?:\]|\))?")


def strip_inline_ids(s: str | None) -> str:
    """Remove inline evidence ids (see INLINE_ID_RE) and collapse the whitespace they leave behind."""
    return collapse_ws(INLINE_ID_RE.sub(" ", s or ""))


# ---- number extraction ------------------------------------------------------------------------------------

_SUPERSCRIPT_TR = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻", "0123456789+-")
#: '10³' / '10⁻³' → '10^3' / '10^-3' (only when the superscript follows a digit).
_SUP_AFTER_DIGIT_RE = re.compile(r"(?<=\d)([⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻]+)")
#: Unit exponents and author markers ('mm²', 's⁻¹', 'Smith¹') are dropped before NFKC, which would otherwise turn
#: them into plain digits ('mm2' → 2).
_SUP_NOT_AFTER_DIGIT_RE = re.compile(r"(?<!\d)[⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻]+")

_TENS = "twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety"
_UNITS = "one|two|three|four|five|six|seven|eight|nine"
_SINGLE = "|".join(sorted(NUMBER_WORDS, key=len, reverse=True))
_CJK_CHARS = "".join(CJK_DIGITS) + "".join(CJK_MULT)

#: One scanner for every kind of number occurrence; alternatives are tried in this order at each position.
#: Digit alternatives must not start right after a digit or after 'digit.' (so '3.1.2' yields nothing) and must
#: not be followed by '.digit' or a digit (so an alternative never stops in the middle of a number).
_NUM_RE = re.compile(
    rf"""
      (?P<words>\b(?:(?P<tens>{_TENS})(?:[-\s]+(?P<units>{_UNITS})\b)?|(?P<single>{_SINGLE}))\b)
    | (?P<pct>百分之\s*(?P<pct_val>[{_CJK_CHARS}]+|\d+(?:\.\d+)?))
    | (?<!\d)(?<!\d\.)(?P<dmult>\d+(?:\.\d+)?)\s?(?P<dmult_m>[十百千万亿]+)
    | (?<!\d)(?<!\d\.)(?P<sci>\d+(?:\.\d+)?[eE][+-]?\d+)(?!\.?\d)
    | (?<!\d)(?<!\d\.)(?P<pow>\d+)\^(?P<pow_e>[+-]?\d{{1,3}})(?!\.?\d)
    | (?<!\d)(?<!\d\.)(?P<thou>\d{{1,3}}(?:,\d{{3}})+(?:\.\d+)?)(?!\.?\d)
    | (?<!\d)(?<!\d\.)(?P<eur>\d{{1,3}}(?:\.\d{{3}}){{2,}})(?!\.?\d)
    | (?<!\d)(?<!\d\.)(?P<dec>\d+)\.(?P<frac>\d+)(?!\.?\d)
    | (?<!\d)(?<!\d\.)(?P<int>\d+)(?!\.?\d)
    | (?<![\w.])\.(?P<ldec>\d+)(?!\.?\d)
    | (?P<cjk>[{_CJK_CHARS}]+)
    """,
    re.X | re.I,
)


def _fold(s: str | None) -> str:
    """NFKC fold for number parsing: full-width digits/percent, superscript exponents, Unicode minus."""
    s = _SUP_AFTER_DIGIT_RE.sub(lambda m: "^" + m.group(1).translate(_SUPERSCRIPT_TR), s or "")
    s = _SUP_NOT_AFTER_DIGIT_RE.sub("", s)
    return nfkc(s).replace("−", "-")


def _canon(d: Decimal) -> str:
    """Canonical decimal string: integers without leading zeros, decimals without trailing zeros."""
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def _dec(s: str) -> Decimal | None:
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def _cjk_value(s: str) -> int | None:
    """Value of a run of Chinese numerals, or None when the run is not a number (e.g. '百' alone)."""
    if not s:
        return None
    if all(ch in CJK_DIGITS for ch in s):
        # pure digit run: '三' → 3, '二〇一九' → 2019
        return int("".join(str(CJK_DIGITS[ch]) for ch in s))
    if not any(ch in CJK_DIGITS for ch in s) and not s.startswith("十"):
        return None  # multipliers only ('百', '千万', '亿') are words, not numbers
    total, section, number = 0, 0, 0
    for ch in s:
        if ch in CJK_DIGITS:
            number = CJK_DIGITS[ch]
        else:
            mult = CJK_MULT[ch]
            if mult < 10_000:
                section += (number if number else 1) * mult
                number = 0
            else:
                section += number
                number = 0
                if section == 0:
                    total *= mult          # '一万亿' → 1e12
                else:
                    total += section * mult
                section = 0
    return total + section + number


def _number_groups(s: str | None) -> list[list[str]]:
    """Every number occurrence in `s`, in order, as a list of alternative canonical readings (primary first)."""
    out: list[list[str]] = []
    for m in _NUM_RE.finditer(_fold(s)):
        kind = m.lastgroup
        alts: list[str] = []
        if m.group("words"):
            if m.group("tens"):
                v = NUMBER_WORDS[m.group("tens").lower()]
                if m.group("units"):
                    v += NUMBER_WORDS[m.group("units").lower()]
            else:
                v = NUMBER_WORDS[m.group("single").lower()]
            alts = [str(v)]
        elif m.group("pct"):
            raw = m.group("pct_val")
            if raw[0].isdigit():
                d = _dec(raw)
                alts = [_canon(d)] if d is not None else []
            else:
                v = _cjk_value(raw)
                alts = [str(v)] if v is not None else []
        elif m.group("dmult"):
            d = _dec(m.group("dmult"))
            if d is not None:
                for ch in m.group("dmult_m"):
                    d *= CJK_MULT[ch]
                alts = [_canon(d)]
        elif m.group("sci"):
            d = _dec(m.group("sci"))
            alts = [_canon(d)] if d is not None else []
        elif m.group("pow"):
            base, exp = int(m.group("pow")), int(m.group("pow_e"))
            alts = [f"{base}^{exp}"]
            if abs(exp) <= 40 and base <= 1_000_000:
                alts.append(_canon(Decimal(base) ** exp))
        elif m.group("thou"):
            d = _dec(m.group("thou").replace(",", ""))
            alts = [_canon(d)] if d is not None else []
        elif m.group("eur"):
            alts = [_canon(Decimal(m.group("eur").replace(".", "")))]
        elif m.group("dec"):
            ip, fp = m.group("dec"), m.group("frac")
            canon = _canon(Decimal(f"{ip}.{fp}"))
            if len(fp) == 3 and re.fullmatch(r"[1-9]\d{0,2}", ip):
                # '1.000' is ambiguous (decimal or European thousands): keep the raw form, the thousands
                # reading and the plain decimal reading
                alts = [f"{ip}.{fp}", _canon(Decimal(ip + fp)), canon]
            else:
                alts = [canon]
        elif m.group("int"):
            alts = [_canon(Decimal(m.group("int")))]
        elif m.group("ldec"):
            alts = [_canon(Decimal("0." + m.group("ldec")))]
        elif kind == "cjk":
            v = _cjk_value(m.group("cjk"))
            alts = [str(v)] if v is not None else []
        if alts:
            dedup: list[str] = []
            for a in alts:
                if a not in dedup:
                    dedup.append(a)
            out.append(dedup)
    return out


def extract_numbers(s: str | None) -> set[str]:
    """Numbers in a text, canonicalised as decimal strings, for subset checks.

    Rules (all must be unit-tested):
    - digits: '1,000' → '1000'; '1.000' is ambiguous → keep '1.000' AND '1000'; '0.50' → '0.5'; '15%' and '15 ％'
      → '15'; '10^3' → '10^3' and '1000'; '3.2e-3' → '0.0032'; ranges '50–150' → both numbers; '±4.1' → '4.1';
      full-width digits are NFKC-folded first; thousands separators only when the pattern is \\d{1,3}(,\\d{3})+;
    - English number words incl. tens-units compounds ('eighty-nine', 'eighty nine') → '89'; 'one' alone → '1';
    - Chinese: '三' → '3', '十五' → '15', '二十' → '20', '3万' → '30000', '1.5亿' → '150000000', '百分之十' → '10';
    - inline ids (INLINE_ID_RE) and years inside refs like '(2019)' are still numbers (do not special-case years);
    - return a set of canonical strings; integers without leading zeros, decimals without trailing zeros.
    """
    found: set[str] = set()
    for alts in _number_groups(s):
        found.update(alts)
    return found


def numbers_missing(statement: str | None, sources: Iterable[str], ignore: Iterable[str] = ("1", "2", "3")) -> list[str]:
    """Numbers of `statement` (after strip_inline_ids) that appear in none of `sources`; sorted. `ignore` lists
    trivially common numbers that never count as missing."""
    ignored = set(ignore)
    present: set[str] = set()
    for src in sources:
        present |= extract_numbers(src)
    missing: set[str] = set()
    for alts in _number_groups(strip_inline_ids(statement)):
        if alts[0] in ignored or any(a in present for a in alts):
            continue
        missing.add(alts[0])
    return sorted(missing)
