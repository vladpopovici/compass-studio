"""Synthetic store is a valid COMPASS store and agrees with the Arrow files."""

import sqlite3

import numpy as np
import pyarrow as pa
import shapely

from compass_studio.annot.schema import SCHEMA_VERSION, TYPE_CODE
from compass_studio.synth.generate import Params, generate


def test_small_run(tmp_path):
    p = Params(width=20_000, height=10_000, points=3000, cells=1500, regions=5,
               region_vertices=(100, 300), region_radius_px=(200.0, 800.0), attrs=10)
    generate(tmp_path, "t", p, log=lambda m: None)
    c = sqlite3.connect(f"file:{tmp_path / 't.ann.sqlite'}?mode=ro", uri=True)
    meta = dict(c.execute("SELECT key, value FROM meta"))
    assert meta["schema_version"] == SCHEMA_VERSION
    assert int(meta["last_object_id"]) == 3000 + 1500 + 5 - 1
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert dict(c.execute("SELECT type_code, COUNT(*) FROM objects GROUP BY 1")) == {
        TYPE_CODE["POINT"]: 3000, TYPE_CODE["POLYGON"]: 1505}
    assert c.execute("SELECT COUNT(*) FROM objects_rtree").fetchone()[0] == 4505
    assert c.execute("SELECT COUNT(*) FROM object_groups").fetchone()[0] == 4505
    # groups of an object's layer only, ids global
    bad = c.execute("""SELECT COUNT(*) FROM object_groups og JOIN groups g USING(group_id)
                       JOIN objects o USING(object_id)
                       WHERE (o.object_id < 3000) != (g.layer_id = 1)""").fetchone()[0]
    assert bad == 0

    wkb = [r[0] for r in c.execute("SELECT wkb FROM objects ORDER BY object_id")]
    geoms = shapely.from_wkb(wkb)
    assert shapely.is_valid(geoms).all()
    b = np.array(c.execute("SELECT min_x, min_y, max_x, max_y FROM objects "
                           "ORDER BY object_id").fetchall())
    np.testing.assert_allclose(b, shapely.bounds(geoms))
    assert (b[:, 0] >= 0).all() and (b[:, 2] <= p.width + p.region_radius_px[1] * 2).all()

    ids, vals = c.execute("SELECT ids_u32, vals_f32 FROM object_scalar_sparse LIMIT 1").fetchone()
    ids = np.frombuffer(ids, np.uint32)
    assert len(ids) == len(np.frombuffer(vals, np.float32)) and (np.diff(ids) > 0).all()

    pts = pa.ipc.open_file(str(tmp_path / "t.points.arrow")).read_all()
    polys = pa.ipc.open_file(str(tmp_path / "t.polygons.arrow")).read_all()
    assert pts.num_rows == 3000 and polys.num_rows == 1505
    xy = np.column_stack([pts["x"].to_numpy(), pts["y"].to_numpy()])
    np.testing.assert_allclose(xy, shapely.get_coordinates(geoms[:3000]), atol=0.01)
    first = np.asarray(polys["coords"][0].as_py()).reshape(-1, 2)
    np.testing.assert_allclose(first, shapely.get_coordinates(geoms[3000])[:-1], atol=0.01)
    og = dict(c.execute("SELECT object_id, group_id FROM object_groups"))
    assert [og[i] for i in pts["object_id"].to_pylist()[:50]] == pts["group"].to_pylist()[:50]
