"""Concept matcher (pure). Port of the previous viewer's `matcher.py` + `rules_v2.py` *semantics*; the data (forms,
bad-neighbour lists, thresholds) comes from `concepts.yaml` / `patterns.yaml`, never from this module.

Implemented by: increment 2 (derived layer).

Forms mode (`mode: forms`):
- tokens: `TOKEN = [A-Za-z0-9]*[A-Za-z][A-Za-z0-9]*` over a per-character NFKC-folded copy of the sentence
  (`fold_map`: ligatures expanded, mathematical italics and ℓ folded to ASCII letters, combining marks dropped,
  '-' replaced by a space) whose offset map takes token spans back to the stored sentence text; keys are
  lower-cased tokens joined by one space;
- `forms + gen_forms` are case-insensitive keys; `case_forms` are case-sensitive single tokens (abbreviations);
  an all-caps case form with <= `short_caps_max_letters` letters (after a trailing 's') does not match inside a
  run of all-caps words (`in_caps_run`: both neighbouring alphabetic tokens all-caps with >= 2 letters);
- single-token keys obey `bad_next` / `bad_prev` and the stem-caps rule (an all-caps token longer than 2 letters
  never matches `stem` / `stems`);
- longest match wins per start token and concept; overlapping matches of different concepts are both kept; a
  match fully inside a longer match of another concept gets `nested_in` = that concept (v2 "body_nested").

Literal mode (`mode: literal`): `rx` is applied to `lit_norm(sentence)` (NFKC, lower, quotes removed, dashes and
slashes to space, `:<>=±` spaced, other punctuation to space, whitespace collapsed) with `probe` as a cheap
substring pre-check; `cstart/cend` are mapped back to the stored sentence text through an offset map.

`Matcher.sentence(text)` / `Matcher.text(text)` return a deterministic list of `Occurrence` sorted by
(cstart, cend, concept_id).
"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

MATCHER_VERSION = "m2"       # bump when any rule in this module changes
                             # m2: forms mode tokenises a per-character NFKC-folded copy of the stored text
                             # (ligatures ﬁ/ﬂ, ℓ, mathematical italics, combining marks) with an offset map,
                             # as the previous system scanned NFKC-normalised text

DEFAULT_TOKEN_REGEX = r"[A-Za-z0-9]*[A-Za-z][A-Za-z0-9]*"
DEFAULT_PATTERNS: dict = {
    "version": "v2",
    "token_regex": DEFAULT_TOKEN_REGEX,
    "bad_next": {"prior": ["to", "work", "works", "studies", "study", "research", "art", "knowledge", "literature"],
                 "stems": ["from"], "stem": ["from", "cell", "cells"]},
    "bad_prev": {"gap": ["research", "knowledge", "this", "the"]},
    "short_caps_max_letters": 3,
    "stem_caps_rule": True,
    "stem_caps_words": ["stem", "stems"],
}

_QUOTES_RE = re.compile(r"[’‘'`´]")
_DASHES = set("-‐‑‒–—/")
_SPACED = set(":<>=±")
_LIT_KEEP = set("abcdefghijklmnopqrstuvwxyz0123456789%:<>=± ")
#: Letters NFKC leaves alone but the previous system's corpus writes for Latin ones ('relative ℓ2 error').
_FOLD_EXTRA = {"ℓ": "l"}


def fold_map(s: str | None) -> tuple[str, list[int]]:
    """(folded text, map) for forms-mode tokenising: every character NFKC-normalised (ﬁ → fi, 𝐼 → I, ¼ → 1⁄4),
    `ℓ` → l, combining marks dropped, '-' → ' '; map[i] is the offset in `s` of the character that produced
    folded[i], so token spans index the stored text (a ligature's two letters both map to its one offset; the end
    of a span is `map[last] + 1`)."""
    out: list[str] = []
    idx: list[int] = []
    for i, ch in enumerate(s or ""):
        if ch == "-":
            out.append(" ")
            idx.append(i)
            continue
        for c in unicodedata.normalize("NFKC", ch):
            c = _FOLD_EXTRA.get(c, c)
            if unicodedata.category(c) == "Mn":          # combining mark (from '˜' → ' ̃', 'é' stays precomposed)
                continue
            out.append(c)
            idx.append(i)
    return "".join(out), idx


@dataclass(frozen=True, order=True)
class Occurrence:
    cstart: int
    cend: int
    concept_id: str
    form: str
    nested_in: str | None = None


def tokenizer(pattern: str | None = None) -> re.Pattern[str]:
    return re.compile(pattern or DEFAULT_TOKEN_REGEX)


def norm_key(label: str | None, token_re: re.Pattern[str] | None = None) -> str:
    """Lower-case key of a label or form, tokenised exactly like the text: NFKC → ’→' → "'s\\b"→"s" → tokens."""
    rx = token_re or tokenizer()
    s = unicodedata.normalize("NFKC", label or "").replace("’", "'")
    s = re.sub(r"'s\b", "s", s)
    return " ".join(t.lower() for t in rx.findall(s.replace("-", " ")))


