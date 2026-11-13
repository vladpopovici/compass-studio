"""S1: tile throughput from a COMPASS image, readers and encoders.

    cd server
    uv run python ../spikes/S1/bench.py tiles IMAGE            # sample tile list
    uv run python ../spikes/S1/bench.py inproc IMAGE           # readers x threads, encoders
    uv run python ../spikes/S1/bench.py http URL TOKEN IMAGE_ID  # load a running server

`tiles` writes spikes/S1/results/tiles.json: tissue tiles (level 0 to 4,
weighted towards high resolution, as when viewing) plus 20 % background tiles.
It holds tile coordinates only, no paths.

`inproc` reads every tile with the direct shard reader and with zarr-python
on 1 to 64 threads (read + decode only), then encodes a tissue subset as
JPEG q80/q90, PNG (levels 1 and 6) and lossless WebP (methods 0 and 4).

`http` requests the tile list (shuffled) from a running compass-studio-serve
at 1 to 64 concurrent requests, JPEG q90, and reports throughput and latency
including the server's read and encode times (Server-Timing).
"""

import argparse
import asyncio
import io
import json
import random
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

RESULTS = Path(__file__).parent / "results"
CONC = [1, 2, 4, 8, 16, 32, 64]


def pct(xs, ps=(50, 95, 99)):
    if not len(xs):
        return {"n": 0}
    a = np.asarray(xs, dtype=float)
    return {"n": len(a), **{f"p{p}": round(float(np.percentile(a, p)), 2) for p in ps},
            "max": round(float(a.max()), 2), "mean": round(float(a.mean()), 2)}


def save(name: str, data: dict) -> Path:
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"{time.strftime('%Y%m%dT%H%M%S')}-s1-{name}.json"
    out.write_text(json.dumps(data, indent=1))
    print("saved", out)
    return out


# ---------------------------------------------------------------------------

def cmd_tiles(a):
    from compass_studio.imaging.pyramid import ZarrPyramid
    p = ZarrPyramid(a.image)
    top = len(p.levels) - 1
    thumb = np.asarray(p._zarr[top][:])  # coarsest level, whole
    tissue_mask = thumb[..., :3].astype(int).sum(-1) < 3 * 220
    rng = random.Random(a.seed)
    weights = {0: 0.4, 1: 0.25, 2: 0.15, 3: 0.12, 4: 0.08}
    tissue, background = [], []
    for level, w in weights.items():
        L, s = p.levels[level], 2 ** (top - level)
        nx, ny = -(-L.width // p.tile), -(-L.height // p.tile)
        for ty in range(ny):
            for tx in range(nx):
                # tile footprint in thumbnail pixels
                x0, y0 = tx * p.tile // s, ty * p.tile // s
                x1, y1 = max(x0 + 1, (tx + 1) * p.tile // s), max(y0 + 1, (ty + 1) * p.tile // s)
                (tissue if tissue_mask[y0:y1, x0:x1].any() else background).append((level, tx, ty))
    by_level = {l: [t for t in tissue if t[0] == l] for l in weights}
    picked = []
    for level, w in weights.items():
        pool = by_level[level]
        picked += rng.sample(pool, min(len(pool), int(a.n * 0.8 * w)))
    picked += rng.sample(background, min(len(background), int(a.n * 0.2)))
    rng.shuffle(picked)
    out = RESULTS / "tiles.json"
    RESULTS.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"tile": p.tile, "levels": len(p.levels),
                               "tiles": [{"level": l, "col": c, "row": r,
                                          "tissue": (l, c, r) in set(tissue)} for l, c, r in picked]}))
    print(f"{len(picked)} tiles ({sum(1 for t in picked if t in set(tissue))} tissue) -> {out}")


def _tiles():
    return json.loads((RESULTS / "tiles.json").read_text())["tiles"]


def cmd_inproc(a):
    from PIL import Image

    from compass_studio.imaging.pyramid import ZarrPyramid
    tiles = _tiles()
    readers = {"fast": ZarrPyramid(a.image), "zarr": ZarrPyramid(a.image, reader="zarr")}
    result = {"spike": "S1", "bench": "inproc", "tiles": len(tiles), "readers": {}, "encoders": {}}
    for name, p in readers.items():
        for t in tiles[:20]:
            p.read_tile(t["level"], t["col"], t["row"])  # warm metadata, shard indexes, OS cache
        rows = []
        for n in CONC:
            lat: list[float] = []

            def one(t, p=p):
                t0 = time.perf_counter()
                p.read_tile(t["level"], t["col"], t["row"])
                return (time.perf_counter() - t0) * 1e3

            t0 = time.perf_counter()
            with ThreadPoolExecutor(n) as ex:
                lat = list(ex.map(one, tiles))
            wall = time.perf_counter() - t0
            rows.append({"threads": n, "tiles_per_s": round(len(tiles) / wall, 1), "latency_ms": pct(lat)})
            print(f"read {name:4} {n:3} threads: {rows[-1]['tiles_per_s']:8} tiles/s  "
                  f"p50 {rows[-1]['latency_ms']['p50']} p95 {rows[-1]['latency_ms']['p95']} ms", flush=True)
        result["readers"][name] = rows

    p = readers["fast"]
    sample = [p.read_tile(t["level"], t["col"], t["row"]) for t in tiles if t["tissue"]][:150]
    encoders = {
        "jpeg_q80": lambda im, b: im.save(b, "JPEG", quality=80),
        "jpeg_q90": lambda im, b: im.save(b, "JPEG", quality=90),
        "png_1": lambda im, b: im.save(b, "PNG", compress_level=1),
        "png_6": lambda im, b: im.save(b, "PNG", compress_level=6),
        "webp_lossless_m0": lambda im, b: im.save(b, "WEBP", lossless=True, method=0, quality=0),
        "webp_lossless_m4": lambda im, b: im.save(b, "WEBP", lossless=True, method=4, quality=50),
    }
    raw_kb = np.mean([t.nbytes for t in sample]) / 1024
    for name, enc in encoders.items():
        ms, kb = [], []
        for arr in sample:
            im = Image.fromarray(np.ascontiguousarray(arr))
            buf = io.BytesIO()
            t0 = time.perf_counter()
            enc(im, buf)
            ms.append((time.perf_counter() - t0) * 1e3)
            kb.append(buf.tell() / 1024)
        result["encoders"][name] = {"encode_ms": pct(ms), "kbytes": pct(kb)}
        print(f"encode {name:17} p50 {np.median(ms):6.2f} ms p95 {np.percentile(ms, 95):6.2f} ms  "
              f"size p50 {np.median(kb):6.1f} kB (raw {raw_kb:.0f} kB)", flush=True)
    import PIL
    result["pillow"] = PIL.__version__
    try:
        from PIL import features
        result["pillow_features"] = {k: features.version(k) for k in ("jpg", "webp", "zlib")}
    except Exception:  # noqa: BLE001
        pass
    save("inproc", result)


