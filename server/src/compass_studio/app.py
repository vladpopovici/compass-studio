"""HTTP server implementing contract v1 (``contract/openapi.yaml``).

Security (D10): binds to 127.0.0.1 unless told otherwise, requires a
per-session bearer token on ``/api``, checks Host and Origin, no CORS.

Tile work (read + encode) runs on a dedicated pool of ``tile_threads``
threads, one per available CPU by default: against Starlette's default
40-thread pool it gave about 15 % more throughput and kept server-side read
and encode times flat under load (S1, 2026-11-13). ``tile_threads=0``
restores Starlette's pool for comparison.

Annotation stores are served together with their sidecar (one sidecar per
store, D8). Layer ids on the wire are strings: ``{store}:{layer}`` for the
read-only store and ``{store}:edit:{layer}`` for sidecar layers. Edits run on
one thread per sidecar. A background thread polls each sidecar's
``PRAGMA data_version`` (S6) and publishes revision events (``/events``).

Spike-only endpoints live under ``/api/spike`` and are not part of v1.
"""

import asyncio
import io
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import zlib
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image
from starlette.concurrency import run_in_threadpool

from .annot.sidecar import Conflict, Sidecar, saved_revision, unsaved_entries
from .annot.store import (CENTROID_TILE_VERSION, ID_LIMIT, LINE_LAYER_VERSION, POINT_LAYER_VERSION,
                          POLYGON_TILE_VERSION, AnnotationStore, centroid_tile, encode_ipc, line_layer,
                          point_layer, polygon_tile)
from .imaging.pyramid import Pyramid
from .imaging.tilecache import TileCache

API_VERSION = "1.7.0"
TILE = 512
POLYGON_MAX_LEVEL = 3  # S4: polygons up to level 3, centroids above
ARROW = "application/vnd.apache.arrow.stream"
_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_MEDIA = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp", "bin": "application/octet-stream"}
_FORMATS = {"image": {"jpg", "webp", "png"}, "labels": {"bin", "png"}, "channels": {"bin"}}
HISTOGRAM_PIXELS = 1 << 20  # histograms come from the finest level of at most this many pixels
PERCENTILES = (0.1, 0.5, 1, 2, 5, 50, 95, 98, 99, 99.5, 99.8, 99.9, 99.99)


class Guard:
    """Pure ASGI middleware: Host, Origin and bearer-token checks (D10)."""

    def __init__(self, app, token: str, allowed_hosts: set[str], allowed_origins: set[str]):
        self.app, self.auth = app, f"Bearer {token}".encode()
        self.hosts = {h.encode() for h in allowed_hosts}
        self.origins = {o.encode() for o in allowed_origins}

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        h = dict(scope["headers"])
        if h.get(b"host", b"") not in self.hosts:
            return await _reject(send, 421, b"bad host")
        origin = h.get(b"origin")
        if origin is not None and origin not in self.origins:
            return await _reject(send, 403, b"bad origin")
        if scope["path"].startswith("/api/") and \
                not secrets.compare_digest(h.get(b"authorization", b""), self.auth):
            return await _reject(send, 401, b"unauthorized")
        return await self.app(scope, receive, send)


async def _reject(send, status: int, detail: bytes) -> None:
    body = b'{"detail":"' + detail + b'"}'
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


def default_tile_threads() -> int:
    return len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else (os.cpu_count() or 4)


@dataclass
class Dataset:
    """One annotation store, optionally with its sidecar."""
    id: str
    store: AnnotationStore
    sidecar: Sidecar | None = None
    view: AnnotationStore | None = None          # read-only connections to the sidecar file
    edit_pool: ThreadPoolExecutor | None = None
    cache: dict = field(default_factory=dict)    # (layer id, kind, revision) -> whole-layer Arrow bytes
    aligned_to: str | None = None                # id of the image it annotates

    def size(self) -> tuple[int, int] | None:
        w, h = _int(self.store.meta.get("image_width")), _int(self.store.meta.get("image_height"))
        return (w, h) if w and h else None

    @classmethod
    def open(cls, store_id: str, store_path: Path, sidecar_path: Path | None) -> "Dataset":
        ds = cls(store_id, AnnotationStore(store_path))
        if sidecar_path is not None:
            ds.sidecar = Sidecar(sidecar_path, source=store_path)
            ds.view = AnnotationStore(sidecar_path)
            ds.edit_pool = ThreadPoolExecutor(1, thread_name_prefix=f"edit-{store_id}")
        return ds

    def revision(self) -> int:
        return self.view.sync() if self.view else 0


