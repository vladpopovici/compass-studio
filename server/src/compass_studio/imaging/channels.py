"""Multichannel images (milestone 8): display defaults and a synthetic test image.

Images that are not 8-bit RGB are served one raw channel per tile request
and mixed in the browser (D4). Display defaults come from a viewer-side
JSON file next to the image (``slide.zarr`` -> ``slide.channels.json``) or
``--channels``; COMPASS stores channel names but no colours or contrast::

    {"channels": [{"name": "DAPI", "color": "#0000ff", "min": 200, "max": 6000,
                   "visible": true}, ...]}

Entries are matched by ``name`` (or by ``index``, 0-based); every field but
the key is optional. Unmatched channels and missing fields are null (the
viewer picks them).
"""

import json
import math
import re
from pathlib import Path

import numpy as np

from .pyramid import LevelInfo, Pyramid

_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


def channels_file_for(path: str | Path) -> Path | None:
    """``slide.zarr`` -> ``slide.channels.json`` if it exists."""
    p = Path(path)
    f = p.with_name(p.name.removesuffix(".zarr") + ".channels.json")
    return f if f.is_file() else None


def load_channel_defaults(path: str | Path, names: list[str], max_value: int) -> list[dict]:
    """Per channel (in image order): {color, min, max, visible}, each possibly null."""
    d = json.loads(Path(path).read_text())
    if isinstance(d, list):
        d = {"channels": d}
    if not isinstance(d, dict) or not isinstance(d.get("channels"), list):
        raise ValueError(f"{path}: expected {{\"channels\": [...]}}")
    out: list[dict] = [{"color": None, "min": None, "max": None, "visible": None} for _ in names]
    for i, c in enumerate(d["channels"]):
        if not isinstance(c, dict):
            raise ValueError(f"{path}: channel entry {i}: expected an object")
        if "index" in c:
            k = c["index"]
            if not isinstance(k, int) or isinstance(k, bool) or not 0 <= k < len(names):
                raise ValueError(f"{path}: channel entry {i}: index must be 0 to {len(names) - 1}")
        elif isinstance(c.get("name"), str) and c["name"] in names:
            k = names.index(c["name"])
        else:
            raise ValueError(f"{path}: channel entry {i}: no channel named {c.get('name')!r} "
                             f"(channels: {', '.join(names)})")
        e = out[k]
        color = c.get("color")
        if color is not None:
            if not (isinstance(color, str) and _COLOR.match(color)):
                raise ValueError(f"{path}: channel entry {i}: color must be #rrggbb")
            e["color"] = color.lower()
        for f in ("min", "max"):
            v = c.get(f)
            if v is not None:
                if not isinstance(v, (int, float)) or isinstance(v, bool) or not 0 <= v <= max_value:
                    raise ValueError(f"{path}: channel entry {i}: {f} must be a number from 0 to {max_value}")
                e[f] = v
        if e["min"] is not None and e["max"] is not None and e["min"] >= e["max"]:
            raise ValueError(f"{path}: channel entry {i}: min must be below max")
        v = c.get("visible")
        if v is not None:
            if not isinstance(v, bool):
                raise ValueError(f"{path}: channel entry {i}: visible must be true or false")
            e["visible"] = v
    return out


# -- synthetic multichannel tissue ----------------------------------------------

def _hash(*xs: np.ndarray | int) -> np.ndarray:
    """Deterministic pseudo-random floats in [0, 1) from integer arrays."""
    h = np.uint32(2166136261)
    for x in xs:
        h = (h ^ np.asarray(x).astype(np.uint32)) * np.uint32(16777619)
        h ^= h >> np.uint32(15)
        h = h * np.uint32(2246822519)
        h ^= h >> np.uint32(13)
    return (h >> np.uint32(8)).astype(np.float32) / np.float32(1 << 24)


CELL = 40  # level-0 px between cell centres
MARKERS = ("nuclear", "cytoplasm", "membrane")


def channel_names(n: int) -> list[str]:
    return ["DAPI"] + [f"Marker {k}" for k in range(1, n)]


def synth_region(y0: int, x0: int, h: int, w: int, step: int, n: int, size: tuple[int, int]) -> np.ndarray:
    """uint16 (h, w, n) of synthetic fluorescence at level-0 positions
    ``y0 + i * step``, ``x0 + j * step``: cells on a jittered 40 px grid inside
    blob-shaped tissue; channel 0 nuclei (DAPI), channel k a marker in a
    share of the cells, nuclear, cytoplasmic or membranous by k mod 3."""
    W, H = size
    with np.errstate(over="ignore"):
        y = (y0 + np.arange(h, dtype=np.int64) * step)[:, None]
        x = (x0 + np.arange(w, dtype=np.int64) * step)[None, :]
        fy, fx = y / H, x / W
        tissue = (np.sin(fx * 7.1 + 1.3) + np.sin(fy * 5.3 + 0.4) + np.sin((fx + fy) * 3.7)) > -0.6
        gx, gy = x // CELL, y // CELL
        cx = gx * CELL + CELL / 2 + (_hash(gx, gy, 1) - 0.5) * CELL * 0.5
        cy = gy * CELL + CELL / 2 + (_hash(gx, gy, 2) - 0.5) * CELL * 0.5
        d = np.hypot(x - cx, y - cy)
        rn = 6 + 4 * _hash(gx, gy, 3)  # nucleus radius
        alive = tissue & (_hash(gx, gy, 4) < 0.85)
        noise = _hash(y, x, 9)
        out = np.empty((h, w, n), np.uint16)
        bg = 120 + 80 * noise
        nucleus = alive & (d < rn)
        out[..., 0] = np.where(nucleus, 3000 + 9000 * _hash(gx, gy, 5), bg + np.where(tissue, 150, 0))
        for k in range(1, n):
            share = 0.05 + 0.4 * _hash(k, 7)
            pos = alive & (_hash(gx, gy, k + 100) < share)
            kind = MARKERS[k % 3]
            if kind == "nuclear":
                m = d < rn
            elif kind == "cytoplasm":
                m = d < rn + 7
            else:
                m = (d > rn + 4) & (d < rn + 7)
            level = (1500 + 15000 * _hash(gx, gy, k + 200)) * (0.6 + 0.4 * _hash(k, 8))
            out[..., k] = np.where(pos & m, level, bg * (0.5 + _hash(k, 10)))
    return out


class SyntheticChannels(Pyramid):
    """Procedural multichannel uint16 image (tests and measurements); levels
    sample level-0 positions every 2^level px."""

    def __init__(self, width: int, height: int, n: int = 8, tile: int = 512):
        levels = [LevelInfo(width=max(1, width >> i), height=max(1, height >> i))
                  for i in range(max(1, math.ceil(math.log2(max(width, height) / tile)) + 1))]
        super().__init__(width=width, height=height, levels=levels, tile=tile, channels=channel_names(n),
                         mpp=0.5, kind="synthetic", dtype="uint16", display="channels",
                         fast_levels=[False] * len(levels), name=f"synthetic {n} channels {width}x{height}",
                         version=f"synthch-{width}x{height}-{n}-{tile}-1")
        self.n = n

    def read_tile(self, level: int, col: int, row: int) -> np.ndarray:
        h, w = self.tile_shape(level, col, row)
        s = 2**level
        return synth_region(row * self.tile * s, col * self.tile * s, h, w, s, self.n, (self.width, self.height))
