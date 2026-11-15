"""Synthetic annotations for the spikes (S3, S4, S6).

One seeded run produces the same objects twice:

- a COMPASS annotation store (``<name>.ann.sqlite``, schema version 1), for
  the server read and write paths;
- Arrow IPC files (``<name>.points.arrow``, ``<name>.polygons.arrow``), loaded
  statically by the renderer spike so it measures the GPU, not the server.

Objects are placed inside a procedural "tissue" mask (no slide data is used),
with spatially clustered classes. Three layers:

- ``points``: POINT objects (cell centroids), one group per class;
- ``cells``: small star-shaped POLYGONs, 8 to 32 vertices;
- ``regions``: large star-shaped POLYGONs, hundreds to thousands of vertices;
- ``lines`` (only with ``lines`` > 0): POLYLINEs, smooth random walks.

All polygons are star-shaped around their centre, hence valid and simple.
Cells may overlap each other; regions overlap cells.
"""

import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pyarrow as pa
import shapely

from compass_studio.annot.schema import SCHEMA_VERSION, SQLITE_DDL, TYPE_CODE
from compass_studio.annot.triangulate import triangulate

_MASK = 512  # resolution of the tissue and class fields


@dataclass
class Params:
    width: int = 200_000
    height: int = 200_000
    mpp: float = 0.5
    points: int = 5_000_000
    cells: int = 1_000_000
    regions: int = 200
    point_classes: int = 8
    cell_classes: int = 8
    region_classes: int = 4
    cell_radius_px: float = 10.0
    cell_vertices: tuple[int, int] = (8, 32)
    region_radius_px: tuple[float, float] = (500.0, 8000.0)
    region_vertices: tuple[int, int] = (500, 5000)
    lines: int = 0
    line_classes: int = 3
    line_vertices: tuple[int, int] = (20, 200)
    line_step_px: float = 40.0
    attrs: int = 40
    attrs_per_object: float = 4.0
    order: str = "spatial"  # object_id order within a layer: spatial | random
    seed: int = 0


# ---------------------------------------------------------------------------
# Fields and sampling
# ---------------------------------------------------------------------------

