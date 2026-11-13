# CompassStudio: decision log

CompassStudio is a browser-based viewer and editor for COMPASS-format slides and annotations.

Status: planning. First entry 2026-11-12. Name chosen 2026-11-12. License and several details are open (see section 8).

This document records what was decided, why, and which alternatives were
rejected, so that settled questions are not reopened without new evidence.
Items marked **[verify]** are assumptions that a spike (section 6) must confirm.

## 1. Purpose and scope

A fast, fluid, browser-based viewer and editor for whole-slide images and
their annotations, reading COMPASS-format data. It is a separate project from
COMPASS and its Qt viewer, with its own dependencies. Open source; runs on
Linux, and in practice on any OS with a modern browser.

**Data it must handle**

- Pyramidal multi-channel raster, up to ~200,000 x 200,000 px, with
  microns-per-pixel calibration.
- Vector annotations (points, polygons, lines) in level-0 pixel coordinates,
  millions of objects, with attributes (including sparse per-object
  expression), organised as layers and groups.
- Label maps: single-channel rasters aligned 1:1 with the image.

**Goals**: smooth pan/zoom; per-layer and per-group visibility; selecting objects
and inspecting attributes; creating and editing vector annotations; a documented
HTTP contract between client and server.

**Non-goals (v1)**: painting or editing raster label maps; multi-user
collaboration; authentication beyond a local session token; public hosting;
image or annotation analysis.

## 2. Performance budget

Measured on a low-end reference machine (laptop, integrated GPU only):

- 60 fps pan/zoom with 5M points and 1M polygons resident.
- Tile-to-screen latency under 100 ms for cached-cold tiles.
- Group visibility toggle under 50 ms.
- Edit check-out and commit round trip under 100 ms.

- Memory (set 2026-11-13): the machine has 32 GB RAM. A discrete GPU is
  optional: the viewer must run on integrated graphics, which shares system
  RAM, so GPU buffers count against the same 32 GB.

These are proposals to be confirmed or adjusted by the spikes.

Spike runs (revised 2026-11-13): after S2, all spikes run on the development
workstation only, limited to 4 CPUs and 32 GB RAM. This is representative for
the server spikes (S1, S4, S6). For the GPU-bound ones (S3, S5) the workstation
GPU is far stronger than the reference laptop's, so their frame-rate numbers
are optimistic; the owner accepts that risk (S2 on the laptop was close to the
workstation result). The laptop is not used for further spike runs.

## 3. Architecture

```
 Browser (TypeScript, deck.gl, React)
        |   HTTP (OpenAPI 3.1, /api/v1) + Arrow IPC + SSE
 Local server (Python, FastAPI)
        |-- read-only: Zarr v3 sharded directory store (image, label maps)
        |-- read-only: COMPASS annotation store (SQLite, R-tree, WKB)
        '-- read-write: sidecar curation database (SQLite)
```

## 4. Decisions

### D1. Browser client plus local Python server
Server reads COMPASS formats and serves tiles and annotation queries; the browser
never parses COMPASS formats. Rejected: a static client reading files directly
(local file access is Chromium-only; Blosc, sharding and SQLite decoders would
have to be reimplemented in JS). A static or serverless backend remains possible
later because the client depends only on the contract (D2).

### D2. Documented, versioned HTTP contract
- OpenAPI 3.1, path-versioned (`/api/v1`); TypeScript client generated from it.
- Binary payloads (tiles, point buffers, polygon tiles) use Arrow IPC or raw
  buffers with schemas versioned separately from the JSON API.
- `GET /info` returns API version, capabilities and supported schema versions.
- Writes are batched operations carrying `base_revision`; conflicts return 409.
- Server-sent events notify the client of changes by other processes.
- Coordinates are level-0 pixels; mpp is metadata; the UI shows microns.
- The contract directory (spec, schemas, conformance tests) is a first-class
  deliverable. It is frozen only after the spikes.

### D3. Image store access
COMPASS images are Zarr v3 directory stores (`.zarr` folders), one array per
pyramid level (`0/`, `1/`, ...), using the sharding codec; written once at
ingestion, read-only afterwards (COMPASS `docs/architecture.md` section 2,
`core/zarr_raster.py`). The server must not assume chunk shape or codec; it
reads them from metadata. The sample slide (2026-11-12) has 4096 px shards of
512 px chunks, Blosc zstd with bitshuffle, shard index at the end with crc32c,
so one 512 px tile is one inner chunk.

