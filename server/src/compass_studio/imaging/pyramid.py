"""Pyramids the tile server can serve: COMPASS Zarr images and a synthetic one.

Level ``l`` is downsampled by exactly ``2**l``: level pixel ``i`` covers
level-0 pixels ``[i * 2**l, (i + 1) * 2**l)``. Tiles are ``tile x tile``
except at the right and bottom edges, where they are cropped.
"""

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import zarr
from PIL import Image, ImageDraw

from .shards import ShardedArray, open_level


@dataclass
class LevelInfo:
    width: int
    height: int


@dataclass
class Pyramid:
    width: int
    height: int
    levels: list[LevelInfo]
    tile: int
    channels: list[str]
    mpp: float | None
    kind: str
    fast_levels: list[bool] = field(default_factory=list)

    def info(self) -> dict:
        return {"width": self.width, "height": self.height, "tile_size": self.tile,
                "levels": [{"width": l.width, "height": l.height, "downsample": 2 ** i}
                           for i, l in enumerate(self.levels)],
                "channels": self.channels, "mpp": self.mpp, "kind": self.kind}

    def tile_shape(self, level: int, col: int, row: int) -> tuple[int, int]:
        """(height, width) of a tile; IndexError outside the grid."""
        if not 0 <= level < len(self.levels):
            raise IndexError("level")
        L = self.levels[level]
        x0, y0 = col * self.tile, row * self.tile
        if col < 0 or row < 0 or x0 >= L.width or y0 >= L.height:
            raise IndexError("tile")
        return min(self.tile, L.height - y0), min(self.tile, L.width - x0)

    def read_tile(self, level: int, col: int, row: int) -> np.ndarray:
        raise NotImplementedError


class ZarrPyramid(Pyramid):
    """A COMPASS image: Zarr v3 group with arrays ``0``, ``1``, ... (y, x, c)."""

    def __init__(self, path: str | Path, reader: str = "auto"):
        """``reader``: ``auto`` (direct shard reader where supported, else
        zarr-python) or ``zarr`` (zarr-python only; for comparison, S1)."""
        if reader not in ("auto", "zarr"):
            raise ValueError(reader)
        path = Path(path)
        attrs = json.loads((path / "zarr.json").read_text()).get("attributes", {})
        group = zarr.open_group(str(path), mode="r")
        n = int(attrs.get("level_count", len(list(group.array_keys()))))
        self._zarr = [group[str(i)] for i in range(n)]
        self._fast: list[ShardedArray | None] = (
            [open_level(path / str(i)) for i in range(n)] if reader == "auto" else [None] * n)
        shapes = [a.shape for a in self._zarr]
        if any(len(s) != 3 for s in shapes):
            raise ValueError("expected (y, x, c) arrays")
        chunk = self._zarr[0].chunks  # inner chunk shape for sharded arrays
        super().__init__(
            width=shapes[0][1], height=shapes[0][0],
            levels=[LevelInfo(width=s[1], height=s[0]) for s in shapes],
            tile=int(chunk[0]), channels=list(attrs.get("channel_names", [])),
            mpp=attrs.get("base_mpp"), kind="zarr",
            fast_levels=[f is not None for f in self._fast])

    def read_tile(self, level: int, col: int, row: int) -> np.ndarray:
        h, w = self.tile_shape(level, col, row)
        fast = self._fast[level]
        if fast is not None and fast.layout.chunk_shape[2] == fast.layout.shape[2]:
            # a tile is one inner chunk if, on each axis, chunk and tile edges
            # agree (equal size, or one chunk covers a level no wider than a tile)
            L = self.levels[level]
            cy, cx = fast.layout.chunk_shape[:2]
            if all(c == self.tile or (c >= n and n <= self.tile)
                   for c, n in ((cy, L.height), (cx, L.width))):
                return fast.read_chunk((row, col, 0))
        y0, x0 = row * self.tile, col * self.tile
        return np.asarray(self._zarr[level][y0:y0 + h, x0:x0 + w])


class SyntheticPyramid(Pyramid):
    """Procedural RGB pyramid for precision tests at any size.

    Each tile shows its level and grid position, a 1 px border, and level-0
    grid lines every ``grid`` px, so misplaced or jittering tiles are visible.
    """

    def __init__(self, width: int, height: int, tile: int = 512, grid: int = 1000):
        n = max(1, math.ceil(math.log2(max(width, height) / tile)) + 1)
        levels = [LevelInfo(width=max(1, width >> i), height=max(1, height >> i))
                  for i in range(n)]
        super().__init__(width=width, height=height, levels=levels, tile=tile,
                         channels=["R", "G", "B"], mpp=0.5, kind="synthetic",
                         fast_levels=[False] * n)
        self.grid = grid

    def read_tile(self, level: int, col: int, row: int) -> np.ndarray:
        h, w = self.tile_shape(level, col, row)
        hue = (level * 47) % 360
        base = Image.new("RGB", (w, h), _hsv(hue, 0.25, 0.95))
        d = ImageDraw.Draw(base)
        s = 2 ** level
        x0, y0 = col * self.tile * s, row * self.tile * s
        step = self.grid
        while step / s < 8:  # keep lines at least 8 px apart
            step *= 10
        for gx in range(math.ceil(x0 / step) * step, x0 + w * s, step):
            d.line([((gx - x0) // s, 0), ((gx - x0) // s, h)], fill=(40, 40, 160))
        for gy in range(math.ceil(y0 / step) * step, y0 + h * s, step):
            d.line([(0, (gy - y0) // s), (w, (gy - y0) // s)], fill=(160, 40, 40))
        d.rectangle([0, 0, w - 1, h - 1], outline=(0, 0, 0))
        d.text((6, 4), f"L{level} c{col} r{row}\nx0={x0} y0={y0}", fill=(0, 0, 0))
        return np.asarray(base)


def _hsv(h: float, s: float, v: float) -> tuple[int, int, int]:
    import colorsys
    r, g, b = colorsys.hsv_to_rgb(h / 360, s, v)
    return int(r * 255), int(g * 255), int(b * 255)
