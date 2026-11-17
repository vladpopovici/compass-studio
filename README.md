# CompassStudio

A browser-based viewer and editor for COMPASS whole-slide images and their
annotations. A local Python server reads the slides (Zarr v3 pyramids),
label maps and annotation stores (SQLite); the browser shows them with
deck.gl and talks only to that server, over a documented HTTP contract.

- Pan and zoom through slides of up to about 200,000 x 200,000 px, RGB or
  multichannel (uint8 or uint16, per-channel colour and contrast).
- Label maps over the slide, with per-class colours and toggles.
- Point, polygon and polyline annotations: layers and groups, picking,
  attributes, level of detail for millions of objects.
- Editing: draw and reshape objects, change groups and attributes, undo and
  redo. Edits go to a sidecar database; the COMPASS store is never written.

Everything stays on your machine: the server listens on 127.0.0.1 behind a
per-session token, and the client loads nothing from the network.

## Install

Requirements: Linux (other systems are untested), Python 3.12 or newer,
[uv](https://docs.astral.sh/uv/), and a recent Firefox or Chromium with
WebGL2.

From a wheel (no Node.js needed):

    uv tool install compass_studio-0.1.0-py3-none-any.whl

## Run

    compass-studio /path/to/SLIDE.zarr --open

serves the slide with its label maps (`SLIDE.*.zarr`) and annotation stores
(`SLIDE.ann.sqlite`, `SLIDE.*.ann.sqlite`) found next to it, and opens the
viewer in the browser. `compass-studio /path/to/folder` serves every slide
and store in the folder. `compass-studio --help` lists all options.

## Documentation

- [User guide](docs/user-guide.md): every command, option, key and viewer
  feature.
- [Decision log](docs/decisions.md): design, measurements, rejected
  alternatives, open items.
- [Contract](contract/README.md): the HTTP API (OpenAPI 3.1) and binary
  schemas between client and server.

## Repository

| Folder | Contents |
|---|---|
| `server/` | Python server (FastAPI, uv), test-data generators, tests |
| `web/` | browser client (TypeScript, React, deck.gl, Vite, pnpm), end-to-end test |
| `contract/` | API specification, binary schemas, conformance tests |
| `docs/` | user guide, decision log, spike reports |
| `spikes/` | spike benchmarks (S1 to S6) |

Building the wheel from a checkout needs Node.js and pnpm as well (the
client is built into it):

    cd server && uv build --wheel     # into server/dist/

Development setup: section 9 of the user guide.

## License

MIT; see [LICENSE](LICENSE).
