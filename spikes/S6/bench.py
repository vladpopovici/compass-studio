"""S6: SQLite write path of the sidecar (commit cost, fork cost, a second process).

    cd server && uv run python ../spikes/S6/bench.py SOURCE_STORE WORKDIR [--layer cells]

1. fork: copy the layer (1M polygons in the synthetic store) into a fresh
   sidecar (R-tree trigger per row), synchronous NORMAL.
2. commits: batches of 1, 10, 100 and 1,000 ops of each kind (create,
   update_geometry, move, update_attributes, delete, mixed) on that sidecar,
   synchronous NORMAL and FULL; also an undo batch.
3. second process: an external writer commits every 100 ms for 5 s while
   this process polls ``PRAGMA data_version`` every 10 ms and runs bbox
   queries; reports detection lag and read latency during writes.
4. contention: two processes commit as fast as they can for 5 s
   (busy_timeout 5 s); reports commit latency, conflicts and errors.
"""

import argparse
import json
import multiprocessing as mp
import random
import shutil
import sqlite3
import time
from pathlib import Path

import numpy as np

from compass_studio.annot.sidecar import Conflict, Sidecar

RESULTS = Path(__file__).parent / "results"


def pct(xs, ps=(50, 95, 99)):
    if not len(xs):
        return {"n": 0}
    a = np.asarray(xs, float)
    return {"n": len(a), **{f"p{p}": round(float(np.percentile(a, p)), 2) for p in ps},
            "max": round(float(a.max()), 2), "mean": round(float(a.mean()), 2)}


def cell(rng: random.Random, cx=None, cy=None):
    cx = rng.uniform(1000, 199000) if cx is None else cx
    cy = rng.uniform(1000, 199000) if cy is None else cy
    n = rng.randint(8, 32)
    r = rng.uniform(6, 14)
    ang = sorted(rng.uniform(0, 6.283) for _ in range(n))
    return [[[cx + r * np.cos(a), cy + r * np.sin(a)] for a in ang]]


