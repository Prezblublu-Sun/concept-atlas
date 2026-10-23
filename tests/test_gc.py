"""`atlas evidence gc` on the fixture corpus: three ingest generations (rule version bumped twice), one build that
pins the first generation, one note that points at a sentence only the second generation holds. gc keeps the
current, the pinned and the note-protected extractions, removes the rest, survives VACUUM (FTS rebuilt) and is
idempotent. Also: it refuses while a build is open."""
from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path

import pytest

from atlas import cli
from atlas.config import init_project
from atlas.doctypes.paper import PaperDocType
from atlas.pipeline import build as B
from atlas.pipeline.stages.ingest import IngestStage, register_documents
from atlas.store.build import read_manifest, write_manifest
from atlas.store.evidence import EvidenceStore
from atlas.store.user import UserStore
from atlas.tools import gc
from atlas.tools.fixture import make_fixture

PLANTED = "Fig. 3 shows the micromotion pattern"


def _current(project) -> dict[str, str]:
    st = EvidenceStore.open(project.evidence_db, "ro")
    try:
        return {r[0]: r[1] for r in st.conn.execute("SELECT doc_id, ext_id FROM extractions WHERE is_current=1")}
    finally:
        st.close()


def _ext_ids(project) -> set[str]:
    st = EvidenceStore.open(project.evidence_db, "ro")
    try:
        return {r[0] for r in st.conn.execute("SELECT ext_id FROM extractions")}
    finally:
        st.close()


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    base = tmp_path_factory.mktemp("gc 测试")
    info = make_fixture(base / "fx", seed=1)
    project = init_project(base / "proj", name="gc", pdf_dir=info["pdf_dir"])
    assert register_documents(project, manifest=info["manifest"]).items_failed == 0
    assert IngestStage().run(project).items_done == 9
    yield {"info": info, "project": project}
    for p in project.builds_dir.glob("*/build.sqlite"):
        if p.exists():
            os.chmod(p, stat.S_IWRITE | stat.S_IREAD)