def in_caps_run(otoks: Sequence[str], i: int) -> bool:
    """True when both neighbouring alphabetic tokens are all-caps words of >= 2 letters."""
    def caps(t: str) -> bool:
        return t.isalpha() and t.isupper() and len(t) >= 2
    prev = otoks[i - 1] if i > 0 else ""
    nxt = otoks[i + 1] if i + 1 < len(otoks) else ""
    return caps(prev) and caps(nxt)


def lit_norm(s: str | None) -> str:
    """The literal-mode normalisation of the previous system (string only)."""
    return lit_norm_map(s)[0]


def lit_norm_map(s: str | None) -> tuple[str, list[int]]:
    """(normalised string, map) where map[i] is the offset in `s` of the character that produced norm[i].
    Per-character NFKC + lower so that every output character has a source offset; `:<>=±` become ' x ' (the
    inserted spaces map to the symbol), other punctuation becomes a space, whitespace runs collapse to one space
    (mapped to the first source character of the run), leading/trailing spaces dropped."""
    chars: list[tuple[str, int]] = []
    for i, ch in enumerate(s or ""):
        for c in unicodedata.normalize("NFKC", ch).lower():
            if _QUOTES_RE.match(c):
                continue
            if c in _DASHES:
                chars.append((" ", i))
            elif c in _SPACED:
                chars.append((" ", i))
                chars.append((c, i))
                chars.append((" ", i))
            elif c in _LIT_KEEP:
                chars.append((c, i))
            else:
                chars.append((" ", i))
    out: list[str] = []
    idx: list[int] = []
    pending_space: int | None = None
    for c, i in chars:
        if c == " ":
            if out and pending_space is None:
                pending_space = i
            continue
        if pending_space is not None:
            out.append(" ")
            idx.append(pending_space)
            pending_space = None
        out.append(c)
        idx.append(i)
    return "".join(out), idx


