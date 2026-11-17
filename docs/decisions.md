# CompassStudio: decision log

CompassStudio is a browser-based viewer and editor for COMPASS-format slides and annotations.

Status: spikes S1 to S6 done, contract v1 frozen and milestones 3 to 8 done (2026-11-17); next is milestone 9. First entry 2026-11-12. Name chosen 2026-11-12. License and several details are open (see section 8).

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
  deliverable. **v1 frozen 2026-11-13** (`contract/`): within `/api/v1` only
  backward-compatible additions; binary schemas never change once released
  (a change is a new schema version). Object ids are integers below 2^53
  (int64 in Arrow, exact in JSON). Layer ids are opaque strings,
  `{store}:{layer}` and `{store}:edit:{layer}`. Edits carry GeoJSON
  geometry; invalid operations or geometry return 422. Server-sent events
  report revision changes, detected by polling the sidecar's
  `PRAGMA data_version` (S6), so changes by other processes arrive too.
  Conformance tests run with the server's test suite; TypeScript types are
  generated with openapi-typescript (in its own package, `web/tools/gen-api`,
  because it needs the TypeScript 5 compiler API).
  1.1.0 (2026-11-13, milestone 3, additive): image `name`,
  `objective_power` and `version`; image tiles with `?v={version}` are
  immutable in the browser cache, others revalidate (`ETag`, 304).
  1.3.0 (2026-11-13, milestone 5, additive): `Store.aligned_to`, the image
  a store annotates, paired like label maps (explicit `--annotations-of`,
  else dotted name prefix, `slide` / `slide.cells`, else the only image if
  the recorded size matches); `--images DIR` also serves `*.ann.sqlite`.
  1.4.0 (2026-11-15, milestone 6, additive): `GET /layers/{id}/lines`, a
  layer's polylines whole (`line-layer` v1: object_id, group, coords),
  capability `lines`.
  1.5.0 (2026-11-16, additive): polygon tiles take `min_area` (polygons
  at any level) or `max_area` (centroids at any level), filters on the
  R-tree bounding-box area; capability `tiles.area`. `ObjectInfo.area`
  (polygons) and `ObjectInfo.length` (perimeter or polyline length), in
  level-0 px units, computed when objects are fetched. Duplicate layer
  names and a second fork of a layer are 422 (were 500): forked objects
  keep their ids, so a layer can be forked once.
  1.6.0 (2026-11-16, milestone 7, additive): save points of editable
  layers: `Layer.saved_revision`, `Layer.unsaved` (batches that changed
  the layer since), `POST /layers/{id}/save`, `POST /layers/{id}/discard`
  (the inverses since the save point in one logged batch; `{reverted}` in
  its client label names the revisions); capability `save`.
  1.7.0 (2026-11-17, milestone 8, additive): multichannel images:
  `Image.display` (`rgb` or `channels`), `Image.channel_defaults`; their
  tiles are `bin` planes of one channel (`?channel=`); `GET
  /images/{id}/histogram`; capability `channels`.

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
longer apply. Decided by S1 (2026-11-13): a direct shard reader caches each
shard index and reads one inner chunk with `os.pread`, falling back to
zarr-python for layouts or codecs it does not handle. In process it is about
3x faster than zarr-python at 4 or more threads (zarr-python stops scaling at
2); end to end over HTTP it gives about 50 % more throughput (1,100 against
720 tiles/s on 4 CPUs), with no collapse up to 64 concurrent requests.
Details: `docs/spikes/S1.md`.
Group attributes carry import provenance, including source storage paths, so
test fixtures must never copy them.

### D4. Tile pipeline
- RGB: server decodes Zarr chunks and encodes JPEG (quality configurable,
  default about 90) for navigation; lossless on demand as WebP lossless,
  method 0 (S1: 5.8 ms and 424 kB per 512 px tile, against 13.7 ms and
  457 kB for PNG level 1). Disk cache for transcoded tiles: S1 measured JPEG
  q90 encoding at about 0.5 ms and a tile read at about 1.5 ms, so the cache
  is deferred until slow storage (network mounts) makes it worthwhile.
