"""UserStore unit tests: notes (rev conflicts, history throttle), collections (ordering), annotations (cap)."""
from __future__ import annotations

from pathlib import Path

import pytest

from atlas.store import user as user_mod
from atlas.store.db import USER_SCHEMA_VERSION, user_version
from atlas.store.user import ConflictError, UserStore


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch):
    """Controllable time.time() for the user store."""
    state = {"t": 1_800_000_000.0}

    def fake_time() -> float:
        return state["t"]

    monkeypatch.setattr(user_mod.time, "time", fake_time)
    return state


@pytest.fixture
def store(tmp_path: Path, clock) -> UserStore:
    s = UserStore.open(tmp_path / "用户 dir" / "user.sqlite")
    yield s
    s.close()


# ---- notes ---------------------------------------------------------------------------------------------------

def test_create_and_get_note(store: UserStore):
    n = store.create_note(title="Calcar", body_md="See [[S:S1abc]] and [[D:P001]] and [[S:S1abc]] again.",
                          tags=["Def", " Quant ", "def", ""],
                          targets=[{"ref": "S:S1abc", "snapshot_text": "the calcar…", "snapshot_json": {"page": 3},
                                    "snapshot_build": "evidence"}])
    assert n["note_id"].startswith("N_") and n["rev"] == 1 and n["created"] == n["updated"]
    assert n["title"] == "Calcar" and n["tags"] == ["def", "quant"]
    assert n["links"] == ["S:S1abc", "D:P001"]
    assert n["targets"] == [{"ref": "S:S1abc", "snapshot_text": "the calcar…", "snapshot_json": {"page": 3},
                             "snapshot_build": "evidence", "status": "live", "resolved_ref": None}]
    assert store.get_note(n["note_id"]) == n
    assert store.get_note("N_nope") is None
    raw = store.conn.execute("SELECT tags FROM notes WHERE note_id=?", (n["note_id"],)).fetchone()[0]
    assert raw == ",def,quant,"
    assert user_version(store.conn) == USER_SCHEMA_VERSION
    h = store.history(n["note_id"])
    assert len(h) == 1 and h[0]["reason"] == "explicit" and h[0]["rev"] == 1 and h[0]["tags"] == ["def", "quant"]


def test_update_note_and_conflict(store: UserStore, clock):
    n = store.create_note(title="t", body_md="b [[S:S1a]]")
    nid = n["note_id"]
    clock["t"] += 5
    n2 = store.update_note(nid, rev=1, body_md="b2 [[S:S1b]]")
    assert n2["rev"] == 2 and n2["body_md"] == "b2 [[S:S1b]]" and n2["title"] == "t"
    assert n2["links"] == ["S:S1b"] and n2["updated"] > n2["created"]
    # stale rev → ConflictError carrying the server copy; attempt saved to history as 'conflict'
    with pytest.raises(ConflictError) as ei:
        store.update_note(nid, rev=1, body_md="lost edit")
    assert ei.value.server_note["rev"] == 2 and ei.value.server_note["body_md"] == "b2 [[S:S1b]]"
    assert store.get_note(nid)["body_md"] == "b2 [[S:S1b]]"
    h = store.history(nid)
    assert h[0]["reason"] == "conflict" and h[0]["body_md"] == "lost edit" and h[0]["rev"] == 1
    # tags-only update keeps body and links; None fields untouched
    n3 = store.update_note(nid, rev=2, tags=["X"])
    assert n3["rev"] == 3 and n3["tags"] == ["x"] and n3["links"] == ["S:S1b"]
    with pytest.raises(KeyError):
        store.update_note("N_nope", rev=1, title="x")


