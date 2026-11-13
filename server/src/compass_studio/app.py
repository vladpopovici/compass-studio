"""HTTP server (draft; the contract is not frozen until the spikes are done).

Security (D10): binds to 127.0.0.1 unless told otherwise, requires a
per-session bearer token on ``/api``, checks Host and Origin, no CORS.
"""

import io
import json
import re
import secrets
import time
from pathlib import Path

import numpy as np
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image

from .imaging.pyramid import Pyramid

API_VERSION = "0.0.0"
_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def create_app(images: dict[str, Pyramid], token: str, allowed_hosts: set[str],
               allowed_origins: set[str], results_dir: Path | None = None,
               web_dir: Path | None = None, spike_files: Path | None = None) -> FastAPI:
    app = FastAPI(title="CompassStudio", version=API_VERSION, docs_url=None,
                  redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.headers.get("host", "") not in allowed_hosts:
            return JSONResponse({"detail": "bad host"}, status_code=421)
        origin = request.headers.get("origin")
        if origin is not None and origin not in allowed_origins:
            return JSONResponse({"detail": "bad origin"}, status_code=403)
        if request.url.path.startswith("/api/"):
            auth = request.headers.get("authorization", "")
            if not secrets.compare_digest(auth, f"Bearer {token}"):
                return JSONResponse({"detail": "unauthorized"}, status_code=401)
        return await call_next(request)

    @app.get("/api/v1/info")
    def info():
        caps = ["tiles.jpeg", "tiles.png"]
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
    def tile(image_id: str, level: int, col: int, row: int, fmt: str, q: int = 90):
        p = _image(image_id)
        if fmt not in ("jpg", "png") or not 1 <= q <= 100:
            raise HTTPException(400, "format")
        t0 = time.perf_counter()
        try:
            arr = p.read_tile(level, col, row)
        except IndexError:
            raise HTTPException(404, "no such tile") from None
        t1 = time.perf_counter()
        body = _encode(arr, fmt, q)
        t2 = time.perf_counter()
        return Response(body, media_type="image/jpeg" if fmt == "jpg" else "image/png",
                        headers={"Cache-Control": "private, max-age=3600",
                                 "Server-Timing": f"read;dur={(t1 - t0) * 1e3:.2f}, "
                                                  f"encode;dur={(t2 - t1) * 1e3:.2f}"})

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
    else:
        im.save(buf, "PNG", compress_level=1)
    return buf.getvalue()
