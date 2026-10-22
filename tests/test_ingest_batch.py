"""Bulk writes of `run ingest`: several documents per transaction, per-document fallback when a batch fails."""
from __future__ import annotations

from pathlib import Path

import pytest

from atlas.config import init_project
from atlas.pipeline.stages import ingest as ingest_mod
from atlas.pipeline.stages.ingest import IngestStage, register_documents
from atlas.store.evidence import EvidenceStore
from atlas.tools.fixture import make_fixture


@pytest.fixture
def project(tmp_path: Path):
    info = make_fixture(tmp_path / "fx", seed=1)
    proj = init_project(tmp_path / "proj", name="fx", pdf_dir=info["pdf_dir"])
    rep = register_documents(proj, manifest=info["manifest"])
    assert rep.items_failed == 0 and len(rep.details["added"]) == 9
    return proj


def _current(proj) -> dict[str, dict]:
    store = EvidenceStore.open(proj.evidence_db, "ro")
    try:
        return {d["doc_id"]: store.current_extraction(d["doc_id"])
                for d in store.list_documents(limit=100)["rows"]}
    finally:
        store.close()


def test_small_batches_store_every_document(project, monkeypatch):
    monkeypatch.setattr(ingest_mod, "BATCH_DOCS", 2)        # 9 documents → 4 batches of 2 + 1 single
    rep = IngestStage().run(project)
    assert rep.items_done == 9 and rep.items_failed == 0, rep.summary()
    cur = _current(project)
    assert len(cur) == 9 and all(v is not None for v in cur.values())
    assert all(d["status"] == "done" for d in rep.details["documents"].values())


def test_failing_document_in_a_batch_falls_back_to_single_writes(project, monkeypatch):
    real = EvidenceStore.add_extraction
    bad = {"P003"}

    def flaky(self, *, doc_id, **kw):
        if doc_id in bad:
            raise RuntimeError("disk full (simulated)")
        return real(self, doc_id=doc_id, **kw)

    monkeypatch.setattr(EvidenceStore, "add_extraction", flaky)
    rep = IngestStage().run(project)                        # one batch of 9 → rollback → 9 single writes
    assert rep.items_done == 8 and rep.items_failed == 1, rep.summary()
    docs = rep.details["documents"]
    assert docs["P003"]["status"] == "failed" and "disk full" in docs["P003"]["error"]
    assert sum(1 for d in docs.values() if d["status"] == "done") == 8
    cur = _current(project)
    assert cur["P003"] is None and sum(1 for v in cur.values() if v is not None) == 8
    # the bad document is retried on the next run and the good ones are skipped
    bad.clear()
    rep2 = IngestStage().run(project)
    assert rep2.items_done == 1 and rep2.items_skipped == 8 and rep2.items_failed == 0, rep2.summary()
    assert _current(project)["P003"] is not None


def test_auto_checkpoint_is_restored_after_the_run(project):
    IngestStage().run(project)
    store = EvidenceStore.open(project.evidence_db, "rw")
    try:
        assert store.conn.execute("PRAGMA wal_autocheckpoint").fetchone()[0] == 1000
        wal = Path(str(project.evidence_db) + "-wal")
        assert not wal.exists() or wal.stat().st_size == 0          # TRUNCATE checkpoint ran at the end
    finally:
        store.close()