def _split_layer(layer_id: str) -> tuple[str, bool, int]:
    parts = layer_id.split(":")
    try:
        if len(parts) == 2:
            return parts[0], False, int(parts[1])
        if len(parts) == 3 and parts[1] == "edit":
            return parts[0], True, int(parts[2])
    except ValueError:
        pass
    raise HTTPException(404, "no such layer")


def create_app(images: dict[str, Pyramid], token: str, allowed_hosts: set[str],
               allowed_origins: set[str], results_dir: Path | None = None,
               web_dir: Path | None = None, spike_files: Path | None = None,
               tile_threads: int | None = None,
               datasets: dict[str, Dataset] | None = None) -> FastAPI:
    datasets = datasets or {}
    subscribers: set[asyncio.Queue] = set()
    stop = threading.Event()

    @asynccontextmanager
    async def lifespan(_app):
        loop = asyncio.get_running_loop()
        watched = [d for d in datasets.values() if d.sidecar is not None]
        t = None
        if watched:
            t = threading.Thread(target=_watch, args=(watched, loop, subscribers, stop),
                                 name="revision-watch", daemon=True)
            t.start()
        yield
        stop.set()
        if t:
            t.join(2)

    app = FastAPI(title="CompassStudio", version=API_VERSION, docs_url=None, redoc_url=None,
                  openapi_url=None, lifespan=lifespan)
    app.add_middleware(Guard, token=token, allowed_hosts=allowed_hosts, allowed_origins=allowed_origins)
    if tile_threads is None:
        tile_threads = default_tile_threads()
    pool = ThreadPoolExecutor(tile_threads, thread_name_prefix="tile") if tile_threads else None

    tiles = TileCache()
    histograms: dict[tuple[str, str], dict] = {}
    app.state.tiles = tiles

    async def in_pool(fn):
        if pool is not None:
            return await asyncio.get_running_loop().run_in_executor(pool, fn)
        return await run_in_threadpool(fn)

    # -- info -------------------------------------------------------------

    @app.get("/api/v1/info")
    def info():
        caps = ["images", "tiles.jpg", "tiles.png", "tiles.webp", "labels", "tiles.bin", "channels", "annotations", "lines",
                "tiles.area", "events"]
        if any(d.sidecar for d in datasets.values()):
            caps += ["edit", "save"]
        return {"api_version": API_VERSION, "capabilities": caps,
                "schemas": {"point-layer": [POINT_LAYER_VERSION], "polygon-tile": [POLYGON_TILE_VERSION],
                            "centroid-tile": [CENTROID_TILE_VERSION], "line-layer": [LINE_LAYER_VERSION]}}

    # -- images -----------------------------------------------------------

    def _image(image_id: str) -> Pyramid:
        try:
            return images[image_id]
        except KeyError:
            raise HTTPException(404, "no such image") from None

    @app.get("/api/v1/images")
    def list_images():
        return [{"id": k, **p.info()} for k, p in images.items()]

    @app.get("/api/v1/images/{image_id}")
    def image_info(image_id: str):
        return {"id": image_id, **_image(image_id).info()}

    @app.get("/api/v1/images/{image_id}/tiles/{level}/{col}/{row}.{fmt}")
    async def image_tile(request: Request, image_id: str, level: int, col: int, row: int, fmt: str,
                         q: int = 90, v: str | None = None, channel: int | None = None):
        p = _image(image_id)
        kind = "channels" if p.role == "image" and p.display == "channels" else p.role
        if fmt not in _FORMATS[kind] or not 1 <= q <= 100:
            raise HTTPException(400, f"format: {kind} tiles come as {', '.join(sorted(_FORMATS[kind]))}")
        if (kind == "channels") != (channel is not None):
            raise HTTPException(400, "channel: required for multichannel images, only for them")
        if channel is not None and not 0 <= channel < len(p.channels):
            raise HTTPException(400, f"channel: 0 to {len(p.channels) - 1}")
        if fmt == "png" and p.role == "labels" and p.dtype not in ("uint8", "uint16"):
            raise HTTPException(400, "format: png only for 8 and 16-bit labels")
        gz = fmt == "bin" and "gzip" in request.headers.get("accept-encoding", "")
        etag = f'"{p.version}-gz"' if gz else f'"{p.version}"'
        # with ?v= matching the image version the URL names fixed content
        cache = "private, max-age=31536000, immutable" if v == p.version else "private, no-cache"
        if p.version and f'"{p.version}' in request.headers.get("if-none-match", ""):
            try:
                p.tile_shape(level, col, row)
            except IndexError:
                raise HTTPException(404, "no such tile") from None
            return Response(status_code=304, headers={"ETag": etag, "Cache-Control": cache})

        def work():
            t0 = time.perf_counter()
            if kind == "channels":  # every channel of the tile, read once for all channel requests
                arr = tiles.get((image_id, p.version, level, col, row), lambda: p.read_tile(level, col, row))
                arr = arr if arr.ndim == 2 else arr[..., channel]
            else:
                arr = p.read_tile(level, col, row)
            t1 = time.perf_counter()
            body = _encode(arr, fmt, q) if kind == "image" else _encode_labels(arr, fmt, p.dtype, gz)
            return body, t1 - t0, time.perf_counter() - t1

        try:
            body, tr, te = await in_pool(work)
        except IndexError:
            raise HTTPException(404, "no such tile") from None
        headers = {"Cache-Control": cache, "ETag": etag,
                   "Server-Timing": f"read;dur={tr * 1e3:.2f}, encode;dur={te * 1e3:.2f}"}
        if fmt == "bin":
            headers["Vary"] = "Accept-Encoding"
            if gz:
                headers["Content-Encoding"] = "gzip"
        return Response(body, media_type=_MEDIA[fmt], headers=headers)

    @app.get("/api/v1/images/{image_id}/histogram")
    async def image_histogram(image_id: str):
        p = _image(image_id)
        if p.role != "image" or p.display != "channels":
            raise HTTPException(400, "histograms are for multichannel images")
        key = (image_id, p.version)
        if key not in histograms:
            histograms[key] = await in_pool(lambda: _histogram(p))
        return histograms[key]

    # -- stores and layers ------------------------------------------------

    def _dataset(store_id: str) -> Dataset:
        try:
            return datasets[store_id]
        except KeyError:
            raise HTTPException(404, "no such store") from None

    def _resolve(layer_id: str) -> tuple[Dataset, AnnotationStore, int, bool]:
        sid, editable, lid = _split_layer(layer_id)
        ds = _dataset(sid)
        view = ds.view if editable else ds.store
        if view is None:
            raise HTTPException(404, "no such layer")
        if editable:
            view.sync()
        if lid not in view.layers:
            raise HTTPException(404, "no such layer")
        return ds, view, lid, editable

    def _store_info(ds: Dataset) -> dict:
        m = ds.store.meta
        return {"id": ds.id, "name": m.get("name") or ds.id,
                "image_width": _int(m.get("image_width")), "image_height": _int(m.get("image_height")),
                "mpp": _float(m.get("mpp")), "editable": ds.sidecar is not None, "revision": ds.revision(),
                "aligned_to": ds.aligned_to}

    def _layer_info(ds: Dataset, view: AnnotationStore, lid: int, editable: bool) -> dict:
        forked = None
        if editable:
            row = view._conn.execute("SELECT source_layer_id FROM cs_forks WHERE layer_id = ?", (lid,)).fetchone()
            forked = f"{ds.id}:{row[0]}" if row else None
        return {"id": f"{ds.id}:edit:{lid}" if editable else f"{ds.id}:{lid}", "store_id": ds.id,
                "name": view.layers[lid], "editable": editable, "forked_from": forked,
                "groups": [{"id": g.id, "name": g.name, "index": g.index} for g in view.groups[lid]],
                "counts": view.counts(lid), "polygon_max_level": POLYGON_MAX_LEVEL,
                "revision": ds.revision() if editable else 0,
                **({"saved_revision": saved_revision(view._conn, lid),
                    "unsaved": len(unsaved_entries(view._conn, lid))} if editable else {})}

    @app.get("/api/v1/stores")
    def list_stores():
        return [_store_info(d) for d in datasets.values()]

    @app.get("/api/v1/stores/{store_id}")
    def store_info(store_id: str):
        return _store_info(_dataset(store_id))

    @app.get("/api/v1/stores/{store_id}/attributes")
    async def store_attributes(store_id: str):
        ds = _dataset(store_id)

        def work():
            names = ds.store.attribute_names()
            if ds.view:
                names += [n for n in ds.view.attribute_names() if n not in names]
            return {"store_id": store_id, "attributes": names}
        return await in_pool(work)

    @app.get("/api/v1/layers")
    async def list_layers(store: str | None = None):
        chosen = [_dataset(store)] if store else list(datasets.values())

        def work():
            out = []
            for ds in chosen:
                out += [_layer_info(ds, ds.store, lid, False) for lid in ds.store.layers]
                if ds.view:
                    ds.view.sync()
                    out += [_layer_info(ds, ds.view, lid, True) for lid in ds.view.layers]
            return out
        return await in_pool(work)

    @app.get("/api/v1/layers/{layer_id}")
    async def layer_info(layer_id: str):
        ds, view, lid, editable = _resolve(layer_id)
        return await in_pool(lambda: _layer_info(ds, view, lid, editable))

    async def _whole_layer(kind: str, layer_id: str, build) -> Response:
        """A whole-layer Arrow payload (points, lines), cached in memory per
        (layer, kind, revision). ``build(view, lid, meta) -> RecordBatch``."""
        ds, view, lid, editable = _resolve(layer_id)
        rev = ds.revision() if editable else 0

        def work():
            key = (layer_id, kind, rev)
            body = ds.cache.get(key)
            if body is None:
                body = encode_ipc(build(view, lid, dict(layer_id=layer_id, revision=rev)))
                for k in [k for k in ds.cache if k[:2] == (layer_id, kind)]:
                    del ds.cache[k]
                ds.cache[key] = body
            return body
        body = await in_pool(work)
        return Response(body, media_type=ARROW, headers={"ETag": f'"{layer_id}@{rev}"',
                                                         "Cache-Control": "private, no-cache"})

    def _points_batch(view, lid, meta):
        ids, gids, xy = view.points(lid)
        return point_layer(ids, view.group_index(lid, gids), xy, **meta)

    def _lines_batch(view, lid, meta):
        ids, gids, xy, starts = view.polylines(lid)
        return line_layer(ids, view.group_index(lid, gids), xy, starts, **meta)

    @app.get("/api/v1/layers/{layer_id}/points")
    async def layer_points(layer_id: str):
        return await _whole_layer("points", layer_id, _points_batch)

    @app.get("/api/v1/layers/{layer_id}/lines")
    async def layer_lines(layer_id: str):
        return await _whole_layer("lines", layer_id, _lines_batch)

    @app.get("/api/v1/layers/{layer_id}/tiles/{level}/{col}/{row}")
    async def layer_tile(layer_id: str, level: int, col: int, row: int,
                         min_area: float | None = None, max_area: float | None = None):
        ds, view, lid, editable = _resolve(layer_id)
        if min_area is not None and max_area is not None:
            raise HTTPException(400, "min_area and max_area are exclusive")
        if any(a is not None and not 0 <= a < float("inf") for a in (min_area, max_area)):
            raise HTTPException(400, "areas must be finite and non-negative")
        if not 0 <= level <= 30 or col < 0 or row < 0:
            raise HTTPException(404, "no such tile")
        side = TILE * 2 ** level
        w, h = _int(ds.store.meta.get("image_width")), _int(ds.store.meta.get("image_height"))
        if (w and col * side >= w) or (h and row * side >= h):
            raise HTTPException(404, "no such tile")
        bbox = (col * side, row * side, (col + 1) * side, (row + 1) * side)
        rev = ds.revision() if editable else 0
        meta = dict(layer_id=layer_id, revision=rev, level=level, col=col, row=row)

        def work():
            # min_area: polygons at any level; max_area: centroids at any level
            if min_area is not None or (max_area is None and level <= POLYGON_MAX_LEVEL):
                b = view.polygons(lid, bbox, min_area=min_area)
                return encode_ipc(polygon_tile(b, view.group_index(lid, b.group_ids), **meta))
            ids, gids, xy = view.centroids(lid, bbox, max_area=max_area)
            return encode_ipc(centroid_tile(ids, view.group_index(lid, gids), xy, **meta))
        body = await in_pool(work)
        cache = "private, no-cache" if editable else "private, max-age=3600"
        return Response(body, media_type=ARROW, headers={"ETag": f'"{layer_id}@{rev}"', "Cache-Control": cache})

    def _ids(ids: str) -> list[int]:
        try:
            out = [int(x) for x in ids.split(",") if x]
        except ValueError:
            raise HTTPException(400, "ids must be integers") from None
        if not 0 < len(out) <= 1000 or any(not 0 <= i < ID_LIMIT for i in out):
            raise HTTPException(400, "1 to 1000 ids in [0, 2^53)")
        return out

    @app.get("/api/v1/layers/{layer_id}/features")
    async def layer_features(layer_id: str, ids: str):
        ds, view, lid, editable = _resolve(layer_id)
        oids = _ids(ids)
        rev = ds.revision() if editable else 0
        feats = await in_pool(lambda: view.features(lid, oids))
        return {"type": "FeatureCollection", "layer_id": layer_id, "revision": rev, "features": feats}

    @app.get("/api/v1/layers/{layer_id}/objects")
    async def layer_objects(layer_id: str, ids: str):
        ds, view, lid, editable = _resolve(layer_id)
        oids = _ids(ids)
        objs = await in_pool(lambda: view.objects(lid, oids))
        return {"layer_id": layer_id, "objects": objs}

    # -- editing ----------------------------------------------------------

    def _editable(store_id: str) -> Dataset:
        ds = _dataset(store_id)
        if ds.sidecar is None:
            raise HTTPException(403, "store is not editable")
        return ds

    async def _edit(ds: Dataset, fn):
        return await asyncio.get_running_loop().run_in_executor(ds.edit_pool, fn)

    @app.get("/api/v1/stores/{store_id}/revision")
    async def store_revision(store_id: str):
        ds = _editable(store_id)
        return {"store_id": store_id, "revision": await _edit(ds, ds.sidecar.revision)}

    @app.get("/api/v1/stores/{store_id}/log")
    async def store_log(store_id: str, since: int = 0, limit: int = 100):
        ds = _editable(store_id)
        if not 1 <= limit <= 1000:
            raise HTTPException(400, "limit must be 1 to 1000")
        return {"store_id": store_id, "entries": await _edit(ds, lambda: ds.sidecar.log(since, limit))}

    @app.post("/api/v1/stores/{store_id}/commit")
    async def store_commit(store_id: str, request: Request):
        ds = _editable(store_id)
        body = await _json(request)
        try:
            base, ops = int(body["base_revision"]), body["ops"]
            if not isinstance(ops, list):
                raise TypeError("ops must be a list")
            c = await _edit(ds, lambda: ds.sidecar.apply(base, ops, client=body.get("client")))
        except Conflict as e:
            return JSONResponse({"detail": "conflict", "current_revision": e.current}, status_code=409)
        except (KeyError, ValueError, TypeError) as e:
            return JSONResponse({"detail": f"invalid operations: {e}"}, status_code=422)
        return {"store_id": store_id, "revision": c.revision, "created": c.object_ids}

    @app.post("/api/v1/stores/{store_id}/layers", status_code=201)
    async def create_layer(store_id: str, request: Request):
        ds = _editable(store_id)
        body = await _json(request)
        name, groups = body.get("name"), body.get("groups")
        if not isinstance(name, str) or not name or not isinstance(groups, list) or \
                not all(isinstance(g, str) and g for g in groups) or len(set(groups)) != len(groups):
            return JSONResponse({"detail": "name and a list of distinct group names required"}, status_code=422)
        try:
            lid, _ = await _edit(ds, lambda: ds.sidecar.create_layer(name, groups))
        except sqlite3.IntegrityError:
            return JSONResponse({"detail": f"a layer named {name!r} exists"}, status_code=422)
        ds.view.sync()
        return _layer_info(ds, ds.view, lid, True)

    @app.post("/api/v1/layers/{layer_id}/fork", status_code=201)
    async def fork_layer(layer_id: str, request: Request):
        ds, view, lid, editable = _resolve(layer_id)
        if editable:
            raise HTTPException(400, "only store layers can be forked")
        if ds.sidecar is None:
            raise HTTPException(403, "store is not editable")
        body = await _json(request, allow_empty=True)
        name = body.get("name")
        if name is not None and (not isinstance(name, str) or not name):
            return JSONResponse({"detail": "name must be a non-empty string"}, status_code=422)
        try:
            new = await _edit(ds, lambda: ds.sidecar.fork_layer(ds.store.path, lid, name))
        except ValueError as e:
            return JSONResponse({"detail": str(e)}, status_code=422)
        ds.view.sync()
        return _layer_info(ds, ds.view, new, True)

    def _editable_layer_id(layer_id: str) -> tuple[Dataset, int]:
        ds, view, lid, editable = _resolve(layer_id)
        if not editable:
            raise HTTPException(400, "only editable layers have save points")
        if ds.sidecar is None:
            raise HTTPException(403, "store is not editable")
        return ds, lid

    @app.post("/api/v1/layers/{layer_id}/save")
    async def save_layer(layer_id: str):
        ds, lid = _editable_layer_id(layer_id)
        await _edit(ds, lambda: ds.sidecar.save_layer(lid))
        ds.view.sync()
        return _layer_info(ds, ds.view, lid, True)

    @app.post("/api/v1/layers/{layer_id}/discard")
    async def discard_layer(layer_id: str, request: Request):
        ds, lid = _editable_layer_id(layer_id)
        body = await _json(request, allow_empty=True)
        try:
            c, reverted = await _edit(ds, lambda: ds.sidecar.discard_layer(lid, client=body.get("client")))
        except (KeyError, ValueError, TypeError) as e:
            return JSONResponse({"detail": f"cannot discard: {e}"}, status_code=422)
        ds.view.sync()
        return {"layer_id": layer_id, "revision": c.revision if c else ds.revision(), "reverted": reverted}

    # -- events -----------------------------------------------------------

    @app.get("/api/v1/events")
    async def events(request: Request):
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        subscribers.add(q)
        hello = {"api_version": API_VERSION,
                 "revisions": {d.id: d.revision() for d in datasets.values() if d.sidecar}}

        async def stream():
            try:
                yield f"event: hello\ndata: {json.dumps(hello)}\n\n"
                while not await request.is_disconnected():
                    try:
                        ev = await asyncio.wait_for(q.get(), timeout=15)
                        yield f"event: revision\ndata: {json.dumps(ev)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keep-alive\n\n"
            finally:
                subscribers.discard(q)
        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache"})

    # -- spike-only (not part of v1) --------------------------------------

    if results_dir is not None:
        @app.post("/api/spike/results/{name}")
        async def spike_results(name: str, request: Request):
            if not _NAME.match(name):
                raise HTTPException(400, "name")
            data = await request.json()
            results_dir.mkdir(parents=True, exist_ok=True)
            out = results_dir / f"{time.strftime('%Y%m%dT%H%M%S')}-{name}.json"
            out.write_text(json.dumps(data, indent=1))
            return {"saved": out.name}

    if spike_files is not None:  # spike inputs (synthetic Arrow files); token-gated
        app.mount("/api/spike/files", StaticFiles(directory=spike_files), name="spike-files")

    if web_dir is not None:  # built client, same origin as the API
        app.mount("/", StaticFiles(directory=web_dir, html=True), name="web")

    return app