def test_gc_rules_on_three_generations(corpus, monkeypatch):
    project = corpus["project"]
    gen1 = _current(project)
    # a build pins generation 1; while it is open gc refuses
    m = B.new_build(project, note="pins gen1")
    build_dir = project.builds_dir / m["build_id"]
    with pytest.raises(gc.GcError, match="still open"):
        gc.plan(project)
    write_manifest(build_dir, dict(read_manifest(build_dir), status="finished"))   # the stages are not needed here

    monkeypatch.setattr(PaperDocType, "version", "paper-gc2")
    assert IngestStage().run(project).items_done == 9
    gen2 = _current(project)
    assert set(gen2.values()).isdisjoint(gen1.values())
    p = gc.plan(project)
    assert p["remove"] == [] and p["kept"] == {"current": 9, "pinned": 9, "protected": 0} and p["builds"] == {m["build_id"]: 9}
    r = gc.run(project, dry_run=True)
    assert r["removed"] == 0 and r["dry_run"] and "would remove 0" in gc.format_report(r)

    monkeypatch.setattr(PaperDocType, "version", "paper-gc3")
    assert IngestStage().run(project).items_done == 9
    gen3 = _current(project)
    # two sentences that exist only in generation 2: one referenced by a note (protects its extraction), one not
    st = EvidenceStore.open(project.evidence_db, "rw")
    try:
        for doc, sid, text in (("P001", "S_gc_noted", "A fabricated sentence kept by a note."),
                               ("P002", "S_gc_orphan", "A fabricated sentence nobody references.")):
            st.conn.execute("INSERT INTO sentences(sid, doc_id, text, first_ext) VALUES (?,?,?,?)", (sid, doc, text, gen2[doc]))
            st.conn.execute("INSERT INTO extraction_sentences(ext_id, sid, seq, unit_id, page_no, cstart, cend, section, is_body) "
                            "VALUES (?,?,9999,?,1,0,1,'other',1)", (gen2[doc], sid, doc))
        assert st.phrase_search("fabricated sentence")["total"] == 0       # not current → not searchable
        assert st.conn.execute("SELECT COUNT(*) FROM sentence_fts WHERE sentence_fts MATCH '\"fabricated sentence\"'").fetchone()[0] == 2
    finally:
        st.close()
    us = UserStore.open(project.user_db)
    try:
        us.create_note(title="keep", body_md="see [[S:S_gc_noted]]", targets=[{"ref": "S:S_gc_noted", "snapshot_text": "x"}])
    finally:
        us.close()
    assert gc.referenced_sids(project) == {"S_gc_noted"}

    p = gc.plan(project)
    assert p["kept"] == {"current": 9, "pinned": 9, "protected": 1}
    assert {e["ext_id"] for e in p["remove"]} == set(gen2.values()) - {gen2["P001"]}
    r = gc.run(project)
    assert r["removed"] == 8 and r["rows"]["extractions"] == 8 and r["rows"]["sentences"] == 1 and r["by_version"] == {
        next(iter(r["by_version"])): 8}
    assert "paper:paper-gc2" in next(iter(r["by_version"]))
    ids = _ext_ids(project)
    assert ids == set(gen1.values()) | set(gen3.values()) | {gen2["P001"]}
    st = EvidenceStore.open(project.evidence_db, "ro")
    try:
        assert st.conn.execute("SELECT COUNT(*) FROM sentences WHERE sid IN ('S_gc_noted', 'S_gc_orphan')").fetchone()[0] == 1
        # the FTS index followed the delete (trigger) and the current generation is still searchable
        assert st.conn.execute("SELECT COUNT(*) FROM sentence_fts WHERE sentence_fts MATCH '\"fabricated sentence\"'").fetchone()[0] == 1
        assert st.phrase_search(PLANTED)["total"] == 1
        assert st.sentence("S_gc_noted")["text"].startswith("A fabricated sentence kept")
        assert st.stats()["extractions"] == 19 and st.stats()["documents"] == 9
        # shared page text survived: every current page still resolves
        assert all(st.page(d, 1) is not None for d in gen3)
    finally:
        st.close()

    # releasing the build releases generation 1; --vacuum shrinks the file and rebuilds the FTS indexes
    os.chmod(build_dir / "build.sqlite", stat.S_IWRITE | stat.S_IREAD)
    shutil.rmtree(build_dir)
    before = project.evidence_db.stat().st_size
    r = gc.run(project, vacuum=True)
    assert r["removed"] == 9 and r["vacuum"] and r["fts_rebuilt"] and r["builds"] == {}
    assert _ext_ids(project) == set(gen3.values()) | {gen2["P001"]}
    assert project.evidence_db.stat().st_size < before and r["bytes_after"] < r["bytes_before"]
    st = EvidenceStore.open(project.evidence_db, "ro")
    try:
        res = st.phrase_search(PLANTED)
        assert res["total"] == 1 and res["rows"][0]["doc_id"] == corpus["info"]["specials"]["fig3"]["doc_id"]
        assert st.page_search(PLANTED)["total"] == 1
        assert st.sentence("S_gc_noted") is not None
    finally:
        st.close()
    # idempotent
    r = gc.run(project)
    assert r["removed"] == 0 and gc.plan(project)["remove"] == []


def test_gc_cli_dry_run_and_rebuild(corpus, capsys):
    project = corpus["project"]
    assert cli.main(["evidence", "gc", str(project.root), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "evidence gc (dry run)" in out and "would remove 0" in out
    assert cli.main(["evidence", "gc", str(project.root), "--rebuild-fts"]) == 0
    assert "FTS indexes rebuilt" in capsys.readouterr().out
    assert cli.main(["evidence", "gc", str(project.root / "nowhere")]) == 1


def test_gc_refuses_without_evidence(tmp_path: Path):
    project = init_project(tmp_path / "empty", name="empty")
    with pytest.raises(gc.GcError, match="no evidence store"):
        gc.plan(project)


def test_gc_plan_opens_the_user_store_read_only(corpus, monkeypatch):
    """Review fix: the planner only reads note_targets / note_links / collection_items — it must not open user.sqlite
    read-write (migration, write lock, WAL files) for a `--dry-run`."""
    project = corpus["project"]
    modes: list[str] = []
    orig = UserStore.open.__func__

    def spy(cls, path, mode="rw"):
        modes.append(mode)
        return orig(cls, path, mode)
    monkeypatch.setattr(UserStore, "open", classmethod(spy))
    assert project.user_db.is_file()
    mtime = project.user_db.stat().st_mtime_ns
    gc.referenced_sids(project)
    gc.run(project, dry_run=True)
    assert modes and set(modes) == {"ro"} and project.user_db.stat().st_mtime_ns == mtime
