"""``compass-studio-serve``: serve images (and the built client) locally."""

import argparse
import os
import secrets
from pathlib import Path

import uvicorn

from .app import create_app
from .imaging.pyramid import SyntheticPyramid, ZarrPyramid


def _kv(s: str) -> tuple[str, str]:
    k, sep, v = s.partition("=")
    if not sep or not k:
        raise argparse.ArgumentTypeError("expected ID=VALUE")
    return k, v


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--image", type=_kv, action="append", default=[], metavar="ID=PATH",
                    help="COMPASS .zarr folder to serve (repeatable)")
    ap.add_argument("--synthetic", type=_kv, action="append", default=[], metavar="ID=WxH",
                    help="procedural test pyramid, e.g. synth200k=200000x200000")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--allow-other-interfaces", action="store_true",
                    help="required to bind anything but 127.0.0.1 (D10)")
    ap.add_argument("--allow-origin", action="append", default=[],
                    help="extra allowed Origin, e.g. a dev server")
    ap.add_argument("--reader", choices=["auto", "zarr"], default="auto",
                    help="image reader: direct shard reader where supported (auto) or zarr-python")
    ap.add_argument("--tile-threads", type=int, default=None,
                    help="tile read/encode threads (default: available CPUs; 0 = Starlette's pool)")
    ap.add_argument("--web", type=Path, help="built client directory to serve at /")
    ap.add_argument("--spike-files", type=Path,
                    help="serve this directory at /api/v1/spike/files (spike inputs)")
    ap.add_argument("--spike-results", type=Path,
                    help="enable POST /api/v1/spike/results and write them here")
    a = ap.parse_args(argv)

    if a.host != "127.0.0.1" and not a.allow_other_interfaces:
        ap.error("binding to other interfaces needs --allow-other-interfaces")
    if a.web is not None and not (a.web / "index.html").is_file():
        ap.error(f"--web {a.web}: no index.html; build the client first "
                 "(cd web && pnpm install && pnpm build)")
    images = {k: ZarrPyramid(v, reader=a.reader) for k, v in a.image}
    for k, v in a.synthetic:
        w, _, h = v.partition("x")
        images[k] = SyntheticPyramid(int(w), int(h))
    if not images and a.spike_files is None:
        ap.error("nothing to serve: give --image, --synthetic or --spike-files")

    token = os.environ.get("COMPASS_STUDIO_TOKEN") or secrets.token_urlsafe(24)
    hosts = {f"{a.host}:{a.port}", f"localhost:{a.port}"}
    origins = {f"http://{h}" for h in hosts} | set(a.allow_origin)
    app = create_app(images, token, hosts, origins, a.spike_results, a.web, a.spike_files,
                     a.tile_threads)

    print(f"CompassStudio on http://{a.host}:{a.port}/#token={token}", flush=True)
    for k, p in images.items():
        print(f"  {k}: {p.width}x{p.height}, {len(p.levels)} levels, tile {p.tile}, "
              f"fast path {sum(p.fast_levels)}/{len(p.levels)} levels", flush=True)
    uvicorn.run(app, host=a.host, port=a.port, log_level="warning")
