"""PhD thesis: front matter, table of contents, chapter units, per-chapter sections. Increment 2."""
from __future__ import annotations

from collections.abc import Sequence

from atlas.core.schemas import DocMeta, LineMask, PageText, SectionMark, Unit
from .base import Thresholds, register


@register
class ThesisDocType:
    name = "thesis"
    version = "thesis0"
    thresholds = Thresholds(max_drop_ratio=0.30)

    def drop_non_body(self, pages: Sequence[PageText]) -> list[LineMask]:
        raise NotImplementedError("increment 2")

    def segment(self, doc_id: str, pages: Sequence[PageText], masks: Sequence[LineMask]) -> list[Unit]:
        raise NotImplementedError("increment 2")

    def label_sections(self, doc_id: str, pages: Sequence[PageText], masks: Sequence[LineMask],
                       units: Sequence[Unit]) -> list[SectionMark]:
        raise NotImplementedError("increment 2")

    def topic_fields(self, unit: Unit, pages: Sequence[PageText], marks: Sequence[SectionMark],
                     meta: DocMeta) -> dict[str, list[str]]:
        raise NotImplementedError("increment 2")
