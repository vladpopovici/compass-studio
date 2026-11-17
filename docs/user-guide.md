# CompassStudio user guide

CompassStudio shows COMPASS whole-slide images and their label maps in the
browser. A local Python server reads the files; the browser only talks to
that server. This guide covers every command and the viewer's controls. It
describes what is implemented now (milestones 1 to 7: images, label maps,
point, polygon and polyline annotations with picking, and editing them);
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
| `--allow-origin ORIGIN` | an extra allowed browser origin, e.g. the development server (section 9). |
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
  the selection. With a drawing tool, clicks draw instead (section 7).

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
| `g` | lock the layer of the last selected object as the working layer; again to release it |
| Esc | leave the current edit tool; with the select tool, clear the selection |
| `[` | show or hide the sidebar |
| `]` | show or hide the inspector |

Editing keys (section 7) when the server can edit: `v` select, `e` edit
shapes, `p` polygon, `d` freehand, `r` rectangle, `c` ellipse, `x` point,
`n` line; Delete (or Backspace) deletes the selection; Ctrl+Z undoes,
Ctrl+Shift+Z or Ctrl+Y redoes.

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
which holds editable layers (section 7).

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
- Lock button (right of the layer name): make the layer the working layer.

**Working layer.** When one layer covers another (tissue regions over the
morphology regions or cells inside them), lock the layer you want to work
on: click the lock on its card, or select one of its objects and press
`g`. Its objects are then drawn above every other layer, and they are the
only objects you can hover or select; all other layers are dimmed to 35 %
of their opacity. Locking also shows the layer if it was hidden, drops
selected objects of other layers, and (when the server can edit) sets
"Into" to it. The card gets an accent frame, and a chip "Working in:
layer" appears at the top of the image (in the edit tool bar when editing
is available); its cross, the lock again or `g` releases the layer. There
is one working layer at a time; it follows its layer to an editable copy
and is released when you switch slides.

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

Drawing order: polygons of all layers, then polylines, then points; the
working layer, if one is locked, above all of them.

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
cross on a card removes that object. When the server can edit, the group
and the attributes are editable here (section 7).

## 7. Editing annotations

Editing needs a server started without `--read-only`; the edit tools then
appear at the top of the image. Every action is saved at once (there is no
Save button and nothing to lose); each saved action can be undone.

**Where edits go.** The annotation store itself is never changed. Edits go
to layers in the curation database (`<ID>.curation.sqlite`): new layers,
and editable copies of store layers. A layer with edits not yet saved is
shown as "<name> (editable)" on a dark grey card, with the number of
unsaved edits and two buttons:

- **Save edits**: the edited layer becomes the layer: the card is plain
  "<name>" again, and from then on there is one layer of that name (the
  store's version is no longer listed). Further edits make it
  "(editable)" again until the next save.
- **Discard edits**: after a confirmation, reverts the layer to its last
  save point (everyone's edits since, not only yours). Redo brings them
  back.

Edits are stored as soon as they are made either way: "Save edits" marks
the point that "Discard edits" returns to.

- **New layer**: in the "Into" picker of the edit tools, choose "New
  layer…", give a name and the group names (comma-separated, in order).
  Layer names are unique within a store.
- **Editing a store layer**: the first edit of an object in a read-only
  layer (drawing into it, editing a shape, deleting, changing a group or
  an attribute) asks to make an editable copy. The copy, "<name>
  (editable)", takes the layer's place in the list (the store's version is
  not listed beside it), keeps the objects' ids, groups and style, and
  the edit is applied to it. A layer is copied once. Copying a million
  objects takes about 10 seconds.

**Tools** (hover a tool for help; keys in brackets):

| Tool | Key | Use |
|---|---|---|
| Select | `v` | click to select, Shift+click to add or remove |
| Edit shapes | `e` | edit the selected objects (up to 50, of one layer): drag a vertex; click or drag an edge to add a vertex; Alt+click a vertex to remove it; drag inside an object to move it |
| Polygon | `p` | click the vertices; double-click, Enter or a click on the first vertex finishes; Backspace removes the last vertex; Esc cancels |
| Freehand | `d` | drag to trace an outline (simplified to about one screen pixel) |
| Rectangle | `r` | drag from corner to corner |
| Ellipse | `c` | drag across its bounding box (saved as a 64-sided polygon) |
| Point | `x` | click |
| Line | `n` | click the vertices; double-click or Enter finishes (the length so far is shown) |

New objects go into the layer and group chosen in "Into" (a read-only
layer is copied first, as above) and are selected when saved. "Into"
follows the selection: selecting an object sets it to that object's layer
and group, so the next object drawn goes next to it. With a working layer
locked (section 6), only its objects can be selected and edited, and new
objects go into it.

**Other actions**

- Delete (or Backspace, or the bin button): delete the selected objects.
- Group: change it in the inspector, for one object or, in the "All N
  selected" block, for every selected object of one layer.
- Attributes: edit a value in the inspector and press Enter (an empty
  value removes it; so does the cross that appears on hover); "attribute"
  and "value" below add one (names known to the stores are suggested); the
  "All N selected" block sets one on every selected object.
- Undo (Ctrl+Z) and redo (Ctrl+Shift+Z or Ctrl+Y, or the arrow buttons):
  step back and forth through your saved actions. The history is kept by
  the server, so it survives reloads (per browser). Undo applies the
  inverse of the action to the objects as they are now: if someone else
  changed the same object since, their change can be reverted too.
- Discard changes (tool bar): reverts every edit you made since the slide
  was opened (or the page reloaded), in every layer, after a
  confirmation, as one step; Redo brings them all back. Edits by others,
  and your edits from before opening, are kept (use Undo for those).
  "Discard edits" on a layer card instead returns that layer to its last
  save point.

**Refused edits.** Polygons whose outline crosses itself, has no area or
fewer than 3 points, and lines with fewer than 2 points are not saved; a
message says why and the shape returns to its last saved form.

**Changes by others.** Another program writing the same curation database
(or another browser on the same server) is seen within a second or two:
changed objects are redrawn without a reload.

## 8. Test data

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

## 9. Development

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

      uv run python ../web/e2e/smoke.py http://127.0.0.1:8765 TOKEN [--shots DIR] [--edit]

  `--edit` also tests editing on the store of the largest polygon layer.
  It writes to the curation databases (a new layer per run, a copy of a
  store layer), so start that server with `--sidecar-dir` pointing at a
  scratch directory, never at real curation databases.

- Benchmark pages: `bench.html` on the same server; see `spikes/README.md`.
- `?debug` in the viewer's address exposes the viewer as `window.__cs` for
  measurements in the browser console.

## 10. Troubleshooting

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
- **No edit tools above the image**: the server runs with `--read-only`,
  or the slide has no annotation store; "This store cannot be edited"
  means the same.
- **"Not saved: …"**: the server refused the edit (for example a
  self-crossing outline); nothing was changed.
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
