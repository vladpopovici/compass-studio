"""COMPASS annotation-store schema (D7), vendored.

Copied from COMPASS ``compass/core/annot/schema.py`` (schema version
1). ``tests/test_schema.py`` checks it against a COMPASS
checkout when one is available (``COMPASS_REPO``, default ``../../COMPASS``
relative to this package's ``server/`` directory).
"""

SCHEMA_VERSION = '1'

# Geometry type codes (``objects.type_code``). CIRCLE rows store the centre
# POINT as WKB and the radius in ``circle_r``; their bbox includes the radius.
TYPE_CODE: dict[str, int] = {'POINT': 1, 'POINTSET': 2, 'POLYLINE': 3, 'POLYGON': 4, 'CIRCLE': 5}

SQLITE_DDL = '''
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS layers (
  layer_id INTEGER PRIMARY KEY,
  name     TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS groups (
  group_id INTEGER PRIMARY KEY,
  layer_id INTEGER NOT NULL REFERENCES layers(layer_id) ON DELETE CASCADE,
  name     TEXT NOT NULL,
  UNIQUE(layer_id, name)
);
CREATE INDEX IF NOT EXISTS idx_groups_layer ON groups(layer_id);

CREATE TABLE IF NOT EXISTS objects (
  object_id  INTEGER PRIMARY KEY,
  type_code  INTEGER NOT NULL,
  name       TEXT,
  wkb        BLOB    NOT NULL,
  circle_r   REAL,
  min_x      REAL    NOT NULL,
  min_y      REAL    NOT NULL,
  max_x      REAL    NOT NULL,
  max_y      REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_objects_type ON objects(type_code);

CREATE TABLE IF NOT EXISTS object_groups (
  object_id INTEGER NOT NULL REFERENCES objects(object_id) ON DELETE CASCADE,
  group_id  INTEGER NOT NULL REFERENCES groups(group_id)  ON DELETE CASCADE,
  PRIMARY KEY (object_id, group_id)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_object_groups_group  ON object_groups(group_id);
CREATE INDEX IF NOT EXISTS idx_object_groups_object ON object_groups(object_id);

CREATE TRIGGER IF NOT EXISTS trg_object_groups_one_layer
BEFORE INSERT ON object_groups
BEGIN
  SELECT CASE
    WHEN EXISTS (
      SELECT 1
      FROM object_groups og
      JOIN groups g_existing ON g_existing.group_id = og.group_id
      JOIN groups g_new      ON g_new.group_id      = NEW.group_id
      WHERE og.object_id = NEW.object_id
        AND g_existing.layer_id != g_new.layer_id
    )
    THEN RAISE(ABORT, 'Object cannot belong to groups from multiple layers')
  END;
END;

CREATE VIRTUAL TABLE IF NOT EXISTS objects_rtree USING rtree(
  object_id,
  min_x, max_x,
  min_y, max_y
);

CREATE TRIGGER IF NOT EXISTS trg_objects_insert AFTER INSERT ON objects
BEGIN
  INSERT INTO objects_rtree(object_id, min_x, max_x, min_y, max_y)
  VALUES (NEW.object_id, NEW.min_x, NEW.max_x, NEW.min_y, NEW.max_y);
END;

CREATE TRIGGER IF NOT EXISTS trg_objects_delete AFTER DELETE ON objects
BEGIN
  DELETE FROM objects_rtree WHERE object_id = OLD.object_id;
END;

CREATE TRIGGER IF NOT EXISTS trg_objects_bbox_update
AFTER UPDATE OF min_x, max_x, min_y, max_y ON objects
BEGIN
  UPDATE objects_rtree
  SET min_x = NEW.min_x, max_x = NEW.max_x, min_y = NEW.min_y, max_y = NEW.max_y
  WHERE object_id = NEW.object_id;
END;

CREATE TABLE IF NOT EXISTS scalar_attributes (
  attr_id   INTEGER PRIMARY KEY,
  attr_name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS object_scalar_sparse (
  object_id INTEGER PRIMARY KEY REFERENCES objects(object_id) ON DELETE CASCADE,
  ids_u32   BLOB NOT NULL,
  vals_f32  BLOB NOT NULL
);
'''