class Matcher:
    """Built from curated concept dicts + the patterns dict (see module docstring)."""

    def __init__(self, concepts: Iterable[dict], patterns: dict | None = None):
        p = dict(DEFAULT_PATTERNS)
        p.update({k: v for k, v in (patterns or {}).items() if v is not None})
        self.version = str(p.get("version", "v2"))
        self.token_re = tokenizer(p.get("token_regex"))
        self.bad_next = {k: frozenset(v or ()) for k, v in (p.get("bad_next") or {}).items()}
        self.bad_prev = {k: frozenset(v or ()) for k, v in (p.get("bad_prev") or {}).items()}
        self.short_caps_max = int(p.get("short_caps_max_letters", 3))
        self.stem_caps_rule = bool(p.get("stem_caps_rule", True))
        self.stem_words = frozenset(p.get("stem_caps_words") or ("stem", "stems"))
        self.key2ids: dict[str, list[str]] = {}
        self.case2ids: dict[str, list[str]] = {}
        self.literals: list[tuple[str, re.Pattern[str], str]] = []
        self.first: dict[str, int] = {}
        for c in concepts:
            cid = str(c["id"])
            if c.get("merged_into"):
                continue
            mode = c.get("mode") or "forms"
            if mode == "literal":
                rx = c.get("rx")
                if rx:
                    self.literals.append((cid, re.compile(rx), str(c.get("probe") or "")))
                continue
            for k in list(c.get("forms") or []) + list(c.get("gen_forms") or []):
                k = str(k).strip()
                if k and cid not in self.key2ids.setdefault(k, []):
                    self.key2ids[k].append(cid)
            for k in c.get("case_forms") or []:
                k = str(k).strip()
                if k and cid not in self.case2ids.setdefault(k, []):
                    self.case2ids[k].append(cid)
        for k in self.key2ids:
            t = k.split()
            self.first[t[0]] = max(self.first.get(t[0], 0), len(t))
        self.short_caps = {k for k in self.case2ids
                           if k.isupper() and len(k.rstrip("s")) <= self.short_caps_max}

    @classmethod
    def from_curated(cls, concepts: Iterable[dict] | dict, patterns: dict | None = None) -> "Matcher":
        if isinstance(concepts, dict):
            concepts = concepts.get("concepts") or []
        return cls(concepts, patterns)

    # ---- forms ----------------------------------------------------------------------------------------------
    def _forms(self, text: str) -> tuple[list[tuple[int, int, str, str]], list[tuple[int, int]]]:
        """((token index, n tokens, concept id, key) for every forms/case match, token spans into `text`)."""
        raw, idx = fold_map(text)
        spans = [(idx[m.start()], idx[m.end() - 1] + 1) for m in self.token_re.finditer(raw)]
        otoks = [raw[m.start():m.end()] for m in self.token_re.finditer(raw)]
        toks = [t.lower() for t in otoks]
        n_tok = len(toks)
        found: set[tuple[int, int, str, str]] = set()
        for i in range(n_tok):
            mx = self.first.get(toks[i], 0)
            key = ""
            for n in range(1, min(mx, n_tok - i) + 1):
                key = toks[i] if n == 1 else key + " " + toks[i + n - 1]
                ids = self.key2ids.get(key)
                if not ids:
                    continue
                if n == 1:
                    w = toks[i]
                    nxt = toks[i + 1] if i + 1 < n_tok else ""
                    prv = toks[i - 1] if i else ""
                    if nxt in self.bad_next.get(w, ()) or prv in self.bad_prev.get(w, ()):
                        continue
                    if self.stem_caps_rule and otoks[i].isupper() and len(otoks[i]) > 2 and w in self.stem_words:
                        continue
                for cid in ids:
                    found.add((i, n, cid, key))
            ids = self.case2ids.get(otoks[i])
            if ids and not (otoks[i] in self.short_caps and in_caps_run(otoks, i)):
                for cid in ids:
                    found.add((i, 1, cid, otoks[i]))
        # longest match per (start token, concept)
        best: dict[tuple[int, str], tuple[int, str]] = {}
        for i, n, cid, key in found:
            cur = best.get((i, cid))
            if cur is None or n > cur[0]:
                best[(i, cid)] = (n, key)
        out = [(i, n, cid, key) for (i, cid), (n, key) in best.items()]
        out.sort()
        return out, spans

    @staticmethod
    def _nested(matches: list[tuple[int, int, str, str]], spans: list[tuple[int, int]]) -> list[Occurrence]:
        occ: list[Occurrence] = []
        for i, n, cid, key in matches:
            container: tuple[int, int, str] | None = None
            for j, n2, jid, _ in matches:
                if j > i:
                    break
                if n2 > n and j + n2 >= i + n and jid != cid:
                    cand = (-n2, j, jid)
                    if container is None or cand < container:
                        container = cand
            occ.append(Occurrence(spans[i][0], spans[i + n - 1][1], cid, key,
                                  container[2] if container else None))
        return occ

    # ---- literal --------------------------------------------------------------------------------------------
    def _literal(self, text: str) -> list[Occurrence]:
        if not self.literals:
            return []
        ln, idx = lit_norm_map(text)
        if not ln:
            return []
        out: list[Occurrence] = []
        for cid, rx, probe in self.literals:
            if probe and probe not in ln:
                continue
            for m in rx.finditer(ln):
                a, b = m.start(), m.end()
                if b <= a:
                    continue
                out.append(Occurrence(idx[a], idx[b - 1] + 1, cid, m.group(0)))
        return out

    # ---- API ------------------------------------------------------------------------------------------------
    def sentence(self, text: str | None) -> list[Occurrence]:
        """All occurrences in one stored sentence (deterministic order)."""
        text = text or ""
        if not text.strip():
            return []
        occ = self._nested(*self._forms(text)) + self._literal(text)
        occ.sort()
        return occ

    def text(self, text: str | None) -> list[Occurrence]:
        """Same for a topic-field text (title, keywords line, abstract)."""
        return self.sentence(text)

    def counts(self, text: str | None) -> dict[str, tuple[int, int]]:
        """{concept_id: (n, n_nested)} for one text."""
        out: dict[str, list[int]] = {}
        for o in self.sentence(text):
            c = out.setdefault(o.concept_id, [0, 0])
            c[0] += 1
            if o.nested_in:
                c[1] += 1
        return {k: (v[0], v[1]) for k, v in out.items()}
