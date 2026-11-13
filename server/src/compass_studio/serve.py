"""``compass-studio-serve``: serve images, annotation stores (and the built
client) locally, implementing contract v1."""

import argparse
import os
import secrets
from pathlib import Path

import uvicorn

from .app import Dataset, create_app
from .imaging.labels import (SyntheticLabels, classes_file_for, load_classes, pair_labels,
                              pair_with_image)
from .imaging.pyramid import SyntheticPyramid, ZarrPyramid, find_images


def _kv(s: str) -> tuple[str, str]:
    k, sep, v = s.partition("=")
    if not sep or not k:
        raise argparse.ArgumentTypeError("expected ID=VALUE")
    if not all(ch.isalnum() or ch in "_-." for ch in k):
        raise argparse.ArgumentTypeError(f"id {k!r}: use letters, digits, '_', '-', '.'")
    return k, v


def find_stores(folder: Path) -> dict[str, Path]:
    """COMPASS annotation stores (``*.ann.sqlite``) directly in ``folder``,
    keyed by the file name without ``.ann.sqlite``."""
    out: dict[str, Path] = {}
    for p in sorted(folder.glob("*.ann.sqlite")):
        if p.is_file():
            k = "".join(ch if ch.isalnum() or ch in "_-." else "-" for ch in p.name.removesuffix(".ann.sqlite"))
            out[k] = p
    return out


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--image", type=_kv, action="append", default=[], metavar="ID=PATH",
                    help="COMPASS .zarr folder to serve (repeatable)")
    ap.add_argument("--images", type=Path, action="append", default=[], metavar="DIR",
                    help="serve every COMPASS .zarr folder directly in DIR, id = folder name (repeatable)")
    ap.add_argument("--synthetic", type=_kv, action="append", default=[], metavar="ID=WxH",
                    help="procedural test pyramid, e.g. synth200k=200000x200000")
    ap.add_argument("--labels", type=_kv, action="append", default=[], metavar="ID=PATH",
                    help="COMPASS label map (.zarr, one integer channel) to serve (repeatable); "
                         "--images DIR picks these up by themselves")
    ap.add_argument("--label-of", type=_kv, action="append", default=[], metavar="ID=IMAGE",
                    help="the image a label map belongs to (default: the image whose id prefixes "
                         "the map's, as in slide / slide.regions, or the only image)")
    ap.add_argument("--classes", type=_kv, action="append", default=[], metavar="ID=FILE",
                    help="class table (JSON) for a label map (default: <map>.classes.json next to it)")
    ap.add_argument("--synthetic-labels", type=_kv, action="append", default=[], metavar="ID=IMAGE",
                    help="procedural test label map the size of IMAGE")
    ap.add_argument("--annotations", type=_kv, action="append", default=[], metavar="ID=PATH",
                    help="COMPASS annotation store (.ann.sqlite) to serve (repeatable)")
    ap.add_argument("--annotations-of", type=_kv, action="append", default=[], metavar="ID=IMAGE",
                    help="the image an annotation store belongs to (default: the image whose id prefixes "
                         "the store's, as in slide / slide.cells, or the only image if the sizes match)")
    ap.add_argument("--sidecar-dir", type=Path,
                    help="directory for sidecar curation databases, one per store, named "
                         "<ID>.curation.sqlite (default: next to each store)")
    ap.add_argument("--read-only", action="store_true", help="no sidecars: annotations cannot be edited")
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
                    help="serve this directory at /api/spike/files (spike inputs, not part of v1)")
    ap.add_argument("--spike-results", type=Path,
                    help="enable POST /api/spike/results and write them here (not part of v1)")
    a = ap.parse_args(argv)

    if a.host != "127.0.0.1" and not a.allow_other_interfaces:
        ap.error("binding to other interfaces needs --allow-other-interfaces")
    if a.web is not None and not (a.web / "index.html").is_file():
        ap.error(f"--web {a.web}: no index.html; build the client first "
                 "(cd web && pnpm install && pnpm build)")
    paths = dict(a.image)
    found_stores: dict[str, str] = {}
    for d in a.images:
        if not d.is_dir():
            ap.error(f"--images {d}: not a directory")
        try:
            found = find_images(d)
        except ValueError as e:
            ap.error(f"--images {d}: {e}")
        stores_found = find_stores(d)
        if not found and not stores_found:
            ap.error(f"--images {d}: no .zarr images")
        for k, v in stores_found.items():
            if k in found_stores:
                ap.error(f"annotation store id {k!r} given twice")
            found_stores[k] = str(v)
        for k, v in found.items():
            if k in paths:
                ap.error(f"image id {k!r} given twice; use --image ID=PATH to rename one")
            paths[k] = str(v)
    for k, v in a.labels:
        if k in paths:
            ap.error(f"id {k!r} given twice")
        paths[k] = v
    opened = {k: ZarrPyramid(v, reader=a.reader) for k, v in paths.items()}
    for k, _ in a.labels:
        if opened[k].role != "labels":
            ap.error(f"--labels {k}: not a label map (needs one integer channel)")
    images = {k: p for k, p in opened.items() if p.role == "image"}
    labels = {k: p for k, p in opened.items() if p.role == "labels"}
    for k, v in a.synthetic:
        w, _, h = v.partition("x")
        images[k] = SyntheticPyramid(int(w), int(h))
    for k, v in a.synthetic_labels:
        if v not in images:
            ap.error(f"--synthetic-labels {k}: no image {v!r}")
        labels[k] = SyntheticLabels(images[v].width, images[v].height)
    explicit_classes = dict(a.classes)
    for k in explicit_classes:
        if k not in labels:
            ap.error(f"--classes {k}: no such label map")
    for k, lab in labels.items():
        f = explicit_classes.get(k) or (classes_file_for(paths[k]) if k in paths else None)
        if f:
            try:
                lab.classes, lab.background = load_classes(f)
            except (OSError, ValueError) as e:
                ap.error(f"label map {k}: {e}")
    try:
        pair_labels(labels, images, dict(a.label_of) | {k: v for k, v in a.synthetic_labels})
    except ValueError as e:
        ap.error(str(e))
    images |= labels
    datasets = {}
    stores = found_stores | dict(a.annotations)
    for k, v in stores.items():
        store = Path(v)
        sidecar = None
        if not a.read_only:
            d = a.sidecar_dir or store.parent
            d.mkdir(parents=True, exist_ok=True)
            sidecar = d / f"{k}.curation.sqlite"
        datasets[k] = Dataset.open(k, store, sidecar)
    store_of = dict(a.annotations_of)
    for k in store_of:
        if k not in datasets:
            ap.error(f"--annotations-of {k}: no such annotation store")
    slides = {k: p for k, p in images.items() if p.role == "image"}
    for k, ds in datasets.items() if slides else ():
        try:
            ds.aligned_to = pair_with_image("annotation store", k, ds.size(), slides, store_of.get(k),
                                            "--annotations-of")
        except ValueError as e:
            named = k in store_of or any(k.startswith(i + ".") for i in slides)
            if named:
                ap.error(str(e))
            # guessed (no name match): served, but not shown on any image
            print(f"  note: {e}; not shown on an image", flush=True)
    if not images and not datasets and a.spike_files is None:
        ap.error("nothing to serve: give --image, --images, --synthetic, --annotations or --spike-files")

    token = os.environ.get("COMPASS_STUDIO_TOKEN") or secrets.token_urlsafe(24)
    hosts = {f"{a.host}:{a.port}", f"localhost:{a.port}"}
    origins = {f"http://{h}" for h in hosts} | set(a.allow_origin)
    app = create_app(images, token, hosts, origins, a.spike_results, a.web, a.spike_files,
                     a.tile_threads, datasets)

    print(f"CompassStudio on http://{a.host}:{a.port}/#token={token}", flush=True)
    for k, p in images.items():
        what = (f"labels {k} ({p.dtype}, on {p.aligned_to}, "
                f"{len(p.classes) if p.classes else 'no'} classes)" if p.role == "labels" else f"image {k}")
        print(f"  {what}: {p.width}x{p.height}, {len(p.levels)} levels, tile {p.tile}, "
              f"fast path {sum(p.fast_levels)}/{len(p.levels)} levels", flush=True)
    for k, ds in datasets.items():
        print(f"  annotations {k}" + (f" (on {ds.aligned_to})" if ds.aligned_to else "")
              + f": {len(ds.store.layers)} layers"
              + (f", sidecar {ds.sidecar.path}" if ds.sidecar else ", read-only"), flush=True)
    uvicorn.run(app, host=a.host, port=a.port, log_level="warning")