def make_ops(sc: Sidecar, kind: str, n: int, rng: random.Random, lid: int) -> list[dict]:
    groups = [g for (g,) in sc._conn.execute("SELECT group_id FROM groups WHERE layer_id = ?", (lid,))]
    if kind == "create":
        return [{"op": "create", "group_id": rng.choice(groups), "coords": cell(rng)} for _ in range(n)]
    ids = [r[0] for r in sc._conn.execute(
        "SELECT og.object_id FROM object_groups og JOIN groups g USING(group_id) "
        "WHERE g.layer_id = ? ORDER BY RANDOM() LIMIT ?", (lid, n))]
    if kind == "update_geometry":
        out = []
        for oid in ids:
            x0, y0 = sc._conn.execute("SELECT min_x, min_y FROM objects WHERE object_id = ?", (oid,)).fetchone()
            out.append({"op": "update_geometry", "object_id": oid, "coords": cell(rng, x0 + 8, y0 + 8)})
        return out
    if kind == "move":
        return [{"op": "move", "object_id": oid, "group_id": rng.choice(groups)} for oid in ids]
    if kind == "update_attributes":
        return [{"op": "update_attributes", "object_id": oid,
                 "values": {"marker_001": rng.random(), "curated": 1.0}} for oid in ids]
    if kind == "delete":
        return [{"op": "delete", "object_id": oid} for oid in ids]
    if kind == "mixed":
        kinds = ["create", "update_geometry", "move", "update_attributes", "delete"]
        per = max(1, n // len(kinds))
        ops = []
        for k in kinds:
            ops += make_ops(sc, k, per, rng, lid)
        return ops[:n]
    raise ValueError(kind)


def bench_commits(path: Path, lid: int, synchronous: str, reps: int, seed: int):
    rng = random.Random(seed)
    sc = Sidecar(path, synchronous=synchronous)
    out = {}
    for kind in ("create", "update_geometry", "move", "update_attributes", "delete", "mixed"):
        for n in (1, 10, 100, 1000):
            ms = []
            r = reps if n < 1000 else max(3, reps // 5)
            for _ in range(r):
                ops = make_ops(sc, kind, n, rng, lid)
                t0 = time.perf_counter()
                sc.apply(sc.revision(), ops)
                ms.append((time.perf_counter() - t0) * 1e3)
            out[f"{kind}/{n}"] = pct(ms)
            print(f"  commit {synchronous:6} {kind:17} {n:5} ops: p50 {out[f'{kind}/{n}']['p50']:8} "
                  f"p95 {out[f'{kind}/{n}']['p95']:8} ms", flush=True)
    # undo the last 20 batches, one at a time
    ms = []
    for _ in range(20):
        rev = sc.revision()
        inv = sc.inverse_of(rev)
        t0 = time.perf_counter()
        sc.apply(rev, inv)
        ms.append((time.perf_counter() - t0) * 1e3)
    out["undo"] = pct(ms)
    print(f"  undo   {synchronous:6} p50 {out['undo']['p50']} p95 {out['undo']['p95']} ms", flush=True)
    sc.close()
    return out


def _writer(path, lid, period_s, duration_s, seed, out_q):
    rng = random.Random(seed)
    sc = Sidecar(path)
    g = sc._conn.execute("SELECT MIN(group_id) FROM groups WHERE layer_id = ?", (lid,)).fetchone()[0]
    end = time.time() + duration_s
    commits, conflicts, lat = 0, 0, []
    while time.time() < end:
        ops = [{"op": "create", "group_id": g, "coords": cell(rng)} for _ in range(10)]
        t0 = time.perf_counter()
        try:
            sc.apply(sc.revision(), ops, client=f"w{seed}")
            commits += 1
        except Conflict:
            conflicts += 1
        except sqlite3.OperationalError as e:  # busy beyond busy_timeout
            out_q.put({"error": str(e)})
        lat.append((time.perf_counter() - t0) * 1e3)
        if period_s:
            time.sleep(period_s)
    sc.close()
    out_q.put({"commits": commits, "conflicts": conflicts, "lat": lat})


def bench_second_process(path: Path, lid: int):
    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    sc = Sidecar(path)
    p = ctx.Process(target=_writer, args=(path, lid, 0.1, 5.0, 1, q))
    p.start()
    seen_rev, lags, reads, v = sc.revision(), [], [], sc.data_version()
    rng = random.Random(2)
    end = time.time() + 6.0
    while time.time() < end:
        nv = sc.data_version()
        if nv != v:
            v = nv
            rev = sc.revision()
            for (ts,) in sc._conn.execute("SELECT ts FROM cs_oplog WHERE revision > ? AND revision <= ?",
                                          (seen_rev, rev)):
                lags.append((time.time() - ts) * 1e3)
            seen_rev = rev
        x, y = rng.uniform(0, 196000), rng.uniform(0, 196000)
        t0 = time.perf_counter()
        sc._conn.execute("SELECT COUNT(*) FROM objects_rtree WHERE max_x >= ? AND min_x <= ? "
                         "AND max_y >= ? AND min_y <= ?", (x, x + 4096, y, y + 4096)).fetchone()
        reads.append((time.perf_counter() - t0) * 1e3)
        time.sleep(0.01)
    p.join(30)
    w = q.get(timeout=5)
    sc.close()
    return {"external_commits": w["commits"], "detected": len(lags), "detection_lag_ms": pct(lags),
            "reader_bbox_query_ms": pct(reads), "writer_commit_ms": pct(w["lat"])}


def bench_contention(path: Path, lid: int):
    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    procs = [ctx.Process(target=_writer, args=(path, lid, 0, 5.0, s, q)) for s in (11, 12)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(60)
    res, errors = [], []
    while not q.empty():
        m = q.get()
        (errors if "error" in m else res).append(m)
    return {"writers": [{"commits": r["commits"], "conflicts": r["conflicts"],
                         "commit_ms": pct(r["lat"])} for r in res], "busy_errors": len(errors)}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("source", type=Path)
    ap.add_argument("workdir", type=Path)
    ap.add_argument("--layer", default="cells")
    ap.add_argument("--reps", type=int, default=30)
    a = ap.parse_args()
    a.workdir.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(f"file:{a.source.resolve()}?mode=ro", uri=True)
    src_lid = src.execute("SELECT layer_id FROM layers WHERE name = ?", (a.layer,)).fetchone()[0]
    n_src = src.execute("SELECT COUNT(DISTINCT og.object_id) FROM object_groups og JOIN groups g "
                        "USING(group_id) WHERE g.layer_id = ?", (src_lid,)).fetchone()[0]
    src.close()
    result = {"spike": "S6", "layer": a.layer, "objects": n_src, "sqlite": sqlite3.sqlite_version}

    base = a.workdir / "fork.sqlite"
    for f in a.workdir.glob("*.sqlite*"):
        f.unlink()
    sc = Sidecar(base)
    t0 = time.perf_counter()
    lid = sc.fork_layer(a.source, src_lid)
    result["fork_s"] = round(time.perf_counter() - t0, 2)
    sc.close()
    result["fork_mb"] = round(base.stat().st_size / 2**20, 1)
    print(f"fork {n_src} objects: {result['fork_s']} s, {result['fork_mb']} MB", flush=True)

    result["commits"] = {}
    for sync in ("NORMAL", "FULL"):
        work = a.workdir / f"commits-{sync}.sqlite"
        shutil.copy(base, work)
        result["commits"][sync] = bench_commits(work, lid, sync, a.reps, seed=7)

    work = a.workdir / "second.sqlite"
    shutil.copy(base, work)
    result["second_process"] = bench_second_process(work, lid)
    sp = result["second_process"]
    print(f"second process: {sp['external_commits']} commits, {sp['detected']} detected, lag p50 "
          f"{sp['detection_lag_ms'].get('p50')} p95 {sp['detection_lag_ms'].get('p95')} ms; reader "
          f"bbox p95 {sp['reader_bbox_query_ms'].get('p95')} ms; writer commit p95 "
          f"{sp['writer_commit_ms'].get('p95')} ms", flush=True)

    work = a.workdir / "contention.sqlite"
    shutil.copy(base, work)
    result["contention"] = bench_contention(work, lid)
    print("contention:", json.dumps(result["contention"]), flush=True)

    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"{time.strftime('%Y%m%dT%H%M%S')}-s6.json"
    out.write_text(json.dumps(result, indent=1))
    print("saved", out)


if __name__ == "__main__":
    main()