- Browser caching (2026-11-13): an image's `version` hashes its `zarr.json`
  files (contents, sizes, modification times), which a re-import rewrites.
  Tiles requested with `?v=` the current version are sent as
  `private, max-age=31536000, immutable`; without it, `no-cache` with an
  `ETag`, so a stale id-to-path mapping never shows old pixels.
- Tile work runs on a dedicated thread pool, one thread per CPU (S1: about
  15 % more throughput than Starlette's default pool).
- Multichannel: raw per-channel tiles with shader-side channel mixing and LUTs.
  Milestone 8 (2026-11-17), chosen by the owner from options: an image is
  `rgb` when it is uint8 with 3 (or 4) channels named R, G, B (or unnamed),
  else `channels` (uint8 or uint16; other types refused for now). One
  channel per tile request (`bin`, gzip), so showing a channel fetches only
  it and the browser caches each channel; COMPASS chunks hold every channel,
  so the server keeps decoded tiles in a 256 MB cache and concurrent
  requests for one tile wait for a single read. In the browser each shown
  channel is a TileLayer (rg8-packed uint16, decoded in the shader) mapped
  through its min/max window and colour and added over a black backdrop
  (additive blending; refinement `no-overlap` so parent and child tiles
  are never added twice); at most 8 shown at once. Contrast and colour are
  uniforms (no refetch). Histograms (256 bins and percentiles per channel)
  come from the finest level of at most 2^20 px, once per image version;
  Auto is the 1st to 99.9th percentile. Defaults: an optional
  `<image>.channels.json` (colour, min, max, visible by name or index);
  changes are remembered per image in the browser (localStorage).
  Rejected: all shown channels in one request (any toggle refetches every
  channel), all channels always (a 40-channel uint16 tile is about 20 MB),
  a minimal panel without histogram or Auto, browser-only settings, and a
  shared settings file written by the viewer. A single-channel integer
  pyramid is a label map only when paired (`--labels`, `--label-of` or a
  dotted name); otherwise a one-channel image (it used to be a label map
  always). Measured (workstation, headless Firefox, 4 server CPUs,
  synthetic 8- and 40-channel uint16 images, 4 channels shown): channel
  tiles to screen p50 46 to 59 ms, p95 79 to 80 ms in the end-to-end test
  (the first opening of a slide up to 150 ms p95); server read of a
  40-channel tile 15 ms p50 for the first channel, the others from the
  cache. Not measured: the laptop, Chromium, frame rate with 8 channels
  while panning, real multiplex slides (synthetic data only, by choice).
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
Tile loading (milestone 3, 2026-11-13, workstation, headless Firefox, sample
slide, flight with 6,000 px/s flings added): no variant stutters on the
workstation (frame p99 about 17 ms, deck render at most 8 ms), so the
laptop stutter is not reproduced here; the variants differ in sharpness
(share of on-screen tiles at the target level, per frame) and latency.
Default now: 6 concurrent requests (deck's scheduler, not the browser,
orders them) and a one-tile prefetch ring at the current level: pans and
flings stay fully sharp (1.0 against 0.94 to 0.98 with the S2 settings),
tile-to-screen p95 45 ms against 52 to 65 ms, at about 1.9x the requests.
Rejected: request debounce (sharpness down to 0.41 when zooming out) and a
per-frame texture-upload limit (0.64), both kept as options
(`TILE_PRESETS`). Whether the laptop stutter remains is to be checked by
the owner in normal use. Details: `docs/spikes/S2.md` (milestone 3).

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
  Contract v1: polygon tiles use the image tile grid
  (`/layers/{id}/tiles/{level}/{col}/{row}`), up to `polygon_max_level` = 3;
  above it, centroid tiles (bbox centres, each object in one tile). A
  density raster for very low zoom is deferred. Point layers are cached in
  server memory per revision (5M points: 5.4 s first, 26 ms cached).
- Label maps: the same tile path as images, as integer textures coloured by a
  palette texture, so class toggling needs no refetch.
  Milestone 4 (2026-11-13): COMPASS maps are 2-D (or one-channel) integer
  Zarr pyramids written by `write_pyramid`, pixel-aligned 1:1 with a slide,
  levels by nearest-neighbour subsampling. Contract 1.2.0 lists them in
  `/images` with `role: labels`, `dtype` (uint8, uint16, uint32, int32),
  `aligned_to`, `classes` and `background`; tiles `.bin` are the raw
  little-endian values, gzip content-encoded (level 1). Pairing: explicit
  `--label-of`, else the image whose id is a dotted prefix of the map's
  (`slide` / `slide.tissue`), else the only image; sizes must match.
  Class tables are viewer-side JSON files (`<map>.classes.json` next to the
  map, or `--classes`): `{"background": 0, "classes": [{"value", "name",
  "color"}]}`; COMPASS defines no attribute for them yet. The client packs
  values into r8/rg8/rgba8 textures, decodes them in the shader and colours
  them from a 256 x 256 palette texture (values from 65536 up get a hashed
  colour); outline mode draws class boundaries. Measured on the sample
  slide with a synthetic tissue map (workstation, 4 server CPUs): server
  read about 0.1 ms and gzip under 1 ms per tile, 6 to 14 kB per tile;
  a class toggle reaches the screen in two frames (32 to 34 ms, headless
  Firefox, 25 tiles on screen), within the 50 ms budget.
- Points in the viewer (milestone 5, 2026-11-13): each point layer is
  fetched whole when its slide opens and drawn as a binary ScatterplotLayer;
  group toggles use DataFilterExtension categories with the
  category-to-bit mapping overridden to the identity (deck assigns bits in
  order of first appearance, which breaks when a layer starts with groups
  hidden); at most 128 groups per layer filter. Colours per group live in
  their own attribute buffer; a change replaces only that attribute's
  descriptor (deck compares binary attribute descriptors by identity, so
  mutating `.value` is ignored). Size (revised 2026-11-15 at the owner's
  request): a diameter in microns, drawn true to size, chosen per
  resolution band of the view (µm per screen px: under 2.5, 2.5 to 12,
  above 12; on the sample slide levels 0-2, 3-4, 5+), each band with its
  own slider range (5-100, 20-200, 100-1000 µm) and remembered value
  (20, 60, 300 µm). A single µm size failed both ways (50 µm is 107 px at
  1:1 and under 1 px at fit on the sample slide), and clamping it to a
  screen-pixel range made the slider do nothing when zoomed out. Line
  widths (outlines, polylines) follow the same bands (0.5-10, 2-40,
  10-200 µm; defaults 1.5, 6, 40). Fill rate: 5M overlapping 6 px discs at
  fit gave frame p95 34 ms; with the default bands, 60 fps at every zoom
  (frame p95 17 ms); the largest low-band sizes with millions of points
  are not measured. Hover and click pick by
  index (object id from the Arrow buffer); attributes are fetched by id
  when selected. Measured (workstation, headless Firefox, 4 server CPUs,
  5M-point synthetic layer): group toggle 16 to 20 ms to screen, pick 3 to
  6 ms p95; first open 10.4 s (layer listing with counts 4.6 s, point
  buffer 5.7 s for 90 MB), later opens 0.8 s (server caches).
- Polygons and polylines in the viewer (milestone 6, 2026-11-15), chosen
  by the owner from options: from level 3 inward, polygon tiles are always
  fetched on the level-3 grid (4096 px squares; the tiles in view plus a
  one-tile ring) and merged on the client into one buffer set per group
  with each polygon once (deduplicated by object id), rebuilt at most once
  per frame when the set of tiles changes; large regions are never lost
  and nothing is drawn twice. Rejected: a tile layer per level with an
  owner tile (large regions vanish when their centre tile is off screen)
  or with duplicates (double fill and outlines at tile borders). Zoomed
  out, centroid tiles at the shown level, drawn as points sized by the
  point bands. Revised 2026-11-16 at the owner's request (cells and
  tissue regions share layers; regions must not become dots): the choice
  is per polygon at every zoom, by on-screen size: bounding-box area at
  least `minShapePx`^2 screen px (default 8 px, per layer, 2 to 64; judged
  at the start of the shown level) draws a shape, smaller draws a centre.
  From level 3 inward the level-3 tiles are split on the client (no
  refetch on zoom or cutoff change); further out, polygon tiles with
  `min_area` and centroid tiles with `max_area` on the shown level's grid,
  the same cutoff. Bounding-box area rather than true area, chosen by the
  owner: it comes from the R-tree, so no geometry is decoded for small
  polygons. Rejected: switching only above level 3 (cells 2 to 3 px wide
  stay shapes), and switching a whole layer by its typical size (a layer
  mixing cells and regions behaves as cells). Measured (1M cells and 200
  regions, workstation, 4 CPUs, densest tile): the area filter adds 5 to
  15 % to a centroid query (985 against 912 ms for a fit-level tile of
  254k cells); the large-polygon tile at fit takes 130 to 150 ms. Each group is one SolidPolygonLayer (server triangles) and
  one PathLayer (outline rings, sharing the vertex buffer), so toggles and
  colours are prop changes. Style per layer: fill opacity (default 30 %),
  outline on or off, width in µm per band. Polylines: the whole layer
  (contract 1.4.0), one PathLayer per group. Picking covers fills,
  outlines, lines and centres; a selection is outlined with its rings.
  Measured (workstation, headless Firefox, 4 server CPUs, 5M points and
  1M cells plus 200 regions, dense area): panning at 60 fps at every zoom
  (frame p95 17 ms); at level 3 (41k polygons merged from 30 tiles) frame
  p99 49 ms, 4 frames over 33 ms in a 3 s pan, when arriving tiles
  trigger a rebuild (15 ms) and re-upload; cell group toggle 16 to 30 ms;
  pick 5 to 7 ms p95. First zoomed-out view: 3.8 s for the one centroid
  tile holding 1M cells (server query), level 6 view 1.4 s, zoomed-in
  views 0.1 to 0.7 s.
  Outline rings arrive unclosed (`polygon-tile` v1); deck closes
  `_pathType: "loop"` paths only when it normalizes them, which it skips
  for binary data, so the last edge was missing (fixed 2026-11-17): the
  client draws outlines from a copy of the rings with the first vertex
  repeated.
