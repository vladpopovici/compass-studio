"""Read access to an annotation store (D7) and the v1 binary payloads (D6).

Works on COMPASS stores (opened read-only, schema version checked) and on
sidecars (same schema, opened through a second read-only connection).
Payloads follow ``contract/schemas``: point-layer v1, polygon-tile v1,
centroid-tile v1, each one Arrow IPC stream with a single record batch.

Object ids are int64 and must stay below 2**53 (JSON-safe, contract v1). An
object in several groups of its layer is reported with its smallest group
id. ``group`` columns carry the group's index within its layer (0..k-1, in
group-id order), as listed by the layers endpoint.
"""

import sqlite3
import struct
import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pyarrow as pa
import shapely
import shapely.geometry

from .schema import SCHEMA_VERSION, TYPE_CODE
from .triangulate import triangulate

POLYGON = TYPE_CODE["POLYGON"]
POINT = TYPE_CODE["POINT"]
CODE_TYPE = {v: k.lower() for k, v in TYPE_CODE.items()}
ID_LIMIT = 2**53

POINT_LAYER_VERSION = "1"
POLYGON_TILE_VERSION = "1"
CENTROID_TILE_VERSION = "1"


class SchemaError(ValueError):
    """The store's schema version is not one this server understands."""


@dataclass
class Group:
    id: int
    name: str
    index: int


@dataclass
class PolygonBatch:
    object_ids: np.ndarray      # (n,) int64
    group_ids: np.ndarray       # (n,) int64, store group ids
    xy: np.ndarray              # (v, 2) float64, exterior then holes, rings not closed
    starts: np.ndarray          # (n + 1,) vertex offsets per polygon
    hole_starts: list[np.ndarray] | None  # per polygon, ring offsets within it (holes only)

    def __len__(self) -> int:
        return len(self.object_ids)


class AnnotationStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._uri = self.path.resolve().as_uri() + "?mode=ro"
        self._local = threading.local()  # one read-only connection per thread
        self._all: list[sqlite3.Connection] = []
        self._lock = threading.Lock()
        self._loaded_revision = -1
        self.reload()

    @property
    def _conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self._uri, uri=True, check_same_thread=False)
            c.execute("PRAGMA query_only = ON")
            c.execute("PRAGMA cache_size = -262144")  # 256 MB page cache
            c.execute("PRAGMA mmap_size = 4294967296")
            self._local.conn = c
            with self._lock:
                self._all.append(c)
        return c

    def revision(self) -> int:
        """Sidecar revision (``cs_state``); 0 for a plain COMPASS store."""
        try:
            return self._conn.execute("SELECT revision FROM cs_state WHERE id = 1").fetchone()[0]
        except sqlite3.OperationalError:
            return 0

    def sync(self) -> int:
        """Reload layers and groups if the revision moved; returns the revision."""
        rev = self.revision()
        if rev != self._loaded_revision:
            with self._lock:
                if rev != self._loaded_revision:
                    self.reload()
        return rev

    def reload(self) -> None:
        """(Re)read metadata, layers and groups (after a sidecar commit)."""
        meta = dict(self._conn.execute("SELECT key, value FROM meta"))
        if meta.get("schema_version") != SCHEMA_VERSION:
            raise SchemaError(f"schema_version {meta.get('schema_version')!r}, "
                              f"expected {SCHEMA_VERSION!r}")
        self.meta = meta
        self.layers = {lid: name for lid, name in self._conn.execute(
            "SELECT layer_id, name FROM layers ORDER BY layer_id")}
        self.groups: dict[int, list[Group]] = {lid: [] for lid in self.layers}
        for gid, lid, name in self._conn.execute(
                "SELECT group_id, layer_id, name FROM groups ORDER BY group_id"):
            self.groups[lid].append(Group(gid, name, len(self.groups[lid])))
        self._counts: dict[int, dict[str, int]] = {}
        self._loaded_revision = self.revision()

    def close(self) -> None:
        with self._lock:
            for c in self._all:
                c.close()
            self._all.clear()
        self._local = threading.local()

    def _layer_groups(self, layer_id: int) -> str:
        gs = self.groups.get(layer_id)
        if not gs:
            raise KeyError(layer_id)
        return ",".join(str(int(g.id)) for g in gs)

    def group_index(self, layer_id: int, group_ids: np.ndarray) -> np.ndarray:
        """Store group ids -> index within the layer (uint16)."""
        ids = np.array([g.id for g in self.groups[layer_id]], np.int64)
        return np.searchsorted(ids, group_ids).astype(np.uint16)

    def counts(self, layer_id: int) -> dict[str, int]:
        """Objects per geometry type in a layer (cached until :meth:`reload`)."""
        if layer_id not in self._counts:
            rows = self._conn.execute(
                f"SELECT o.type_code, COUNT(DISTINCT o.object_id) FROM object_groups og "
                f"JOIN objects o ON o.object_id = og.object_id "
                f"WHERE og.group_id IN ({self._layer_groups(layer_id)}) GROUP BY o.type_code")
            self._counts[layer_id] = {CODE_TYPE.get(t, str(t)): n for t, n in rows}
        return self._counts[layer_id]

    def attribute_names(self) -> list[str]:
        return [n for (n,) in self._conn.execute("SELECT attr_name FROM scalar_attributes ORDER BY attr_id")]

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def query_rows(self, layer_id: int, bbox: tuple[float, float, float, float],
                   type_code: int = POLYGON) -> list[tuple[int, int, bytes]]:
        """(object_id, group_id, wkb) of one layer's objects whose bbox meets ``bbox``."""
        x0, y0, x1, y1 = bbox
        sql = (f"SELECT o.object_id, MIN(og.group_id), o.wkb "
               f"FROM objects_rtree r "
               f"JOIN objects o ON o.object_id = r.object_id "
               f"JOIN object_groups og ON og.object_id = o.object_id "
               f"WHERE r.max_x >= ? AND r.min_x <= ? AND r.max_y >= ? AND r.min_y <= ? "
               f"AND o.type_code = ? AND og.group_id IN ({self._layer_groups(layer_id)}) "
               f"GROUP BY o.object_id")
        return self._conn.execute(sql, (x0, x1, y0, y1, type_code)).fetchall()

    def polygons(self, layer_id: int, bbox: tuple[float, float, float, float],
                 decoder: str = "shapely") -> PolygonBatch:
        return decode_polygons(self.query_rows(layer_id, bbox), decoder)

    def centroids(self, layer_id: int, bbox: tuple[float, float, float, float]):
        """Polygons whose bbox centre lies in ``bbox`` (half-open): (ids, group ids, xy).
        Centres of bounding boxes, no WKB decoding (level-of-detail stand-in)."""
        x0, y0, x1, y1 = bbox
        sql = (f"SELECT o.object_id, MIN(og.group_id), (r.min_x + r.max_x) / 2, (r.min_y + r.max_y) / 2 "
               f"FROM objects_rtree r JOIN objects o ON o.object_id = r.object_id "
               f"JOIN object_groups og ON og.object_id = o.object_id "
               f"WHERE r.max_x >= ? AND r.min_x <= ? AND r.max_y >= ? AND r.min_y <= ? "
               f"AND o.type_code = ? AND og.group_id IN ({self._layer_groups(layer_id)}) "
               f"GROUP BY o.object_id")
        rows = self._conn.execute(sql, (x0, x1, y0, y1, POLYGON)).fetchall()
        ids, gids, xy = _columns(rows)
        keep = (xy[:, 0] >= x0) & (xy[:, 0] < x1) & (xy[:, 1] >= y0) & (xy[:, 1] < y1)
        return ids[keep], gids[keep], xy[keep]

    def points(self, layer_id: int):
        """Whole point layer: (ids int64, group ids int64, xy float64). A point's
        bbox is the point, so no WKB is decoded."""
        sql = (f"SELECT o.object_id, MIN(og.group_id), o.min_x, o.min_y FROM objects o "
               f"JOIN object_groups og ON og.object_id = o.object_id "
               f"WHERE o.type_code = ? AND og.group_id IN ({self._layer_groups(layer_id)}) "
               f"GROUP BY o.object_id")
        return _columns(self._conn.execute(sql, (POINT,)).fetchall())

    def features(self, layer_id: int, object_ids: list[int]) -> list[dict]:
        """GeoJSON features of the given objects of one layer."""
        q = ",".join("?" * len(object_ids))
        index = {g.id: g.index for g in self.groups[layer_id]}
        out = []
        for oid, code, name, wkb, r, gid in self._conn.execute(
                f"SELECT o.object_id, o.type_code, o.name, o.wkb, o.circle_r, MIN(og.group_id) "
                f"FROM objects o JOIN object_groups og ON og.object_id = o.object_id "
                f"WHERE o.object_id IN ({q}) AND og.group_id IN ({self._layer_groups(layer_id)}) "
                f"GROUP BY o.object_id", object_ids):
            props = {"object_id": oid, "type": CODE_TYPE.get(code, str(code)), "name": name,
                     "group_id": gid, "group": index[gid]}
            if r is not None:
                props["radius"] = r
            out.append({"type": "Feature", "id": oid,
                        "geometry": shapely.geometry.mapping(shapely.from_wkb(wkb)), "properties": props})
        return out

    def objects(self, layer_id: int, object_ids: list[int]) -> list[dict]:
        """Attributes (sparse scalars) of the given objects of one layer."""
        q = ",".join("?" * len(object_ids))
        names = dict(self._conn.execute("SELECT attr_id, attr_name FROM scalar_attributes"))
        index = {g.id: g.index for g in self.groups[layer_id]}
        out = []
        for oid, code, name, gid, ids, vals in self._conn.execute(
                f"SELECT o.object_id, o.type_code, o.name, MIN(og.group_id), s.ids_u32, s.vals_f32 "
                f"FROM objects o JOIN object_groups og ON og.object_id = o.object_id "
                f"LEFT JOIN object_scalar_sparse s ON s.object_id = o.object_id "
                f"WHERE o.object_id IN ({q}) AND og.group_id IN ({self._layer_groups(layer_id)}) "
                f"GROUP BY o.object_id", object_ids):
            values = {}
            if ids is not None:
                values = {names[int(i)]: float(v) for i, v in
                          zip(np.frombuffer(ids, np.uint32), np.frombuffer(vals, np.float32))}
            out.append({"object_id": oid, "type": CODE_TYPE.get(code, str(code)), "name": name,
                        "group_id": gid, "group": index[gid], "values": values})
        return out


