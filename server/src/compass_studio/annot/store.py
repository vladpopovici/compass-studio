"""Read-only access to a COMPASS annotation store (D7) for tile delivery (D6).

Opens the SQLite file read-only, refuses unknown schema versions, and answers
viewport queries: polygons of one layer intersecting a bbox, decoded from WKB,
triangulated, and encoded as one Arrow IPC record batch (the candidate
"polygon tile"). Points of a layer can be exported whole.

Object ids are delivered as uint32 (enough for the spike data; whether the
contract needs 64-bit ids is open). An object in several groups of its layer
is reported with its smallest group id.
"""

import sqlite3
import struct
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pyarrow as pa
import shapely

from .schema import SCHEMA_VERSION, TYPE_CODE
from .triangulate import triangulate

POLYGON = TYPE_CODE["POLYGON"]
POINT = TYPE_CODE["POINT"]


class SchemaError(ValueError):
    """The store's schema version is not one this server understands."""


@dataclass
class PolygonBatch:
    object_ids: np.ndarray      # (n,) uint32
    group_ids: np.ndarray       # (n,) uint32
    xy: np.ndarray              # (v, 2) float64, exterior then holes, rings not closed
    starts: np.ndarray          # (n + 1,) vertex offsets per polygon
    hole_starts: list[np.ndarray] | None  # per polygon, ring offsets within it (holes only)

    def __len__(self) -> int:
        return len(self.object_ids)


class AnnotationStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        uri = self.path.resolve().as_uri() + "?mode=ro"
        self._conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        self._conn.execute("PRAGMA query_only = ON")
        self._conn.execute("PRAGMA cache_size = -262144")  # 256 MB page cache
        self._conn.execute("PRAGMA mmap_size = 4294967296")
        meta = dict(self._conn.execute("SELECT key, value FROM meta"))
        if meta.get("schema_version") != SCHEMA_VERSION:
            raise SchemaError(f"schema_version {meta.get('schema_version')!r}, "
                              f"expected {SCHEMA_VERSION!r}")
        self.meta = meta
        self.layers = {lid: name for lid, name in self._conn.execute(
            "SELECT layer_id, name FROM layers ORDER BY layer_id")}
        self.groups: dict[int, list[int]] = {lid: [] for lid in self.layers}
        for gid, lid in self._conn.execute("SELECT group_id, layer_id FROM groups ORDER BY group_id"):
            self.groups[lid].append(gid)

    def close(self) -> None:
        self._conn.close()

    def _layer_groups(self, layer_id: int) -> str:
        gids = self.groups[layer_id]
        if not gids:
            raise KeyError(layer_id)
        return ",".join(str(int(g)) for g in gids)

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
        rows = self.query_rows(layer_id, bbox)
        return decode_polygons(rows, decoder)

    def points(self, layer_id: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Whole point layer: (object_ids uint32, group_ids uint32, xy float64)."""
        sql = (f"SELECT o.object_id, MIN(og.group_id), o.min_x, o.min_y FROM objects o "
               f"JOIN object_groups og ON og.object_id = o.object_id "
               f"WHERE o.type_code = ? AND og.group_id IN ({self._layer_groups(layer_id)}) "
               f"GROUP BY o.object_id")
        a = np.array(self._conn.execute(sql, (POINT,)).fetchall(), dtype=np.float64)
        if not len(a):
            return np.zeros(0, np.uint32), np.zeros(0, np.uint32), np.zeros((0, 2))
        return a[:, 0].astype(np.uint32), a[:, 1].astype(np.uint32), a[:, 2:4]


# ----------------------------------------------------------------------
# WKB decoding
# ----------------------------------------------------------------------

def decode_polygons(rows: list[tuple[int, int, bytes]], decoder: str = "shapely") -> PolygonBatch:
    n = len(rows)
    oids = np.fromiter((r[0] for r in rows), np.uint32, n)
    gids = np.fromiter((r[1] for r in rows), np.uint32, n)
    wkbs = [r[2] for r in rows]
    if decoder == "shapely":
        xy, starts, holes = _decode_shapely(wkbs)
    elif decoder == "direct":
        xy, starts, holes = _decode_direct(wkbs)
    else:
        raise ValueError(decoder)
    return PolygonBatch(oids, gids, xy, starts, holes)


def _decode_shapely(wkbs: list[bytes]):
    geoms = shapely.from_wkb(wkbs)
    if len(geoms) and not (shapely.get_type_id(geoms) == 3).all():
        raise ValueError("only single polygons are supported")
    n_holes = shapely.get_num_interior_rings(geoms)
    xy = shapely.get_coordinates(geoms)
    ncoords = shapely.get_num_coordinates(geoms)
    if not n_holes.any():
        # one closed exterior ring each: drop the closing vertex
        keep = np.ones(len(xy), bool)
        ends = np.cumsum(ncoords)
        keep[ends - 1] = False
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
            ring = np.frombuffer(w, "<f8", 2 * npts, off + 4).reshape(npts, 2)[:-1]
            parts.append(ring)
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
# Tile encoding
# ----------------------------------------------------------------------

POLYGON_TILE_SCHEMA_VERSION = "0"


def polygon_tile(batch: PolygonBatch,
                 triangles: tuple[np.ndarray, np.ndarray] | None = None) -> pa.RecordBatch:
    """Triangulated polygons as one Arrow record batch.

    Columns: object_id uint32, group_id uint32, coords list<float32>
    (x, y interleaved, rings not closed, holes after the exterior),
    triangles list<uint32> (indices local to each polygon). ``triangles``
    may be passed in precomputed (from :func:`triangulate`).
    """
    idx, toff = triangles or triangulate(batch.xy, batch.starts, batch.hole_starts)
    coords = pa.ListArray.from_arrays(
        pa.array((batch.starts * 2).astype(np.int32)),
        pa.array(batch.xy.astype(np.float32).ravel()))
    tris = pa.ListArray.from_arrays(pa.array(toff.astype(np.int32)), pa.array(idx))
    return pa.RecordBatch.from_arrays(
        [pa.array(batch.object_ids), pa.array(batch.group_ids), coords, tris],
        schema=pa.schema([("object_id", pa.uint32()), ("group_id", pa.uint32()),
                          ("coords", pa.list_(pa.float32())), ("triangles", pa.list_(pa.uint32()))],
                         metadata={b"schema": b"polygon-tile", b"version": POLYGON_TILE_SCHEMA_VERSION.encode()}))


def encode_ipc(batch: pa.RecordBatch) -> bytes:
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, batch.schema) as w:
        w.write_batch(batch)
    return sink.getvalue().to_pybytes()