- Working layer (2026-11-17, chosen by the owner, because a layer such
  as tissue can cover another, such as morphology, and block selecting
  it): one locked annotation layer per slide, set by a lock on its card
  in the layers panel or `g` (the selected object's layer). The viewer
  draws that layer's deck layers after all others and clones every other
  annotation layer's deck layers dimmed to 35 % and unpickable; sources
  are unchanged. Rejected: a lock beside "Into" (only when editing, and
  Into could no longer follow the selection), and leaving other layers
  at full opacity. A first version locked a single class (group within
  a layer); the owner corrected it to whole layers.
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
  through the same API. S6 (2026-11-13): batches of up to 100 operations
  commit in under 35 ms p95 on a sidecar holding a forked 1M-polygon layer
  (WAL, synchronous NORMAL; FULL adds about 3 ms); 1,000-operation batches
  take 50 to 230 ms. Forking a 1M-object layer takes 8.4 s and 548 MB; a
  copy-on-write overlay is the fallback if that proves too slow. Another
  process's commits are detected via `PRAGMA data_version` polling within
  about 10 ms, readers are not blocked, and racing writers get conflicts,
  not busy errors. Provisional id scheme: forked objects keep their source
  ids, new objects get ids above every id used. Details: `docs/spikes/S6.md`.
  Decided for v1 (2026-11-13): one sidecar per source store (bound by path
  in its `meta`); forked objects keep their source ids (forks refuse source
  ids at or above 2^40); objects created in the sidecar get ids from 2^40.
  Forking and creating layers advance the revision and appear in the log
  (empty inverse).
- **Milestone 7 (2026-11-16)**, chosen by the owner from options: every
  action commits at once (one batch, against the latest known revision;
  a 409 is retried on the new revision, so concurrent writers are last
  writer wins per object); rejected: an edit session with Save (unsaved
  work, late conflicts) and merging quick successive edits (more logic).
  The first edit of a read-only layer asks to fork it (a layer is forked
  once, since forked objects keep their ids); the fork takes the
  original's name and style and its place in the list (a store layer with
  a fork is not listed), and the selection moves to the fork.
  Save points (2026-11-16, at the owner's request: forks lengthened the
  list and read as duplicates): each editable layer has a save point
  (creation, fork or "Save edits"); with edits after it the layer shows
  as "<name> (editable)" on a dark grey card with "Save edits" (new save
  point: one layer of that name from then on) and "Discard edits"
  (revert to the save point, one batch that Redo restores). Chosen over
  writing the edits back into the COMPASS store, which stays read-only
  (merge-back remains an open item), and over deferring merging. Tools: select, edit shapes (vertex
  drag, edge click or drag to add, Alt+click to remove (editable-layers
  removes on a plain click, too easy by accident), drag inside to move),
  polygon, freehand (Douglas-Peucker to about 1 screen px), rectangle and
  ellipse (drag; the ellipse is drawn as a box and saved as a 64-gon,
  since editable-layers' ellipse mode is geodesic), point, line. Group and
  attributes are edited in the inspector, per object or for the whole
  selection. Added at the owner's request: "Into" (layer and group for
  new objects) follows the last selected object; "Discard changes"
  reverts this browser's edits since the slide was opened, one batch per
  store (the inverses, newest first; label `:undo:<rev>,<rev>,…`), so it
  is one undo step that Redo restores (chosen over resetting a fork to
  its source and over cancelling only the edit in progress).
  How changes reach the view (refines the edit buffer above): bulk layers
  are never refetched for edits. On every new revision (own commits and
  server-sent events alike) the client reads the log since its last
  revision, collects the objects touched, fetches their current GeoJSON
  (`/features`) and keeps them as overrides per store; overridden and
  checked-out objects are hidden in the editable layers' bulk sources
  (points: a 0/1 value filter; polygons: skipped when tiles are merged;
  polylines: rebuilt without them) and drawn by an overlay layer per
  editable layer (GeoJsonLayer). So any writer, not only this client, is
  shown correctly, and tiles never go stale. Undo and redo commit the
  logged inverse; redo inverts the undo commit, so recreated objects keep
  their ids. Commits carry a per-browser client label (`:undo:<rev>`,
  `:redo:<rev>` added), from which the stacks are rebuilt on load, so
  they survive reloads. Validity (self-crossing rings, no area, too few
  points) is checked before sending; the server's 422 is reported and
  the shape reverts.
  Measured (workstation, headless Firefox, 4 server CPUs, store with 300k
  points and 300k cells, end-to-end test): commit to view updated 8 to
  15 ms p50, the slowest of 17 per run 47 to 108 ms (4 runs; which commit
  is slow is not yet identified, so the 100 ms budget holds at the median
  but not always); check-out 2 to 7 ms; a 30-region fork under 1 s, 300k
  points about 2 s.