Resolved 2026-11-12: production images are folders, not zips, so the `zipfile`
lock contention concern (CPython issue 42369) and the pread-based zip store no
longer apply. **[verify]** whether zarr-python's per-chunk overhead limits
throughput under concurrency; candidate fast path: a direct shard reader that
caches each shard index and reads one inner chunk with `os.pread`, falling back
to zarr-python for layouts or codecs it does not handle. Decided by spike S1.
Group attributes carry import provenance, including source storage paths, so
test fixtures must never copy them.

### D4. Tile pipeline
- RGB: server decodes Zarr chunks and encodes JPEG (quality configurable,
  default about 90) for navigation; lossless on demand (PNG or lossless WebP,
  chosen in S1). Disk cache for transcoded tiles.
- Multichannel: raw per-channel tiles with shader-side channel mixing and LUTs.
- Rejected as default: client-side Zarr decoding (needs browser Blosc-bitshuffle
  support **[verify]** and reimplementation of the shard layer).

### D5. Renderer: deck.gl on WebGL2
OrthographicView (non-geographic pixel space), TileLayer, binary-attribute
point and polygon layers, GPU picking and filtering. WebGPU is **not** a
baseline: as of 2026-11 browser support on Linux still sits behind flags or is
in progress, and deck.gl's WebGPU support is partial. Revisit later.
Fallback if the editing spike (S5) fails: OpenLayers (built-in draw, modify and
snap interactions, custom pixel projections). Coordinate precision (S2,
workstation 2026-11-12, reference laptop 2026-11-13, Firefox and Chromium):
no motion jitter up to 262k px and 256x overzoom; absolute level-0
coordinates are exact through
accessor-built layers and BitmapLayer bounds; float32 binary positions give a
static offset of at most 1/128 px x 2^zoom at 131k to 262k px (1 px at 128x
overzoom), acceptable for annotation buffers. `coordinateOrigin` has no effect
for CARTESIAN data in an OrthographicView, and `modelMatrix` does not recover
precision. S2 frame rate on the reference laptop: 60 fps through scripted
pan and zoom in Chromium; in Firefox 60 fps panning, with drops to 30 fps
during zooms; over 52 fps in manual use, with stutter on fast pans (open for
milestone 3). Details: `docs/spikes/S2.md`.

### D6. Annotation delivery
- Points: the whole layer as binary buffers (x, y, class, object id), GPU
  filtered and picked. Attributes are fetched lazily by object id.
- Polygons: R-tree viewport query on the server, triangulation on the server,
  delivered as tiles. Zoomed out, polygons are replaced by centroids or a
  density raster (thresholds tuned in S3). S4 (2026-11-13): server cost per
  512 px tile is under 15 ms p95 up to level 3 and 40 to 63 ms at level 4
  (about 5k polygons, 2 MB), so the proposed switch to centroids or density
  is above level 3. The read path is GIL-bound (4 threads give no speed-up);
  exporting a 5M point layer takes 5.7 s and needs a cache. Details:
  `docs/spikes/S4.md`.
- Label maps: the same tile path as images, as integer textures coloured by a
  palette texture, so class toggling needs no refetch.
- Groups and layers: client-side GPU filtering on layer and group attributes.
  S3 (2026-11-13): points use DataFilterExtension categories (group ids
  rebased to 0..k-1; at most 128 groups per layer). Polygons use one layer
  per group, toggled by visibility, because DataFilterExtension categories
  draw nothing on binary SolidPolygonLayer data in deck.gl 9.4.1. Toggles
  measured 17 to 23 ms; picking 9 to 10 ms p95. Precomputed triangles cut
  the first frame from 4.1 s to 1.6 s (5M points + 1M polygons, 700 MB GPU
  memory). Details: `docs/spikes/S3.md`.
- Rejected: MVT/PMTiles vector tiles (a baking pipeline, and non-geographic
  coordinates fight the tooling).

### D7. Annotation store
SQLite with R-tree and WKB geometry, as produced by COMPASS tooling. The viewer
opens it read-only and checks a schema version, refusing unknown versions. A
frozen schema document is needed from the COMPASS side (section 8).

### D8. Editing
- **Edit buffer**: bulk layers stay GPU-resident and read-only. Selecting objects
  checks them out into a small editable layer (hidden in the bulk layer by a GPU
  filter); new objects are drawn into the same buffer; commit sends operations
  to the server and refreshes the affected tiles. Editing cost is independent of
  object count.
- **Edit model (E3)**: pipeline and imported layers are read-only. "Fork layer"
  creates an editable copy; edits apply only to editable layers.