def _columns(rows) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    n = len(rows)
    ids = np.fromiter((r[0] for r in rows), np.int64, n)
    gids = np.fromiter((r[1] for r in rows), np.int64, n)
    xy = np.fromiter((v for r in rows for v in (r[2], r[3])), np.float64, 2 * n).reshape(n, 2)
    return ids, gids, xy


# ----------------------------------------------------------------------
# WKB decoding
# ----------------------------------------------------------------------

def decode_polygons(rows: list[tuple[int, int, bytes]], decoder: str = "shapely") -> PolygonBatch:
    n = len(rows)
    oids = np.fromiter((r[0] for r in rows), np.int64, n)
    gids = np.fromiter((r[1] for r in rows), np.int64, n)
    wkbs = [r[2] for r in rows]
    if decoder == "shapely":
        xy, starts, holes = _decode_shapely(wkbs)
    elif decoder == "direct":
        xy, starts, holes = _decode_direct(wkbs)
    else:
        raise ValueError(decoder)
    return PolygonBatch(oids, gids, xy, starts, holes)


def _decode_shapely(wkbs: list[bytes]):
    if not wkbs:
        return np.zeros((0, 2)), np.zeros(1, np.int64), None
    geoms = shapely.from_wkb(wkbs)
    if not (shapely.get_type_id(geoms) == 3).all():
        raise ValueError("only single polygons are supported")
    n_holes = shapely.get_num_interior_rings(geoms)
    xy = shapely.get_coordinates(geoms)
    ncoords = shapely.get_num_coordinates(geoms)
    if not n_holes.any():
        # one closed exterior ring each: drop the closing vertex
        keep = np.ones(len(xy), bool)
        keep[np.cumsum(ncoords) - 1] = False
        starts = np.concatenate([[0], np.cumsum(ncoords - 1)])
        return xy[keep], starts, None
    return _decode_direct([shapely.to_wkb(g, byte_order=1) for g in geoms])


_HDR = struct.Struct("<BII")


