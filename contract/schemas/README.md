# Binary schemas (contract v1)

Annotation payloads are Arrow IPC **streams** (`application/vnd.apache.arrow.stream`)
holding exactly one record batch. Each schema is defined by a JSON file here
(field names and Arrow types, plus required schema-metadata keys); the
conformance tests check the server against these files.

| Schema | Version | Endpoint |
|---|---|---|
| `point-layer` | 1 | `GET /layers/{id}/points` |
| `polygon-tile` | 1 | `GET /layers/{id}/tiles/{level}/{col}/{row}` up to `polygon_max_level` |
| `centroid-tile` | 1 | the same endpoint above `polygon_max_level` |
| `line-layer` | 1 | `GET /layers/{id}/lines` (since API 1.4.0) |

Rules:

- The schema metadata keys `schema` and `version` name the payload; clients
  must check them. `/info` lists the versions a server can produce.
- Coordinates are float32 level-0 pixels. S2 measured a static error of at
  most 1/128 px x 2^zoom at 131k to 262k px, acceptable for display; exact
  coordinates come from `/features` (GeoJSON, float64).
- `object_id` is int64 with values below 2^53. `group` is the group's index
  in `Layer.groups`, which clients can use directly as a GPU filter category
  (at most 128 groups per layer for DataFilterExtension, S3).
- Lists use Arrow 32-bit offsets. A polygon's vertices are
  `coords[2i], coords[2i+1]`; its triangle indices point into those vertices,
  so global indices are `triangles[j] + vertex_start`.
- Image tiles are plain image files (JPEG, lossless WebP, PNG), 512 px,
  cropped at the right and bottom edges.

Versioning: a new field, a new metadata key or a wider type is a new version
of the schema (`polygon-tile` 2); servers may support several and announce
them in `/info`. Nothing in a released version changes.
