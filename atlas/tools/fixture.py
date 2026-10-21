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
- deterministic given `seed` (fixed strings + seeded shuffles) so ids are stable across runs.
Theses are added in increment 2.
"""
from __future__ import annotations

from pathlib import Path


def make_fixture(out_dir: str | Path, seed: int = 1) -> dict:
    raise NotImplementedError("increment 1 / ingest")


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="write the synthetic fixture corpus")
    ap.add_argument("out_dir")
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args(argv)
    info = make_fixture(a.out_dir, a.seed)
    print(f"fixture written to {a.out_dir}: {len(info.get('documents', []))} documents")
    return 0
