"""Journal/conference paper (IMRaD): one document unit, non-body masks, section marks, topic fields.

Implemented by: increment 1, agent "doctypes". Port the proven filters from the previous system:
  H:/UCL_theses/2610 opt/术语整理_v2_2026-10-17/_scripts/rules_v2.py  (acknowledgement/declaration blocks,
  page-aware header/footer detection, affiliation lines before the Introduction, publication-info lines,
  residual reference lines) — keep their behaviour and known fixes (e.g. 'Funding:' on page 1 is pub_info/
  declaration only if it starts with an uppercase word or digit; header/footer = a normalised line that appears
  in the first or last 4 lines of at least 3 pages; affiliation rule only before the Introduction heading and
  never on Fig/Table lines; no bare http/www patterns in the pub_info rule).
Use atlas.core.segment.detect_heading for section marks.
"""
from __future__ import annotations

from collections.abc import Sequence

from atlas.core.schemas import DocMeta, LineMask, PageText, SectionMark, Unit
from .base import Thresholds, register


@register
class PaperDocType:
    name = "paper"
    version = "paper1"
    thresholds = Thresholds()

    def drop_non_body(self, pages: Sequence[PageText]) -> list[LineMask]:
        raise NotImplementedError("increment 1 / doctypes")

    def segment(self, doc_id: str, pages: Sequence[PageText], masks: Sequence[LineMask]) -> list[Unit]:
        """Exactly one Unit(unit_id=doc_id, kind='document', ordinal=0, heading=None, page_from=1,
        page_to=len(pages), confidence=1.0)."""
        raise NotImplementedError("increment 1 / doctypes")

    def label_sections(self, doc_id: str, pages: Sequence[PageText], masks: Sequence[LineMask],
                       units: Sequence[Unit]) -> list[SectionMark]:
        """Heading-driven marks with sanity rules: order must be monotone in IMRaD order (a later 'introduction'
        after 'results' is ignored); 'references' must come after 'discussion'/'results' (a 'References' heading
        on page 1 is ignored); if no 'abstract' heading exists but an 'introduction' heading does, the text
        between the first paragraph of ≥ 40 words on page 1 and the 'introduction' heading is 'abstract'; before
        that it is 'front' (title, authors). A 'Keywords:'/'Key words' line yields a 'keywords' mark that ends at
        the next blank line or heading."""
        raise NotImplementedError("increment 1 / doctypes")

    def topic_fields(self, unit: Unit, pages: Sequence[PageText], marks: Sequence[SectionMark],
                     meta: DocMeta) -> dict[str, list[str]]:
        raise NotImplementedError("increment 1 / doctypes")
