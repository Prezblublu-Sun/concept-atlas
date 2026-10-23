"""AtlasService caches (performance work): the /info evidence stats are cached per evidence file state and drop
when the file changes; a promoted build of an older schema is refused with a clear 409 message."""
from __future__ import annotations

from pathlib import Path

import pytest

from atlas.config import init_project
from atlas.core.ids import extraction_id
from atlas.core.schemas import DocMeta, PageText, SentenceRow, Unit
from atlas.service.api import AtlasService, NoBuildError
from atlas.store.build import BuildStore, set_current_build
from atlas.store.evidence import EvidenceStore

SHA = "3" * 64


def _add_doc(ev: EvidenceStore, doc_id: str, text: str) -> None:
    ev.add_document(doc_id, "paper", "manifest", Path("x.pdf"), f"{doc_id}.pdf", SHA, DocMeta(title=doc_id, year=2020))
    ext = extraction_id(doc_id, SHA, "t")
    row = SentenceRow(sid=f"S_{doc_id}", doc_id=doc_id, text=text, ext_id=ext, seq=0, unit_id=doc_id, page_no=1,
                      cstart=0, cend=len(text), section="introduction", is_body=True, tags=(), flags=())
    ev.add_extraction(ext_id=ext, doc_id=doc_id, pdf_sha256=SHA, extractor_version="t", doc_type="paper",
                      pages=[PageText(1, text, 595.0, 842.0)], masks=[], units=[Unit(doc_id, doc_id, "document", 0, None, 1, 1)],
                      sentences=[row], text_ratio=0.9, drop_ratio=0.0, stats={"body_sentences": 1})


@pytest.fixture
def project(tmp_path: Path):
    p = init_project(tmp_path / "proj", name="cache test")
    ev = EvidenceStore.open(p.evidence_db)
    _add_doc(ev, "P001", "The femoral stem transfers load.")
    ev.close()
    return p


def test_info_stats_cache_invalidates_when_the_evidence_changes(project, monkeypatch):
    svc = AtlasService(project)
    calls = {"n": 0}
    orig = EvidenceStore.stats

    def counted(self):
        calls["n"] += 1
        return orig(self)
    monkeypatch.setattr(EvidenceStore, "stats", counted)
    with svc.lease():
        a = svc.info()
        b = svc.info()
    assert a["stats"]["documents"] == 1 and a["stats"]["sentences"] == 1 and b == a and calls["n"] == 1
    # another writer (ingest, another process) changes the evidence → the next /info recomputes
    ev = EvidenceStore.open(project.evidence_db)
    _add_doc(ev, "P002", "Bone remodelling is governed by strain.")
    ev.close()
    with svc.lease():
        c = svc.info()
        d = svc.info()
    assert c["stats"]["documents"] == 2 and c["stats"]["sentences"] == 2 and c["stats"]["body_sentences"] == 2
    assert d == c and calls["n"] == 2
    with svc.lease():
        s = svc.search("femoral stem")
    assert s["total"] == 1 and s["total_capped"] is False
    svc.shutdown()


def test_old_schema_build_is_refused_with_a_message(project):
    st = BuildStore.create(project.builds_dir, "b2026-01-01_001", project.name, project.curated_dir)
    st.conn.execute("PRAGMA user_version = 1")          # a build file written before schema 2
    st.close()
    set_current_build(project.builds_dir, "b2026-01-01_001")
    svc = AtlasService(project)
    with svc.lease():
        assert svc.build_optional() is None
        with pytest.raises(NoBuildError, match="older schema"):
            svc.build()
        info = svc.info()
    assert info["build_info"] is None and info["build"] == "b2026-01-01_001"
    svc.shutdown()
    # and open_build refuses to write into it
    from atlas.store.db import BuildSchemaError
    with pytest.raises(BuildSchemaError, match="schema 1"):
        BuildStore.open(project.builds_dir / "b2026-01-01_001", "rw")


def test_stale_build_is_reported_honestly_and_not_prewarmed(project):
    """Review fix: a promoted build this code refuses is reported as `build_stale` (the UI names the build and the
    `atlas build upgrade` command instead of 'no build'), and prewarm skips its file."""
    from atlas.http.server import prewarm_paths
    st = BuildStore.create(project.builds_dir, "b2026-01-01_001", project.name, project.curated_dir)
    st.conn.execute("PRAGMA user_version = 1")
    st.close()
    set_current_build(project.builds_dir, "b2026-01-01_001")
    svc = AtlasService(project)
    with svc.lease():
        info = svc.info()
        assert info["build_info"] is None and info["build_stale"]["id"] == "b2026-01-01_001"
        assert "atlas build upgrade b2026-01-01_001" in info["build_stale"]["message"]
        with pytest.raises(NoBuildError, match="atlas build upgrade b2026-01-01_001"):
            svc.list_concepts()
    svc.shutdown()
    assert [p.name for p in prewarm_paths(project)] == ["evidence.sqlite"]
    # a current build is prewarmed and reported as build_info
    st = BuildStore.create(project.builds_dir, "b2026-01-01_002", project.name, project.curated_dir)
    st.close()
    set_current_build(project.builds_dir, "b2026-01-01_002")
    assert [p.name for p in prewarm_paths(project)] == ["evidence.sqlite", "build.sqlite"]
    svc = AtlasService(project)
    with svc.lease():
        info = svc.info()
    assert info["build_stale"] is None and info["build_info"]["id"] == "b2026-01-01_002"
    svc.shutdown()
