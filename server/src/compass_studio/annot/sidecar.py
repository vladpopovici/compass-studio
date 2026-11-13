"""Sidecar curation database (D8): editable layers next to a read-only store.

The sidecar uses the COMPASS annotation schema (so COMPASS tools can read it)
plus viewer-owned tables:

- ``cs_state``: a single row with the revision counter;
- ``cs_oplog``: one row per committed batch: revision, time, the operations
  and their inverses (JSON), so undo and redo are new batches;
- ``cs_forks``: provenance of forked layers (source store, layer, version).

Writes are batches of operations applied atomically against a
``base_revision``; a stale base raises :class:`Conflict` (HTTP 409 in the
API). Another process committing to the same file is detected cheaply with
``PRAGMA data_version``.

Object ids (provisional, open item in the decision log): forked objects keep
their source ids; new objects get ids above every id used so far.
"""

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import shapely
import shapely.geometry

from .schema import SCHEMA_VERSION, SQLITE_DDL, TYPE_CODE

SIDECAR_VERSION = "0"

VIEWER_DDL = """
CREATE TABLE IF NOT EXISTS cs_state (
  id       INTEGER PRIMARY KEY CHECK (id = 1),
  revision INTEGER NOT NULL
);
INSERT OR IGNORE INTO cs_state(id, revision) VALUES (1, 0);
CREATE TABLE IF NOT EXISTS cs_oplog (
  revision INTEGER PRIMARY KEY,
  ts       REAL NOT NULL,
  client   TEXT,
  ops      TEXT NOT NULL,
  inverse  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cs_forks (
  layer_id        INTEGER PRIMARY KEY REFERENCES layers(layer_id) ON DELETE CASCADE,
  source_store    TEXT NOT NULL,
  source_layer_id INTEGER NOT NULL,
  source_version  TEXT,
  created         REAL NOT NULL
);
"""

POLYGON = TYPE_CODE["POLYGON"]


class Conflict(Exception):
    """``base_revision`` is not the current revision."""

    def __init__(self, base: int, current: int):
        super().__init__(f"base revision {base}, current {current}")
        self.base, self.current = base, current


@dataclass
class Committed:
    revision: int
    object_ids: list[int]  # ids created by this batch, in op order


def _geom(op: dict) -> tuple[bytes, tuple[float, float, float, float]]:
    rings = op["coords"]  # [exterior, *holes], each [[x, y], ...], open or closed
    g = shapely.Polygon(rings[0], rings[1:])
    if not g.is_valid:
        raise ValueError(f"invalid polygon: {shapely.is_valid_reason(g)}")
    return shapely.to_wkb(g), tuple(shapely.bounds(g))


def _coords(wkb: bytes) -> list[list[list[float]]]:
    g = shapely.from_wkb(wkb)
    return [np.asarray(g.exterior.coords).tolist()] + [np.asarray(r.coords).tolist()
                                                       for r in g.interiors]