def test_history_throttle(store: UserStore, clock):
    nid = store.create_note(title="t", body_md="v1")["note_id"]
    assert [h["reason"] for h in store.history(nid)] == ["explicit"]
    clock["t"] += 60  # 1 min: autosave → no history row
    store.update_note(nid, rev=1, body_md="v2")
    assert len(store.history(nid)) == 1
    clock["t"] += 5 * 60  # 6 min: still throttled
    store.update_note(nid, rev=2, body_md="v3")
    assert len(store.history(nid)) == 1
    clock["t"] += 5 * 60  # 11 min since the last history row → 'throttle' row
    store.update_note(nid, rev=3, body_md="v4")
    h = store.history(nid)
    assert [x["reason"] for x in h] == ["throttle", "explicit"] and h[0]["body_md"] == "v4" and h[0]["rev"] == 4
    clock["t"] += 10  # explicit save always writes
    store.update_note(nid, rev=4, body_md="v5", explicit=True)
    assert [x["reason"] for x in store.history(nid)] == ["explicit", "throttle", "explicit"]
    # a conflict row does not reset the throttle clock
    clock["t"] += 60
    with pytest.raises(ConflictError):
        store.update_note(nid, rev=1, body_md="stale")
    clock["t"] += 9 * 60 + 30  # 10.5 min since the explicit row
    store.update_note(nid, rev=5, body_md="v6")
    assert [x["reason"] for x in store.history(nid)] == ["throttle", "conflict", "explicit", "throttle", "explicit"]


def test_delete_list_and_search(store: UserStore, clock):
    a = store.create_note(title="Alpha stem", body_md="about calcar loading", tags=["mech"])
    clock["t"] += 1
    b = store.create_note(title="Beta cup", body_md="about fixation", tags=["mech", "cup"])
    clock["t"] += 1
    c = store.create_note(title="Gamma", body_md="calcar again", tags=[])
    r = store.list_notes()
    assert r["total"] == 3 and [x["note_id"] for x in r["rows"]] == [c["note_id"], b["note_id"], a["note_id"]]
    assert "body_md" not in r["rows"][0] and r["rows"][0]["excerpt"] == "calcar again"
    assert r["rows"][0]["targets"] == []
    assert {x["note_id"] for x in store.list_notes(q="calcar")["rows"]} == {a["note_id"], c["note_id"]}
    assert store.list_notes(tag="CUP")["total"] == 1
    assert store.list_notes(tag="mech", q="fixation")["rows"][0]["note_id"] == b["note_id"]
    assert store.list_notes(q='"broken (')["total"] == 0
    assert store.list_notes(limit=1, offset=1)["rows"][0]["note_id"] == b["note_id"]
    store.delete_note(c["note_id"])
    assert store.get_note(c["note_id"]) is None
    assert store.list_notes(q="calcar")["total"] == 1
    assert store.conn.execute("SELECT COUNT(*) FROM note_fts WHERE note_id=?", (c["note_id"],)).fetchone()[0] == 0


def test_targets_and_notes_for_ref(store: UserStore, clock):
    a = store.create_note(title="a", targets=[{"ref": "S:S1x"}])
    clock["t"] += 1
    b = store.create_note(title="b", body_md="links [[S:S1x]]")
    clock["t"] += 1
    c = store.create_note(title="c")
    store.add_target(c["note_id"], "D:P001", snapshot_text="doc", snapshot_json={"k": 1})
    assert [n["note_id"] for n in store.notes_for_ref("S:S1x")] == [b["note_id"], a["note_id"]]
    assert [n["note_id"] for n in store.notes_for_ref("D:P001")] == [c["note_id"]]
    assert store.get_note(c["note_id"])["targets"][0]["snapshot_json"] == {"k": 1}
    store.set_target_status(c["note_id"], "D:P001", "aliased", "D:P002")
    assert store.get_note(c["note_id"])["targets"][0]["status"] == "aliased"
    assert [n["note_id"] for n in store.notes_for_ref("D:P002")] == [c["note_id"]]
    store.remove_target(c["note_id"], "D:P001")
    assert store.notes_for_ref("D:P001") == [] and store.get_note(c["note_id"])["targets"] == []
    store.delete_note(a["note_id"])
    assert [n["note_id"] for n in store.notes_for_ref("S:S1x")] == [b["note_id"]]
    with pytest.raises(KeyError):
        store.add_target("N_nope", "S:S1x")


# ---- collections ---------------------------------------------------------------------------------------------

