"""Stage 1 `ingest`: PDFs + manifest → evidence rows. Pure computation per document; no LLM.

Implemented by: increment 1, agent "ingest".

Per document (in doc_id order, `--limit N` for a sample, `--doc-ids a,b`):
 1. pdf_sha256 = sha256_file(path); doc_type from manifest column else project default; doctype = get_doctype(…).
 2. extractor_version = f"{SEGMENT_VERSION}+{doctype.name}:{doctype.version}+{extractor_tag()}+ovr:{sha of the
    document's entry in curated/units_override.yaml if any else '-'}"; ext_id = extraction_id(...).
    If the extraction exists and not `reextract` → skip (items_skipped).
 3. pages = extract_pages(); if text_ratio < thresholds.min_text_ratio → warning 'low text ratio' (still ingest).
 4. masks = doctype.drop_non_body(pages); units = doctype.segment(); marks = doctype.label_sections().
 5. Sentences, per page: remove masked spans (keep a map from the cleaned string back to original offsets),
    split_sentences() on the cleaned page text, map spans back to (cstart, cend) in the original page text, assign
    section by the latest SectionMark at or before (page_no, cstart), unit by page range (chapter whose
    page_from ≤ page ≤ page_to, else the document unit), is_body = section in BODY_SECTIONS, tags =
    tag_sentence(), flags = garbage_flags(). Sentences never cross pages. seq increases in reading order.
 6. sid = sentence_id(doc_id, text, k) where k counts earlier sentences in this extraction with the same norm().
 7. Aliases: if the document had a previous current extraction, for each old sentence find new sentences on the
    same page whose char span overlaps ≥ 0.5 of the old span (split and merge allowed → several rows);
    add_aliases().
 8. store.add_extraction(...) in one transaction; drop_ratio = masked chars / all chars; warn when above
    thresholds.max_drop_ratio.
Manifest: CSV with columns doc_id,title,year,authors,doi,venue,doc_type,file (file relative to pdf_dir); documents
not in the manifest but present in pdf_dir get doc_id 'D' + 4-digit sequence (persisted in the documents table,
so re-running assigns the same ids) and meta from pdf_metadata(). `atlas docs add` registers; `atlas run ingest`
extracts. The stage logs one line per document to work/logs/ingest.log and returns a Report.
"""
from __future__ import annotations

from atlas.config import Project
from atlas.pipeline.runner import Report


class IngestStage:
    name = "ingest"
    version = "ingest1"
    llm = False
    requires: tuple[str, ...] = ()

    def run(self, project: Project, *, reextract: bool = False, limit: int | None = None,
            doc_ids: list[str] | None = None, workers: int = 1) -> Report:
        raise NotImplementedError("increment 1 / ingest")


def register_documents(project: Project, manifest: str | None = None, pattern: str = "**/*.pdf") -> Report:
    """`atlas docs add`: read manifest.csv (if present or given) and/or scan pdf_dir; add documents and files to
    the evidence store; never extracts. Report.details = {added, updated, unchanged, missing_files}."""
    raise NotImplementedError("increment 1 / ingest")