def _watch(datasets: list[Dataset], loop, subscribers: set, stop: threading.Event, period: float = 0.05):
    """Poll each sidecar's data_version on private connections; publish
    revision changes (from this server or from another process, S6)."""
    conns = {d.id: sqlite3.connect(d.sidecar.path.resolve().as_uri() + "?mode=ro", uri=True)
             for d in datasets}
    last = {sid: (c.execute("PRAGMA data_version").fetchone()[0],
                  c.execute("SELECT revision FROM cs_state WHERE id = 1").fetchone()[0])
            for sid, c in conns.items()}
    try:
        while not stop.wait(period):
            for sid, c in conns.items():
                dv = c.execute("PRAGMA data_version").fetchone()[0]
                if dv == last[sid][0]:
                    continue
                rev = c.execute("SELECT revision FROM cs_state WHERE id = 1").fetchone()[0]
                if rev != last[sid][1]:
                    client = c.execute("SELECT client FROM cs_oplog WHERE revision = ?", (rev,)).fetchone()
                    ev = {"store_id": sid, "revision": rev, "client": client[0] if client else None}
                    for q in list(subscribers):
                        loop.call_soon_threadsafe(_offer, q, ev)
                last[sid] = (dv, rev)
    finally:
        for c in conns.values():
            c.close()


