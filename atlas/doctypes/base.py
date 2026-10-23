"""DocType protocol. A doc type is pure: it receives page texts and returns masks, units and section marks.

Offsets everywhere index into PageText.text of the given page (so the reader can highlight exactly).
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from atlas.core.schemas import DocMeta, LineMask, PageText, SectionMark, Unit


@dataclass(frozen=True)
class Thresholds:
    max_drop_ratio: float = 0.15       # ingest warns when masks drop more characters than this share
    min_text_ratio: float = 0.30       # pages with fewer letters than this share of chars → likely scanned/figure
    min_chapter_confidence: float = 0.7


@runtime_checkable
class DocType(Protocol):
    name: str
    version: str                       # bump when any rule changes; enters extractor_version
    thresholds: Thresholds

    def drop_non_body(self, pages: Sequence[PageText]) -> list[LineMask]:
        """Character spans that are not body text and must not become sentences (headers/footers, affiliations,
        publication info, acknowledgement/declaration blocks, residual reference lines; theses: front matter,
        tables of contents). Masks never overlap; sorted by (page_no, cstart)."""
        ...

    def segment(self, doc_id: str, pages: Sequence[PageText], masks: Sequence[LineMask]) -> list[Unit]:
        """Units of analysis. Always exactly one unit with kind='document' (unit_id == doc_id, ordinal 0) first;
        theses append chapter units (kind='chapter', parent_unit=doc_id) in reading order."""
        ...

    def label_sections(self, doc_id: str, pages: Sequence[PageText], masks: Sequence[LineMask],
                       units: Sequence[Unit]) -> list[SectionMark]:
        """Section marks in reading order. The first mark is (1, 0, 'front'). Marks for 'references' and
        'acknowledgements' make everything after them non-body until the next mark. For theses, marks are
        produced per chapter (each chapter starts with its own 'introduction'-like section or 'other')."""
        ...

    def topic_fields(self, unit: Unit, pages: Sequence[PageText], marks: Sequence[SectionMark],
                     meta: DocMeta) -> dict[str, list[str]]:
        """{'title': [...], 'keywords': [...], 'abstract': [...]} — the texts in which an anchor concept counts as
        'topic' for this unit. Papers: manifest/meta title (else first-page title guess), keywords line, abstract.
        Thesis document: title + front-matter abstract; thesis chapter: heading + first 300 words."""
        ...

    def guess_title(self, pages: Sequence[PageText]) -> str | None:
        """The document's title read from its first text page, or None. `atlas docs add` and the ingest stage
        store it in document_meta when the manifest / PDF-metadata title is missing or junk
        (atlas.core.text.junk_title); the first pages suffice as input."""
        ...


DOCTYPES: dict[str, type] = {}


def register(cls):
    DOCTYPES[cls.name] = cls
    return cls


def get_doctype(name: str) -> DocType:
    if name not in DOCTYPES:
        # lazy import so that registering is a side effect of importing the module
        from . import paper, thesis  # noqa: F401
    try:
        return DOCTYPES[name]()
    except KeyError:
        raise KeyError(f"unknown doc type {name!r}; known: {sorted(DOCTYPES)}") from None
