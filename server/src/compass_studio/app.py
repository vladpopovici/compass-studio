"""HTTP server (draft; the contract is not frozen until the spikes are done).

Security (D10): binds to 127.0.0.1 unless told otherwise, requires a
per-session bearer token on ``/api``, checks Host and Origin, no CORS.

Tile work (read + encode) runs on a dedicated pool of ``tile_threads``
threads, one per available CPU by default: against Starlette's default
40-thread pool it gave about 15 % more throughput and kept server-side read
and encode times flat under load (S1, 2026-11-13). ``tile_threads=0``
restores Starlette's pool for comparison.
"""

import asyncio
import io
import json
import os
import re
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image
from starlette.concurrency import run_in_threadpool

from .annot.sidecar import Conflict, Sidecar
from .imaging.pyramid import Pyramid

API_VERSION = "0.0.0"
_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_MEDIA = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}


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


def create_app(images: dict[str, Pyramid], token: str, allowed_hosts: set[str],
               allowed_origins: set[str], results_dir: Path | None = None,
               web_dir: Path | None = None, spike_files: Path | None = None,
               tile_threads: int | None = None, sidecar: Sidecar | None = None) -> FastAPI:
    app = FastAPI(title="CompassStudio", version=API_VERSION, docs_url=None,
                  redoc_url=None, openapi_url=None)
    app.add_middleware(Guard, token=token, allowed_hosts=allowed_hosts,
                       allowed_origins=allowed_origins)
    if tile_threads is None:
        tile_threads = default_tile_threads()
    pool = ThreadPoolExecutor(tile_threads, thread_name_prefix="tile") if tile_threads else None

    @app.get("/api/v1/info")
    def info():
        caps = ["tiles.jpeg", "tiles.png", "tiles.webp"]
        if results_dir is not None:
            caps.append("spike.results")
        return {"api_version": API_VERSION, "capabilities": caps, "schemas": {}}

    @app.get("/api/v1/images")
    def list_images():
        return [{"id": k, **p.info()} for k, p in images.items()]

    def _image(image_id: str) -> Pyramid:
        try:
            return images[image_id]
        except KeyError:
            raise HTTPException(404, "no such image") from None

    @app.get("/api/v1/images/{image_id}")
    def image_info(image_id: str):
        return {"id": image_id, **_image(image_id).info()}

    @app.get("/api/v1/images/{image_id}/tiles/{level}/{col}/{row}.{fmt}")
    async def tile(image_id: str, level: int, col: int, row: int, fmt: str, q: int = 90):
        p = _image(image_id)
        if fmt not in _MEDIA or not 1 <= q <= 100:
            raise HTTPException(400, "format")

        def work():
            t0 = time.perf_counter()
            arr = p.read_tile(level, col, row)
            t1 = time.perf_counter()
            body = _encode(arr, fmt, q)
            return body, t1 - t0, time.perf_counter() - t1

        try:
            if pool is not None:
                body, tr, te = await asyncio.get_running_loop().run_in_executor(pool, work)
            else:
                body, tr, te = await run_in_threadpool(work)
        except IndexError:
            raise HTTPException(404, "no such tile") from None
        return Response(body, media_type=_MEDIA[fmt],
                        headers={"Cache-Control": "private, max-age=3600",
                                 "Server-Timing": f"read;dur={tr * 1e3:.2f}, encode;dur={te * 1e3:.2f}"})

    if sidecar is not None:
        # Draft edit API (D8, spike S5). One sidecar per server; writes are
        # serialised by SQLite (BEGIN IMMEDIATE) and checked against
        # base_revision.
        edit_pool = ThreadPoolExecutor(1, thread_name_prefix="edit")

        async def in_edit_thread(fn):
            return await asyncio.get_running_loop().run_in_executor(edit_pool, fn)

        @app.get("/api/v1/edit/revision")
        async def edit_revision():
            return {"revision": await in_edit_thread(sidecar.revision)}

        @app.get("/api/v1/edit/objects")
        async def edit_objects(ids: str):
            try:
                oids = [int(x) for x in ids.split(",") if x]
            except ValueError:
                raise HTTPException(400, "ids") from None
            if not 0 < len(oids) <= 1000:
                raise HTTPException(400, "1 to 1000 ids")
            return await in_edit_thread(lambda: sidecar.features(oids))

        @app.post("/api/v1/edit/commit")
        async def edit_commit(request: Request):
            body = await request.json()
            try:
                c = await in_edit_thread(lambda: sidecar.apply(int(body["base_revision"]), body["ops"],
                                                               client=body.get("client")))
            except Conflict as e:
                return JSONResponse({"detail": "conflict", "current_revision": e.current}, status_code=409)
            except (KeyError, ValueError, TypeError) as e:
                raise HTTPException(400, str(e)) from None
            return {"revision": c.revision, "created": c.object_ids}

    if results_dir is not None:
        @app.post("/api/v1/spike/results/{name}")
        async def spike_results(name: str, request: Request):
            if not _NAME.match(name):
                raise HTTPException(400, "name")
            data = await request.json()
            results_dir.mkdir(parents=True, exist_ok=True)
            out = results_dir / f"{time.strftime('%Y%m%dT%H%M%S')}-{name}.json"
            out.write_text(json.dumps(data, indent=1))
            return {"saved": out.name}

    if spike_files is not None:  # spike inputs (synthetic Arrow files); token-gated
        app.mount("/api/v1/spike/files", StaticFiles(directory=spike_files), name="spike-files")

    if web_dir is not None:  # built client, same origin as the API
        app.mount("/", StaticFiles(directory=web_dir, html=True), name="web")

    return app


def _encode(arr: np.ndarray, fmt: str, q: int) -> bytes:
    im = Image.fromarray(np.ascontiguousarray(arr[..., :3] if arr.shape[-1] >= 3
                                              else arr[..., 0]))
    buf = io.BytesIO()
    if fmt == "jpg":
        im.save(buf, "JPEG", quality=q)
    elif fmt == "webp":  # lossless; method 0 is fastest and beats PNG on size (S1)
        im.save(buf, "WEBP", lossless=True, method=0, quality=0)
    else:
        im.save(buf, "PNG", compress_level=1)
    return buf.getvalue()
