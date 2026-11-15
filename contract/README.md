# Contract v1

The HTTP contract between the CompassStudio server and client (decision D2).
**Frozen 2026-11-13** after spikes S1 to S6.
Current: 1.4.0. 1.1.0 added image name, objective power and version, and
cacheable image tiles (`v`, `ETag`, 304); 1.2.0 added label maps (images
with `role: labels`, raw `bin` tiles, class tables); 1.3.0 added
`Store.aligned_to`, the image a store annotates; 1.4.0 added whole-layer
polylines (`GET /layers/{id}/lines`, `line-layer` v1).

- `openapi.yaml`: the JSON API, OpenAPI 3.1, served under `/api/v1`.
- `schemas/`: binary payloads (Arrow IPC), versioned separately:
  `point-layer` v1, `polygon-tile` v1, `centroid-tile` v1, `line-layer` v1.
- `conformance/`: tests that check a server against both.

## Stability

Within `/api/v1` only backward-compatible changes are allowed: new
endpoints, new optional request fields, new response fields, new
capabilities and enum values announced through `GET /info`. Clients ignore
fields and capabilities they do not know. Removing or renaming anything,
changing a type or a meaning, or tightening validation needs `/api/v2`.
Binary schemas never change once released; a change is a new version (see
`schemas/README.md`).

## Main rules

- Coordinates: level-0 pixels; microns via `mpp`. Tiles: 512 px at level l,
  covering 512 x 2^l level-0 px, for images and annotation layers alike.
- Object ids: integers in [0, 2^53), int64 in Arrow. A sidecar belongs to
  one store; forked objects keep their source ids, objects created in the
  sidecar get ids from 2^40.
- Layer ids: opaque strings, `{store}:{layer}` (read-only) and
  `{store}:edit:{layer}` (editable).
- Image tiles: request them with `?v={Image.version}`; such responses are
  immutable for the browser cache.
- Label maps: `GET /images` lists them with `role: labels`; tiles
  `.bin` carry raw little-endian values (gzip content encoding); colours
  come from `classes` and are applied by the client, so toggling a class
  needs no refetch.
- Polygon tiles up to `polygon_max_level` (3), centroid tiles above (S4).
- Edits: batched operations against `base_revision` (409 on a stale base,
  422 on invalid operations or geometry); undo by committing a logged
  `inverse`. Revision changes, including those made by another process, are
  pushed as server-sent events.

## Checks

    cd server && uv run pytest                    # includes ../contract/conformance
    cd web && pnpm gen:api                        # TypeScript types -> src/contract/v1.d.ts

The conformance suite runs the Python server in-process on a synthetic
store: the served routes must equal the spec's operations, every JSON
response must validate against the spec, every Arrow payload must match its
schema file, and every operation must be exercised.