def _decode_direct(wkbs: list[bytes]):
    """Little-endian WKB Polygon (type 3, 2D) without GEOS; closing vertices dropped."""
    parts, starts, holes, v, any_holes = [], [0], [], 0, False
    for w in wkbs:
        order, gtype, nrings = _HDR.unpack_from(w, 0)
        if order != 1 or gtype != 3:
            raise ValueError(f"unsupported WKB (byte order {order}, type {gtype})")
        off, ring_starts, nv = 9, [], 0
        for _ in range(nrings):
            npts = int.from_bytes(w[off:off + 4], "little")
            parts.append(np.frombuffer(w, "<f8", 2 * npts, off + 4).reshape(npts, 2)[:-1])
            ring_starts.append(nv)
            nv += npts - 1
            off += 4 + 16 * npts
        v += nv
        starts.append(v)
        holes.append(np.asarray(ring_starts[1:], np.uint32))
        any_holes |= nrings > 1
    xy = np.concatenate(parts) if parts else np.zeros((0, 2))
    return xy, np.asarray(starts, np.int64), (holes if any_holes else None)


# ----------------------------------------------------------------------
# v1 payloads (contract/schemas)
# ----------------------------------------------------------------------

def _meta(schema: str, version: str, extra: dict[str, str]) -> dict[bytes, bytes]:
    return {b"schema": schema.encode(), b"version": version.encode(),
            **{k.encode(): str(v).encode() for k, v in extra.items()}}


def point_layer(ids: np.ndarray, groups: np.ndarray, xy: np.ndarray, **meta) -> pa.RecordBatch:
    """point-layer v1: object_id int64, x float32, y float32, group uint16."""
    return pa.RecordBatch.from_arrays(
        [pa.array(ids, pa.int64()), pa.array(xy[:, 0].astype(np.float32)),
         pa.array(xy[:, 1].astype(np.float32)), pa.array(groups, pa.uint16())],
        schema=pa.schema([("object_id", pa.int64()), ("x", pa.float32()), ("y", pa.float32()),
                          ("group", pa.uint16())], metadata=_meta("point-layer", POINT_LAYER_VERSION, meta)))


def polygon_tile(batch: PolygonBatch, groups: np.ndarray,
                 triangles: tuple[np.ndarray, np.ndarray] | None = None, **meta) -> pa.RecordBatch:
    """polygon-tile v1: object_id int64, group uint16, coords list<float32>
    (x, y interleaved; exterior then holes; rings not closed), holes
    list<uint32> (vertex index where each hole starts, local), triangles
    list<uint32> (earcut indices, local). ``triangles`` may be precomputed."""
    idx, toff = triangles or triangulate(batch.xy, batch.starts, batch.hole_starts)
    coords = pa.ListArray.from_arrays(pa.array((batch.starts * 2).astype(np.int32)),
                                      pa.array(batch.xy.astype(np.float32).ravel()))
    hs = batch.hole_starts or [np.zeros(0, np.uint32)] * len(batch)
    hoff = np.concatenate([[0], np.cumsum([len(h) for h in hs])]).astype(np.int32)
    holes = pa.ListArray.from_arrays(pa.array(hoff), pa.array(
        np.concatenate(hs).astype(np.uint32) if len(hs) else np.zeros(0, np.uint32)))
    tris = pa.ListArray.from_arrays(pa.array(toff.astype(np.int32)), pa.array(idx, pa.uint32()))
    return pa.RecordBatch.from_arrays(
        [pa.array(batch.object_ids, pa.int64()), pa.array(groups, pa.uint16()), coords, holes, tris],
        schema=pa.schema([("object_id", pa.int64()), ("group", pa.uint16()),
                          ("coords", pa.list_(pa.float32())), ("holes", pa.list_(pa.uint32())),
                          ("triangles", pa.list_(pa.uint32()))],
                         metadata=_meta("polygon-tile", POLYGON_TILE_VERSION, meta)))


def centroid_tile(ids: np.ndarray, groups: np.ndarray, xy: np.ndarray, **meta) -> pa.RecordBatch:
    """centroid-tile v1: object_id int64, x float32, y float32, group uint16."""
    return pa.RecordBatch.from_arrays(
        [pa.array(ids, pa.int64()), pa.array(xy[:, 0].astype(np.float32)),
         pa.array(xy[:, 1].astype(np.float32)), pa.array(groups, pa.uint16())],
        schema=pa.schema([("object_id", pa.int64()), ("x", pa.float32()), ("y", pa.float32()),
                          ("group", pa.uint16())], metadata=_meta("centroid-tile", CENTROID_TILE_VERSION, meta)))


def encode_ipc(batch: pa.RecordBatch) -> bytes:
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, batch.schema) as w:
        w.write_batch(batch)
    return sink.getvalue().to_pybytes()
