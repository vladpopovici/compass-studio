# CompassStudio user guide

CompassStudio shows COMPASS whole-slide images and their label maps in the
browser. A local Python server reads the files; the browser only talks to
that server. This guide covers every command and the viewer's controls. It
describes what is implemented now (milestones 1 to 4: images and label maps);
annotations, editing and multichannel images come later.

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
| `--images DIR` | every COMPASS `.zarr` folder directly in `DIR`; id = folder name without `.zarr`. Label maps among them are recognised and paired by themselves (section 5). Repeatable. |
| `--image ID=PATH` | one `.zarr` folder under the id `ID`. Repeatable. |
| `--labels ID=PATH` | one label map (`.zarr`, a single integer channel). Repeatable. |
| `--label-of ID=IMAGE` | the image a label map belongs to, when automatic pairing does not apply. |
| `--classes ID=FILE` | class table (JSON) for a label map; default `<map>.classes.json` next to it. |
| `--annotations ID=PATH` | a COMPASS annotation store (`.ann.sqlite`). Served over the API; the viewer does not show annotations yet (milestone 5). Repeatable. |
| `--sidecar-dir DIR` | where edit databases (`<ID>.curation.sqlite`) go; default next to each store. |
| `--read-only` | no edit databases: annotations cannot be edited. |
| `--synthetic ID=WxH` | a generated test image, e.g. `--synthetic big=200000x200000`. |
| `--synthetic-labels ID=IMAGE` | a generated test label map the size of `IMAGE`. |

### Network and performance

| Option | Meaning |
|---|---|
| `--host HOST`, `--port PORT` | address to listen on; default `127.0.0.1:8765`. |
| `--allow-other-interfaces` | required to listen on anything but 127.0.0.1. Slides may be sensitive: prefer an SSH tunnel (below). |
| `--allow-origin ORIGIN` | an extra allowed browser origin, e.g. the development server (section 7). |
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

The window has a toolbar (image picker, zoom, label maps button), the image,
an optional label maps panel on the right and a status bar.

### Mouse

- Drag: pan.
- Wheel: zoom around the cursor.
- Hover: the status bar shows the cursor position and the label values under it.

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

Keys do nothing while a text field or list has focus.

### Toolbar

- Image picker: the slides being served (label maps are not listed here).
- Zoom out, zoom level, zoom in, fit, 1:1. The zoom shows screen px per
  level-0 px and, when the slide records its objective power, the apparent
  magnification.
- Label maps: opens or closes the panel; shows how many maps the slide has.

### Status bar

From left to right: cursor in level-0 px, cursor in microns, the pyramid
level being shown (`L2/7`), microns per pixel, label values under the
cursor, tile loading (tiles loaded, requests in flight, latency) and a
scale bar.

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

One card per map:

- Eye button: show or hide the map.
- Square button: filled or outlines (class boundaries).
- Opacity slider.
- All / None: show or hide every class.
- One checkbox per class; "Other values" covers values not in the table.

Class toggles take effect at once; nothing is reloaded from the server.

## 6. Test data

Keep generated data under `data/` (it is not committed).

A label map that follows the tissue of a real slide (four classes from
colour: background, light tissue, eosin-rich, haematoxylin-rich), with its
class table:

    uv run compass-studio-synth-labels ../data/SLIDE.zarr ../data/SLIDE.tissue.zarr [--from-level 3]

`--from-level` is the pyramid level that is classified (default 3); lower is
finer and slower.

Synthetic annotation stores for testing (points, cells, regions):

    uv run compass-studio-synth ../data/synth [--name synth] [--points N] [--cells N] [--regions N] ...

Run `uv run compass-studio-synth --help` for all options (image size,
classes, polygon sizes and vertex counts, attributes, seed).

Without any slide at hand:

    uv run compass-studio-serve --synthetic big=200000x200000 --synthetic-labels big.lab=big --web ../web/dist

## 7. Development

- Client with hot reload: start the server with
  `--allow-origin http://127.0.0.1:5173`, then in `web/` run `pnpm dev`
  and open the address it prints, adding `#token=…` from the server.
- `pnpm typecheck` checks the client; `pnpm gen:api` regenerates the
  TypeScript API types from `contract/openapi.yaml`.
- Server and contract tests: in `server/`, `uv run pytest`.
- End-to-end smoke test (headless Firefox; a server running with
  `--web ../web/dist` and at least one image):

      uv run python ../web/e2e/smoke.py http://127.0.0.1:8765 TOKEN [--shots DIR]

- Benchmark pages: `bench.html` on the same server; see `spikes/README.md`.

## 8. Troubleshooting

- **"Session token missing or wrong"**: open the exact URL the server
  printed, including `#token=…`; the token changes at every restart unless
  `COMPASS_STUDIO_TOKEN` is set.
- **"address already in use"**: another server is on that port; stop it or
  use `--port`.
- **A command is not found** (`compass-studio-…`): run `uv sync` in `server/`.
- **The server refuses a label map** ("which image does it belong to?",
  or a size mismatch): name it after its slide or pass `--label-of`
  (section 5); it must be the same size as the slide.
- **A label map does not appear**: it shows only with the slide it is
  paired with (see the server's start-up lines); press `l` in case maps are
  hidden.
- **Panning lags or tiles arrive slowly**: check the machine's load first.
  The server and the browser both need CPU; a CPU quota on the user's
  cgroup or a heavy job in the same session slows both. Run batch jobs with
  `nice -n 19`.
