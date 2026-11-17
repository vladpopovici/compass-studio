"""compass-studio: serve COMPASS slides, their label maps and annotation
stores, with the browser client, on this machine (contract v1).

    compass-studio SLIDE.zarr [MORE ...]   a slide and what lies next to it
    compass-studio DIR                     every slide and store in DIR
"""

import argparse
import os
import secrets
import socket
import threading
import time
import webbrowser
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import uvicorn

from .app import Dataset, create_app
from .imaging.labels import (SyntheticLabels, classes_file_for, load_classes, pair_labels,
                              pair_with_image)
from .imaging.channels import SyntheticChannels, channels_file_for, load_channel_defaults
from .imaging.pyramid import SyntheticPyramid, ZarrPyramid, find_images


def _kv(s: str) -> tuple[str, str]:
    k, sep, v = s.partition("=")
    if not sep or not k:
        raise argparse.ArgumentTypeError("expected ID=VALUE")
    if not all(ch.isalnum() or ch in "_-." for ch in k):
        raise argparse.ArgumentTypeError(f"id {k!r}: use letters, digits, '_', '-', '.'")
    return k, v


def _id(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "_-." else "-" for ch in name)


def find_stores(folder: Path) -> dict[str, Path]:
    """COMPASS annotation stores (``*.ann.sqlite``) directly in ``folder``,
    keyed by the file name without ``.ann.sqlite``."""
    out: dict[str, Path] = {}
    for p in sorted(folder.glob("*.ann.sqlite")):
        if p.is_file():
            out[_id(p.name.removesuffix(".ann.sqlite"))] = p
    return out


def slide_files(slide: Path) -> tuple[dict[str, Path], dict[str, Path]]:
    """A slide (``NAME.zarr``) and what belongs to it by name in its folder:
    images and label maps ``NAME.*.zarr``, stores ``NAME.ann.sqlite`` and
    ``NAME.*.ann.sqlite``. Returns (images, stores), keyed by id."""
    name = slide.name.removesuffix(".zarr")
    images = {_id(name): slide}
    for p in sorted(slide.parent.iterdir()):
        if p.is_dir() and p.name.startswith(name + ".") and p.suffix == ".zarr" and (p / "zarr.json").is_file():
            images[_id(p.name.removesuffix(".zarr"))] = p
    stores = {k: p for k, p in find_stores(slide.parent).items() if k == _id(name) or k.startswith(_id(name) + ".")}
    return images, stores


def bundled_web() -> Path | None:
    """The built client: bundled in the wheel, else ``web/dist`` of a checkout."""
    here = Path(__file__).resolve().parent
    for d in (here / "web", here.parents[2] / "web" / "dist"):
        if (d / "index.html").is_file():
            return d
    return None


def _version() -> str:
    try:
        return version("compass-studio")
    except PackageNotFoundError:
        return "unknown"


def port_busy(host: str, port: int) -> str | None:
    """Why ``host:port`` cannot be listened on, or None (a clear message instead of uvicorn's traceback)."""
    try:
        with socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET) as s:
            s.bind((host, port))
    except OSError as e:
        return e.strerror or str(e)
    return None