class Sidecar:
    def __init__(self, path: str | Path, synchronous: str = "NORMAL", busy_timeout_ms: int = 5000):
        self.path = Path(path)
        new = not self.path.exists()
        self._conn = sqlite3.connect(self.path.resolve().as_uri(), uri=True, isolation_level=None,
                                     check_same_thread=False)
        c = self._conn
        c.execute("PRAGMA journal_mode = WAL")
        c.execute(f"PRAGMA synchronous = {synchronous}")
        c.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
        c.execute("PRAGMA foreign_keys = ON")
        if new:
            c.executescript(SQLITE_DDL)
            c.executescript(VIEWER_DDL)
            for k, v in (("schema_version", SCHEMA_VERSION), ("cs_sidecar_version", SIDECAR_VERSION),
                         ("last_layer_id", "0"), ("last_group_id", "0"), ("last_object_id", "-1")):
                c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (k, v))
        v = c.execute("SELECT value FROM meta WHERE key = 'cs_sidecar_version'").fetchone()
        if v is None or v[0] != SIDECAR_VERSION:
            raise ValueError(f"not a version {SIDECAR_VERSION} sidecar: {path}")

    def close(self) -> None:
        self._conn.close()

    # -- state ---------------------------------------------------------------

    def revision(self) -> int:
        return self._conn.execute("SELECT revision FROM cs_state WHERE id = 1").fetchone()[0]

    def data_version(self) -> int:
        """Changes whenever *another* connection commits to the file."""
        return self._conn.execute("PRAGMA data_version").fetchone()[0]

    # -- forks ---------------------------------------------------------------

    def fork_layer(self, source: str | Path, layer_id: int, name: str | None = None) -> int:
        """Copy one layer (groups, objects, sparse scalars) from a read-only
        store into a new editable layer. Returns the new layer id."""
        source = Path(source)
        c = self._conn
        c.execute("ATTACH DATABASE ? AS src", (source.resolve().as_uri() + "?mode=ro",))
        try:
            c.execute("BEGIN IMMEDIATE")
            src_name, = c.execute("SELECT name FROM src.layers WHERE layer_id = ?", (layer_id,)).fetchone()
            src_version = c.execute("SELECT value FROM src.meta WHERE key = 'schema_version'").fetchone()
            new_lid = self._alloc("layers", "layer_id", "last_layer_id", 1)
            c.execute("INSERT INTO layers(layer_id, name) VALUES (?, ?)",
                      (new_lid, name or f"{src_name} (fork)"))
            groups = c.execute("SELECT group_id, name FROM src.groups WHERE layer_id = ? "
                               "ORDER BY group_id", (layer_id,)).fetchall()
            g0 = self._alloc("groups", "group_id", "last_group_id", len(groups))
            gmap = {old: g0 + i for i, (old, _) in enumerate(groups)}
            c.executemany("INSERT INTO groups(group_id, layer_id, name) VALUES (?, ?, ?)",
                          [(gmap[old], new_lid, n) for old, n in groups])
            c.execute("CREATE TEMP TABLE gmap(old INTEGER PRIMARY KEY, new INTEGER)")
            c.executemany("INSERT INTO gmap VALUES (?, ?)", gmap.items())
            c.execute("CREATE TEMP TABLE ids AS SELECT DISTINCT og.object_id AS id "
                      "FROM src.object_groups og JOIN gmap ON gmap.old = og.group_id")
            c.execute("INSERT INTO objects SELECT o.* FROM src.objects o JOIN ids ON ids.id = o.object_id")
            c.execute("INSERT INTO object_groups(object_id, group_id) SELECT og.object_id, gmap.new "
                      "FROM src.object_groups og JOIN gmap ON gmap.old = og.group_id")
            c.execute("INSERT OR IGNORE INTO scalar_attributes SELECT * FROM src.scalar_attributes")
            c.execute("INSERT INTO object_scalar_sparse SELECT s.* FROM src.object_scalar_sparse s "
                      "JOIN ids ON ids.id = s.object_id")
            mx = c.execute("SELECT MAX(id) FROM ids").fetchone()[0]
            if mx is not None:
                last = int(c.execute("SELECT value FROM meta WHERE key = 'last_object_id'").fetchone()[0])
                c.execute("UPDATE meta SET value = ? WHERE key = 'last_object_id'", (str(max(last, mx)),))
            c.execute("INSERT INTO cs_forks VALUES (?, ?, ?, ?, ?)",
                      (new_lid, str(source), layer_id, src_version[0] if src_version else None, time.time()))
            c.execute("DROP TABLE temp.gmap")
            c.execute("DROP TABLE temp.ids")
            c.execute("COMMIT")
        except BaseException:
            if c.in_transaction:
                c.execute("ROLLBACK")
            raise
        finally:
            c.execute("DETACH DATABASE src")
        return new_lid

    def create_layer(self, name: str, groups: list[str]) -> tuple[int, list[int]]:
        """New empty editable layer: (layer_id, group_ids)."""
        c = self._conn
        c.execute("BEGIN IMMEDIATE")
        try:
            lid = self._alloc("layers", "layer_id", "last_layer_id", 1)
            c.execute("INSERT INTO layers(layer_id, name) VALUES (?, ?)", (lid, name))
            g0 = self._alloc("groups", "group_id", "last_group_id", len(groups))
            c.executemany("INSERT INTO groups(group_id, layer_id, name) VALUES (?, ?, ?)",
                          [(g0 + i, lid, n) for i, n in enumerate(groups)])
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise
        return lid, [g0 + i for i in range(len(groups))]

    def _alloc(self, table: str, column: str, key: str, n: int) -> int:
        c = self._conn
        last = int(c.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()[0])
        mx = c.execute(f"SELECT MAX({column}) FROM {table}").fetchone()[0]
        start = max(last, -1 if mx is None else int(mx)) + 1
        c.execute("UPDATE meta SET value = ? WHERE key = ?", (str(start + n - 1), key))
        return start

    # -- edits ---------------------------------------------------------------

    def apply(self, base_revision: int, ops: list[dict[str, Any]], client: str | None = None) -> Committed:
        """Apply ``ops`` atomically if ``base_revision`` is current.

        Ops: ``create`` {layer_id?, group_id, coords}, ``update_geometry``
        {object_id, coords}, ``update_attributes`` {object_id, values:
        {name: float | None}}, ``delete`` {object_id}, ``move`` {object_id,
        group_id}. ``coords`` is [exterior, *holes] of [x, y] pairs.
        """
        c = self._conn
        c.execute("BEGIN IMMEDIATE")
        try:
            current = self.revision()
            if base_revision != current:
                raise Conflict(base_revision, current)
            inverse: list[dict] = []
            created: list[int] = []
            for op in ops:
                inverse.append(self._apply_one(op, created))
            rev = current + 1
            c.execute("UPDATE cs_state SET revision = ? WHERE id = 1", (rev,))
            c.execute("INSERT INTO cs_oplog(revision, ts, client, ops, inverse) VALUES (?, ?, ?, ?, ?)",
                      (rev, time.time(), client, json.dumps(ops), json.dumps(inverse[::-1])))
            c.execute("COMMIT")
            return Committed(rev, created)
        except BaseException:
            c.execute("ROLLBACK")
            raise

    def inverse_of(self, revision: int) -> list[dict]:
        """Operations that undo the batch committed at ``revision``."""
        row = self._conn.execute("SELECT inverse FROM cs_oplog WHERE revision = ?", (revision,)).fetchone()
        if row is None:
            raise KeyError(revision)
        return json.loads(row[0])

    def _editable_group(self, group_id: int) -> int:
        """Layer of ``group_id``. Every sidecar layer is editable (E3: read-only
        layers live in the COMPASS store and are forked into the sidecar)."""
        row = self._conn.execute("SELECT layer_id FROM groups WHERE group_id = ?", (group_id,)).fetchone()
        if row is None:
            raise ValueError(f"no group {group_id} in the sidecar")
        return row[0]

    def _object_group(self, object_id: int) -> int:
        row = self._conn.execute("SELECT MIN(group_id) FROM object_groups WHERE object_id = ?",
                                 (object_id,)).fetchone()
        if row is None or row[0] is None:
            raise KeyError(f"no object {object_id}")
        self._editable_group(row[0])
        return row[0]

    def _apply_one(self, op: dict, created: list[int]) -> dict:
        c = self._conn
        kind = op["op"]
        if kind == "create":
            self._editable_group(op["group_id"])
            wkb, b = _geom(op)
            oid = op.get("object_id")  # set when replaying an inverse of delete
            if oid is None:
                oid = self._alloc("objects", "object_id", "last_object_id", 1)
            c.execute("INSERT INTO objects(object_id, type_code, name, wkb, circle_r, min_x, min_y, "
                      "max_x, max_y) VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)",
                      (oid, POLYGON, op.get("name"), wkb, *b))
            c.execute("INSERT INTO object_groups(object_id, group_id) VALUES (?, ?)", (oid, op["group_id"]))
            if op.get("values"):
                self._set_values(oid, op["values"])
            created.append(oid)
            return {"op": "delete", "object_id": oid}
        if kind == "update_geometry":
            self._object_group(op["object_id"])
            old, = c.execute("SELECT wkb FROM objects WHERE object_id = ?", (op["object_id"],)).fetchone()
            wkb, b = _geom(op)
            c.execute("UPDATE objects SET wkb = ?, min_x = ?, min_y = ?, max_x = ?, max_y = ? "
                      "WHERE object_id = ?", (wkb, *b, op["object_id"]))
            return {"op": "update_geometry", "object_id": op["object_id"], "coords": _coords(old)}
        if kind == "update_attributes":
            self._object_group(op["object_id"])
            old = self._get_values(op["object_id"], list(op["values"]))
            self._set_values(op["object_id"], op["values"])
            return {"op": "update_attributes", "object_id": op["object_id"], "values": old}
        if kind == "move":
            old_gid = self._object_group(op["object_id"])
            if self._editable_group(op["group_id"]) != self._editable_group(old_gid):
                raise ValueError("move: groups are in different layers")
            c.execute("UPDATE object_groups SET group_id = ? WHERE object_id = ? AND group_id = ?",
                      (op["group_id"], op["object_id"], old_gid))
            return {"op": "move", "object_id": op["object_id"], "group_id": old_gid}
        if kind == "delete":
            gid = self._object_group(op["object_id"])
            name, wkb = c.execute("SELECT name, wkb FROM objects WHERE object_id = ?",
                                  (op["object_id"],)).fetchone()
            values = self._get_values(op["object_id"], None)
            c.execute("DELETE FROM objects WHERE object_id = ?", (op["object_id"],))
            return {"op": "create", "object_id": op["object_id"], "group_id": gid, "name": name,
                    "coords": _coords(wkb), "values": values}
        raise ValueError(f"unknown op {kind!r}")

    # -- reads ---------------------------------------------------------------

    def features(self, object_ids: list[int]) -> dict:
        """GeoJSON FeatureCollection of the given objects (check-out), with the
        current revision. Coordinates are level-0 pixels."""
        c = self._conn
        q = ",".join("?" * len(object_ids))
        feats = []
        for oid, wkb, gid, lid in c.execute(
                f"SELECT o.object_id, o.wkb, MIN(og.group_id), g.layer_id FROM objects o "
                f"JOIN object_groups og ON og.object_id = o.object_id "
                f"JOIN groups g ON g.group_id = og.group_id "
                f"WHERE o.object_id IN ({q}) GROUP BY o.object_id", object_ids):
            feats.append({"type": "Feature", "id": oid,
                          "geometry": shapely.geometry.mapping(shapely.from_wkb(wkb)),
                          "properties": {"object_id": oid, "group_id": gid, "layer_id": lid}})
        return {"type": "FeatureCollection", "revision": self.revision(), "features": feats}

    # -- sparse scalars ------------------------------------------------------

    def _get_values(self, oid: int, names: list[str] | None) -> dict[str, float | None]:
        c = self._conn
        row = c.execute("SELECT ids_u32, vals_f32 FROM object_scalar_sparse WHERE object_id = ?",
                        (oid,)).fetchone()
        have = {}
        if row:
            ids, vals = np.frombuffer(row[0], np.uint32), np.frombuffer(row[1], np.float32)
            id_name = dict(c.execute("SELECT attr_id, attr_name FROM scalar_attributes"))
            have = {id_name[int(i)]: float(v) for i, v in zip(ids, vals)}
        return have if names is None else {n: have.get(n) for n in names}

    def _set_values(self, oid: int, values: dict[str, float | None]) -> None:
        c = self._conn
        cur = self._get_values(oid, None)
        for k, v in values.items():
            if v is None:
                cur.pop(k, None)
            else:
                cur[k] = float(v)
        if not cur:
            c.execute("DELETE FROM object_scalar_sparse WHERE object_id = ?", (oid,))
            return
        c.executemany("INSERT OR IGNORE INTO scalar_attributes(attr_name) VALUES (?)", [(k,) for k in cur])
        name_id = dict(c.execute(
            f"SELECT attr_name, attr_id FROM scalar_attributes WHERE attr_name IN ({','.join('?' * len(cur))})",
            list(cur)))
        pairs = sorted((name_id[k], v) for k, v in cur.items())
        c.execute("INSERT OR REPLACE INTO object_scalar_sparse(object_id, ids_u32, vals_f32) VALUES (?, ?, ?)",
                  (oid, np.array([p[0] for p in pairs], np.uint32).tobytes(),
                   np.array([p[1] for p in pairs], np.float32).tobytes()))
