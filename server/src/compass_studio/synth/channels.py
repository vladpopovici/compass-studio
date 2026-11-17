"""``compass-studio-synth-channels``: a synthetic multichannel (uint16) image.

Cells on a jittered grid inside blob-shaped tissue; channel 0 is nuclear
(DAPI), the others markers in a share of the cells (nuclear, cytoplasmic or
membranous). Written in the COMPASS image layout: (y, x, c) uint16, 512 px
chunks holding every channel in 4096 px shards, Blosc zstd bitshuffle,
channel names in the group attributes. Coarser levels sample the pattern
every 2^level px (COMPASS box-averages; for a test image the difference
does not matter). Keep outputs under data/.

    compass-studio-synth-channels OUT.zarr [--size 16384x16384] [--channels 8]
"""

import argparse
import time
from pathlib import Path

import zarr
from zarr.codecs import BloscCodec

from ..imaging.channels import channel_names, synth_region


def write(out: Path, width: int, height: int, n: int, log=print) -> None:
    root = zarr.open_group(str(out), mode="w", zarr_format=3)
    codec = BloscCodec(cname="zstd", clevel=5, shuffle="bitshuffle", typesize=2)
    levels = []
    w, h = width, height
    while True:
        levels.append((w, h))
        if max(w, h) <= 512:
            break
        w, h = max(1, w // 2), max(1, h // 2)
    for i, (lw, lh) in enumerate(levels):
        chunk = (min(512, lh), min(512, lw), n)
        shard = (min(4096, -(-lh // chunk[0]) * chunk[0]), min(4096, -(-lw // chunk[1]) * chunk[1]), n)
        a = root.create_array(str(i), shape=(lh, lw, n), dtype="uint16", chunks=chunk, shards=shard,
                              compressors=codec, fill_value=0, dimension_names=["y", "x", "c"])
        s = 2**i
        for y0 in range(0, lh, shard[0]):
            for x0 in range(0, lw, shard[1]):
                hh, ww = min(shard[0], lh - y0), min(shard[1], lw - x0)
                a[y0:y0 + hh, x0:x0 + ww] = synth_region(y0 * s, x0 * s, hh, ww, s, n, (width, height))
        log(f"level {i}: {lw}x{lh}")
    root.attrs.update({"compass_version": 2, "level_count": len(levels), "base_mpp": 0.5,
                       "base_objective_power": 20.0, "base_mag_step": 2.0,
                       "channel_names": channel_names(n), "dimension_names": ["y", "x", "c"]})
    log(f"wrote {out}")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("out", type=Path, help="output .zarr folder")
    ap.add_argument("--size", default="16384x16384", help="WxH (default 16384x16384)")
    ap.add_argument("--channels", type=int, default=8, help="number of channels (default 8)")
    a = ap.parse_args(argv)
    w, h = (int(x) for x in a.size.split("x"))
    t0 = time.perf_counter()
    write(a.out, w, h, a.channels, log=lambda m: print(f"[{time.perf_counter() - t0:6.1f}s] {m}", flush=True))