def _offer(q: asyncio.Queue, ev: dict) -> None:
    try:
        q.put_nowait(ev)
    except asyncio.QueueFull:  # slow client: drop; it can resync from /revision
        pass


async def _json(request: Request, allow_empty: bool = False) -> dict:
    raw = await request.body()
    if not raw and allow_empty:
        return {}
    try:
        body = json.loads(raw)
    except ValueError:
        raise HTTPException(400, "body must be JSON") from None
    if not isinstance(body, dict):
        raise HTTPException(400, "body must be a JSON object")
    return body


def _int(v) -> int | None:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _float(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _histogram(p: Pyramid) -> dict:
    """Per channel: range, percentiles and 256 counts over [min, max], from the
    finest level of at most ``HISTOGRAM_PIXELS`` pixels (the coarsest if none)."""
    lv = next((i for i, L in enumerate(p.levels) if L.width * L.height <= HISTOGRAM_PIXELS), len(p.levels) - 1)
    L, t = p.levels[lv], p.tile
    data = np.empty((L.height, L.width, len(p.channels)), np.dtype(p.dtype))
    for row in range(-(-L.height // t)):
        for col in range(-(-L.width // t)):
            a = p.read_tile(lv, col, row)
            data[row * t:row * t + a.shape[0], col * t:col * t + a.shape[1]] = a.reshape(a.shape[0], a.shape[1], -1)
    out = []
    for c in range(data.shape[2]):
        v = data[..., c].ravel()
        lo, hi = int(v.min()), int(v.max())
        counts, _ = np.histogram(v, bins=256, range=(lo, hi if hi > lo else lo + 1))
        pct = np.percentile(v, PERCENTILES)
        out.append({"min": lo, "max": hi, "counts": counts.tolist(),
                    "percentiles": {f"{q:g}": float(x) for q, x in zip(PERCENTILES, pct)}})
    return {"level": lv, "channels": out}


def _encode_labels(arr: np.ndarray, fmt: str, dtype: str, gz: bool) -> bytes:
    """Label tile: raw little-endian values, row-major (gzip-compressed if
    the client accepts it), or a grayscale PNG for 8 and 16-bit labels."""
    if fmt == "png":
        im = Image.fromarray(np.ascontiguousarray(arr.astype(np.uint16) if dtype == "uint16" else arr))
        buf = io.BytesIO()
        im.save(buf, "PNG", compress_level=1)
        return buf.getvalue()
    raw = np.ascontiguousarray(arr, dtype=np.dtype(dtype).newbyteorder("<")).tobytes()
    if not gz:
        return raw
    c = zlib.compressobj(1, zlib.DEFLATED, 31)  # level 1; wbits 31 = gzip container
    return c.compress(raw) + c.flush()


def _encode(arr: np.ndarray, fmt: str, q: int) -> bytes:
    im = Image.fromarray(np.ascontiguousarray(arr[..., :3] if arr.shape[-1] >= 3 else arr[..., 0]))
    buf = io.BytesIO()
    if fmt == "jpg":
        im.save(buf, "JPEG", quality=q)
    elif fmt == "webp":  # lossless; method 0 is fastest and beats PNG on size (S1)
        im.save(buf, "WEBP", lossless=True, method=0, quality=0)
    else:
        im.save(buf, "PNG", compress_level=1)
    return buf.getvalue()
