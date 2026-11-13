"""Label maps: class tables (viewer-side JSON) and pairing maps with images.

A label map is a COMPASS map (one integer channel, pixel-aligned 1:1 with a
slide). Class names and colours come from a JSON file next to it (D9/M4,
2026-11-13; COMPASS defines no attribute for them yet)::

    {"background": 0,
     "classes": [{"value": 1, "name": "tumour", "color": "#d55e00"}, ...]}

``background`` (default 0) is drawn transparent; ``color`` is optional.
"""

import json
import math
import re
from pathlib import Path

import numpy as np

from .pyramid import LevelInfo, Pyramid

_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
MAX_VALUE = 2**32 - 1


def load_classes(path: str | Path) -> tuple[list[dict], int]:
    """(classes sorted by value, background) from a class table file."""
    d = json.loads(Path(path).read_text())
    if isinstance(d, list):
        d = {"classes": d}
    if not isinstance(d, dict) or not isinstance(d.get("classes", []), list):
        raise ValueError(f"{path}: expected {{\"classes\": [...]}}")
    bg = d.get("background", 0)
    if not isinstance(bg, int) or isinstance(bg, bool) or not 0 <= bg <= MAX_VALUE:
        raise ValueError(f"{path}: background must be an integer label value")
    out, seen = [], set()
    for i, c in enumerate(d.get("classes", [])):
        v, name, color = (c.get("value"), c.get("name"), c.get("color")) if isinstance(c, dict) else (None,) * 3
        if not isinstance(v, int) or isinstance(v, bool) or not 0 <= v <= MAX_VALUE or v in seen:
            raise ValueError(f"{path}: class {i}: value must be a distinct integer in [0, 2^32)")
        if not isinstance(name, str) or not name:
            raise ValueError(f"{path}: class {i}: name must be a non-empty string")
        if color is not None and not (isinstance(color, str) and _COLOR.match(color)):
            raise ValueError(f"{path}: class {i}: color must be #rrggbb")
        seen.add(v)
        out.append({"value": v, "name": name, "color": color.lower() if color else None})
    return sorted(out, key=lambda c: c["value"]), bg


def classes_file_for(path: str | Path) -> Path | None:
    """``slide.regions.zarr`` -> ``slide.regions.classes.json`` if it exists."""
    p = Path(path)
    f = p.with_name(p.name.removesuffix(".zarr") + ".classes.json")
    return f if f.is_file() else None


def pair_with_image(kind: str, key: str, size: tuple[int, int] | None, images: dict[str, Pyramid],
                    target: str | None, flag: str) -> str:
    """The image something belongs to: ``target`` if given, else the image
    whose id is the longest dotted prefix of ``key`` (``slide.regions`` ->
    ``slide``), else the only image served. ``size`` (if known) must match.
    Raises ValueError naming ``flag`` when nothing fits."""
    if target is None:
        parts = key.split(".")
        for n in range(len(parts) - 1, 0, -1):
            if ".".join(parts[:n]) in images:
                target = ".".join(parts[:n])
                break
    if target is None and len(images) == 1:
        target = next(iter(images))
    if target is None:
        raise ValueError(f"{kind} {key!r}: which image does it belong to? use {flag} {key}=IMAGE")
    if target not in images:
        raise ValueError(f"{kind} {key!r}: no image {target!r}")
    im = images[target]
    if size is not None and size != (im.width, im.height):
        raise ValueError(f"{kind} {key!r} is {size[0]}x{size[1]}, image {target!r} is "
                         f"{im.width}x{im.height}: they must be pixel-aligned 1:1")
    return target


def pair_labels(labels: dict[str, Pyramid], images: dict[str, Pyramid],
                explicit: dict[str, str] | None = None) -> None:
    """Set ``aligned_to`` on every label map (see :func:`pair_with_image`)."""
    explicit = explicit or {}
    for k, lab in labels.items():
        lab.aligned_to = pair_with_image("label map", k, (lab.width, lab.height), images,
                                         explicit.get(k), "--label-of")


class SyntheticLabels(Pyramid):
    """Procedural label map for tests: classes 1..n in vertical bands with a
    grid of discs of class n + 1; value 0 outside a margin (background).
    Coarser levels are nearest-neighbour subsamples, as COMPASS writes them."""

    def __init__(self, width: int, height: int, n: int = 4, dtype: str = "uint8", tile: int = 512):
        levels = [LevelInfo(width=max(1, width >> i), height=max(1, height >> i))
                  for i in range(max(1, math.ceil(math.log2(max(width, height) / tile)) + 1))]
        super().__init__(width=width, height=height, levels=levels, tile=tile, channels=["label"],
                         mpp=0.5, kind="synthetic", role="labels", dtype=dtype, background=0,
                         name=f"synthetic labels {width}x{height}",
                         version=f"synthlabels-{width}x{height}-{n}-{dtype}-1")
        self.n = n

    def read_tile(self, level: int, col: int, row: int) -> np.ndarray:
        h, w = self.tile_shape(level, col, row)
        s = 2**level
        y = (row * self.tile + np.arange(h)) * s
        x = (col * self.tile + np.arange(w)) * s
        X, Y = np.meshgrid(x, y)
        out = 1 + (X * self.n // self.width)
        cell = 2000
        disc = (X % cell - cell // 2) ** 2 + (Y % cell - cell // 2) ** 2 < (cell // 4) ** 2
        out[disc] = self.n + 1
        m = min(self.width, self.height) // 20
        out[(X < m) | (Y < m) | (X >= self.width - m) | (Y >= self.height - m)] = 0
        return out.astype(self.dtype)