def _open_when_up(host: str, port: int, url: str) -> None:
    """Open the browser once the server accepts connections (at most 30 s)."""
    def run() -> None:
        for _ in range(300):
            try:
                with socket.create_connection((host, port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            return
        webbrowser.open(url)
    threading.Thread(target=run, daemon=True).start()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", type=Path, nargs="*", metavar="PATH",
                    help="a slide (NAME.zarr, served with NAME.*.zarr label maps and NAME*.ann.sqlite "
                         "stores next to it), a folder (as --images), or an annotation store (.ann.sqlite)")
    ap.add_argument("--version", action="version", version=f"CompassStudio {_version()}")
    ap.add_argument("--image", type=_kv, action="append", default=[], metavar="ID=PATH",
                    help="COMPASS .zarr folder to serve (repeatable)")
    ap.add_argument("--images", type=Path, action="append", default=[], metavar="DIR",
                    help="serve every COMPASS .zarr folder directly in DIR, id = folder name (repeatable)")
    ap.add_argument("--synthetic", type=_kv, action="append", default=[], metavar="ID=WxH",
                    help="procedural test pyramid, e.g. synth200k=200000x200000")
    ap.add_argument("--synthetic-channels", type=_kv, action="append", default=[], metavar="ID=WxHxN",
                    help="procedural multichannel (uint16) test image, e.g. mc=40000x30000x8")
    ap.add_argument("--channels", type=_kv, action="append", default=[], metavar="ID=FILE",
                    help="channel display defaults (JSON) for a multichannel image "
                         "(default: <image>.channels.json next to it)")
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
    ap.add_argument("--web", type=Path, help="built client directory to serve at / (default: the bundled client)")
    ap.add_argument("--no-web", action="store_true", help="serve the API only")
    ap.add_argument("--open", action="store_true", help="open the viewer in the default browser")
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
    web = None if a.no_web else a.web or bundled_web()
    if web is None and not a.no_web:
        print("  note: no built client found (cd web && pnpm build); serving the API only", flush=True)
    paths = dict(a.image)
    found_stores: dict[str, str] = {}
    seen = {Path(v).resolve() for v in paths.values()}

    def add(kind: str, into: dict[str, str], k: str, p: Path) -> None:
        if p.resolve() in seen:  # the same file reached twice (a slide and its folder)
            return
        if k in into:
            ap.error(f"{kind} id {k!r} given twice; use --image ID=PATH or --annotations ID=PATH to rename one")
        seen.add(p.resolve())
        into[k] = str(p)

    dirs = list(a.images)
    for p in a.paths:
        if p.is_dir() and p.suffix == ".zarr":
            if not (p / "zarr.json").is_file():
                ap.error(f"{p}: not a Zarr v3 image (no zarr.json)")
            imgs, sts = slide_files(p)
            for k, q in imgs.items():
                add("image", paths, k, q)
            for k, q in sts.items():
                add("annotation store", found_stores, k, q)
        elif p.is_dir():
            dirs.append(p)
        elif p.is_file() and p.name.endswith(".ann.sqlite"):
            add("annotation store", found_stores, _id(p.name.removesuffix(".ann.sqlite")), p)
        elif not p.exists():
            ap.error(f"{p}: no such file or folder")
        else:
            ap.error(f"{p}: expected a .zarr slide, a folder or an .ann.sqlite store")
    for d in dirs:
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
            add("annotation store", found_stores, k, v)
        for k, v in found.items():
            add("image", paths, k, v)
    for k, v in a.labels:
        if k in paths:
            ap.error(f"id {k!r} given twice")
        paths[k] = v
    opened = {}
    for k, v in paths.items():
        try:
            opened[k] = ZarrPyramid(v, reader=a.reader)
        except ValueError as e:
            ap.error(f"{k}: {e}")
    for k, _ in a.labels:
        if opened[k].role != "labels":
            ap.error(f"--labels {k}: not a label map (needs one integer channel)")
    # a single integer channel is a label map only when it pairs with an image
    # (--labels, --label-of, or a dotted name: slide / slide.tissue); else a one-channel image
    explicit_maps = {k for k, _ in a.labels} | {k for k, _ in a.label_of}
    for k, p in opened.items():
        if p.role == "labels" and k not in explicit_maps and not any(
                k.startswith(i + ".") and q.role == "image" for i, q in opened.items()):
            p.as_image()
    images = {k: p for k, p in opened.items() if p.role == "image"}
    labels = {k: p for k, p in opened.items() if p.role == "labels"}
    for k, v in a.synthetic:
        w, _, h = v.partition("x")
        images[k] = SyntheticPyramid(int(w), int(h))
    for k, v in a.synthetic_channels:
        try:
            w, h, n = (int(x) for x in v.split("x"))
        except ValueError:
            ap.error(f"--synthetic-channels {k}: expected WxHxN, e.g. 40000x30000x8")
        images[k] = SyntheticChannels(w, h, n)
    explicit_channels = dict(a.channels)
    for k in explicit_channels:
        if k not in images or images[k].display != "channels":
            ap.error(f"--channels {k}: no such multichannel image")
    for k, im in images.items():
        if im.display != "channels":
            continue
        f = explicit_channels.get(k) or (channels_file_for(paths[k]) if k in paths else None)
        if f:
            try:
                im.channel_defaults = load_channel_defaults(f, im.channels, 255 if im.dtype == "uint8" else 65535)
            except (OSError, ValueError) as e:
                ap.error(f"image {k}: {e}")
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
        ap.error("nothing to serve: give a slide (.zarr), a folder, or --image, --synthetic, --annotations")
    busy = port_busy(a.host, a.port)
    if busy:
        ap.error(f"cannot listen on {a.host}:{a.port} ({busy}); another server may be running: "
                 "choose another port with --port")

    token = os.environ.get("COMPASS_STUDIO_TOKEN") or secrets.token_urlsafe(24)
    hosts = {f"{a.host}:{a.port}", f"localhost:{a.port}"}
    origins = {f"http://{h}" for h in hosts} | set(a.allow_origin)
    app = create_app(images, token, hosts, origins, a.spike_results, web, a.spike_files,
                     a.tile_threads, datasets)

    url = f"http://{a.host}:{a.port}/#token={token}"
    print(f"CompassStudio {_version()} on {url}" + ("" if web else " (API only)"), flush=True)
    for k, p in images.items():
        what = (f"labels {k} ({p.dtype}, on {p.aligned_to}, "
                f"{len(p.classes) if p.classes else 'no'} classes)" if p.role == "labels"
                else f"image {k} ({len(p.channels)} channels, {p.dtype})" if p.display == "channels" else f"image {k}")
        print(f"  {what}: {p.width}x{p.height}, {len(p.levels)} levels, tile {p.tile}, "
              f"fast path {sum(p.fast_levels)}/{len(p.levels)} levels", flush=True)
    for k, ds in datasets.items():
        print(f"  annotations {k}" + (f" (on {ds.aligned_to})" if ds.aligned_to else "")
              + f": {len(ds.store.layers)} layers"
              + (f", sidecar {ds.sidecar.path}" if ds.sidecar else ", read-only"), flush=True)
    if a.open and web:
        _open_when_up(a.host, a.port, url)
    uvicorn.run(app, host=a.host, port=a.port, log_level="warning")
