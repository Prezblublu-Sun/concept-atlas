"""Concept matcher (pure). Port of the previous viewer's `matcher.py` + `rules_v2.py` *semantics*; the data (forms,
bad-neighbour lists, thresholds) comes from `concepts.yaml` / `patterns.yaml`, never from this module.

Implemented by: increment 2 (derived layer).

Forms mode (`mode: forms`):
- tokens: `TOKEN = [A-Za-z0-9]*[A-Za-z][A-Za-z0-9]*` over a per-character NFKC-folded copy of the sentence
  (`fold_map`: ligatures expanded, mathematical italics and ℓ folded to ASCII letters, combining marks dropped,
  hyphens replaced by a space) whose offset map takes token spans back to the stored sentence text; keys are
  lower-cased tokens joined by one space (`norm_key`, applied to the curated forms as well);
- hyphen readings (m3): a word with an intra-word hyphen ('peri-prosthetic', 'co‑kriging' — hyphen-minus, U+2010 or
  U+2011 directly between two tokens, see `_HYPHENS`) is read both ways, as the split tokens ('peri', 'prosthetic')
  and as the joined token ('periprosthetic'), so a form written either way matches text written either way; the
  curated forms are folded the same way (`form_keys`: 'co-kriging' gives the keys 'co kriging' and 'cokriging';
  `case_key`: the case form 'micro-CT' is the case key 'microCT', matched against a token and against a
  hyphen-joined word — the joined reading of case forms is limited to case forms written with a hyphen, since a
  hyphen between abbreviations usually is a relation: a plain case form 'EI' does not match 'E-I').
  Per start token and concept the longest token span wins and, on a tie, the reading with fewer joins (the split
  reading, whose `form` is what m2 recorded), then a case-insensitive key over a case form; so a span is one
  occurrence per concept; matches of different concepts through the two readings are both kept and nest as
  usual. Dashes (en/em) stay separators;
- `forms + gen_forms` are case-insensitive keys; `case_forms` are case-sensitive single tokens (abbreviations);
  an all-caps case form with <= `short_caps_max_letters` letters (after a trailing 's') does not match inside a
  run of all-caps words (`in_caps_run`: both neighbouring alphabetic tokens all-caps with >= 2 letters);
- single-word keys obey `bad_next` / `bad_prev` (the neighbours of the whole word, joined or not) and the stem-caps
  rule (an all-caps token longer than 2 letters never matches `stem` / `stems`);
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
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass

MATCHER_VERSION = "m3"       # bump when any rule in this module changes
                             # m2: forms mode tokenises a per-character NFKC-folded copy of the stored text
                             # (ligatures ﬁ/ﬂ, ℓ, mathematical italics, combining marks) with an offset map,
                             # as the previous system scanned NFKC-normalised text
                             # m3: an intra-word hyphen is read both ways (split tokens and joined token) on the
                             # text side and in the curated forms (the previous system's counting joined every
                             # line-end hyphen, which the stored text keeps when the document writes the word with
                             # the hyphen); U+2010 / U+2011 count as hyphens

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

#: Hyphens of forms mode: hyphen-minus, hyphen (U+2010) and non-breaking hyphen (U+2011, which NFKC folds to U+2010).
#: They split a word into tokens and, directly between two tokens, give the joined reading. En/em dashes are not
#: hyphens ('bone–implant' is two words, 'stress–strain' a range).
_HYPHENS = frozenset("-\u2010\u2011")
_HYPHEN_TR = {ord(h): " " for h in _HYPHENS}
#: A hyphen directly between two token characters (the intra-word hyphen of a curated form).
_INTRA_HYPHEN_RE = re.compile("(?<=[A-Za-z0-9])[-\u2010\u2011](?=[A-Za-z0-9])")

_QUOTES_RE = re.compile(r"[’‘'`´]")
_DASHES = set("-‐‑‒–—/")
_SPACED = set(":<>=±")
_LIT_KEEP = set("abcdefghijklmnopqrstuvwxyz0123456789%:<>=± ")
#: Letters NFKC leaves alone but the previous system's corpus writes for Latin ones ('relative ℓ2 error').
_FOLD_EXTRA = {"ℓ": "l"}


def fold_map(s: str | None) -> tuple[str, list[int]]:
    """(folded text, map) for forms-mode tokenising: every character NFKC-normalised (ﬁ → fi, 𝐼 → I, ¼ → 1⁄4),
    `ℓ` → l, combining marks dropped, hyphens (`_HYPHENS`) → ' '; map[i] is the offset in `s` of the character that
    produced folded[i], so token spans index the stored text (a ligature's two letters both map to its one offset;
    the end of a span is `map[last] + 1`)."""
    out: list[str] = []
    idx: list[int] = []
    for i, ch in enumerate(s or ""):
        if ch in _HYPHENS:
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
    """Lower-case key of a label or form, tokenised exactly like the text: NFKC → ’→' → "'s\\b"→"s" → hyphens to
    spaces → tokens."""
    rx = token_re or tokenizer()
    s = unicodedata.normalize("NFKC", label or "").replace("’", "'")
    s = re.sub(r"'s\b", "s", s)
    return " ".join(t.lower() for t in rx.findall(s.translate(_HYPHEN_TR)))


def form_keys(form: str | None, token_re: re.Pattern[str] | None = None) -> list[str]:
    """Keys of one curated form (`forms` / `gen_forms`): `norm_key(form)` and, when the form has an intra-word
    hyphen, the key of the joined spelling as well ('co-kriging' → ['co kriging', 'cokriging'], 'state-of-the-art'
    → ['state of the art', 'stateoftheart']); the text side reads a hyphenated word both ways, so a form written
    either way matches text written either way."""
    s = unicodedata.normalize("NFKC", form or "")
    split = norm_key(s, token_re)
    keys = [split] if split else []
    if _INTRA_HYPHEN_RE.search(s):
        joined = norm_key(_INTRA_HYPHEN_RE.sub("", s), token_re)
        if joined and joined not in keys:
            keys.append(joined)
    return keys


def case_key(form: str | None) -> str:
    """Key of one curated case form: the form with its intra-word hyphens removed ('micro-CT' → 'microCT'), matched
    case-sensitively against a token and, for a case form written with a hyphen, against a hyphen-joined word."""
    return _INTRA_HYPHEN_RE.sub("", unicodedata.normalize("NFKC", form or "").strip())


def has_intra_hyphen(form: str | None) -> bool:
    """True when the form has a hyphen directly between two token characters ('micro-CT', 'co-kriging')."""
    return bool(_INTRA_HYPHEN_RE.search(unicodedata.normalize("NFKC", form or "")))


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
        self.case2ids: dict[str, list[str]] = {}          # case key -> ids, matched against one token
        self.case_joined2ids: dict[str, list[str]] = {}   # the case forms written with a hyphen ('micro-CT'):
                                                          # matched against hyphen-joined words as well
        self.literals: list[tuple[str, re.Pattern[str], str]] = []
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
                for key in form_keys(str(k), self.token_re):
                    if cid not in self.key2ids.setdefault(key, []):
                        self.key2ids[key].append(cid)
            for k in c.get("case_forms") or []:
                key = case_key(str(k))
                if not key:
                    continue
                if cid not in self.case2ids.setdefault(key, []):
                    self.case2ids[key].append(cid)
                if has_intra_hyphen(str(k)) and cid not in self.case_joined2ids.setdefault(key, []):
                    self.case_joined2ids[key].append(cid)
        #: proper word-prefixes of the multi-word keys: a candidate key is extended only while it is one of them
        self.prefixes: set[str] = set()
        for k in self.key2ids:
            t = k.split()
            for n in range(1, len(t)):
                self.prefixes.add(" ".join(t[:n]))
        self.short_caps = {k for k in self.case2ids
                           if k.isupper() and len(k.rstrip("s")) <= self.short_caps_max}

    @classmethod
    def from_curated(cls, concepts: Iterable[dict] | dict, patterns: dict | None = None) -> "Matcher":
        if isinstance(concepts, dict):
            concepts = concepts.get("concepts") or []
        return cls(concepts, patterns)

    # ---- forms ----------------------------------------------------------------------------------------------
    def _single_ok(self, w: str, orig: str, i: int, end: int, toks: Sequence[str]) -> bool:
        """The single-word rules for the word `w` (`orig` in the text's case) spanning tokens [i, end): `bad_next` /
        `bad_prev` look at the tokens after and before the whole word; the stem-caps rule."""
        nxt = toks[end] if end < len(toks) else ""
        prv = toks[i - 1] if i else ""
        if nxt in self.bad_next.get(w, ()) or prv in self.bad_prev.get(w, ()):
            return False
        if self.stem_caps_rule and orig.isupper() and len(orig) > 2 and w in self.stem_words:
            return False
        return True

    def _forms(self, text: str) -> tuple[list[tuple[int, int, str, str]], list[tuple[int, int]]]:
        """((token index, n tokens, concept id, key) for every forms/case match, token spans into `text`)."""
        raw, idx = fold_map(text)
        ms = list(self.token_re.finditer(raw))
        spans = [(idx[m.start()], idx[m.end() - 1] + 1) for m in ms]
        otoks = [m.group(0) for m in ms]
        toks = [t.lower() for t in otoks]
        n_tok = len(toks)
        # hyphenated[k]: tokens k and k+1 are one hyphenated word ('peri-prosthetic'): exactly one folded character
        # between them, standing for a hyphen of the stored text (fold_map writes ' ' for it)
        hyphenated = [k + 1 < n_tok and ms[k + 1].start() == ms[k].end() + 1 and text[idx[ms[k].end()]] in _HYPHENS
                      for k in range(n_tok)]

        def words(k: int) -> Iterator[tuple[str, str, int, int]]:
            """Readings of the word starting at token k: (key word, word in the text's case, end token, joins) —
            the token alone, then every hyphen-joined extension ('peri', 'periprosthetic')."""
            lo, orig, j = toks[k], otoks[k], k
            yield lo, orig, k + 1, 0
            while hyphenated[j]:
                j += 1
                lo += toks[j]
                orig += otoks[j]
                yield lo, orig, j + 1, j - k

        # (start, concept) -> (n tokens, -joins, key kind, key): the longest span wins, then the reading with fewer
        # joins (the split one), then a case-insensitive key over a case form (m2 left that tie to set order)
        best: dict[tuple[int, str], tuple[int, int, int, str]] = {}

        def record(i: int, end: int, cid: str, key: str, joins: int, kind: int) -> None:
            cand = (end - i, -joins, kind, key)
            if cand > best.get((i, cid), (0, 0, 0, "")):
                best[(i, cid)] = cand

        for i in range(n_tok):
            stack: list[tuple[int, str, int]] = [(i, "", 0)]          # (next token, key so far, joins so far)
            while stack:
                k, key, joins = stack.pop()
                for lo, orig, end, j in words(k):
                    key2 = lo if not key else key + " " + lo
                    ids = self.key2ids.get(key2)
                    cont = key2 in self.prefixes
                    if not ids and not cont:
                        continue
                    if ids and (key or self._single_ok(lo, orig, i, end, toks)):
                        for cid in ids:
                            record(i, end, cid, key2, joins + j, 1)
                    if cont and end < n_tok:
                        stack.append((end, key2, joins + j))
            # case forms: the token itself; a hyphen-joined word only for the case forms written with a hyphen
            # ('micro-CT' → 'microCT'; a plain 'EI' must not match 'E-I', where the hyphen is a relation)
            for lo, orig, end, j in words(i):
                ids = (self.case2ids if j == 0 else self.case_joined2ids).get(orig)
                if not ids:
                    continue
                if orig in self.short_caps and in_caps_run([otoks[i - 1] if i else "", orig,
                                                            otoks[end] if end < n_tok else ""], 1):
                    continue
                for cid in ids:
                    record(i, end, cid, orig, j, 0)
        out = [(i, n, cid, key) for (i, cid), (n, _, _, key) in best.items()]
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
