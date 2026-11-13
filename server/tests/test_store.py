"""Read-only annotation store: schema check, bbox queries, decoders, tiles."""

import sqlite3

import numpy as np
import pyarrow as pa
import pytest
import shapely

from compass_studio.annot.schema import SQLITE_DDL, TYPE_CODE
from compass_studio.annot.store import (AnnotationStore, SchemaError, decode_polygons, encode_ipc,
                                        polygon_tile)
from compass_studio.synth.generate import Params, generate


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    d = tmp_path_factory.mktemp("s")
    generate(d, "t", Params(width=20_000, height=10_000, points=2000, cells=3000, regions=4,
                            region_vertices=(100, 200), region_radius_px=(300.0, 900.0)),
             arrow=False, log=lambda m: None)
    s = AnnotationStore(d / "t.ann.sqlite")
    yield s
    s.close()


def test_layers_and_points(store):
    assert store.layers == {1: "points", 2: "cells", 3: "regions"}
    oid, gid, xy = store.points(1)
    assert len(oid) == 2000 and set(gid) <= set(store.groups[1])
    assert (xy >= 0).all() and (xy[:, 0] <= 20_000).all()


def test_bbox_query_matches_brute_force(store):
    bbox = (5000.0, 2000.0, 9000.0, 6000.0)
    got = {r[0] for r in store.query_rows(2, bbox)}
    c = store._conn
    expect = {oid for oid, x0, y0, x1, y1 in c.execute(
        "SELECT o.object_id, min_x, min_y, max_x, max_y FROM objects o "
        "JOIN object_groups og USING(object_id) JOIN groups g USING(group_id) "
        "WHERE g.layer_id = 2")
        if x1 >= bbox[0] and x0 <= bbox[2] and y1 >= bbox[1] and y0 <= bbox[3]}
    assert got == expect and got


def test_decoders_agree_and_tile_roundtrip(store):
    rows = store.query_rows(2, (0, 0, 20_000, 10_000))
    a, b = decode_polygons(rows, "direct"), decode_polygons(rows, "shapely")
    np.testing.assert_array_equal(a.starts, b.starts)
    np.testing.assert_array_equal(a.xy, b.xy)
    tile = pa.ipc.open_stream(encode_ipc(polygon_tile(a))).read_all()
    assert tile.num_rows == len(rows)
    ring = np.asarray(tile["coords"][7].as_py()).reshape(-1, 2)
    tri = np.asarray(tile["triangles"][7].as_py()).reshape(-1, 3)
    area = sum(shapely.Polygon(ring[t]).area for t in tri)
    assert area == pytest.approx(shapely.Polygon(ring).area, rel=1e-4)


def test_holes(tmp_path):
    p = tmp_path / "h.ann.sqlite"
    c = sqlite3.connect(p)
    c.executescript(SQLITE_DDL)
    c.execute("INSERT INTO meta VALUES ('schema_version', '1')")
    c.execute("INSERT INTO layers VALUES (1, 'l')")
    c.execute("INSERT INTO groups VALUES (1, 1, 'g')")
    poly = shapely.Polygon([(0, 0), (100, 0), (100, 100), (0, 100)],
                           [[(20, 20), (40, 20), (40, 40), (20, 40)]])
    c.execute("INSERT INTO objects VALUES (0, ?, NULL, ?, NULL, 0, 0, 100, 100)",
              (TYPE_CODE["POLYGON"], shapely.to_wkb(poly)))
    c.execute("INSERT INTO object_groups VALUES (0, 1)")
    c.commit()
    c.close()
    s = AnnotationStore(p)
    for dec in ("direct", "shapely"):
        b = s.polygons(1, (0, 0, 10, 10), decoder=dec)
        assert b.starts.tolist() == [0, 8] and b.hole_starts[0].tolist() == [4]
        t = pa.ipc.open_stream(encode_ipc(polygon_tile(b))).read_all()
        xy = np.asarray(t["coords"][0].as_py()).reshape(-1, 2)
        tri = np.asarray(t["triangles"][0].as_py()).reshape(-1, 3)
        assert sum(shapely.Polygon(xy[k]).area for k in tri) == pytest.approx(poly.area)


def test_refuses_unknown_schema(tmp_path):
    p = tmp_path / "x.ann.sqlite"
    c = sqlite3.connect(p)
    c.executescript(SQLITE_DDL)
    c.execute("INSERT INTO meta VALUES ('schema_version', '99')")
    c.commit()
    c.close()
    with pytest.raises(SchemaError):
        AnnotationStore(p)