def _smooth_field(rng: np.random.Generator, n_blobs: int, scale: tuple[float, float]
                  ) -> np.ndarray:
    """Sum of random Gaussian blobs on a _MASK x _MASK grid, in [0, 1]."""
    yy, xx = np.mgrid[0:_MASK, 0:_MASK].astype(np.float32) / _MASK
    f = np.zeros((_MASK, _MASK), np.float32)
    for cx, cy, s, w in zip(rng.uniform(0.1, 0.9, n_blobs), rng.uniform(0.1, 0.9, n_blobs),
                            rng.uniform(*scale, n_blobs), rng.uniform(0.5, 1.0, n_blobs)):
        f += w * np.exp(-((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * s * s))
    return f / f.max()


def _sample_positions(rng, n: int, density: np.ndarray, p: Params) -> np.ndarray:
    """n (x, y) level-0 positions drawn from a density grid."""
    prob = density.ravel().astype(np.float64)
    cell = rng.choice(prob.size, size=n, p=prob / prob.sum())
    cy, cx = np.divmod(cell, _MASK)
    x = (cx + rng.random(n)) * (p.width / _MASK)
    y = (cy + rng.random(n)) * (p.height / _MASK)
    return np.column_stack([x, y])


def _classes(rng, xy: np.ndarray, k: int, p: Params) -> np.ndarray:
    """Spatially correlated class labels: argmax of k smooth fields plus noise."""
    fields = np.stack([_smooth_field(rng, 12, (0.03, 0.12)) for _ in range(k)])
    ix = np.minimum((xy[:, 0] * _MASK / p.width).astype(np.int64), _MASK - 1)
    iy = np.minimum((xy[:, 1] * _MASK / p.height).astype(np.int64), _MASK - 1)
    score = fields[:, iy, ix].T + rng.gumbel(scale=0.15, size=(len(xy), k)).astype(np.float32)
    return score.argmax(axis=1).astype(np.int64)


def _order(rng, xy: np.ndarray, how: str) -> np.ndarray:
    if how == "random":
        return rng.permutation(len(xy))
    if how != "spatial":
        raise ValueError(f"unknown order {how!r}")
    q = np.clip(xy / xy.max(initial=1.0) * 65535, 0, 65535).astype(np.uint64)
    return np.argsort(_morton(q[:, 0], q[:, 1]), kind="stable")


def _morton(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    def spread(v):
        v = (v | (v << np.uint64(8))) & np.uint64(0x00FF00FF)
        v = (v | (v << np.uint64(4))) & np.uint64(0x0F0F0F0F)
        v = (v | (v << np.uint64(2))) & np.uint64(0x33333333)
        v = (v | (v << np.uint64(1))) & np.uint64(0x55555555)
        return v
    return spread(x) | (spread(y) << np.uint64(1))


def _star_rings(rng, centres: np.ndarray, radii: np.ndarray, nverts: int,
                roughness: float, harmonics: int) -> np.ndarray:
    """(n, nverts, 2) star-shaped rings, counter-clockwise, not closed."""
    n = len(centres)
    theta = np.sort(rng.random((n, nverts)), axis=1) * 2 * np.pi
    theta += rng.random((n, 1)) * 2 * np.pi
    r = np.ones((n, nverts))
    for h in range(1, harmonics + 1):
        amp = roughness / h * rng.random((n, 1))
        r += amp * np.sin(h * theta + rng.random((n, 1)) * 2 * np.pi)
    r = np.clip(r, 0.3, None) * radii[:, None]
    ring = np.empty((n, nverts, 2))
    ring[..., 0] = centres[:, 0, None] + r * np.cos(theta)
    ring[..., 1] = centres[:, 1, None] + r * np.sin(theta)
    return ring


@dataclass
class Layer:
    name: str
    type_code: int
    classes: int
    group: np.ndarray             # class index per object
    xy: np.ndarray | None = None  # points
    rings: list[np.ndarray] | None = None  # polygons (not closed) or polylines, each (nv, 2)


def _points(rng, tissue, p: Params) -> Layer:
    xy = _sample_positions(rng, p.points, tissue, p)
    xy = xy[_order(rng, xy, p.order)]
    return Layer("points", TYPE_CODE["POINT"], p.point_classes,
                 _classes(rng, xy, p.point_classes, p), xy=xy)


def _cells(rng, tissue, p: Params) -> Layer:
    centres = _sample_positions(rng, p.cells, tissue, p)
    centres = centres[_order(rng, centres, p.order)]
    radii = p.cell_radius_px * rng.lognormal(0.0, 0.3, p.cells)
    nv = rng.integers(p.cell_vertices[0], p.cell_vertices[1] + 1, p.cells)
    rings: list[np.ndarray] = [None] * p.cells  # type: ignore[list-item]
    for k in np.unique(nv):
        idx = np.flatnonzero(nv == k)
        block = _star_rings(rng, centres[idx], radii[idx], int(k), 0.25, 3)
        for i, ring in zip(idx.tolist(), block):
            rings[i] = ring
    return Layer("cells", TYPE_CODE["POLYGON"], p.cell_classes,
                 _classes(rng, centres, p.cell_classes, p), rings=rings)


def _regions(rng, tissue, p: Params) -> Layer:
    centres = _sample_positions(rng, p.regions, tissue, p)
    centres = centres[_order(rng, centres, p.order)]
    lo, hi = np.log(p.region_radius_px)
    radii = np.exp(rng.uniform(lo, hi, p.regions))
    nv = rng.integers(p.region_vertices[0], p.region_vertices[1] + 1, p.regions)
    rings = [_star_rings(rng, centres[i:i + 1], radii[i:i + 1], int(nv[i]), 0.6, 12)[0]
             for i in range(p.regions)]
    return Layer("regions", TYPE_CODE["POLYGON"], p.region_classes,
                 _classes(rng, centres, p.region_classes, p), rings=rings)


def _lines(rng, tissue, p: Params) -> Layer:
    starts = _sample_positions(rng, p.lines, tissue, p)
    nv = rng.integers(p.line_vertices[0], p.line_vertices[1] + 1, p.lines)
    rings = []
    for i in range(p.lines):
        turn = np.cumsum(rng.normal(0.0, 0.25, int(nv[i])))
        turn += rng.random() * 2 * np.pi
        steps = p.line_step_px * np.column_stack([np.cos(turn), np.sin(turn)])
        xy = starts[i] + np.cumsum(steps, axis=0)
        rings.append(np.clip(xy, 0, [p.width - 1, p.height - 1]))
    return Layer("lines", TYPE_CODE["POLYLINE"], p.line_classes,
                 _classes(rng, starts, p.line_classes, p), rings=rings)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def _group_bases(layers: list[Layer]) -> list[int]:
    """First group_id of each layer; ids are global and start at 1."""
    return (1 + np.concatenate([[0], np.cumsum([l.classes for l in layers])[:-1]])).tolist()


def _wkb_and_bounds(layer: Layer) -> tuple[list[bytes], np.ndarray]:
    if layer.xy is not None:
        geoms = shapely.points(layer.xy)
        return list(shapely.to_wkb(geoms)), np.hstack([layer.xy, layer.xy])
    if layer.type_code == TYPE_CODE["POLYLINE"]:
        geoms = [shapely.LineString(r) for r in layer.rings]
        b = np.array([[r[:, 0].min(), r[:, 1].min(), r[:, 0].max(), r[:, 1].max()] for r in layer.rings])
        return list(shapely.to_wkb(geoms)), b.reshape(-1, 4)
    n = len(layer.rings)
    wkb: list[bytes] = [b""] * n
    bounds = np.empty((n, 4))
    nv = np.array([len(r) for r in layer.rings])
    for k in np.unique(nv):
        idx = np.flatnonzero(nv == k)
        block = np.stack([layer.rings[i] for i in idx])
        closed = np.concatenate([block, block[:, :1]], axis=1)
        for i, w in zip(idx.tolist(), shapely.to_wkb(shapely.polygons(closed))):
            wkb[i] = w
        bounds[idx] = np.column_stack([block[..., 0].min(1), block[..., 1].min(1),
                                       block[..., 0].max(1), block[..., 1].max(1)])
    return wkb, bounds


def _sparse_blobs(rng, n: int, p: Params) -> tuple[list[bytes], list[bytes], np.ndarray]:
    """Per-object sparse scalars: (ids_u32 blobs, vals_f32 blobs, row mask)."""
    if p.attrs == 0 or p.attrs_per_object <= 0:
        return [], [], np.zeros(n, bool)
    prob = min(1.0, p.attrs_per_object / p.attrs)
    present = rng.random((n, p.attrs)) < prob
    rows, cols = np.nonzero(present)  # row-major: ids sorted within each row
    vals = rng.lognormal(0.0, 1.0, rows.size).astype(np.float32)
    counts = present.sum(axis=1)
    split = np.cumsum(counts)[:-1]
    keep = counts > 0
    ids_b = [a.tobytes() for a, k in zip(np.split(cols.astype(np.uint32), split), keep) if k]
    val_b = [a.tobytes() for a, k in zip(np.split(vals, split), keep) if k]
    return ids_b, val_b, keep


def write_sqlite(path: Path, layers: list[Layer], rng, p: Params, batch: int = 200_000,
                 log=print) -> None:
    if path.exists():
        raise FileExistsError(path)
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(SQLITE_DDL)
        conn.execute("PRAGMA journal_mode = OFF")
        conn.execute("PRAGMA synchronous = OFF")
        conn.execute("PRAGMA cache_size = -1048576")
        conn.executemany("INSERT INTO scalar_attributes(attr_id, attr_name) VALUES (?, ?)",
                         [(i, f"marker_{i:03d}") for i in range(p.attrs)])
        next_oid = 0
        for lid, (layer, base) in enumerate(zip(layers, _group_bases(layers)), start=1):
            conn.execute("INSERT INTO layers(layer_id, name) VALUES (?, ?)", (lid, layer.name))
            gids = np.arange(base, base + layer.classes)
            conn.executemany("INSERT INTO groups(group_id, layer_id, name) VALUES (?, ?, ?)",
                             [(int(g), lid, f"class_{c:02d}") for c, g in enumerate(gids)])
            n = len(layer.group)
            for s in range(0, n, batch):
                e = min(n, s + batch)
                sub = Layer(layer.name, layer.type_code, layer.classes, layer.group[s:e],
                            None if layer.xy is None else layer.xy[s:e],
                            None if layer.rings is None else layer.rings[s:e])
                wkb, b = _wkb_and_bounds(sub)
                oids = np.arange(next_oid + s, next_oid + e)
                conn.executemany(
                    "INSERT INTO objects(object_id, type_code, name, wkb, circle_r, "
                    "min_x, min_y, max_x, max_y) VALUES (?, ?, NULL, ?, NULL, ?, ?, ?, ?)",
                    zip(oids.tolist(), [layer.type_code] * (e - s), wkb, *b.T.tolist()))
                conn.executemany("INSERT INTO object_groups(object_id, group_id) VALUES (?, ?)",
                                 zip(oids.tolist(), gids[layer.group[s:e]].tolist()))
                if layer.name != "regions":
                    ids_b, val_b, keep = _sparse_blobs(rng, e - s, p)
                    conn.executemany(
                        "INSERT INTO object_scalar_sparse(object_id, ids_u32, vals_f32) "
                        "VALUES (?, ?, ?)", zip(oids[keep].tolist(), ids_b, val_b))
                conn.commit()
                log(f"  sqlite {layer.name}: {e}/{n}")
            next_oid += n
        meta = {
            "schema_version": SCHEMA_VERSION,
            "last_layer_id": len(layers), "last_group_id": sum(l.classes for l in layers),
            "last_object_id": next_oid - 1,
            "name": path.name.removesuffix(".ann.sqlite"),
            "image_width": p.width, "image_height": p.height, "mpp": p.mpp,
            "annotation_set": "synthetic", "synthetic": 1,
            "generator": "compass_studio.synth", "generator_params": json.dumps(asdict(p)),
        }
        conn.executemany("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                         [(k, str(v)) for k, v in meta.items()])
        conn.commit()
        conn.execute("PRAGMA journal_mode = WAL")  # as COMPASS stores are
        conn.execute("ANALYZE")
    finally:
        conn.close()


def _arrow_meta(layers: list[Layer], p: Params) -> dict[bytes, bytes]:
    info = {"params": asdict(p), "layers": []}
    for lid, (l, base) in enumerate(zip(layers, _group_bases(layers)), start=1):
        info["layers"].append({"layer_id": lid, "name": l.name, "type_code": l.type_code,
                               "groups": {base + c: f"class_{c:02d}" for c in range(l.classes)}})
    return {b"compass_studio.synth": json.dumps(info).encode()}


def write_arrow(stem: Path, layers: list[Layer], p: Params) -> None:
    """``<stem>.points.arrow`` and ``<stem>.polygons.arrow`` (Arrow IPC files).

    Coordinates are float32 level-0 pixels. Polygons are one exterior ring
    each, interleaved x, y, not closed (``coords`` list offsets / 2 are the
    vertex start indices). ``group`` is the store's ``group_id``.
    ``triangles`` holds earcut indices local to each polygon (add the
    polygon's vertex start for global indices), standing in for the
    server-side triangulation of D6. Each file is a single record batch.
    """
    meta = _arrow_meta(layers, p)
    oid = 0
    point_tables, poly_tables = [], []
    for lid, (l, base) in enumerate(zip(layers, _group_bases(layers)), start=1):
        n = len(l.group)
        common = {"object_id": pa.array(np.arange(oid, oid + n, dtype=np.uint32)),
                  "layer": pa.array(np.full(n, lid, np.uint8)),
                  "group": pa.array((l.group + base).astype(np.uint16))}
        if l.type_code == TYPE_CODE["POLYLINE"]:
            pass  # the spike files hold points and polygons only
        elif l.xy is not None:
            point_tables.append(pa.table({**common,
                                          "x": pa.array(l.xy[:, 0].astype(np.float32)),
                                          "y": pa.array(l.xy[:, 1].astype(np.float32))}))
        else:
            lens = np.array([len(r) for r in l.rings], np.int64)
            offsets = np.concatenate([[0], np.cumsum(lens * 2)]).astype(np.int32)
            flat = np.concatenate(l.rings).astype(np.float32).ravel()
            coords = pa.ListArray.from_arrays(pa.array(offsets), pa.array(flat))
            starts = np.concatenate([[0], np.cumsum(lens)])
            idx, toff = triangulate(flat.reshape(-1, 2), starts)
            tris = pa.ListArray.from_arrays(pa.array(toff.astype(np.int32)), pa.array(idx))
            poly_tables.append(pa.table({**common, "coords": coords, "triangles": tris}))
        oid += n
    for suffix, tables in (("points", point_tables), ("polygons", poly_tables)):
        if not tables:
            continue
        t = pa.concat_tables(tables).combine_chunks().replace_schema_metadata(meta)
        with pa.OSFile(str(stem) + f".{suffix}.arrow", "wb") as f, \
                pa.ipc.new_file(f, t.schema) as w:
            w.write_table(t)


def generate(out_dir: Path, name: str, p: Params, sqlite: bool = True, arrow: bool = True,
             log=print) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(p.seed)
    tissue = _smooth_field(rng, 30, (0.04, 0.15))
    tissue = np.where(tissue > 0.35, tissue, 0.0)
    layers = []
    for make, count in ((_points, p.points), (_cells, p.cells), (_regions, p.regions), (_lines, p.lines)):
        if count:
            layers.append(make(rng, tissue, p))
            log(f"generated {layers[-1].name}: {count}")
    if arrow:
        write_arrow(out_dir / name, layers, p)
        log("wrote arrow")
    if sqlite:
        # separate stream so the SQLite attributes do not depend on --no-arrow
        write_sqlite(out_dir / f"{name}.ann.sqlite", layers,
                     np.random.default_rng([p.seed, 1]), p, log=log)
        log("wrote sqlite")