- **Editing library**: `@deck.gl-community/editable-layers` (successor of
  nebula.gl). Its repository is flagged semi-maintained and it does not support
  deck.gl's WebGPU path, which is acceptable under D5. Verified by S5
  (2026-11-13, version 9.4.8): with `coordinateSystem: CARTESIAN` it works on
  the orthographic, basemap-free canvas (ModifyMode, DrawPolygonMode).
  Check-out and commit round trips: 18 to 22 ms for cells, 40 to 66 ms for
  5,000-vertex regions, with 1M polygons resident. Checked-out objects are
  hidden by a value filter whose GPU buffer is rewritten only for the
  object's vertices. Dragging a vertex of a 5,000-vertex polygon runs at
  about 30 fps (editable-layers' per-move JavaScript); accepted 2026-11-13:
  polygons with many vertices are rarely edited by hand, so no special tool
  is planned. Client-side validity checks are still needed (the server
  refuses self-intersections). Details: `docs/spikes/S5.md`.
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
Milestone 3 (2026-11-13): React 19, Tailwind 4 and shadcn/ui components
(Radix) in a fixed layout (toolbar, viewer, status bar); dockview waits for
the panels of milestone 5. The viewer core (`web/src/viewer/`) is
framework-free and shared with the spike benches (`web/bench.html`). System
fonts only. Keyboard: arrows, `+`/`-`, `0`/`f` fit, `1` actual pixels,
PageUp/PageDown images; the view is kept in the URL query.
Milestone 5 (2026-11-13): tabbed sidebars instead of dockview: a left
sidebar (tabs Layers and Labels) and a right inspector, both collapsible
(`[`, `]`) and resizable. dockview is deferred until the annotation table
and minimap need free docking (they were not part of milestone 7). Selection: click, Shift+click
to add, Esc to clear; hover names the point in the status bar.

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
2. Spikes S1 to S6; freeze contract v1. Done 2026-11-13.
3. RGB tile server and pan/zoom viewer. Done 2026-11-13 (laptop check by the owner pending).
4. Label maps with class toggles. Done 2026-11-13.
5. Points: layer view, picking, attribute panel. Done 2026-11-13.
6. Polygons with level of detail. Done 2026-11-15 (polylines included).
7. Editing: selection and check-out, draw tools, vertex editing, attribute forms,
   undo and redo, commits with revisions. Done 2026-11-16.
8. Multichannel. Done 2026-11-17 (synthetic data only).
9. Packaging (one command to serve a slide) and documentation.

## 8. Open items

- Editing (milestone 7), not measured: hiding edited objects in a forked
  multi-million point layer (the first hide sorts the ids, then each
  change rewrites one float per point); edits on a forked 1M-cell layer
  in the viewer; the laptop and Chromium. Many overrides (thousands of
  edits in one session) are drawn by the overlay without a limit; a
  reload resets them into the bulk data.

- License.
- Frozen COMPASS annotation-schema document (ideally in the COMPASS repository).
- Merge-back path from sidecar layers to a COMPASS store.
- dockview maintenance status; browser Blosc-bitshuffle decode (only relevant if
  D4's rejected option is revisited).
- Whether and when to move to WebGPU. No full Rust server (reviewed
  2026-11-13: none of S1 to S6 broke a budget in Python). If stores outgrow
  the synthetic sizes, a small Rust/PyO3 extension for building polygon
  tiles and point buffers (the GIL-bound loops of S4); the Arrow output, and
  so the contract, stays the same. Faster ASGI servers measured 2026-11-13
  (S1 tile set, 4 CPUs): uvicorn with httptools + uvloop and Granian 2.8 both
  gave about 1,100 to 1,150 tiles/s against 1,030 for uvicorn with h11, i.e.
  8 to 12 %, close to run-to-run noise; the overhead is in the Python app,
  not the HTTP parser. Granian is not worth a switch; the server depends on
  `uvicorn[standard]` (httptools + uvloop, picked automatically; plain asyncio
  where uvloop is unavailable). HTTP/2 is ruled out
  for now: browsers only use it over TLS, which on 127.0.0.1 means
  self-signed certificates or a locally installed CA.
- Zoomed-out level of detail (D6): polygons switch to centroids by
  on-screen size (2026-11-16); the density raster is open.
- Layer object counts (`Layer.counts`) are computed with a full join on
  first use: 3.3 s for the 6M-object synthetic store (4.6 s for the first
  layer listing in milestone 5, which delays the first open of a slide).
  Cache them in the sidecar or compute them in the background. The first
  5M-point buffer takes 5.7 s; a disk cache would remove it.
- Polygons (milestone 6): a zoomed-out centroid tile holding a whole large
  layer takes 3.8 s (1M cells); a cached or precomputed coarse level, or
  the density raster, would remove it. Merging tiles re-uploads the whole
  visible set on each change (frame p99 49 ms at level 3 in a dense area);
  incremental buffers would smooth it if real stores show it.
