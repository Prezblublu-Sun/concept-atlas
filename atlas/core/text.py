"""Text utilities (pure): normalisation helpers and number extraction for the verification checks.

Implemented by: increment 1, agent "core". Must be covered by tests/unit/test_text.py.
"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

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


#: Inline evidence ids that LLM text sometimes contains and that must never count as numbers:
#: E12, R3, F7, ST_ab12cd, S1 + 16 hex, optionally wrapped in [] or () and separated by , ， 、 or spaces.
INLINE_ID_RE = re.compile(r"(?<![A-Za-z0-9])(?:\[|\()?\s*(?:(?:E|R|F)\d{1,4}|ST_[0-9a-f]{6,}|S1[0-9a-f]{16})"
                          r"(?:\s*[,，、]\s*(?:(?:E|R|F)\d{1,4}|ST_[0-9a-f]{6,}|S1[0-9a-f]{16}))*\s*(?:\]|\))?")


def strip_inline_ids(s: str | None) -> str:
    """Remove inline evidence ids (see INLINE_ID_RE) and collapse the whitespace they leave behind."""
    raise NotImplementedError("increment 1 / core")


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
    raise NotImplementedError("increment 1 / core")


def numbers_missing(statement: str | None, sources: Iterable[str], ignore: Iterable[str] = ("1", "2", "3")) -> list[str]:
    """Numbers of `statement` (after strip_inline_ids) that appear in none of `sources`; sorted. `ignore` lists
    trivially common numbers that never count as missing."""
    raise NotImplementedError("increment 1 / core")
