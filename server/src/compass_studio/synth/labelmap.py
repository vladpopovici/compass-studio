"""``compass-studio-synth-labels``: a test label map aligned with a slide.

Classes come from the colour of a coarse level of the image (background,
light tissue, eosin-rich, haematoxylin-rich), so the map follows the
tissue; level 0 repeats each coarse pixel (blocky at 2^level px). Written
in the COMPASS map layout (2-D uint8, 512 px chunks in 4096 px shards,
Blosc zstd bitshuffle, nearest-neighbour levels) with a class table next
to it. Keep outputs under data/.

    compass-studio-synth-labels IMAGE.zarr OUT.zarr [--from-level 3]
"""

import argparse
import json
import time
from pathlib import Path

import numpy as np
import zarr
from zarr.codecs import BloscCodec, BloscShuffle

from ..imaging.pyramid import ZarrPyramid

CLASSES = [
    {"value": 1, "name": "light tissue", "color": "#f0e442"},
    {"value": 2, "name": "eosin-rich", "color": "#cc79a7"},
    {"value": 3, "name": "haematoxylin-rich", "color": "#0072b2"},
]


def classify(rgb: np.ndarray) -> np.ndarray:
    """Coarse colour classes of an RGB uint8 array."""
    f = rgb.astype(np.float32)
    r, g, b = f[..., 0], f[..., 1], f[..., 2]
    tissue = (r + g + b) / 3 < 215
    out = np.zeros(rgb.shape[:2], np.uint8)
    out[tissue] = 1
    out[tissue & (r - b > 25) & (g < 170)] = 2
    out[tissue & (b - r > -5) & ((r + g + b) / 3 < 170)] = 3
    return out


def write(image: Path, out: Path, from_level: int, log=print) -> None:
    src = ZarrPyramid(image)
    if src.role != "image":
        raise SystemExit(f"{image}: not an RGB image")
    lv = min(from_level, len(src.levels) - 1)
    L = src.levels[lv]
    coarse = np.zeros((L.height, L.width), np.uint8)
    t = src.tile
    for row in range(-(-L.height // t)):
        for col in range(-(-L.width // t)):
            tile = src.read_tile(lv, col, row)
            coarse[row * t:row * t + tile.shape[0], col * t:col * t + tile.shape[1]] = classify(tile[..., :3])
    log(f"classified level {lv} ({L.width}x{L.height}): " +
        ", ".join(f"{v}: {int((coarse == v).sum())}" for v in range(4)))

    root = zarr.open_group(str(out), mode="w", zarr_format=3)
    codec = BloscCodec(cname="zstd", clevel=5, shuffle=BloscShuffle.bitshuffle, typesize=1)
    s = 2**lv
    for i, lev in enumerate(src.levels):
        chunk = (min(512, lev.height), min(512, lev.width))
        shard = (min(4096, -(-lev.height // chunk[0]) * chunk[0]), min(4096, -(-lev.width // chunk[1]) * chunk[1]))
        a = root.create_array(str(i), shape=(lev.height, lev.width), dtype="uint8", chunks=chunk,
                              shards=shard, compressors=codec, fill_value=0, dimension_names=["y", "x"])
        step = 2**i  # level-i pixel y covers level-0 rows [y*step, ...); coarse pixel = y*step // s
        for y0 in range(0, lev.height, shard[0]):
            ys = (np.arange(y0, min(lev.height, y0 + shard[0])) * step) // s
            ys = np.minimum(ys, coarse.shape[0] - 1)
            rows = coarse[ys]
            for x0 in range(0, lev.width, shard[1]):
                xs = np.minimum((np.arange(x0, min(lev.width, x0 + shard[1])) * step) // s, coarse.shape[1] - 1)
                a[y0:y0 + len(ys), x0:x0 + len(xs)] = rows[:, xs]
        log(f"level {i}: {lev.width}x{lev.height}")
    root.attrs.update({"compass_version": 2, "level_count": len(src.levels), "base_mpp": src.mpp,
                       "base_objective_power": src.objective_power, "base_mag_step": 2.0,
                       "channel_names": ["class"], "dimension_names": ["y", "x"]})
    classes = out.with_name(out.name.removesuffix(".zarr") + ".classes.json")
    classes.write_text(json.dumps({"background": 0, "classes": CLASSES}, indent=1) + "\n")
    log(f"wrote {out} and {classes.name}")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("image", type=Path)
    ap.add_argument("out", type=Path, help="output .zarr folder (name it <slide>.<map>.zarr to pair it)")
    ap.add_argument("--from-level", type=int, default=3)
    a = ap.parse_args(argv)
    t0 = time.perf_counter()
    write(a.image, a.out, a.from_level, log=lambda m: print(f"[{time.perf_counter() - t0:6.1f}s] {m}", flush=True))