def test_collections_order(store: UserStore):
    k = store.create_collection("Reading list", "stems")
    assert k["coll_id"].startswith("K_") and k["items"] == [] and k["name"] == "Reading list"
    i1 = store.add_item(k["coll_id"], "S:S1a", {"text": "one"}, "first")
    i2 = store.add_item(k["coll_id"], "S:S1b", {"text": "two"})
    i3 = store.add_item(k["coll_id"], None, None, "free comment")
    assert (i1["seq"], i2["seq"], i3["seq"]) == (1.0, 2.0, 3.0)
    assert i1["item_id"].startswith("KI_") and i1["snapshot"] == {"text": "one"} and i3["ref"] is None
    mid = store.add_item(k["coll_id"], "S:S1c", None, "between 1 and 2", after_item=i1["item_id"])
    assert mid["seq"] == 1.5
    tail = store.add_item(k["coll_id"], "S:S1d", None, after_item=i3["item_id"])
    assert tail["seq"] == 4.0
    items = store.get_collection(k["coll_id"])["items"]
    assert [i["item_id"] for i in items] == [i1["item_id"], mid["item_id"], i2["item_id"], i3["item_id"],
                                             tail["item_id"]]
    store.update_item(i3["item_id"], comment_md="edited", seq=0.5)
    items = store.get_collection(k["coll_id"])["items"]
    assert items[0]["item_id"] == i3["item_id"] and items[0]["comment_md"] == "edited"
    store.update_item(i1["item_id"])  # no-op
    store.remove_item(mid["item_id"])
    store.remove_item("KI_nope")
    assert len(store.get_collection(k["coll_id"])["items"]) == 4
    lst = store.list_collections()
    assert len(lst) == 1 and lst[0]["n_items"] == 4 and lst[0]["coll_id"] == k["coll_id"]
    with pytest.raises(KeyError):
        store.add_item("K_nope", None, None)
    with pytest.raises(KeyError):
        store.add_item(k["coll_id"], None, None, after_item="KI_nope")
    with pytest.raises(KeyError):
        store.update_item("KI_nope", comment_md="x")
    store.delete_collection(k["coll_id"])
    assert store.get_collection(k["coll_id"]) is None and store.list_collections() == []


# ---- annotations ---------------------------------------------------------------------------------------------

def test_annotations_versions_cap_and_conflict(store: UserStore, clock):
    assert store.get_annotations("P001") is None
    r = store.save_annotations("P001", '{"v":1}', base_version=0, device="laptop")
    assert r == {"version": 1, "saved": r["saved"]} and "conflict" not in r
    assert store.save_annotations("P001", '{"v":9}', base_version=0) == {"conflict": True, "version": 1}
    for v in range(2, 13):
        clock["t"] += 1
        assert store.save_annotations("P001", '{"v":%d}' % v, base_version=v - 1)["version"] == v
    a = store.get_annotations("P001")
    assert a["version"] == 12 and a["annots_json"] == '{"v":12}' and a["device"] is None
    assert [x["version"] for x in a["versions"]] == list(range(3, 13))  # last 10 kept
    assert store.get_annotations("P001", version=3)["annots_json"] == '{"v":3}'
    assert store.get_annotations("P001", version=2) is None
    assert store.get_annotations("P002") is None


# ---- ref aliases ---------------------------------------------------------------------------------------------

def test_resolve_ref_chain_and_cycle(store: UserStore, clock):
    assert store.resolve_ref("ST:a") == ("ST:a", "live")
    store.add_ref_alias("ST:a", "ST:b", "regenerated", "B1")
    clock["t"] += 1
    store.add_ref_alias("ST:b", "ST:c", "regenerated", "B2")
    assert store.resolve_ref("ST:a") == ("ST:c", "aliased")
    assert store.resolve_ref("ST:b") == ("ST:c", "aliased")
    store.add_ref_alias("ST:c", "ST:a")
    assert store.resolve_ref("ST:a") == ("ST:c", "aliased")  # a→b→c→(a seen): terminates
    assert store.resolve_ref("ST:c") == ("ST:b", "aliased")  # c→a→b→(c seen)