async def _load(url, token, image, tiles, conc, fmt, raw=False):
    import httpx
    lat, read, enc, errors = [], [], [], 0
    queue = list(tiles)
    limits = httpx.Limits(max_connections=conc, max_keepalive_connections=conc)
    async with httpx.AsyncClient(base_url=url, headers={"Authorization": f"Bearer {token}"},
                                 limits=limits, timeout=60) as c:
        async def worker():
            nonlocal errors
            while queue:
                t = queue.pop()
                t0 = time.perf_counter()
                r = await c.get(f"/api/v1/images/{image}/tiles/{t['level']}/{t['col']}/{t['row']}.{fmt}")
                lat.append((time.perf_counter() - t0) * 1e3)
                if r.status_code != 200:
                    errors += 1
                    continue
                for part in r.headers.get("server-timing", "").split(","):
                    k, _, v = part.strip().partition(";dur=")
                    (read if k == "read" else enc if k == "encode" else []).append(float(v or 0))
        t0 = time.perf_counter()
        await asyncio.gather(*(worker() for _ in range(conc)))
        wall = time.perf_counter() - t0
    if raw:
        return {"wall": wall, "lat": lat, "read": read, "enc": enc, "errors": errors, "n": len(tiles)}
    return {"concurrency": conc, "tiles_per_s": round(len(tiles) / wall, 1), "errors": errors,
            "latency_ms": pct(lat), "server_read_ms": pct(read), "server_encode_ms": pct(enc)}


def _load_proc(args):
    return asyncio.run(_load(*args, raw=True))


def load_multi(url, token, image, tiles, conc, fmt, procs):
    """Split ``conc`` connections over ``procs`` client processes (one event
    loop each), so the load generator itself is not the bottleneck."""
    from concurrent.futures import ProcessPoolExecutor
    procs = max(1, min(procs, conc))
    shares = [tiles[i::procs] for i in range(procs)]
    per = [conc // procs + (1 if i < conc % procs else 0) for i in range(procs)]
    with ProcessPoolExecutor(procs) as ex:
        parts = list(ex.map(_load_proc, [(url, token, image, sh, c, fmt) for sh, c in zip(shares, per)]))
    wall = max(x["wall"] for x in parts)
    cat = lambda k: [v for x in parts for v in x[k]]  # noqa: E731
    return {"concurrency": conc, "client_procs": procs, "tiles_per_s": round(len(tiles) / wall, 1),
            "errors": sum(x["errors"] for x in parts), "latency_ms": pct(cat("lat")),
            "server_read_ms": pct(cat("read")), "server_encode_ms": pct(cat("enc"))}


def cmd_http(a):
    tiles = _tiles()
    rows = []
    asyncio.run(_load(a.url, a.token, a.image_id, tiles[:50], 4, a.fmt))  # warm-up
    for n in CONC:
        rng = random.Random(n)
        order = tiles[:]
        rng.shuffle(order)
        r = (load_multi(a.url, a.token, a.image_id, order, n, a.fmt, a.procs) if a.procs > 1
             else asyncio.run(_load(a.url, a.token, a.image_id, order, n, a.fmt)))
        rows.append(r)
        print(f"http {a.label:6} {n:3} conc: {r['tiles_per_s']:7} tiles/s  latency p50 "
              f"{r['latency_ms']['p50']} p95 {r['latency_ms']['p95']} p99 {r['latency_ms']['p99']} ms  "
              f"server read p95 {r['server_read_ms'].get('p95')} encode p95 "
              f"{r['server_encode_ms'].get('p95')}  errors {r['errors']}", flush=True)
    save(f"http-{a.label}", {"spike": "S1", "bench": "http", "label": a.label, "fmt": a.fmt,
                             "client_procs": a.procs,
                             "tiles": len(tiles), "rows": rows})


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("tiles"); t.add_argument("image"); t.add_argument("--n", type=int, default=2000)
    t.add_argument("--seed", type=int, default=0)
    i = sub.add_parser("inproc"); i.add_argument("image")
    h = sub.add_parser("http"); h.add_argument("url"); h.add_argument("token"); h.add_argument("image_id")
    h.add_argument("--label", default="run"); h.add_argument("--fmt", default="jpg")
    h.add_argument("--procs", type=int, default=1, help="client processes")
    a = ap.parse_args()
    {"tiles": cmd_tiles, "inproc": cmd_inproc, "http": cmd_http}[a.cmd](a)


if __name__ == "__main__":
    main()
