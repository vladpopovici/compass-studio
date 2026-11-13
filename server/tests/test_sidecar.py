"""Sidecar: fork, batched edits with revisions, undo via inverses, conflicts,
detection of another process's commits."""

import multiprocessing as mp
import sqlite3

import pytest
import shapely

from compass_studio.annot.sidecar import Conflict, Sidecar
from compass_studio.synth.generate import Params, generate

SQ = [[[0, 0], [10, 0], [10, 10], [0, 10]]]


@pytest.fixture(scope="module")
def source(tmp_path_factory):
    d = tmp_path_factory.mktemp("src")
    generate(d, "s", Params(width=10_000, height=10_000, points=100, cells=500, regions=2,
                            region_vertices=(50, 80), region_radius_px=(100.0, 200.0), attrs=5),
             arrow=False, log=lambda m: None)
    return d / "s.ann.sqlite"


def objects_in(sc, lid):
    return {r[0] for r in sc._conn.execute(
        "SELECT og.object_id FROM object_groups og JOIN groups g USING(group_id) WHERE g.layer_id = ?",
        (lid,))}


def test_fork_copies_layer(tmp_path, source):
    sc = Sidecar(tmp_path / "c.sqlite")
    lid = sc.fork_layer(source, 2)
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    expect = {r[0] for r in src.execute(
        "SELECT og.object_id FROM object_groups og JOIN groups g USING(group_id) WHERE g.layer_id = 2")}
    assert objects_in(sc, lid) == expect
    n_rtree = sc._conn.execute("SELECT COUNT(*) FROM objects_rtree").fetchone()[0]
    assert n_rtree == len(expect)
    assert sc._conn.execute("SELECT source_layer_id FROM cs_forks WHERE layer_id = ?", (lid,)).fetchone()[0] == 2


def test_edit_undo_and_conflict(tmp_path):
    sc = Sidecar(tmp_path / "c.sqlite")
    lid, (g1, g2) = sc.create_layer("mine", ["a", "b"])
    r1 = sc.apply(0, [{"op": "create", "group_id": g1, "coords": SQ, "values": {"ki67": 0.5}},
                      {"op": "create", "group_id": g1, "coords": SQ}])
    assert r1.revision == 1 and len(r1.object_ids) == 2
    a, b = r1.object_ids
    with pytest.raises(Conflict):
        sc.apply(0, [{"op": "delete", "object_id": a}])
    moved = [[[100, 100], [120, 100], [120, 130], [100, 130]]]
    r2 = sc.apply(1, [{"op": "update_geometry", "object_id": a, "coords": moved},
                      {"op": "move", "object_id": b, "group_id": g2},
                      {"op": "update_attributes", "object_id": a, "values": {"ki67": 0.9, "cd8": 2.0}},
                      {"op": "delete", "object_id": b}])
    assert r2.revision == 2 and objects_in(sc, lid) == {a}
    rt = sc._conn.execute("SELECT min_x, max_y FROM objects_rtree WHERE object_id = ?", (a,)).fetchone()
    assert rt == (100.0, 130.0)
    # undo revision 2 as a new batch
    r3 = sc.apply(2, sc.inverse_of(2))
    assert r3.revision == 3 and objects_in(sc, lid) == {a, b}
    assert sc._conn.execute("SELECT group_id FROM object_groups WHERE object_id = ?", (b,)).fetchone()[0] == g1
    wkb = sc._conn.execute("SELECT wkb FROM objects WHERE object_id = ?", (a,)).fetchone()[0]
    assert shapely.from_wkb(wkb).equals(shapely.Polygon(SQ[0]))
    assert sc._get_values(a, None) == {"ki67": 0.5}
    # failed batch leaves nothing behind
    with pytest.raises(ValueError):
        sc.apply(3, [{"op": "delete", "object_id": a}, {"op": "create", "group_id": 999, "coords": SQ}])
    assert sc.revision() == 3 and objects_in(sc, lid) == {a, b}


def _external_commit(path, base):
    sc = Sidecar(path)
    g = sc._conn.execute("SELECT MIN(group_id) FROM groups").fetchone()[0]
    sc.apply(base, [{"op": "create", "group_id": g, "coords": SQ}], client="other")
    sc.close()


def test_other_process_detected(tmp_path):
    path = tmp_path / "c.sqlite"
    sc = Sidecar(path)
    sc.create_layer("mine", ["a"])
    v0, rev0 = sc.data_version(), sc.revision()
    p = mp.get_context("spawn").Process(target=_external_commit, args=(path, rev0))
    p.start(); p.join(30)
    assert p.exitcode == 0
    assert sc.data_version() != v0 and sc.revision() == rev0 + 1
