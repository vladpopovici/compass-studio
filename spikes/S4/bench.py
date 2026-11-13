"""S4: per-tile cost of the SQLite read path for polygon tiles.

    cd server && uv run python ../spikes/S4/bench.py STORE [STORE ...] \
        [--layer cells] [--levels 0 1 2 3 4] [--tiles 200] [--threads 1 4]

For each store (e.g. spatial and random object order), level and placement:
  uniform   tile positions uniform over the image grid (includes empty tiles)
  tissue    tiles containing a randomly chosen object of the layer
the stages R-tree query, WKB decode (direct and shapely), earcut and Arrow
encode are timed per tile; then whole tiles under 1..N threads (one
connection per thread) for latency and throughput. Also times exporting the
whole point layer. Tile at level l = 512 * 2**l level-0 px.
"""

import argparse
import json
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

from compass_studio.annot.store import AnnotationStore, decode_polygons, encode_ipc, polygon_tile
from compass_studio.annot.triangulate import triangulate

TILE = 512


def pct(xs, ps=(50, 95, 99)):
    if not xs:
        return {"n": 0}
    a = np.asarray(xs)
    return {"n": len(a), **{f"p{p}": round(float(np.percentile(a, p)), 2) for p in ps},
            "max": round(float(a.max()), 2), "mean": round(float(a.mean()), 2)}


def tile_boxes(store: AnnotationStore, layer_id: int, level: int, n: int, how: str, seed: int):
    W, H = int(store.meta["image_width"]), int(store.meta["image_height"])
    side = TILE * 2 ** level
    rng = random.Random(seed)
    boxes = []
    if how == "uniform":
        nx, ny = -(-W // side), -(-H // side)
        for _ in range(n):
            tx, ty = rng.randrange(nx), rng.randrange(ny)
            boxes.append((tx * side, ty * side, (tx + 1) * side, (ty + 1) * side))
    else:
        gids = ",".join(map(str, store.groups[layer_id]))
        lo, hi = store._conn.execute(
            f"SELECT MIN(object_id), MAX(object_id) FROM object_groups WHERE group_id IN ({gids})").fetchone()
        while len(boxes) < n:
            row = store._conn.execute("SELECT min_x, min_y FROM objects WHERE object_id = ?",
                                      (rng.randint(lo, hi),)).fetchone()
            if row is None:
                continue
            tx, ty = int(row[0] // side), int(row[1] // side)
            boxes.append((tx * side, ty * side, (tx + 1) * side, (ty + 1) * side))
    return boxes


def stages(store: AnnotationStore, layer_id: int, boxes):
    out = {k: [] for k in ("query", "decode_direct", "decode_shapely", "triangulate",
                           "arrow_encode", "total")}
    objects, vertices, nbytes = [], [], []
    for b in boxes:
        t0 = time.perf_counter()
        rows = store.query_rows(layer_id, b)
        t1 = time.perf_counter()
        batch = decode_polygons(rows, "direct")
        t2 = time.perf_counter()
        decode_polygons(rows, "shapely")
        t3 = time.perf_counter()
        tri = triangulate(batch.xy, batch.starts, batch.hole_starts)
        t4 = time.perf_counter()
        body = encode_ipc(polygon_tile(batch, tri))
        t5 = time.perf_counter()
        for k, v in (("query", t1 - t0), ("decode_direct", t2 - t1), ("decode_shapely", t3 - t2),
                     ("triangulate", t4 - t3), ("arrow_encode", t5 - t4),
                     ("total", (t2 - t0) + (t5 - t3))):
            out[k].append(v * 1e3)
        objects.append(len(rows)); vertices.append(len(batch.xy)); nbytes.append(len(body))
    return ({k: pct(v) for k, v in out.items()},
            {"objects": pct(objects), "vertices": pct(vertices), "kbytes": pct([b / 1024 for b in nbytes])})


def full_tile(store: AnnotationStore, layer_id: int, box) -> float:
    t0 = time.perf_counter()
    encode_ipc(polygon_tile(store.polygons(layer_id, box)))
    return (time.perf_counter() - t0) * 1e3


def concurrent(path: Path, layer_id: int, boxes, threads: int):
    stores = [AnnotationStore(path) for _ in range(threads)]
    chunks = [boxes[i::threads] for i in range(threads)]
    lat: list[float] = []

    def work(i):
        return [full_tile(stores[i], layer_id, b) for b in chunks[i]]

    t0 = time.perf_counter()
    with ThreadPoolExecutor(threads) as ex:
        for r in ex.map(work, range(threads)):
            lat.extend(r)
    wall = time.perf_counter() - t0
    for s in stores:
        s.close()
    return {"threads": threads, "tiles_per_s": round(len(boxes) / wall, 1), "latency_ms": pct(lat)}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("stores", nargs="+", type=Path)
    ap.add_argument("--layer", default="cells")
    ap.add_argument("--levels", type=int, nargs="+", default=[0, 1, 2, 3, 4])
    ap.add_argument("--tiles", type=int, default=200)
    ap.add_argument("--threads", type=int, nargs="+", default=[1, 4])
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", type=Path, default=Path(__file__).parent / "results")
    a = ap.parse_args()

    result = {"spike": "S4", "args": {k: str(v) if isinstance(v, Path) else v
                                      for k, v in vars(a).items() if k != "stores"},
              "python": sys.version.split()[0], "stores": []}
    for path in a.stores:
        store = AnnotationStore(path)
        layer_id = next(k for k, v in store.layers.items() if v == a.layer)
        entry = {"store": path.name, "order": json.loads(store.meta.get("generator_params", "{}")).get("order"),
                 "cases": []}
        point_layer = next((k for k, v in store.layers.items() if v == "points"), None)
        if point_layer is not None:
            t0 = time.perf_counter()
            oid, _, _ = store.points(point_layer)
            entry["points_export_s"] = round(time.perf_counter() - t0, 2)
            entry["points"] = len(oid)
        for level in a.levels:
            for how in ("uniform", "tissue"):
                boxes = tile_boxes(store, layer_id, level, a.tiles, how, a.seed + level)
                # first pass on a fresh connection (SQLite page cache cold), then warm
                cold, _ = stages(AnnotationStore(path), layer_id, boxes[: a.tiles // 4])
                st, size = stages(store, layer_id, boxes)
                case = {"level": level, "tile_px": TILE * 2 ** level, "placement": how,
                        "stages_ms": st, "cold_conn_total_ms": cold["total"], "size": size,
                        "concurrency": [concurrent(path, layer_id, boxes, t) for t in a.threads]}
                entry["cases"].append(case)
                c1 = case["concurrency"]
                print(f"{path.name:24} L{level} {how:7} objs p50 {size['objects'].get('p50')} "
                      f"p95 {size['objects'].get('p95')} | total p50 {st['total'].get('p50')} "
                      f"p95 {st['total'].get('p95')} ms (q {st['query'].get('p95')}, "
                      f"dec {st['decode_direct'].get('p95')}/{st['decode_shapely'].get('p95')}, "
                      f"tri {st['triangulate'].get('p95')}, enc {st['arrow_encode'].get('p95')}) | "
                      + "  ".join(f"{c['threads']}t {c['tiles_per_s']}/s p95 {c['latency_ms'].get('p95')}"
                                  for c in c1), flush=True)
        store.close()
        result["stores"].append(entry)
    a.out.mkdir(parents=True, exist_ok=True)
    out = a.out / f"{time.strftime('%Y%m%dT%H%M%S')}-s4.json"
    out.write_text(json.dumps(result, indent=1))
    print("saved", out)


if __name__ == "__main__":
    main()
