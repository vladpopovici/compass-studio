# CompassStudio user guide

CompassStudio shows COMPASS whole-slide images and their label maps in the
browser. A local Python server reads the files; the browser only talks to
that server. This guide covers every command and the viewer's controls. It
describes what is implemented now (milestones 1 to 6: images, label maps,
and point, polygon and polyline annotations with picking); editing and
multichannel images come later.

## 1. Requirements

- Linux (other systems are untested).
- [uv](https://docs.astral.sh/uv/) for the server (Python 3.12 or newer,
  installed by uv when needed).
- Node.js and [pnpm](https://pnpm.io/) to build the browser client.
- A recent Firefox or Chromium with WebGL2.

## 2. Install and build

From the repository root:

    cd server && uv sync          # server and its tools
    cd ../web && pnpm install && pnpm build   # browser client, into web/dist

Run `uv sync` again after pulling changes that add commands, and
`pnpm build` after changes to the client.

## 3. Start the server

All commands below run from `server/` with `uv run`.

    uv run compass-studio-serve --images ../data --web ../web/dist

The server prints one line per image and label map, then a URL such as

    CompassStudio on http://127.0.0.1:8765/#token=O2Oov9Z3...

Open that URL. The part after `#token=` is the session key; without it the
viewer shows "Session token missing or wrong". A new random token is made
at every start; set `COMPASS_STUDIO_TOKEN` to keep a fixed one:

    COMPASS_STUDIO_TOKEN=mysecret uv run compass-studio-serve --images ../data --web ../web/dist

Stop the server with Ctrl+C.

### What to serve

| Option | Meaning |
|---|---|
| `--images DIR` | every COMPASS `.zarr` folder and every annotation store (`*.ann.sqlite`) directly in `DIR`; id = file or folder name without `.zarr` / `.ann.sqlite`. Label maps and stores are paired with their slide by name (sections 5 and 6). Repeatable. |
| `--image ID=PATH` | one `.zarr` folder under the id `ID`. Repeatable. |
| `--labels ID=PATH` | one label map (`.zarr`, a single integer channel). Repeatable. |
| `--label-of ID=IMAGE` | the image a label map belongs to, when automatic pairing does not apply. |
| `--classes ID=FILE` | class table (JSON) for a label map; default `<map>.classes.json` next to it. |
| `--annotations ID=PATH` | a COMPASS annotation store (`.ann.sqlite`). Repeatable. |
| `--annotations-of ID=IMAGE` | the image an annotation store belongs to, when automatic pairing does not apply (section 6). |
| `--sidecar-dir DIR` | where edit databases (`<ID>.curation.sqlite`) go; default next to each store. |
| `--read-only` | no edit databases: annotations cannot be edited. |
| `--synthetic ID=WxH` | a generated test image, e.g. `--synthetic big=200000x200000`. |
| `--synthetic-labels ID=IMAGE` | a generated test label map the size of `IMAGE`. |

### Network and performance

| Option | Meaning |
|---|---|
| `--host HOST`, `--port PORT` | address to listen on; default `127.0.0.1:8765`. |
| `--allow-other-interfaces` | required to listen on anything but 127.0.0.1. Slides may be sensitive: prefer an SSH tunnel (below). |
| `--allow-origin ORIGIN` | an extra allowed browser origin, e.g. the development server (section 8). |
| `--web DIR` | the built client to serve at `/` (normally `../web/dist`). Without it only the API is served. |
| `--tile-threads N` | threads that read and encode tiles; default one per available CPU. |
| `--reader {auto,zarr}` | `auto` (default) uses the fast direct shard reader where possible; `zarr` forces zarr-python. |

`--spike-files` and `--spike-results` exist for the benchmark pages only.

### Viewing a slide on another machine

The server stays on 127.0.0.1; forward the port over SSH from the machine
with the browser:

    ssh -L 8765:127.0.0.1:8765 workstation

then open the printed URL (with its token) in the local browser.

## 4. Using the viewer

The window has a toolbar, a sidebar on the left (tabs **Layers** and
**Labels**), the image, an inspector on the right (opens when you select a
point) and a status bar. Drag a sidebar's inner edge to resize it; the
width is remembered in this browser.

### Mouse

- Drag: pan.
- Wheel: zoom around the cursor.
- Hover: the status bar shows the cursor position, the annotation object
  under it (layer, group, id) and the label values under it.
- Click a point, polygon or polyline: select it and show it in the
  inspector. Shift+click: add or remove one. Click on empty space: clear
  the selection.

### Keys

| Key | Action |
|---|---|
| Arrows | pan by a fifth of the window (Shift: half) |
| `+` or `=` / `-` | zoom in / out by one pyramid level (2x) |
| `0` or `f` | fit the image in the window |
| `1` | actual pixels (1 screen px = 1 level-0 px) |
| PageDown / PageUp | next / previous image |
| `l` | show or hide all label maps |
| `o` | label maps as outlines or filled |
| Esc | clear the selection |
| `[` | show or hide the sidebar |
| `]` | show or hide the inspector |

Keys do nothing while a text field or list has focus.

### Toolbar

- Image picker: the slides being served (label maps are not listed here).
- Zoom out, zoom level, zoom in, fit, 1:1. The zoom shows screen px per
  level-0 px and, when the slide records its objective power, the apparent
  magnification.
- Sidebar and inspector buttons (right end): show or hide them (`[`, `]`).

### Status bar

From left to right: cursor in level-0 px, cursor in microns, the pyramid
level being shown (`L2/7`), microns per pixel, the annotation object under the
cursor (`layer › group #id`), label values under the cursor, tile loading (tiles
loaded, requests in flight, latency) and a scale bar.

### Keeping your place

The view is kept in the address bar (`?image=…&x=…&y=…&z=…`). Reloading, or
sharing the link together with a valid token, returns to the same place.

## 5. Label maps

A label map is a single-channel integer COMPASS pyramid (uint8, uint16,
uint32 or int32) with the same size as its slide, one value per pixel.

### Pairing with a slide

Applied in order:

1. `--label-of MAP=IMAGE`.
2. Name: `slide.zarr` gets `slide.tissue.zarr`, `slide.cells.zarr` and so
   on (the longest image id that is a dotted prefix of the map's id).
3. If only one image is served, every map belongs to it.

The map and the slide must have the same size, or the server refuses to start.

### Class tables

Optional; names and colours for the values. Put `slide.tissue.classes.json`
next to `slide.tissue.zarr`, or pass `--classes`:

```json
{
  "background": 0,
  "classes": [
    {"value": 1, "name": "tumour", "color": "#cc79a7"},
    {"value": 2, "name": "stroma", "color": "#f0e442"}
  ]
}
```

`background` is drawn transparent and is optional. Colours are `#rrggbb`.
Without a table, the panel lists the values found at the coarsest level and
colours them automatically.

### The label maps panel

In the sidebar's **Labels** tab, one card per map:

- Eye button: show or hide the map.
- Square button: filled or outlines (class boundaries).
- Opacity slider.
- All / None: show or hide every class.
- One checkbox per class; "Other values" covers values not in the table.

Class toggles take effect at once; nothing is reloaded from the server.

## 6. Annotation layers

An annotation store (`.ann.sqlite`, written by COMPASS tools) holds layers;
each layer holds objects (points, polygons, lines) sorted into groups
(classes). Coordinates are level-0 pixels of the slide. This version draws
**points, polygons and polylines**; other types (point sets, circles) are
listed but not drawn yet. The server never writes a store; unless `--read-only` is given it creates
an edit database `<ID>.curation.sqlite` next to it (or in `--sidecar-dir`),
used by editing in a later version.

### Pairing with a slide

As for label maps: `--annotations-of STORE=IMAGE`, else by name
(`slide.zarr` gets `slide.cells.ann.sqlite`, id `slide.cells`), else the
only image served if the store records the same image size. A store paired
by name or explicitly must record the slide's size (if it records one), or
the server refuses to start; a store that pairs with nothing is served but
not shown, and the server says so at start-up.

### The layers panel

In the sidebar's **Layers** tab, stores of the current slide, each with its
layers:

- Eye button: show or hide the layer.
- Object counts per type; types not drawn yet are noted.
- A line naming the current zoom band (below).
- Sizes and widths are in microns, drawn true to size. One size cannot
  suit every zoom, so there are three bands, chosen by the image resolution
  on screen (µm per screen pixel). Each band has its own slider range and
  keeps its own value; the sliders show the band you are in, and sizes
  switch when you zoom across a band:

  | Band | µm per screen px | On a 0.47 µm/px slide | Points / Centres | Lines |
  |---|---|---|---|---|
  | high magnification | under 2.5 | levels 0–2 | 5–100 µm (20) | 0.5–10 µm (1.5) |
  | medium magnification | 2.5–12 | levels 3–4 | 20–200 µm (60) | 2–40 µm (6) |
  | low magnification | 12 and up | levels 5+ and fit | 100–1000 µm (300) | 10–200 µm (40) |

  (defaults in brackets). Hover a slider for its range and how many screen
  pixels it covers now. Everything is at least 1 px wide. Images without a
  calibration assume 0.5 µm/px.
- Points: point diameter. Centres: the diameter of the dots that stand in
  for polygons too small to draw (below).
- Lines: polygon outline and polyline width.
- Shapes (polygons): the smallest on-screen size, in screen pixels, at
  which a polygon is drawn as a shape (default 8 px, range 2–64); smaller
  ones are drawn as dots (below).
- Outline and Fill (polygons): outlines on or off, and the fill opacity
  (0: outlines only; default 30 %).
- Opacity: the whole layer.
- All / None: show or hide every group.
- One row per group: checkbox (show or hide), colour swatch (click to pick
  a colour; points, fills, outlines and lines use it) and the number of
  points and polylines.

**Level of detail.** Each polygon is drawn by its size on screen: when its
bounding box covers at least Shapes × Shapes screen pixels (by area), it is
drawn as a shape; otherwise as a dot at the centre of its bounding box, in
its group colour and sized like points ("Centres"). So tissue regions stay
shapes at fit, while cells become dots as you zoom out (with 8 px, a cell
about 10 µm across on a 0.47 µm/px slide turns into a dot around level 2
or 3). The size is judged at the start of each pyramid level, so polygons
switch when you zoom across a level, not continuously. Polygons are
fetched by area as you pan and zoom, so a layer of a million cells opens
at once; the first fully zoomed-out view of a large layer can take a few
seconds (one million cells: about 4 s). From level 3 inward a change of
Shapes applies at once; zoomed out further it refetches the tiles in view.

Point and polyline layers load when a slide opens (all at once, a spinner
shows while loading). The first load of a large layer after a server start
takes a few seconds (5 million points: about 10 s); later loads come from
the server's memory (under 1 s). Group toggles and colour changes apply at
once. Point layers with more than 128 groups draw every group (toggles are
off).

Drawing order: polygons of all layers, then polylines, then points.

### The inspector

Click a point, a polygon (inside it, or on its outline when the fill is
off) or a polyline to select it; Shift+click adds or removes; click on
empty space or press Esc to clear. Selected objects are outlined in black
and white. The inspector shows each: name (or id), id, layer, group, type,
position (points) or the centre of its bounding box (polygons, polylines)
in pixels and microns, the area and perimeter of a polygon (holes
excluded from the area; the perimeter counts every ring) or the length of
a polyline (in µm² and µm, or px² and px for images without a
calibration), and its attributes (numbers stored with the object;
fetched from the server when selected). "Clear" empties the selection; the
cross on a card removes that object.

## 7. Test data

Keep generated data under `data/` (it is not committed).

A label map that follows the tissue of a real slide (four classes from
colour: background, light tissue, eosin-rich, haematoxylin-rich), with its
class table:

    uv run compass-studio-synth-labels ../data/SLIDE.zarr ../data/SLIDE.tissue.zarr [--from-level 3]

`--from-level` is the pyramid level that is classified (default 3); lower is
finer and slower.

Synthetic annotation stores for testing (points, cells, regions and,
with `--lines`, polylines). To show one on a real slide, give it the
slide's size, its mpp and a name that pairs:

    uv run compass-studio-synth ../data/m6 --name SLIDE.synth --no-arrow --width W --height H --mpp MPP \
        --points 300000 --cells 300000 --regions 30 --region-radius-px 800 6000 --lines 60 --attrs 8
    uv run compass-studio-serve --images ../data --annotations SLIDE.synth=../data/m6/SLIDE.synth.ann.sqlite --web ../web/dist

(`W` and `H` are the slide's size in pixels, printed by the server at
start-up.) Run `uv run compass-studio-synth --help` for all options (image
size, classes, polygon sizes and vertex counts, attributes, seed).

Without any slide at hand:

    uv run compass-studio-serve --synthetic big=200000x200000 --synthetic-labels big.lab=big --web ../web/dist

## 8. Development

- Client with hot reload: start the server with
  `--allow-origin http://127.0.0.1:5173`, then in `web/` run `pnpm dev`
  and open the address it prints, adding `#token=…` from the server.
- `pnpm typecheck` checks the client; `pnpm gen:api` regenerates the
  TypeScript API types from `contract/openapi.yaml`.
- Server and contract tests: in `server/`, `uv run pytest`.
- End-to-end smoke test (headless Firefox; a server running with
  `--web ../web/dist` and at least two images; it also checks label maps,
  points, polygons and polylines when the first image has them: points
  with a layer of 1,000 points or more, polygons with 100 or more, such as
  the store above):

      uv run python ../web/e2e/smoke.py http://127.0.0.1:8765 TOKEN [--shots DIR]

- Benchmark pages: `bench.html` on the same server; see `spikes/README.md`.
- `?debug` in the viewer's address exposes the viewer as `window.__cs` for
  measurements in the browser console.

## 9. Troubleshooting

- **"Session token missing or wrong"**: open the exact URL the server
  printed, including `#token=…`; the token changes at every restart unless
  `COMPASS_STUDIO_TOKEN` is set.
- **"address already in use"**: another server is on that port; stop it or
  use `--port`.
- **A command is not found** (`compass-studio-…`): run `uv sync` in `server/`.
- **The server refuses a label map** ("which image does it belong to?",
  or a size mismatch): name it after its slide or pass `--label-of`
  (section 5); it must be the same size as the slide.
- **The server refuses an annotation store** (size mismatch or "which
  image does it belong to?"): name it after its slide or pass
  `--annotations-of` (section 6).
- **An annotation store does not appear in the Layers tab**: the start-up
  lines show which image it is on ("annotations X (on SLIDE)"); a store
  without "on" pairs with nothing.
- **A label map does not appear**: it shows only with the slide it is
  paired with (see the server's start-up lines); press `l` in case maps are
  hidden.
- **Panning lags or tiles arrive slowly**: check the machine's load first.
  The server and the browser both need CPU; a CPU quota on the user's
  cgroup or a heavy job in the same session slows both. Run batch jobs with
  `nice -n 19`.