- **Storage**: edits and forked layers live in a **sidecar curation database**
  (SQLite). It uses the COMPASS annotation schema, so COMPASS tools can read it,
  plus viewer-owned tables (operation log, fork provenance: source layer and
  source store version at fork time). The COMPASS store is never written by the
  viewer.
- **Operations**: create, update geometry, update attributes, delete, move
  between groups. Operations are batched and atomic, append to a linear log, and
  advance a revision counter. Undo and redo are inverse operations replayed
  through the same API.
- **Editing library**: `@deck.gl-community/editable-layers` (successor of
  nebula.gl). Its repository is flagged semi-maintained and it does not support
  deck.gl's WebGPU path, which is acceptable under D5. **[verify]** it works on
  an orthographic, basemap-free canvas (S5).
- Out of v1: merge-back of curated layers into a COMPASS store; raster label
  painting (the image store is write-once by design, and a sharded
  chunk update rewrites the whole shard; it would need a sparse delta overlay).

### D9. UI
Neutral dark theme (light optional), compact density, one accent colour; dockable
panels (layer tree, properties, annotation table, minimap); status bar with
px and micron coordinates and a scale bar; command palette; keyboard-first tools.
Stack: React, shadcn/ui (Tailwind), dockview **[verify maintenance]**, TanStack
Table with virtualization, Lucide icons. Alternative: Mantine (batteries
included).

### D10. Security and privacy
Slides may be sensitive. The server binds to 127.0.0.1 by default; a random
per-session token is required; Host and Origin headers are checked (protection
against DNS rebinding); no wildcard CORS. All assets are self-hosted: no CDN, no
web fonts from third parties, no telemetry. Remote use goes through an SSH
tunnel; binding to other interfaces requires an explicit flag.

### D11. Repository and process
Monorepo: `contract/`, `server/` (Python, uv), `web/` (TypeScript, Vite, pnpm),
`docs/`. No assistant-specific files or commit attribution in the repository;
the repository is solely authored by its owner. Name: CompassStudio. Slugs always
use a separator (`compass-studio`, `compass_studio`), because the lowercase run
`compassstudio` has three consecutive s characters and is typo-prone. Slide data,
sample stores and manifests live in a gitignored `data/` directory. License: open.

## 5. Rejected alternatives (summary)

OpenSeadragon (image only); MapLibre (Web Mercator); static client-only
architecture (D1); client-side Zarr decoding as default (D4); vector tiles via
MVT/PMTiles (D6); in-place editing of the COMPASS database (D8); WebGPU as a
baseline (D5).

## 6. Spikes (before the contract is frozen)

| Id | Question | Pass criterion |
|---|---|---|
| S1 | Tile throughput from the sharded directory store: zarr-python vs a direct shard reader; JPEG and lossless encoders | No throughput collapse under concurrency; server p95 per 512 px tile low enough to fit the 100 ms budget |
| S2 | deck.gl OrthographicView and TileLayer on a real slide, low-end laptop | 60 fps pan/zoom; no coordinate jitter at ~200k px deep zoom |
| S3 | 5M points and 1M polygons (synthetic generator) | 60 fps; memory within budget; picking and group toggle within budget |
| S4 | SQLite read path: bbox query and WKB triangulation per tile | Per-tile p95 within the latency budget |
| S5 | editable-layers on an orthographic canvas with a 1M-object bulk layer behind it | Check-out and commit round trip under 100 ms; vertex editing on large polygons usable |
| S6 | SQLite write path: R-tree trigger cost per batch; WAL with a second process | Batch commit within budget; external writer detected via the revision counter |

## 7. Milestones

1. Repository, contract skeleton, sample data and synthetic annotation generator.
2. Spikes S1 to S6; freeze contract v1.
3. RGB tile server and pan/zoom viewer.
4. Label maps with class toggles.
5. Points: layer view, picking, attribute panel.
6. Polygons with level of detail.
7. Editing: selection and check-out, draw tools, vertex editing, attribute forms,
   undo and redo, commits with revisions.
8. Multichannel.
9. Packaging (one command to serve a slide) and documentation.

## 8. Open items

- License.
- Object id scheme for the sidecar database (namespacing against source ids).
- Frozen COMPASS annotation-schema document (ideally in the COMPASS repository).
- Merge-back path from sidecar layers to a COMPASS store.
- dockview maintenance status; browser Blosc-bitshuffle decode (only relevant if
  D4's rejected option is revisited).
- Whether and when to move to WebGPU; whether a Rust server is ever needed
  (only if profiling demands it).
- Zoomed-out level-of-detail threshold for polygons (D6): not tunable on the
  workstation GPU; needs a weaker GPU or an object-count rule.
